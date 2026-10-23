"""Criterion 13: error categories, retry and the byte cap."""

from __future__ import annotations

import json

import httpx
import pytest

from labbioagentos.kernel import (
    KernelEventKind,
    ProviderProtocolError,
    ProviderTransportError,
)
from labbioagentos.kernel.http_stream import response_byte_cap

from .harness import DEADLINE, PROMPT, capability_loop, event_dumps
from .wire import WIRES, Call, Turn

DONE = Turn(text="done")


def _retries(loop):
    return [(event.reason_code, event.numeric_facts["wait_ms"]) for event in loop.events
            if event.kind is KernelEventKind.PROVIDER_RETRY_OBSERVED]


def _status(code: int, body: bytes = b"") -> httpx.Response:
    return httpx.Response(code, stream=httpx.ByteStream(body))


def test_formats_declare_their_differences():
    assert {
        wire.name: (wire.carries_reasoning, wire.stream_retry, wire.filter_category)
        for wire in WIRES
    } == {
        "chat-vendor": (True, None, None),
        "chat-official": (False, None, None),
        "responses": (True, "STREAM_RATE_LIMITED", None),
        "anthropic": (True, "STREAM_OVERLOADED", "bio"),
        "gemini": (True, "STREAM_OVERLOADED", "safety"),
        "interactions": (True, "STREAM_OVERLOADED", "safety"),
    }


async def test_rate_limited_request_is_retried_and_recorded(wire, server):
    server.script(_status(429), wire.encode(DONE))
    loop, _effects = capability_loop(wire.provider(wire.binding()))

    result = await loop.run(messages=PROMPT, deadline_monotonic=DEADLINE)

    assert result.output == "done"
    assert _retries(loop) == [("HTTP_429", 500)]
    assert len(server.requests) == 2


@pytest.mark.parametrize("wire", [wire for wire in WIRES if wire.stream_retry],
                         ids=lambda wire: wire.name)
async def test_in_stream_overload_or_rate_limit_is_retried(wire, server):
    server.script(wire.retryable_error(), wire.encode(DONE))
    loop, _effects = capability_loop(wire.provider(wire.binding()))

    result = await loop.run(messages=PROMPT, deadline_monotonic=DEADLINE)

    assert result.output == "done"
    assert _retries(loop) == [(wire.stream_retry, 500)]


async def test_fatal_stream_error_fails_without_retry_or_provider_text(wire, server):
    server.script(wire.fatal_error(), wire.encode(DONE))
    loop, _effects = capability_loop(wire.provider(wire.binding()))

    with pytest.raises(ProviderTransportError) as caught:
        await loop.run(messages=PROMPT, deadline_monotonic=DEADLINE)

    assert caught.value.validation_type == "STREAM_ERROR"
    assert "PRIVATE_CANARY" not in str(caught.value) + repr(caught.value) + event_dumps(loop)
    assert _retries(loop) == []
    assert len(server.requests) == 1


async def test_rejected_request_fails_without_retry(wire, server):
    rejection = json.dumps({"error": {"message": "PRIVATE_CANARY"}}).encode()
    server.script(_status(400, rejection), wire.encode(DONE))
    loop, _effects = capability_loop(wire.provider(wire.binding()))

    with pytest.raises(ProviderTransportError) as caught:
        await loop.run(messages=PROMPT, deadline_monotonic=DEADLINE)

    assert caught.value.validation_type == "HTTP_400"
    assert "PRIVATE_CANARY" not in str(caught.value) + event_dumps(loop)
    assert len(server.requests) == 1


async def test_truncated_stream_is_incomplete_and_not_retried(wire, server):
    server.script(wire.truncated(Turn(text="partial", calls=(Call("call-a", {"value": "a"}),))),
                  wire.encode(DONE))
    loop, effects = capability_loop(wire.provider(wire.binding()))

    with pytest.raises(ProviderTransportError) as caught:
        await loop.run(messages=PROMPT, deadline_monotonic=DEADLINE)

    assert caught.value.validation_type == "STREAM_INCOMPLETE"
    assert effects == []
    assert len(server.requests) == 1


@pytest.mark.parametrize("max_output_tokens,fails", [(1, True), (1_024, False)],
                         ids=["over-cap", "within-cap"])
async def test_byte_cap_follows_the_requested_output_limit(
    wire, server, max_output_tokens, fails,
):
    # A comment line carries no event, so only its size matters.
    padding = ": " + "x" * response_byte_cap(1) + "\n\n"
    server.script(padding + wire.encode(DONE))
    loop, _effects = capability_loop(wire.provider(wire.binding()))

    if fails:
        with pytest.raises(ProviderTransportError) as caught:
            await loop.run(messages=PROMPT, deadline_monotonic=DEADLINE,
                           max_output_tokens=max_output_tokens)
        assert caught.value.validation_type == "RESPONSE_TOO_LARGE"
    else:
        result = await loop.run(messages=PROMPT, deadline_monotonic=DEADLINE,
                                max_output_tokens=max_output_tokens)
        assert result.output == "done"
    assert len(server.requests) == 1


async def test_unknown_content_type_fails_and_is_named(wire, server):
    body, fields = wire.unknown_content()
    server.script(body)
    loop, _effects = capability_loop(wire.provider(wire.binding()))

    with pytest.raises(ProviderProtocolError) as caught:
        await loop.run(messages=PROMPT, deadline_monotonic=DEADLINE)

    assert caught.value.validation_type == "unsupported_type"
    assert caught.value.validation_fields == fields


@pytest.mark.parametrize("failure,termination", [
    ("fatal", "PROVIDER_TRANSPORT_STOP"), ("unknown", "PROVIDER_PROTOCOL_STOP"),
])
async def test_provider_failure_after_evidence_stops_the_capability_run(
    wire, server, failure, termination,
):
    server.script(
        wire.encode(Turn(calls=(Call("call-a", {"value": "a"}),), reasoning="PRIVATE_X"), 1),
        wire.fatal_error() if failure == "fatal" else wire.unknown_content()[0],
    )
    loop, effects = capability_loop(wire.provider(wire.binding()))

    result = await loop.run(messages=PROMPT, deadline_monotonic=DEADLINE)

    assert result.output == {"capability_stop_reason": termination}
    assert effects == ["a"]
    assert "PRIVATE_X" not in result.model_dump_json() + event_dumps(loop)
