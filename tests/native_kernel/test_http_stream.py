"""C14 step 1: shared provider transport, response byte cap and bounded retry."""

from __future__ import annotations

import asyncio
import gzip
import socket
import tracemalloc
import zlib
from datetime import datetime, timezone
from email.utils import format_datetime
from uuid import uuid4

import httpx
import pytest

from labbioagentos.kernel import (
    HttpxChatCompletionTransport,
    KernelDeadlineExceededError,
    ModelRequest,
    OpenAICompatibleChatProvider,
    ProviderBinding,
    ProviderProtocolError,
    ProviderRetry,
    ProviderTransportError,
    PublicMessage,
)
from labbioagentos.kernel.http_stream import (
    MAX_RESPONSE_BYTES,
    HttpStreamResult,
    HttpStreamTransport,
    RetryableStreamError,
    RetryPolicy,
    response_byte_cap,
)

URL = "https://provider.invalid/v1/chat/completions"
MIB = 1024 * 1024


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def denied(*_args, **_kwargs):
        pytest.fail("Transport tests must not contact a network")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


class Clock:
    def __init__(self, now=1_000.0):
        self.now = now

    def __call__(self):
        return self.now


class Sleeps:
    def __init__(self, clock):
        self.clock = clock
        self.waits = []

    async def __call__(self, seconds):
        self.waits.append(seconds)
        self.clock.now += seconds


class Chunks(httpx.AsyncByteStream):
    def __init__(self, *parts):
        self.parts = parts

    async def __aiter__(self):
        for part in self.parts:
            if isinstance(part, Exception):
                raise part
            yield part


def _on_the_wire(response):
    """An unread copy of a pre-read test response, as the network delivers it."""

    if not response.is_stream_consumed:
        return response
    return httpx.Response(
        response.status_code,
        headers=response.headers,
        stream=httpx.ByteStream(response.content),
    )


class Provider:
    """Scripted MockTransport handler that records every request it receives."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, Exception):
            raise outcome
        return _on_the_wire(outcome() if callable(outcome) else outcome)


def _route(monkeypatch, provider):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "labbioagentos.kernel.http_stream.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(provider), **kwargs),
    )


def _transport(*, max_retries=0, clock=None, random_fraction=0.0, wall_clock=0.0):
    clock = clock or Clock()
    sleeps = Sleeps(clock)
    return HttpStreamTransport(
        retry=RetryPolicy(max_retries=max_retries),
        sleep=sleeps,
        clock=clock,
        wall_clock=lambda: wall_clock,
        random_fraction=lambda: random_fraction,
    ), sleeps


async def _post(transport, *, deadline=1_600.0, limit=MIB, decode=None, headers=None):
    return await transport.post(
        url=URL,
        headers=headers or {"Authorization": "Bearer fixture-key"},
        payload={"model": "fixture", "stream": decode is not None},
        deadline_monotonic=deadline,
        max_response_bytes=limit,
        decode_stream=decode,
    )


async def _collect(events):
    return {"events": [(event.event, event.data) async for event in events]}


def _ok():
    return httpx.Response(200, json={"ok": True})


def _sse(text):
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)


def _binding():
    return ProviderBinding(
        provider="compatible",
        model="model-a",
        endpoint="https://provider.invalid/v1",
        transport="OPENAI_COMPATIBLE_CHAT",
        thinking_mode="DISABLED",
    )


def _request(binding, *, max_output_tokens=512):
    return ModelRequest(
        binding=binding,
        messages=(PublicMessage(role="USER", content="Run."),),
        max_output_tokens=max_output_tokens,
        deadline_monotonic=10_000_000_000.0,
        kernel_run_id=uuid4(),
        invocation_id=uuid4(),
        turn_index=1,
    )


# Response byte cap (criterion 2)


def test_byte_cap_scales_with_requested_output_tokens():
    assert response_byte_cap(1) == 8 * MIB + 1024
    assert response_byte_cap(8_192) == 16 * MIB
    assert response_byte_cap(16_384) == 24 * MIB
    assert response_byte_cap(32_768) == 40 * MIB
    assert response_byte_cap(131_072) == 136 * MIB == MAX_RESPONSE_BYTES
    for invalid in (0, -1, 1.0, True, None):
        with pytest.raises(ValueError):
            response_byte_cap(invalid)
    # MAX_RESPONSE_BYTES covers exactly the largest legal request.
    binding = _binding()
    _request(binding, max_output_tokens=131_072)
    with pytest.raises(ValueError):
        _request(binding, max_output_tokens=131_073)


@pytest.mark.parametrize("decode,response", [
    (_collect, lambda: httpx.Response(200, stream=Chunks(*[b"data: x\n\n"] * 200))),
    (_collect, lambda: httpx.Response(200, stream=Chunks(b"data: ", *[b"x" * 100] * 20))),
    (None, lambda: httpx.Response(200, json={"text": "x" * 2_000})),
    (None, lambda: httpx.Response(
        200, headers={"content-encoding": "gzip"},
        stream=Chunks(gzip.compress(b'{"text":"' + b"x" * 100_000 + b'"}')),
    )),
], ids=["many-events", "one-unterminated-line", "json-body", "gzip-decoded"])
async def test_byte_cap_rejects_oversized_response(monkeypatch, decode, response):
    provider = Provider(response)
    _route(monkeypatch, provider)
    transport, sleeps = _transport(max_retries=2)

    with pytest.raises(ProviderTransportError) as caught:
        await _post(transport, limit=1_024, decode=decode)

    assert caught.value.validation_type == "RESPONSE_TOO_LARGE"
    assert len(provider.requests) == 1
    assert sleeps.waits == []


@pytest.mark.parametrize("decode", [None, _collect])
async def test_byte_cap_admits_a_response_of_exactly_the_cap(monkeypatch, decode):
    body = b'{"ok":true}' if decode is None else b"data: exact\n\n"
    _route(monkeypatch, Provider(lambda: httpx.Response(200, content=body)))
    transport, _sleeps = _transport()

    await _post(transport, limit=len(body), decode=decode)
    with pytest.raises(ProviderTransportError) as caught:
        await _post(transport, limit=len(body) - 1, decode=decode)
    assert caught.value.validation_type == "RESPONSE_TOO_LARGE"


async def test_provider_applies_the_cap_of_its_own_request():
    class Recording:
        def __init__(self):
            self.caps = []

        async def post_json(self, *, url, headers, payload, timeout_seconds, max_response_bytes):
            self.caps.append(max_response_bytes)
            return HttpStreamResult(body={
                "choices": [{"finish_reason": "stop", "message": {"content": "done"}}],
            })

    binding = _binding()
    transport = Recording()
    provider = OpenAICompatibleChatProvider(binding=binding, api_key="key", transport=transport)

    await provider.generate(_request(binding, max_output_tokens=512))
    await provider.generate(_request(binding, max_output_tokens=16_384))

    assert transport.caps == [8 * MIB + 512 * 1024, 24 * MIB]


async def test_oversized_stream_returns_no_partial_turn(monkeypatch):
    body = 'data: {"choices":[{"index":0,"delta":{"content":"' + "x" * (8 * MIB + 2_048) + '"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
    _route(monkeypatch, Provider(lambda: _sse(body)))
    binding = _binding()
    provider = OpenAICompatibleChatProvider(
        binding=binding, api_key="key", transport=HttpxChatCompletionTransport(),
    )

    with pytest.raises(ProviderTransportError) as caught:
        await provider.generate(_request(binding, max_output_tokens=1))

    assert caught.value.validation_type == "RESPONSE_TOO_LARGE"


# Redirects (criterion 3)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("credential", [
    {"Authorization": "Bearer PRIVATE_CANARY"},
    {"x-api-key": "PRIVATE_CANARY"},
    {"x-goog-api-key": "PRIVATE_CANARY"},
])
async def test_redirect_is_final_for_every_credential_header(monkeypatch, status, credential):
    provider = Provider(lambda: httpx.Response(
        status, headers={"location": "https://collector.invalid/PRIVATE_CANARY"},
    ))
    _route(monkeypatch, provider)
    transport, sleeps = _transport(max_retries=2)

    with pytest.raises(ProviderTransportError) as caught:
        await _post(transport, headers=credential)

    assert caught.value.validation_type == f"HTTP_{status}"
    assert [request.url.host for request in provider.requests] == ["provider.invalid"]
    assert sleeps.waits == []
    assert "PRIVATE_CANARY" not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


# Retry scope, attempts, retry-after and deadline (criterion 4)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 408, 409, 422])
async def test_non_retryable_status_fails_at_once(monkeypatch, status):
    provider = Provider(lambda: httpx.Response(status, json={"error": "PRIVATE_CANARY"}))
    _route(monkeypatch, provider)
    transport, sleeps = _transport(max_retries=2)

    with pytest.raises(ProviderTransportError) as caught:
        await _post(transport)

    assert caught.value.validation_type == f"HTTP_{status}"
    assert len(provider.requests) == 1
    assert sleeps.waits == []
    assert "PRIVATE_CANARY" not in str(caught.value)


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 529])
async def test_retryable_status_is_retried_and_recorded(monkeypatch, status):
    provider = Provider(lambda: httpx.Response(status), _ok)
    _route(monkeypatch, provider)
    transport, sleeps = _transport(max_retries=2)

    result = await _post(transport)

    assert result.body == {"ok": True}
    assert result.retries == (ProviderRetry(category=f"HTTP_{status}", wait_ms=500),)
    assert len(provider.requests) == 2
    assert sleeps.waits == [0.5]


@pytest.mark.parametrize("failure,category", [
    (httpx.ConnectError("PRIVATE_CANARY"), "NETWORK"),
    (httpx.WriteError("PRIVATE_CANARY"), "NETWORK"),
    (httpx.ReadError("PRIVATE_CANARY"), "NETWORK_READ"),
    (httpx.RemoteProtocolError("PRIVATE_CANARY"), "NETWORK_REMOTE_PROTOCOL"),
])
async def test_connection_failure_is_retried(monkeypatch, failure, category):
    provider = Provider(failure, _ok)
    _route(monkeypatch, provider)
    transport, _sleeps = _transport(max_retries=2)

    result = await _post(transport)

    assert result.retries == (ProviderRetry(category=category, wait_ms=500),)
    assert len(provider.requests) == 2


async def test_connection_lost_mid_stream_discards_the_attempt(monkeypatch):
    provider = Provider(
        lambda: httpx.Response(200, stream=Chunks(b"data: partial\n\n", httpx.ReadError("x"))),
        lambda: _sse("data: complete\n\n"),
    )
    _route(monkeypatch, provider)
    transport, _sleeps = _transport(max_retries=2)

    result = await _post(transport, decode=_collect)

    assert result.body == {"events": [(None, "complete")]}
    assert result.retries == (ProviderRetry(category="NETWORK_READ", wait_ms=500),)


@pytest.mark.parametrize("outcome,decode,category", [
    (httpx.ReadTimeout("x"), None, "TIMEOUT"),
    (httpx.ConnectTimeout("x"), None, "TIMEOUT"),
    (httpx.ProxyError("x"), None, "NETWORK"),
    (lambda: httpx.Response(200, content=b'{"a":1,"a":2}'), None, "INVALID_JSON"),
    (lambda: httpx.Response(200, content=b"[1]"), None, "INVALID_JSON"),
    (lambda: httpx.Response(200, content=b'{"a":NaN}'), None, "INVALID_JSON"),
    (lambda: httpx.Response(200, content=b'{"a":1e999}'), None, "INVALID_JSON"),
    (lambda: httpx.Response(200, content=b"not json"), None, "INVALID_JSON"),
], ids=["read-timeout", "connect-timeout", "proxy", "duplicate-key", "not-object",
        "nan", "overflow", "not-json"])
async def test_final_failures_are_not_retried(monkeypatch, outcome, decode, category):
    provider = Provider(outcome)
    _route(monkeypatch, provider)
    transport, sleeps = _transport(max_retries=2)

    with pytest.raises(ProviderTransportError) as caught:
        await _post(transport, decode=decode)

    assert caught.value.validation_type == category
    assert len(provider.requests) == 1
    assert sleeps.waits == []


async def test_decoder_protocol_error_is_not_retried(monkeypatch):
    async def reject(events):
        async for _event in events:
            raise ProviderProtocolError(validation_fields=("stream.choices",))

    provider = Provider(lambda: _sse("data: {}\n\n"))
    _route(monkeypatch, provider)
    transport, _sleeps = _transport(max_retries=2)

    with pytest.raises(ProviderProtocolError) as caught:
        await _post(transport, decode=reject)

    assert caught.value.validation_fields == ("stream.choices",)
    assert len(provider.requests) == 1


@pytest.mark.parametrize("category", ["STREAM_OVERLOADED", "STREAM_RATE_LIMITED"])
async def test_stream_overload_event_is_retried(monkeypatch, category):
    async def decode(events):
        collected = await _collect(events)
        if collected["events"] == [("error", "busy")]:
            raise RetryableStreamError(category)
        return collected

    provider = Provider(
        lambda: _sse("event: error\ndata: busy\n\n"),
        lambda: _sse("data: complete\n\n"),
    )
    _route(monkeypatch, provider)
    transport, _sleeps = _transport(max_retries=2)

    result = await _post(transport, decode=decode)

    assert result.body == {"events": [(None, "complete")]}
    assert result.retries == (ProviderRetry(category=category, wait_ms=500),)


def test_stream_retry_category_is_bounded():
    with pytest.raises(ValueError):
        RetryableStreamError("PRIVATE_PROVIDER_MESSAGE")


async def test_chat_stream_error_event_fails_without_retry(monkeypatch):
    provider = Provider(lambda: _sse(
        'event: error\ndata: {"error":{"type":"overloaded_error"}}\n\n'
    ))
    _route(monkeypatch, provider)

    # Chat documents no in-stream error codes, so none is retried.
    with pytest.raises(ProviderTransportError) as caught:
        await HttpxChatCompletionTransport(max_retries=2).post_json(
            url=URL, headers={}, payload={"model": "fixture", "stream": True},
            timeout_seconds=600,
        )

    assert caught.value.validation_type == "STREAM_ERROR"
    assert caught.value.transport_retries == ()
    assert len(provider.requests) == 1


async def test_attempts_are_capped(monkeypatch):
    provider = Provider(lambda: httpx.Response(503))
    _route(monkeypatch, provider)
    transport, sleeps = _transport(max_retries=2)

    with pytest.raises(ProviderTransportError) as caught:
        await _post(transport)

    assert caught.value.validation_type == "HTTP_503"
    assert len(provider.requests) == 3
    assert sleeps.waits == [0.5, 1.0]


async def test_default_policy_retries_twice(monkeypatch):
    provider = Provider(lambda: httpx.Response(503))
    _route(monkeypatch, provider)
    clock = Clock()
    sleeps = Sleeps(clock)

    assert RetryPolicy().max_retries == 2
    assert HttpxChatCompletionTransport()._http.retry == RetryPolicy()
    with pytest.raises(ProviderTransportError) as caught:
        await HttpStreamTransport(
            sleep=sleeps, clock=clock, random_fraction=lambda: 0.0,
        ).post(
            url=URL, headers={}, payload={}, deadline_monotonic=clock.now + 600,
            max_response_bytes=MIB,
        )
    assert len(provider.requests) == 3
    assert caught.value.transport_retries == (
        ProviderRetry(category="HTTP_503", wait_ms=500),
        ProviderRetry(category="HTTP_503", wait_ms=1000),
    )


@pytest.mark.parametrize("max_retries", [-1, 11, True, 1.0, None])
def test_retry_policy_rejects_unbounded_attempts(max_retries):
    with pytest.raises(ValueError):
        RetryPolicy(max_retries=max_retries)


@pytest.mark.parametrize("headers,expected", [
    ({"retry-after": "3"}, 3.0),
    ({"retry-after": "2.5"}, 2.5),
    ({"retry-after-ms": "1500"}, 1.5),
    ({"retry-after-ms": "1500", "retry-after": "9"}, 1.5),
    ({"retry-after-ms": "soon", "retry-after": "9"}, 9.0),
    ({"retry-after": format_datetime(datetime.fromtimestamp(5_007, timezone.utc), usegmt=True)}, 7.0),
    ({"retry-after": format_datetime(datetime.fromtimestamp(4_000, timezone.utc), usegmt=True)}, 0.0),
    ({"retry-after": "soon"}, 0.5),
    ({"retry-after": "-1"}, 0.5),
    ({"retry-after": "nan"}, 0.5),
    ({"retry-after": "inf"}, 0.5),
    ({}, 0.5),
])
async def test_retry_after_is_honored(monkeypatch, headers, expected):
    provider = Provider(lambda: httpx.Response(429, headers=headers), _ok)
    _route(monkeypatch, provider)
    transport, sleeps = _transport(max_retries=1, wall_clock=5_000.0)

    result = await _post(transport)

    assert sleeps.waits == [expected]
    assert result.retries == (ProviderRetry(category="HTTP_429", wait_ms=round(expected * 1000)),)


@pytest.mark.parametrize("headers,deadline", [
    ({"retry-after": "30"}, 1_010.0),
    # The 0.5 s backoff would end inside the 0.5 s deadline margin.
    ({}, 1_000.9),
])
async def test_retry_never_waits_past_the_deadline(monkeypatch, headers, deadline):
    provider = Provider(lambda: httpx.Response(429, headers=headers), _ok)
    _route(monkeypatch, provider)
    transport, sleeps = _transport(max_retries=2)

    with pytest.raises(ProviderTransportError) as caught:
        await _post(transport, deadline=deadline)

    assert caught.value.validation_type == "HTTP_429"
    assert len(provider.requests) == 1
    assert sleeps.waits == []


class Stalls(httpx.AsyncByteStream):
    """A response body that sends one event and then never finishes."""

    async def __aiter__(self):
        yield b"data: partial\n\n"
        await asyncio.Event().wait()


async def test_deadline_ends_a_stalled_attempt_with_its_retry_records(monkeypatch):
    provider = Provider(
        lambda: httpx.Response(503, headers={"retry-after": "0"}),
        lambda: httpx.Response(200, stream=Stalls()),
    )
    _route(monkeypatch, provider)
    clock = Clock(1_000.0)
    transport, _sleeps = _transport(max_retries=2, clock=clock)

    with pytest.raises(KernelDeadlineExceededError) as caught:
        # 0.2 s of real time remain once the margin is taken off; the outer
        # limit only turns a missing deadline into a failure instead of a hang.
        await asyncio.wait_for(_post(transport, deadline=1_000.7, decode=_collect), 5)

    assert caught.value.transport_retries == (ProviderRetry(category="HTTP_503", wait_ms=0),)
    assert len(provider.requests) == 2
    # Per-phase httpx limits run to the deadline itself, after the total limit.
    assert provider.requests[1].extensions["timeout"]["read"] == pytest.approx(0.7)


async def test_a_wait_that_ends_too_late_keeps_the_original_failure(monkeypatch):
    provider = Provider(lambda: httpx.Response(503), _ok)
    _route(monkeypatch, provider)
    clock = Clock(1_000.0)

    async def oversleeping(seconds):
        # The process was slow: the wait ends well after it was due.
        clock.now += seconds + 5

    transport = HttpStreamTransport(
        retry=RetryPolicy(max_retries=2), sleep=oversleeping, clock=clock,
        wall_clock=lambda: 0.0, random_fraction=lambda: 0.0,
    )

    with pytest.raises(ProviderTransportError) as caught:
        await _post(transport, deadline=1_006.0)

    assert caught.value.validation_type == "HTTP_503"
    assert caught.value.transport_retries == (ProviderRetry(category="HTTP_503", wait_ms=500),)
    assert len(provider.requests) == 1


async def test_no_attempt_starts_inside_the_deadline_margin(monkeypatch):
    provider = Provider(_ok)
    _route(monkeypatch, provider)
    transport, _sleeps = _transport()

    with pytest.raises(KernelDeadlineExceededError) as caught:
        await _post(transport, deadline=1_000.5)

    assert caught.value.transport_retries == ()
    assert provider.requests == []


@pytest.mark.parametrize("fraction,factor", [(0.0, 1.0), (0.5, 0.875)])
async def test_backoff_doubles_to_its_ceiling_with_jitter(monkeypatch, fraction, factor):
    _route(monkeypatch, Provider(lambda: httpx.Response(500)))
    transport, sleeps = _transport(max_retries=6, random_fraction=fraction)

    with pytest.raises(ProviderTransportError):
        await _post(transport)

    assert sleeps.waits == [base * factor for base in (0.5, 1.0, 2.0, 4.0, 8.0, 8.0)]


async def test_retry_resends_the_identical_request_with_remaining_time(monkeypatch):
    provider = Provider(lambda: httpx.Response(503, headers={"retry-after": "4"}), _ok)
    _route(monkeypatch, provider)
    clock = Clock(1_000.0)
    transport, _sleeps = _transport(max_retries=1, clock=clock)

    await _post(transport, deadline=1_100.0)

    first, second = provider.requests
    for request in (first, second):
        assert request.method == "POST"
        assert str(request.url) == URL
    assert first.content == second.content == b'{"model":"fixture","stream":false}'
    assert first.headers.multi_items() == second.headers.multi_items()
    assert first.headers["content-type"] == "application/json"
    assert first.extensions["timeout"]["read"] == 100.0
    assert second.extensions["timeout"]["read"] == 96.0


# Content encoding (DECIDED-C14-8)


def _gzip_bomb(size):
    deflate = zlib.compressobj(9, zlib.DEFLATED, zlib.MAX_WBITS | 16)
    block = bytes(1024 * 1024)
    parts = [deflate.compress(block) for _ in range(size // len(block))]
    return b"".join([*parts, deflate.flush()])


async def test_request_asks_only_for_gzip(monkeypatch):
    provider = Provider(_ok)
    _route(monkeypatch, provider)
    transport, _sleeps = _transport()

    await _post(transport, headers={"Authorization": "Bearer key", "Accept-Encoding": "br"})

    assert provider.requests[0].headers.get_list("accept-encoding") == ["gzip"]


@pytest.mark.parametrize("encoding", [None, "identity", "gzip", "x-gzip", "GZIP"])
@pytest.mark.parametrize("split", ["whole", "bytes"])
async def test_identity_and_gzip_bodies_decode(monkeypatch, encoding, split):
    text = "data: first\n\ndata: caf\u00e9\n\n".encode("utf-8")
    raw = gzip.compress(text) if encoding not in {None, "identity"} else text
    parts = (raw,) if split == "whole" else tuple(raw[i:i + 1] for i in range(len(raw)))
    headers = {} if encoding is None else {"content-encoding": encoding}
    _route(monkeypatch, Provider(lambda: httpx.Response(200, headers=headers, stream=Chunks(*parts))))
    transport, _sleeps = _transport()

    result = await _post(transport, decode=_collect)

    assert result.body == {"events": [(None, "first"), (None, "caf\u00e9")]}


async def test_gzip_body_may_hold_several_members(monkeypatch):
    raw = gzip.compress(b'{"ok":') + gzip.compress(b"true}")
    _route(monkeypatch, Provider(lambda: httpx.Response(
        200, headers={"content-encoding": "gzip"}, stream=Chunks(raw),
    )))
    transport, _sleeps = _transport()

    result = await _post(transport)

    assert result.body == {"ok": True}


@pytest.mark.parametrize("encoding", ["br", "zstd", "deflate", "compress", "gzip, br", "gzip, gzip"])
async def test_other_encodings_are_rejected_without_retry(monkeypatch, encoding):
    provider = Provider(lambda: httpx.Response(
        200, headers={"content-encoding": encoding}, stream=Chunks(b"opaque"),
    ))
    _route(monkeypatch, provider)
    transport, sleeps = _transport(max_retries=2)

    with pytest.raises(ProviderTransportError) as caught:
        await _post(transport, decode=_collect)

    assert caught.value.validation_type == "UNSUPPORTED_ENCODING"
    assert len(provider.requests) == 1
    assert sleeps.waits == []


VALID_GZIP = gzip.compress(b'{"ok":true}')


@pytest.mark.parametrize("raw", [
    VALID_GZIP[:-8],
    VALID_GZIP[:10] + b"not deflate data",
    VALID_GZIP + b"trailing",
    b"plain text",
], ids=["truncated", "corrupt", "trailing-bytes", "not-gzip"])
async def test_corrupt_gzip_is_rejected_without_retry(monkeypatch, raw):
    provider = Provider(lambda: httpx.Response(
        200, headers={"content-encoding": "gzip"}, stream=Chunks(raw),
    ))
    _route(monkeypatch, provider)
    transport, _sleeps = _transport(max_retries=2)

    with pytest.raises(ProviderTransportError) as caught:
        await _post(transport)

    assert caught.value.validation_type == "INVALID_ENCODING"
    assert len(provider.requests) == 1


@pytest.mark.parametrize("decode", [None, _collect])
async def test_gzip_bomb_never_inflates_past_the_cap(monkeypatch, decode):
    bomb = _gzip_bomb(64 * MIB)
    assert len(bomb) < MIB // 8
    _route(monkeypatch, Provider(lambda: httpx.Response(
        200, headers={"content-encoding": "gzip"}, stream=Chunks(bomb),
    )))
    transport, _sleeps = _transport()

    tracemalloc.start()
    try:
        with pytest.raises(ProviderTransportError) as caught:
            await _post(transport, limit=MIB, decode=decode)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert caught.value.validation_type == "RESPONSE_TOO_LARGE"
    # The 64 MiB body is never inflated; memory stays near the 1 MiB cap.
    assert peak < 8 * MIB


# SSE framing and strict decoding


@pytest.mark.parametrize("split", ["whole", "bytes"])
async def test_sse_framing_accepts_every_line_ending_and_split_point(monkeypatch, split):
    text = (
        'event: alpha\r\ndata: {"a":1}\r\n\r\n'
        ": comment\nid: 7\nretry: 10\ndata: first\ndata: second\n\n"
        "data: cr\r\r"
        "data: caf\u00e9 \u2028 \u4e2d\n\n"
    )
    raw = text.encode("utf-8")
    parts = (raw,) if split == "whole" else tuple(raw[i:i + 1] for i in range(len(raw)))
    _route(monkeypatch, Provider(lambda: httpx.Response(200, stream=Chunks(*parts))))
    transport, _sleeps = _transport()

    result = await _post(transport, decode=_collect)

    assert result.body == {"events": [
        ("alpha", '{"a":1}'),
        (None, "first\nsecond"),
        (None, "cr"),
        (None, "caf\u00e9 \u2028 \u4e2d"),
    ]}


@pytest.mark.parametrize("body", [b"data: partial\n", b"data: partial", b"event: x\ndata: partial\r"])
async def test_sse_stream_ending_inside_an_event_is_incomplete(monkeypatch, body):
    provider = Provider(lambda: httpx.Response(200, content=body))
    _route(monkeypatch, provider)
    transport, sleeps = _transport(max_retries=2)

    with pytest.raises(ProviderTransportError) as caught:
        await _post(transport, decode=_collect)

    assert caught.value.validation_type == "STREAM_INCOMPLETE"
    assert len(provider.requests) == 1
    assert sleeps.waits == []


@pytest.mark.parametrize("body", [b"data: \xff\n\n", b"data: \xe4\xb8"])
async def test_sse_requires_strict_utf8(monkeypatch, body):
    _route(monkeypatch, Provider(lambda: httpx.Response(200, content=body)))
    transport, _sleeps = _transport()

    with pytest.raises(ProviderTransportError) as caught:
        await _post(transport, decode=_collect)

    assert caught.value.validation_type == "INVALID_JSON"


@pytest.mark.parametrize("chunk", [
    '{"id":"r","id":"r","choices":[]}',
    '{"choices":[{"index":0,"delta":{"content":"x"},"finish_reason":Infinity}]}',
])
async def test_chat_stream_chunks_are_strict_json(monkeypatch, chunk):
    _route(monkeypatch, Provider(lambda: _sse(f"data: {chunk}\n\ndata: [DONE]\n\n")))

    with pytest.raises(ProviderTransportError) as caught:
        await HttpxChatCompletionTransport().post_json(
            url=URL, headers={}, payload={"model": "fixture", "stream": True},
            timeout_seconds=600,
        )

    assert caught.value.validation_type == "INVALID_JSON"
