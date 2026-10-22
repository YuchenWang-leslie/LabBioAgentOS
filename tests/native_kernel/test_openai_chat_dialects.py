"""C14 step 3: vendor and official OpenAI Chat Completions dialects."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import get_args
from uuid import uuid4

import httpx
import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from labbioagentos.kernel import (
    AgentInvocation,
    AgentLoop,
    AgentMode,
    BudgetLedger,
    FinishReason,
    HttpxChatCompletionTransport,
    IncompleteProviderResponseError,
    KernelEventKind,
    KernelIdentity,
    ModelRequest,
    OpenAIChatDialect,
    OpenAICompatibleChatProvider,
    OpenAIResponsesDialect,
    ProviderBinding,
    ProviderProtocolError,
    PublicMessage,
    ToolSpec,
)
from labbioagentos.kernel.contracts import ReasoningEffort
from labbioagentos.kernel.http_stream import HttpStreamResult

REAL_ASYNC_CLIENT = httpx.AsyncClient
OFFICIAL = OpenAIChatDialect(
    thinking_parameter="REASONING_EFFORT",
    reasoning_effort_enabled="high",
    reasoning_effort_disabled="none",
    output_limit_parameter="max_completion_tokens",
    stream_include_usage=True,
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {"view": {"type": "string", "maxLength": 16}},
    "required": ["view"],
    "additionalProperties": False,
}
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"result": {"type": "string"}},
    "required": ["result"],
    "additionalProperties": False,
}


class Recording:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.payloads = []

    async def post_json(self, *, url, headers, payload, timeout_seconds, max_response_bytes):
        self.payloads.append(deepcopy(payload))
        return HttpStreamResult(body=deepcopy(self.responses.pop(0)))


def _binding(dialect=None, *, thinking="ENABLED"):
    return ProviderBinding(
        provider="compatible",
        model="model-a",
        endpoint="https://provider.invalid/v1",
        transport="OPENAI_COMPATIBLE_CHAT",
        dialect=dialect,
        thinking_mode=thinking,
    )


def _request(binding, *, tools=True, schema=True):
    return ModelRequest(
        binding=binding,
        messages=(PublicMessage(role="USER", content="Run."),),
        tools=(ToolSpec(
            name="inspect",
            description="Inspect a bounded object.",
            parameters=TOOL_PARAMETERS,
        ),) if tools else (),
        response_schema=RESPONSE_SCHEMA if schema else None,
        max_output_tokens=512,
        deadline_monotonic=10_000_000_000.0,
        kernel_run_id=uuid4(),
        invocation_id=uuid4(),
        turn_index=1,
    )


def _stop(content="done"):
    return {"id": "request-1", "choices": [{
        "finish_reason": "stop", "message": {"content": content},
    }]}


async def _payload(binding, **request_values):
    transport = Recording(_stop())
    provider = OpenAICompatibleChatProvider(binding=binding, api_key="key", transport=transport)
    await provider.generate(_request(binding, **request_values))
    return transport.payloads[0]


def _expected_tail():
    return {
        "tools": [{"type": "function", "function": {
            "name": "inspect",
            "description": "Inspect a bounded object.",
            "parameters": TOOL_PARAMETERS,
            "strict": True,
        }}],
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "structured_output", "strict": True, "schema": RESPONSE_SCHEMA,
        }},
    }


# Request forms


@pytest.mark.parametrize("thinking", ["ENABLED", "DISABLED"])
@pytest.mark.asyncio
async def test_vendor_dialect_request_is_unchanged(thinking):
    payload = await _payload(_binding(thinking=thinking))

    assert payload == {
        "model": "model-a",
        "messages": [{"role": "user", "content": "Run."}],
        "max_tokens": 512,
        "n": 1,
        "stream": True,
        "thinking": {"type": thinking.lower()},
        **_expected_tail(),
    }
    assert list(payload) == [
        "model", "messages", "max_tokens", "n", "stream", "thinking",
        "tools", "response_format",
    ]


@pytest.mark.parametrize("thinking,effort", [("ENABLED", "high"), ("DISABLED", "none")])
@pytest.mark.asyncio
async def test_official_dialect_request(thinking, effort):
    payload = await _payload(_binding(OFFICIAL, thinking=thinking))

    assert payload == {
        "model": "model-a",
        "messages": [{"role": "user", "content": "Run."}],
        "max_completion_tokens": 512,
        "n": 1,
        "stream": True,
        "reasoning_effort": effort,
        "stream_options": {"include_usage": True},
        **_expected_tail(),
    }


@pytest.mark.parametrize("thinking", ["ENABLED", "DISABLED"])
@pytest.mark.asyncio
async def test_omitted_thinking_parameter_sends_neither_form(thinking):
    payload = await _payload(
        _binding(OpenAIChatDialect(thinking_parameter="OMIT"), thinking=thinking),
        tools=False, schema=False,
    )

    assert "thinking" not in payload
    assert "reasoning_effort" not in payload


@pytest.mark.parametrize("dialect", [None, OFFICIAL], ids=["vendor", "official"])
@pytest.mark.asyncio
async def test_neither_dialect_stores_or_offers_built_in_tools(dialect):
    payload = await _payload(_binding(dialect))

    assert "store" not in payload
    assert [tool["type"] for tool in payload["tools"]] == ["function"]


def test_every_documented_reasoning_effort_is_accepted():
    assert set(get_args(ReasoningEffort)) == {
        "none", "minimal", "low", "medium", "high", "xhigh", "max",
    }
    for effort in get_args(ReasoningEffort):
        OpenAIChatDialect(
            thinking_parameter="REASONING_EFFORT",
            reasoning_effort_enabled=effort, reasoning_effort_disabled=effort,
        )
    with pytest.raises(ValidationError):
        OpenAIChatDialect(
            thinking_parameter="REASONING_EFFORT",
            reasoning_effort_enabled="ultra", reasoning_effort_disabled="none",
        )


def test_responses_efforts_are_given_for_both_thinking_states_or_neither():
    OpenAIResponsesDialect()
    OpenAIResponsesDialect(reasoning_effort_enabled="high", reasoning_effort_disabled="none")
    with pytest.raises(ValidationError):
        OpenAIResponsesDialect(reasoning_effort_enabled="high")


# Official stream: usage chunk, obfuscation and refusal


def _route(monkeypatch, body):
    monkeypatch.setattr(
        "labbioagentos.kernel.http_stream.httpx.AsyncClient",
        lambda **kwargs: REAL_ASYNC_CLIENT(transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                stream=httpx.ByteStream(body.encode("utf-8")),
            ),
        ), **kwargs),
    )


def _sse(*chunks):
    return "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"


def _chunk(delta, finish=None):
    return {
        "id": "chatcmpl-1", "object": "chat.completion.chunk", "model": "model-a",
        "obfuscation": "pad", "usage": None,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish, "logprobs": None}],
    }


async def _official_turn(monkeypatch, *chunks):
    _route(monkeypatch, _sse(*chunks))
    binding = _binding(OFFICIAL)
    provider = OpenAICompatibleChatProvider(
        binding=binding, api_key="key", transport=HttpxChatCompletionTransport(),
    )
    return await provider.generate(_request(binding, tools=False, schema=False))


@pytest.mark.asyncio
async def test_official_stream_reports_usage_from_its_final_chunk(monkeypatch):
    turn = await _official_turn(
        monkeypatch,
        _chunk({"role": "assistant", "content": "", "refusal": None}),
        _chunk({"content": "done"}),
        _chunk({}, finish="stop"),
        {"id": "chatcmpl-1", "object": "chat.completion.chunk", "choices": [],
         "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12,
                   "completion_tokens_details": {"reasoning_tokens": 2}}},
    )

    assert turn.content == "done"
    assert turn.finish_reason is FinishReason.STOP
    assert (turn.usage.input_tokens, turn.usage.output_tokens, turn.usage.total_tokens) == (
        9, 3, 12,
    )
    assert turn.private_reasoning_present is False


@pytest.mark.asyncio
async def test_refusal_is_kept_only_as_a_content_filter_finish(monkeypatch):
    turn = await _official_turn(
        monkeypatch,
        _chunk({"role": "assistant", "refusal": "PRIVATE_REFUSAL "}),
        _chunk({"refusal": "text."}),
        _chunk({}, finish="stop"),
    )

    assert turn.finish_reason is FinishReason.CONTENT_FILTER
    assert turn.content is None
    assert turn.tool_calls == ()
    assert "PRIVATE_REFUSAL" not in repr(turn)


@pytest.mark.asyncio
async def test_empty_refusal_placeholder_is_not_a_refusal(monkeypatch):
    turn = await _official_turn(
        monkeypatch,
        _chunk({"role": "assistant", "content": "done", "refusal": ""}),
        _chunk({}, finish="stop"),
    )

    assert turn.finish_reason is FinishReason.STOP
    assert turn.content == "done"


@pytest.mark.asyncio
async def test_non_string_refusal_is_a_protocol_error(monkeypatch):
    with pytest.raises(ProviderProtocolError) as caught:
        await _official_turn(monkeypatch, _chunk({"refusal": 7}, finish="stop"))

    assert caught.value.validation_fields == ("stream.delta.refusal",)


# A refusal follows the loop's existing content-filter paths


def _refusal():
    return {"id": "request-2", "choices": [{
        "finish_reason": "stop", "message": {"refusal": "PRIVATE_REFUSAL"},
    }]}


@pytest.mark.asyncio
async def test_capability_with_evidence_stops_on_a_refusal():
    from test_agent_loop import _tool_loop

    loop, _provider, effects = _tool_loop(())
    binding = _binding(OFFICIAL)
    loop.provider = OpenAICompatibleChatProvider(
        binding=binding, api_key="key", transport=Recording(
            {"id": "request-1", "choices": [{"finish_reason": "tool_calls", "message": {
                "tool_calls": [{"id": "call-1", "type": "function", "function": {
                    "name": "record_value", "arguments": '{"value":"kept"}',
                }}],
            }}]},
            _refusal(),
        ),
    )

    result = await loop.run(
        messages=(PublicMessage(role="USER", content="Run."),),
        deadline_monotonic=10_000_000_000.0,
    )

    assert effects == [{"value": "kept", "nested": None}]
    assert result.termination == "INCOMPLETE_PROVIDER_STOP"
    assert result.output["finish_reason"] == "CONTENT_FILTER"
    assert "PRIVATE_REFUSAL" not in json.dumps(
        [message.model_dump(mode="json") for message in result.messages]
    )


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    result: str


@pytest.mark.asyncio
async def test_finalization_fails_at_once_on_a_refusal():
    from test_agent_loop import _limits

    binding = _binding(OFFICIAL)
    invocation_id = uuid4()
    transport = Recording(_refusal())
    loop = AgentLoop(
        provider=OpenAICompatibleChatProvider(
            binding=binding, api_key="key", transport=transport,
        ),
        invocation=AgentInvocation(
            identity=KernelIdentity(
                thread_id=uuid4(), workflow_run_id=uuid4(), kernel_run_id=uuid4(),
                invocation_id=invocation_id, chain=(invocation_id,),
            ),
            agent_id="leader",
            mode=AgentMode.FINALIZE,
        ),
        budget_ledger=BudgetLedger(_limits()),
    )

    # A content-filtered turn without content is an incomplete provider
    # response, rejected at once with no correction attempt.
    with pytest.raises(IncompleteProviderResponseError) as caught:
        await loop.run(
            messages=(PublicMessage(role="USER", content="Finish."),),
            deadline_monotonic=10_000_000_000.0,
            response_model=Envelope,
        )

    assert caught.value.validation_type == "CONTENT_FILTER"
    assert len(transport.payloads) == 1
    observed = [
        event.status_code for event in loop.events
        if event.kind is KernelEventKind.MODEL_TURN_OBSERVED
    ]
    assert observed == ["CONTENT_FILTER"]
