"""Rejected provider output must not erase independently valid usage facts."""

import asyncio
import json
from time import monotonic

import httpx
import pytest

from labbioagentos.kernel import (
    AgentLoop, BudgetLedger, HttpxChatCompletionTransport,
    KernelDeadlineExceededError, OpenAICompatibleChatProvider,
    ProviderProtocolError, ProviderTransportError,
    PublicMessage,
)
from test_agent_loop import FinalEnvelope, _binding, _invocation, _limits
from test_openai_chat_provider import RecordingTransport


def _loop(transport, *, tokens=100):
    ledger = BudgetLedger(_limits(tokens=tokens))
    loop = AgentLoop(
        provider=OpenAICompatibleChatProvider(
            binding=_binding(), api_key="fixture-only", transport=transport,
        ),
        invocation=_invocation(final=True), budget_ledger=ledger,
    )
    return loop, ledger


async def _run(loop, *, timeout=5):
    return await loop.run(
        messages=(PublicMessage(role="USER", content="Synthetic accounting check"),),
        deadline_monotonic=monotonic() + timeout, max_output_tokens=64,
        response_model=FinalEnvelope,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("usage,total,event", [
    ({"prompt_tokens": 9, "completion_tokens": 12, "total_tokens": 21},
     21, "MODEL_USAGE_ON_FAILURE"),
    ({"prompt_tokens": 9}, None, "MODEL_USAGE_ON_FAILURE"),
    ({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
     0, "MODEL_USAGE_ON_FAILURE"),
    (None, None, "MODEL_USAGE_UNREPORTED"),
    ({"prompt_tokens": 9, "total_tokens": 2}, None, "MODEL_USAGE_UNREPORTED"),
    ({"total_tokens": True}, None, "MODEL_USAGE_UNREPORTED"),
])
async def test_invalid_response_preserves_valid_usage_without_accepting_output(usage, total, event):
    transport = RecordingTransport(({
        "choices": [{"finish_reason": "stop", "message": {
            "content": {"invalid": "PRIVATE_CANARY"},
        }}],
        "usage": usage,
    },))
    loop, ledger = _loop(transport, tokens=10)
    with pytest.raises(ProviderProtocolError) as caught:
        await _run(loop)
    assert caught.value.validation_fields == ("message.content",)
    assert ledger.usage.tokens == total
    outcomes = [item for item in loop.events if "USAGE" in item.kind.value]
    assert len(outcomes) == 1
    assert outcomes[0].kind.value == event
    assert outcomes[0].numeric_facts["total_tokens"] == total
    assert outcomes[0].numeric_facts["input_tokens"] == (
        usage.get("prompt_tokens") if event == "MODEL_USAGE_ON_FAILURE" else None
    )
    assert not loop.observations and not loop.tool_results
    assert len(transport.requests) == 1
    assert "PRIVATE_CANARY" not in str(caught.value)
    assert "PRIVATE_CANARY" not in outcomes[0].model_dump_json()


class _Stream(httpx.AsyncByteStream):
    def __init__(self, suffix):
        self.suffix = suffix

    async def __aiter__(self):
        yield (
            b'data: {"id":"fixture","choices":[{"index":0,"delta":{"content":"partial"},'
            b'"finish_reason":"stop"}]}\n\n'
            b'data: {"id":"fixture","choices":[],"usage":{"prompt_tokens":9,'
            b'"completion_tokens":12,"total_tokens":21}}\n\n'
        )
        if self.suffix == "network":
            raise httpx.ReadError("PRIVATE_CANARY")
        if self.suffix == "cancel":
            raise asyncio.CancelledError()
        if self.suffix == "deadline":
            await asyncio.Event().wait()
        if self.suffix == "invalid_usage":
            yield b'data: {"id":"fixture","choices":[],"usage":{"total_tokens":-1}}\n\n'
        if self.suffix == "invalid_usage_done":
            yield b'data: {"id":"fixture","choices":[],"usage":{"total_tokens":-1}}\n\ndata: [DONE]\n\n'
        if self.suffix == "foreign_id":
            yield b'data: {"id":"another","choices":[]}\n\n'


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix,error,snapshot_total", [
    ("truncated", ProviderTransportError, 21),
    ("network", ProviderTransportError, 21),
    ("cancel", asyncio.CancelledError, 21),
    ("deadline", KernelDeadlineExceededError, 21),
    ("invalid_usage", ProviderTransportError, 21),
    ("invalid_usage_done", ProviderProtocolError, 21),
    ("foreign_id", ProviderProtocolError, None),
])
async def test_stream_failure_preserves_only_valid_request_usage(
    monkeypatch, suffix, error, snapshot_total,
):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "labbioagentos.kernel.openai_chat.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                stream=_Stream(suffix),
            ),
        ), **kwargs),
    )
    loop, ledger = _loop(HttpxChatCompletionTransport(trust_env=False), tokens=10)
    with pytest.raises(error):
        await _run(loop, timeout=1 if suffix == "deadline" else 5)
    assert ledger.usage.tokens is None
    outcomes = [item for item in loop.events if "USAGE" in item.kind.value]
    assert len(outcomes) == 1 and outcomes[0].kind.value == "MODEL_USAGE_UNREPORTED"
    assert outcomes[0].numeric_facts["total_tokens"] is None
    assert outcomes[0].numeric_facts.get("last_validated_total_tokens") == snapshot_total
    assert outcomes[0].status_code == (
        "PARTIAL_RESPONSE_USAGE" if snapshot_total is not None else "UNKNOWN_USAGE"
    )
    assert not loop.observations and not loop.tool_results
    assert "PRIVATE_CANARY" not in outcomes[0].model_dump_json()


@pytest.mark.asyncio
async def test_valid_response_still_records_one_normal_turn():
    transport = RecordingTransport(({
        "choices": [{"finish_reason": "stop", "message": {
            "content": '{"result":{"count":1}}',
        }}],
        "usage": {"prompt_tokens": 9, "completion_tokens": 12, "total_tokens": 21},
    },))
    loop, ledger = _loop(transport)
    result = await _run(loop)
    assert result.output == {"result": {"count": 1}}
    assert ledger.usage.tokens == 21
    assert len(loop.observations) == 1
    assert not any("USAGE" in item.kind.value for item in loop.events)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_role", [False, True])
async def test_complete_stream_uses_latest_valid_usage_once(monkeypatch, invalid_role):
    frames = [
        {"id": "fixture", "choices": [{"index": 0, "delta": {
            "role": "user" if invalid_role else "assistant",
            "content": '{"result":{"count":1}}',
        }, "finish_reason": "stop"}]},
        {"id": "fixture", "choices": [], "usage": {"total_tokens": 21}},
        {"id": "fixture", "choices": [], "usage": {"total_tokens": -1}},
        {"id": "fixture", "choices": [], "usage": {"total_tokens": 24}},
    ]
    body = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames) + "data: [DONE]\n\n"
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "labbioagentos.kernel.openai_chat.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                stream=httpx.ByteStream(body.encode()),
            ),
        ), **kwargs),
    )
    loop, ledger = _loop(HttpxChatCompletionTransport(trust_env=False))
    if invalid_role:
        with pytest.raises(ProviderProtocolError):
            await _run(loop)
    else:
        await _run(loop)
    assert ledger.usage.tokens == 24
    assert len(loop.observations) == (0 if invalid_role else 1)
    outcomes = [item for item in loop.events if "USAGE" in item.kind.value]
    assert len(outcomes) == (1 if invalid_role else 0)
    if invalid_role:
        assert outcomes[0].kind.value == "MODEL_USAGE_ON_FAILURE"
        assert outcomes[0].numeric_facts["total_tokens"] == 24
        assert "last_validated_total_tokens" not in outcomes[0].numeric_facts


@pytest.mark.asyncio
async def test_complete_stream_invalid_tool_index_retains_final_usage(monkeypatch):
    frames = [
        {"id": "fixture", "choices": [{"index": 0, "delta": {
            "tool_calls": [{"index": 1, "id": "bad-index", "type": "function",
                            "function": {"name": "unused", "arguments": "{}"}}],
        }, "finish_reason": "tool_calls"}]},
        {"id": "fixture", "choices": [], "usage": {"total_tokens": 21}},
    ]
    body = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames) + "data: [DONE]\n\n"
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "labbioagentos.kernel.openai_chat.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                stream=httpx.ByteStream(body.encode()),
            ),
        ), **kwargs),
    )
    loop, ledger = _loop(HttpxChatCompletionTransport(trust_env=False), tokens=10)
    with pytest.raises(ProviderProtocolError) as caught:
        await _run(loop)
    assert caught.value.validation_fields == ("stream.delta.tool_calls",)
    assert ledger.usage.tokens == 21
    outcomes = [item for item in loop.events if "USAGE" in item.kind.value]
    assert len(outcomes) == 1 and outcomes[0].kind.value == "MODEL_USAGE_ON_FAILURE"
    assert outcomes[0].numeric_facts["total_tokens"] == 21
    assert "last_validated_total_tokens" not in outcomes[0].numeric_facts
    assert not loop.tool_results and not loop.observations
