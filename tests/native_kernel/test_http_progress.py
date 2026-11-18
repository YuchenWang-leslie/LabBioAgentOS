"""Content-free HTTP progress describes observed phases, not remote inference."""

import asyncio
import json

import httpcore
from httpcore._backends.mock import AsyncMockBackend, AsyncMockStream
import httpx
import pytest

from labbioagentos.kernel.http_stream import observe_http_progress
from test_http_stream import (
    Clock, Provider, _collect, _ok, _post, _route, _sse, _transport,
    forbid_network,  # noqa: F401 - apply the existing network prohibition
)


def _observer(events, *, fail=False):
    def observe(status, reason, facts):
        events.append((status, reason, dict(facts)))
        if fail:
            raise RuntimeError("private observer failure")
    return observe


def _events(events, status):
    return [event for event in events if event[0] == status]


@pytest.mark.asyncio
@pytest.mark.parametrize("pause,phase", [
    ("connect", "CONNECT_TCP"),
    ("headers", "WAIT_RESPONSE_HEADERS"),
    ("body", "READ_RESPONSE_BODY"),
])
async def test_real_httpcore_trace_distinguishes_stalled_phases(monkeypatch, pause, phase):
    waiting = asyncio.Event()
    body = b"data: partial\n\n"

    class Stream(AsyncMockStream):
        async def read(self, max_bytes, timeout=None):
            if pause == "headers" or not self._buffer:
                waiting.set()
                await asyncio.Event().wait()
            return await super().read(max_bytes, timeout)

    class Backend(AsyncMockBackend):
        async def connect_tcp(self, **kwargs):
            if pause == "connect":
                waiting.set()
                await asyncio.Event().wait()
            return Stream([
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n",
                f"{len(body):x}\r\n".encode() + body + b"\r\n",
            ])

    wire = httpx.AsyncHTTPTransport()
    wire._pool = httpcore.AsyncConnectionPool(network_backend=Backend([]))
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "labbioagentos.kernel.http_stream.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=wire, **kwargs),
    )
    events = []
    transport, _ = _transport()
    with observe_http_progress(_observer(events)):
        task = asyncio.create_task(_post(transport, decode=_collect))
        await asyncio.wait_for(waiting.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    cancelled = _events(events, "ATTEMPT_CANCELLED")
    assert len(cancelled) == 1
    assert cancelled[0][1] == phase
    assert cancelled[0][2]["attempt_index"] == 1
    assert not _events(events, "ATTEMPT_COMPLETED")
    headers = _events(events, "RESPONSE_HEADERS_RECEIVED")
    first_body = _events(events, "FIRST_RESPONSE_BODY_BYTES")
    assert bool(headers) is (pause == "body")
    assert bool(first_body) is (pause == "body")
    if pause == "body":
        assert cancelled[0][2]["response_status"] == 200
        assert cancelled[0][2]["response_bytes"] == len(body)
        assert cancelled[0][2]["response_chunks"] == 1
    else:
        assert cancelled[0][2]["response_status"] is None
        assert cancelled[0][2]["response_bytes"] == 0
        assert cancelled[0][2]["first_body_elapsed_ms"] is None


@pytest.mark.asyncio
async def test_success_records_headers_and_actual_body_separately_without_content(monkeypatch):
    marker = "private response body"
    body = json.dumps({"ok": marker}).encode()
    _route(monkeypatch, Provider(lambda: httpx.Response(
        200, headers={"secret-header": "private header"}, content=body,
    )))
    events = []
    transport, _ = _transport()
    with observe_http_progress(_observer(events)):
        result = await _post(transport)

    assert result.body == {"ok": marker}
    headers = _events(events, "RESPONSE_HEADERS_RECEIVED")
    first = _events(events, "FIRST_RESPONSE_BODY_BYTES")
    completed = _events(events, "ATTEMPT_COMPLETED")
    assert len(headers) == len(first) == len(completed) == 1
    assert events.index(headers[0]) < events.index(first[0]) < events.index(completed[0])
    assert headers[0][2]["response_bytes"] == 0
    assert headers[0][2]["first_body_elapsed_ms"] is None
    assert completed[0][1] == "COMPLETED"
    assert completed[0][2]["response_bytes"] == len(body)
    assert completed[0][2]["response_chunks"] == 1
    encoded = json.dumps(events)
    for private in (marker, "private header", "secret-header", "fixture-key", "provider.invalid"):
        assert private not in encoded


@pytest.mark.asyncio
@pytest.mark.parametrize("observer_fails", [False, True])
async def test_decoder_wait_after_body_eof_is_not_reported_as_network_wait(
    monkeypatch, observer_fails,
):
    _route(monkeypatch, Provider(lambda: _sse("data: complete\n\n")))
    waiting = asyncio.Event()

    async def decoder(events):
        assert [event.data async for event in events] == ["complete"]
        waiting.set()
        await asyncio.Event().wait()

    events = []
    transport, _ = _transport()
    with observe_http_progress(_observer(events, fail=observer_fails)):
        task = asyncio.create_task(_post(transport, decode=decoder))
        await asyncio.wait_for(waiting.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert len(_events(events, "BODY_COMPLETE")) == 1
    cancelled = _events(events, "ATTEMPT_CANCELLED")
    assert len(cancelled) == 1
    assert cancelled[0][1] == "DECODE_RESPONSE"
    assert not _events(events, "ATTEMPT_COMPLETED")


@pytest.mark.asyncio
async def test_retry_has_independent_attempt_counters_and_progress(monkeypatch):
    _route(monkeypatch, Provider(lambda: httpx.Response(503), _ok))
    events = []
    transport, _ = _transport(max_retries=1)
    with observe_http_progress(_observer(events)):
        result = await _post(transport)

    assert result.body == {"ok": True}
    starts = _events(events, "ATTEMPT_STARTED")
    assert [event[2]["attempt_index"] for event in starts] == [1, 2]
    assert all(event[2]["response_bytes"] == event[2]["response_chunks"] == 0 for event in starts)
    assert all(event[2]["response_status"] is None for event in starts)
    assert all(event[2]["first_body_elapsed_ms"] is None for event in starts)
    assert len(_events(events, "HTTP_RETRY_WAIT")) == 1
    assert _events(events, "ATTEMPT_FAILED")[0][2]["response_status"] == 503
    assert _events(events, "ATTEMPT_COMPLETED")[0][2]["attempt_index"] == 2


@pytest.mark.asyncio
async def test_body_progress_is_throttled_but_terminal_counters_are_complete(monkeypatch):
    clock = Clock()
    parts = [b" "] * 6 + [b'{"ok":true}']

    class TimedBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            for part in parts:
                yield part
                clock.now += 2

    _route(monkeypatch, Provider(lambda: httpx.Response(200, stream=TimedBody())))
    events = []
    transport, _ = _transport(clock=clock)
    with observe_http_progress(_observer(events)):
        result = await _post(transport)

    assert result.body == {"ok": True}
    progress = _events(events, "BODY_PROGRESS")
    assert 1 <= len(progress) <= 2
    times = [event[2]["elapsed_ms"] for event in progress]
    assert all(later - earlier >= 5000 for earlier, later in zip(times, times[1:]))
    completed = _events(events, "ATTEMPT_COMPLETED")[0][2]
    assert completed["response_bytes"] == sum(map(len, parts))
    assert completed["response_chunks"] == len(parts)


@pytest.mark.asyncio
async def test_observer_failure_cannot_change_success(monkeypatch):
    _route(monkeypatch, Provider(_ok))
    events = []
    transport, _ = _transport()
    with observe_http_progress(_observer(events, fail=True)):
        result = await _post(transport)
    assert result.body == {"ok": True}
    assert len(_events(events, "ATTEMPT_COMPLETED")) == 1


@pytest.mark.asyncio
async def test_overlapping_observers_are_context_local(monkeypatch):
    _route(monkeypatch, Provider(_ok))
    barriers = [asyncio.Event(), asyncio.Event()]
    histories = [[], []]
    transport, _ = _transport()

    async def invoke(index):
        with observe_http_progress(_observer(histories[index])):
            barriers[index].set()
            await barriers[1 - index].wait()
            return await _post(transport)

    results = await asyncio.gather(invoke(0), invoke(1))
    assert all(result.body == {"ok": True} for result in results)
    for events in histories:
        assert len(_events(events, "HTTP_REQUEST_STARTED")) == 1
        assert len(_events(events, "ATTEMPT_COMPLETED")) == 1
