"""C14 step 4: Anthropic Messages API adapter (kernel layer only)."""

from __future__ import annotations

import json
import socket
from uuid import uuid4

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from labbioagentos.kernel import (
    AgentInvocation,
    AgentLoop,
    AgentMode,
    AnthropicMessagesDialect,
    AnthropicMessagesProvider,
    BudgetLedger,
    FinishReason,
    KernelIdentity,
    ModelRequest,
    ProviderBinding,
    ProviderProtocolError,
    ProviderRetry,
    ProviderTransportError,
    PublicMessage,
    ToolCall,
    ToolSpec,
)
from labbioagentos.kernel.anthropic_messages import STRICT_SCHEMA_REMOVED_KEYWORDS
from labbioagentos.kernel.http_stream import HttpStreamTransport
from labbioagentos.kernel.schema_projection import schema_instruction

REAL_ASYNC_CLIENT = httpx.AsyncClient
KEY = "sk-ant-PRIVATE_KEY_CANARY"
DIALECT = AnthropicMessagesDialect(
    thinking_off="OMIT", effort_enabled="high", effort_disabled="low",
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "view": {"type": "string", "minLength": 1, "maxLength": 16},
        "limit": {"type": "integer", "minimum": 1, "maximum": 5, "default": 3},
    },
    "required": ["view"],
    "additionalProperties": False,
}
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "result": {"type": "string", "maxLength": 100},
        "items": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
    },
    "required": ["result"],
    "additionalProperties": False,
}


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def denied(*_args, **_kwargs):
        pytest.fail("Adapter tests must not contact a network")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


# SSE fixtures


def _event(kind, **data):
    return f"event: {kind}\ndata: {json.dumps({'type': kind, **data})}\n\n"


def _start(message_id="msg_1", **usage):
    return _event("message_start", message={
        "id": message_id, "type": "message", "role": "assistant", "content": [],
        "model": "claude-opus-5-5", "stop_reason": None, "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 1, **usage},
    })


def _text(index, *parts):
    return (
        _event("content_block_start", index=index, content_block={"type": "text", "text": ""})
        + "".join(
            _event("content_block_delta", index=index, delta={"type": "text_delta", "text": part})
            for part in parts
        )
        + _event("content_block_stop", index=index)
    )


def _thinking(index, *signature_parts, thinking=""):
    return (
        _event("content_block_start", index=index,
               content_block={"type": "thinking", "thinking": "", "signature": ""})
        + _event("content_block_delta", index=index,
                 delta={"type": "thinking_delta", "thinking": thinking})
        + "".join(
            _event("content_block_delta", index=index,
                   delta={"type": "signature_delta", "signature": part})
            for part in signature_parts
        )
        + _event("content_block_stop", index=index)
    )


def _redacted(index, data):
    return (
        _event("content_block_start", index=index,
               content_block={"type": "redacted_thinking", "data": data})
        + _event("content_block_stop", index=index)
    )


def _tool(index, call_id, name, *json_parts):
    return (
        _event("content_block_start", index=index, content_block={
            "type": "tool_use", "id": call_id, "name": name, "input": {},
        })
        + "".join(
            _event("content_block_delta", index=index,
                   delta={"type": "input_json_delta", "partial_json": part})
            for part in json_parts
        )
        + _event("content_block_stop", index=index)
    )


def _end(stop_reason, output_tokens=5, stop_details=None):
    return (
        _event("message_delta", delta={
            "stop_reason": stop_reason, "stop_sequence": None, "stop_details": stop_details,
        }, usage={"output_tokens": output_tokens})
        + _event("message_stop")
    )


def _response(body):
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"},
        stream=httpx.ByteStream(body.encode("utf-8")),
    )


# Provider fixtures


def _binding(*, dialect=DIALECT, thinking="ENABLED", strict=False, json_object=False):
    return ProviderBinding(
        provider="anthropic",
        model="claude-opus-5-5",
        endpoint="https://api.anthropic.invalid/v1",
        transport="ANTHROPIC_MESSAGES",
        dialect=dialect,
        thinking_mode=thinking,
        provider_tool_schema_strict=strict,
        finalization_json_object=json_object,
    )


def _provider(monkeypatch, *outcomes, **binding_values):
    pending = list(outcomes)
    requests = []

    def handler(request):
        requests.append(request)
        outcome = pending.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome if isinstance(outcome, httpx.Response) else _response(outcome)

    monkeypatch.setattr(
        "labbioagentos.kernel.http_stream.httpx.AsyncClient",
        lambda **kwargs: REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs),
    )

    async def no_wait(_seconds):
        return None

    binding = _binding(**binding_values)
    provider = AnthropicMessagesProvider(
        binding=binding, api_key=KEY,
        transport=HttpStreamTransport(sleep=no_wait, random_fraction=lambda: 0.0),
    )
    return provider, binding, requests


def _request(binding, messages=None, *, tools=(), schema=None, run=None, invocation=None):
    return ModelRequest(
        binding=binding,
        messages=messages or (PublicMessage(role="USER", content="Run."),),
        tools=tools,
        response_schema=schema,
        max_output_tokens=4096,
        deadline_monotonic=10_000_000_000.0,
        kernel_run_id=run or uuid4(),
        invocation_id=invocation or uuid4(),
        turn_index=1,
    )


def _body(request):
    return json.loads(request.content)


TOOL_SPEC = ToolSpec(name="inspect", description="Inspect a bounded object.",
                     parameters=TOOL_PARAMETERS)
DONE = _start() + _text(0, "done") + _end("end_turn")


# Request form (criterion 10)


@pytest.mark.asyncio
async def test_key_travels_only_in_its_header(monkeypatch):
    provider, binding, requests = _provider(monkeypatch, DONE)

    await provider.generate(_request(binding))

    request = requests[0]
    assert str(request.url) == "https://api.anthropic.invalid/v1/messages"
    assert request.headers["x-api-key"] == KEY
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in request.headers
    assert "anthropic-beta" not in request.headers
    assert request.headers.get_list("accept-encoding") == ["gzip"]
    elsewhere = [value for name, value in request.headers.items() if name != "x-api-key"]
    assert KEY not in str(request.url) + request.content.decode() + "".join(elsewhere)


@pytest.mark.asyncio
async def test_system_messages_and_grouped_tool_results(monkeypatch):
    provider, binding, requests = _provider(monkeypatch, DONE)
    history = (
        PublicMessage(role="SYSTEM", content="Operator rules."),
        PublicMessage(role="USER", content="Run."),
        PublicMessage(role="ASSISTANT", content="Checking.", tool_calls=(
            ToolCall(call_id="toolu_a", name="inspect", arguments_json='{"view":"A"}'),
            ToolCall(call_id="toolu_b", name="inspect", arguments_json='{"view":"B"}'),
        )),
        PublicMessage(role="TOOL", content='{"status":"FAILED"}', tool_call_id="toolu_a",
                      tool_error=True),
        PublicMessage(role="TOOL", content='{"status":"SUCCEEDED"}', tool_call_id="toolu_b"),
        PublicMessage(role="USER", content="Again."),
        PublicMessage(role="SYSTEM", content="Mid-conversation rule."),
    )

    await provider.generate(_request(binding, history, tools=(TOOL_SPEC,)))

    body = _body(requests[0])
    assert body["system"] == [{"type": "text", "text": "Operator rules."}]
    assert body["messages"] == [
        {"role": "user", "content": "Run."},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Checking."},
            {"type": "tool_use", "id": "toolu_a", "name": "inspect", "input": {"view": "A"}},
            {"type": "tool_use", "id": "toolu_b", "name": "inspect", "input": {"view": "B"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_a",
             "content": '{"status":"FAILED"}', "is_error": True},
            {"type": "tool_result", "tool_use_id": "toolu_b",
             "content": '{"status":"SUCCEEDED"}'},
        ]},
        {"role": "user", "content": "Again."},
        {"role": "system", "content": "Mid-conversation rule."},
    ]


@pytest.mark.parametrize("thinking,dialect,expected_thinking,expected_effort", [
    ("ENABLED", DIALECT, {"type": "adaptive"}, "high"),
    ("ENABLED", AnthropicMessagesDialect(thinking_off="DISABLED"), {"type": "adaptive"}, None),
    ("DISABLED", AnthropicMessagesDialect(thinking_off="DISABLED"), {"type": "disabled"}, None),
    ("DISABLED", AnthropicMessagesDialect(thinking_off="BETWEEN_TOOLS", effort_disabled="medium"),
     {"type": "between_tools"}, "medium"),
    ("DISABLED", DIALECT, None, "low"),
], ids=["adaptive-effort", "adaptive", "disabled", "between-tools", "omitted"])
@pytest.mark.asyncio
async def test_configured_thinking_forms(monkeypatch, thinking, dialect, expected_thinking,
                                         expected_effort):
    provider, binding, requests = _provider(monkeypatch, DONE, dialect=dialect, thinking=thinking)

    await provider.generate(_request(binding))

    body = _body(requests[0])
    assert body.get("thinking") == expected_thinking
    assert body.get("output_config", {}).get("effort") == expected_effort
    assert "display" not in json.dumps(body.get("thinking"))


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.asyncio
async def test_strict_tools_and_json_output_send_the_projected_schema(monkeypatch, strict):
    provider, binding, requests = _provider(monkeypatch, DONE, strict=strict)

    await provider.generate(_request(binding, tools=(TOOL_SPEC,), schema=RESPONSE_SCHEMA))

    body = _body(requests[0])
    projected_output = body["output_config"]["format"]
    assert projected_output["type"] == "json_schema"
    assert "maxLength" not in json.dumps(projected_output["schema"])
    assert "maxItems" not in json.dumps(projected_output["schema"])
    tool = body["tools"][0]
    if strict:
        assert tool["strict"] is True
        assert tool["input_schema"]["properties"]["view"] == {"type": "string"}
        assert tool["input_schema"]["properties"]["limit"] == {"type": "integer", "default": 3}
    else:
        assert "strict" not in tool
        assert tool["input_schema"] == TOOL_PARAMETERS
    assert {"maxLength", "minimum", "maximum", "minLength"} <= STRICT_SCHEMA_REMOVED_KEYWORDS


@pytest.mark.asyncio
async def test_json_object_mode_carries_the_schema_in_the_system_prompt(monkeypatch):
    provider, binding, requests = _provider(monkeypatch, DONE, json_object=True)
    history = (
        PublicMessage(role="SYSTEM", content="Operator rules."),
        PublicMessage(role="USER", content="Finish."),
    )

    await provider.generate(_request(binding, history, schema=RESPONSE_SCHEMA))

    body = _body(requests[0])
    assert "format" not in body.get("output_config", {})
    assert body["system"] == [
        {"type": "text", "text": "Operator rules."},
        {"type": "text", "text": schema_instruction(RESPONSE_SCHEMA)},
    ]


@pytest.mark.asyncio
async def test_request_enables_no_opt_in_features(monkeypatch):
    provider, binding, requests = _provider(monkeypatch, DONE, strict=True)

    await provider.generate(_request(binding, tools=(TOOL_SPEC,), schema=RESPONSE_SCHEMA))

    body = _body(requests[0])
    assert set(body) <= {
        "model", "max_tokens", "stream", "system", "messages", "thinking",
        "output_config", "tools",
    }
    text = json.dumps(body)
    for absent in ("tool_choice", "fallbacks", "cache_control", "eager_input_streaming",
                   "block_binding", "betas"):
        assert absent not in text


# Stream reassembly


@pytest.mark.asyncio
async def test_stream_reassembles_thinking_text_and_tool_input(monkeypatch):
    body = (
        _start(cache_read_input_tokens=4, cache_creation_input_tokens=2)
        + _thinking(0, "SIG_PART_1_", "SIG_PART_2")
        + _event("ping")
        + _text(1, "Let me ", "check.")
        + _tool(2, "toolu_1", "inspect", "", '{"view":', ' "SUMMARY"}')
        + _end("tool_use", output_tokens=30)
    )
    provider, binding, _requests = _provider(monkeypatch, body)

    turn = await provider.generate(_request(binding, tools=(TOOL_SPEC,)))

    assert turn.content == "Let me check."
    assert [(call.call_id, call.name, call.arguments_json) for call in turn.tool_calls] == [
        ("toolu_1", "inspect", '{"view": "SUMMARY"}'),
    ]
    assert turn.finish_reason is FinishReason.TOOL_CALLS
    assert turn.private_reasoning_present is True
    assert turn.provider_request_id == "msg_1"
    assert (turn.usage.input_tokens, turn.usage.output_tokens, turn.usage.total_tokens) == (
        16, 30, 46,
    )
    assert "SIG_PART" not in repr(turn)


@pytest.mark.asyncio
async def test_tool_without_input_deltas_keeps_its_start_input(monkeypatch):
    body = _start() + _tool(0, "toolu_1", "inspect") + _end("tool_use")
    provider, binding, _requests = _provider(monkeypatch, body)

    turn = await provider.generate(_request(binding, tools=(TOOL_SPEC,)))

    assert turn.tool_calls[0].arguments_json == "{}"


@pytest.mark.parametrize("stop_reason,finish", [
    ("end_turn", FinishReason.STOP),
    ("tool_use", FinishReason.TOOL_CALLS),
    ("max_tokens", FinishReason.LENGTH),
    ("model_context_window_exceeded", FinishReason.LENGTH),
    ("stop_sequence", FinishReason.OTHER),
    ("pause_turn", FinishReason.OTHER),
    ("future_reason", FinishReason.OTHER),
])
@pytest.mark.asyncio
async def test_stop_reasons(monkeypatch, stop_reason, finish):
    provider, binding, _requests = _provider(monkeypatch, _start() + _text(0, "x") + _end(stop_reason))

    turn = await provider.generate(_request(binding))

    assert turn.finish_reason is finish


@pytest.mark.parametrize("details,category", [
    ({"type": "refusal", "category": "bio", "explanation": "PRIVATE_EXPLANATION"}, "bio"),
    ({"type": "refusal", "category": None}, None),
    (None, None),
    ({"type": "refusal", "category": "Not An Identifier"}, "unrecognized"),
])
@pytest.mark.asyncio
async def test_refusal_keeps_only_its_finish_and_category(monkeypatch, details, category):
    body = (
        _start() + _thinking(0, "SIG") + _text(1, "PRIVATE_PARTIAL_OUTPUT")
        + _tool(2, "toolu_1", "inspect", '{"view":"A"}')
        + _end("refusal", stop_details=details)
    )
    provider, binding, _requests = _provider(monkeypatch, body)

    turn = await provider.generate(_request(binding, tools=(TOOL_SPEC,)))

    assert turn.finish_reason is FinishReason.CONTENT_FILTER
    assert turn.content is None and turn.tool_calls == ()
    assert turn.refusal_category == category
    assert "PRIVATE" not in repr(turn)


@pytest.mark.parametrize("body,fields", [
    (_start() + _event("content_block_start", index=0, content_block={"type": "server_tool_use"})
     + _end("end_turn"), ("stream.content_block.type", "server_tool_use")),
    (_start() + _event("content_block_start", index=0, content_block={"type": "text", "text": ""})
     + _event("content_block_delta", index=0, delta={"type": "citations_delta", "citation": {}})
     + _event("content_block_stop", index=0) + _end("end_turn"),
     ("stream.delta.type", "citations_delta")),
], ids=["block", "delta"])
@pytest.mark.asyncio
async def test_unknown_content_types_fail_and_are_named(monkeypatch, body, fields):
    provider, binding, _requests = _provider(monkeypatch, body)

    with pytest.raises(ProviderProtocolError) as caught:
        await provider.generate(_request(binding))

    assert caught.value.validation_type == "unsupported_type"
    assert caught.value.validation_fields == fields


@pytest.mark.parametrize("body,field", [
    (_start() + _event("content_block_start", index=0,
                       content_block={"type": "tool_use", "id": "toolu_1", "name": "inspect", "input": {}})
     + _event("content_block_delta", index=0, delta={"type": "text_delta", "text": "x"})
     + _event("content_block_stop", index=0) + _end("tool_use"), "stream.delta"),
    (_start() + _event("content_block_start", index=1,
                       content_block={"type": "text", "text": ""}) + _end("end_turn"),
     "stream.content_block_start"),
    (_text(0, "x") + _end("end_turn"), "stream.message_start"),
    (_start() + _text(0, "x") + _event("message_stop"), "stream.incomplete"),
], ids=["delta-mismatch", "index-gap", "no-start", "no-stop-reason"])
@pytest.mark.asyncio
async def test_malformed_streams_are_protocol_errors(monkeypatch, body, field):
    provider, binding, _requests = _provider(monkeypatch, body)

    with pytest.raises(ProviderProtocolError) as caught:
        await provider.generate(_request(binding))

    assert caught.value.validation_fields == (field,)


@pytest.mark.asyncio
async def test_unknown_event_types_are_ignored(monkeypatch):
    body = (
        _start() + _event("future_status", detail={"x": 1}) + _text(0, "done")
        + _event("another_future_event") + _end("end_turn")
    )
    provider, binding, _requests = _provider(monkeypatch, body)

    turn = await provider.generate(_request(binding))

    assert turn.content == "done"


@pytest.mark.asyncio
async def test_stream_ending_without_message_stop_is_incomplete(monkeypatch):
    provider, binding, requests = _provider(monkeypatch, _start() + _text(0, "partial"), DONE)

    with pytest.raises(ProviderTransportError) as caught:
        await provider.generate(_request(binding))

    assert caught.value.validation_type == "STREAM_INCOMPLETE"
    assert len(requests) == 1


@pytest.mark.parametrize("error_type,category", [
    ("overloaded_error", "STREAM_OVERLOADED"),
    ("rate_limit_error", "STREAM_RATE_LIMITED"),
])
@pytest.mark.asyncio
async def test_overload_and_rate_limit_events_are_retried(monkeypatch, error_type, category):
    failing = _start() + _event("error", error={"type": error_type, "message": "PRIVATE_CANARY"})
    provider, binding, requests = _provider(monkeypatch, failing, DONE)

    turn = await provider.generate(_request(binding))

    assert turn.content == "done"
    assert turn.transport_retries == (ProviderRetry(category=category, wait_ms=500),)
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_other_stream_errors_fail_without_retry_or_text(monkeypatch):
    failing = _start() + _event("error", error={"type": "api_error", "message": "PRIVATE_CANARY"})
    provider, binding, requests = _provider(monkeypatch, failing, DONE)

    with pytest.raises(ProviderTransportError) as caught:
        await provider.generate(_request(binding))

    assert caught.value.validation_type == "STREAM_ERROR"
    assert "PRIVATE_CANARY" not in str(caught.value)
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_overloaded_status_is_retried(monkeypatch):
    provider, binding, requests = _provider(monkeypatch, httpx.Response(529), DONE)

    turn = await provider.generate(_request(binding))

    assert turn.transport_retries == (ProviderRetry(category="HTTP_529", wait_ms=500),)
    assert len(requests) == 2


# Verbatim replay through the real loop


class RecordArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    value: str
    nested: str | None = None


def _loop(provider, *, completion_feedback=None, tools=True):
    from test_agent_loop import _limits

    from labbioagentos.kernel import ModelResultProjector, ToolDispatcher, ToolRegistry

    budget = BudgetLedger(_limits())
    registry = ToolRegistry()
    effects = []

    async def record(arguments: RecordArguments):
        effects.append(arguments.model_dump())
        return {"recorded": arguments.value}

    registry.register(name="record_value", description="Record one synthetic bounded value.",
                      arguments_model=RecordArguments, handler=record)
    invocation_id = uuid4()
    loop = AgentLoop(
        provider=provider,
        invocation=AgentInvocation(
            identity=KernelIdentity(
                thread_id=uuid4(), workflow_run_id=uuid4(), kernel_run_id=uuid4(),
                invocation_id=invocation_id, chain=(invocation_id,),
            ),
            agent_id="leader", mode=AgentMode.CAPABILITY,
            tool_names=("record_value",) if tools else (),
        ),
        budget_ledger=budget,
        dispatcher=ToolDispatcher(registry, ModelResultProjector(max_bytes=16_000),
                                  budget_ledger=budget) if tools else None,
        completion_feedback=completion_feedback,
    )
    return loop, effects


def _assert_append_only(first, second):
    assert second.get("system") == first.get("system")
    assert second.get("tools") == first.get("tools")
    assert second["messages"][:len(first["messages"])] == first["messages"]


@pytest.mark.asyncio
async def test_tool_turn_replays_verbatim_and_history_only_grows(monkeypatch):
    first_response = (
        _start("msg_1") + _thinking(0, "SIGNATURE_ONE")
        + _text(1, "I will record it.")
        + _tool(2, "toolu_1", "record_value", '{"value": "kept", ', '"nested": null}')
        + _end("tool_use")
    )
    provider, _binding_value, requests = _provider(
        monkeypatch, first_response, _start("msg_2") + _text(0, "done") + _end("end_turn"),
    )
    loop, effects = _loop(provider)

    result = await loop.run(
        messages=(PublicMessage(role="SYSTEM", content="Rules."),
                  PublicMessage(role="USER", content="Record it.")),
        deadline_monotonic=10_000_000_000.0,
    )

    assert result.output == "done"
    assert effects == [{"value": "kept", "nested": None}]
    first, second = (_body(request) for request in requests)
    _assert_append_only(first, second)
    replayed = second["messages"][1]
    assert replayed == {"role": "assistant", "content": [
        {"type": "thinking", "thinking": "", "signature": "SIGNATURE_ONE"},
        {"type": "text", "text": "I will record it."},
        {"type": "tool_use", "id": "toolu_1", "name": "record_value",
         "input": {"value": "kept", "nested": None}},
    ]}
    # The model's own key order, not the kernel's canonical (sorted) form.
    assert list(replayed["content"][2]["input"]) == ["value", "nested"]
    assert second["messages"][2]["content"][0]["tool_use_id"] == "toolu_1"
    assert "entries=0" in repr(provider._turns)


@pytest.mark.asyncio
async def test_redacted_thinking_replays_byte_identical(monkeypatch):
    data = "REDACTED+/=DATA" * 8
    first_response = (
        _start() + _redacted(0, data)
        + _tool(1, "toolu_1", "record_value", '{"value":"kept"}') + _end("tool_use")
    )
    provider, _binding_value, requests = _provider(monkeypatch, first_response, DONE)
    loop, _effects = _loop(provider)

    await loop.run(messages=(PublicMessage(role="USER", content="Record it."),),
                   deadline_monotonic=10_000_000_000.0)

    first, second = (_body(request) for request in requests)
    _assert_append_only(first, second)
    assert second["messages"][1]["content"][0] == {"type": "redacted_thinking", "data": data}


@pytest.mark.asyncio
async def test_text_turn_with_thinking_replays_after_completion_feedback(monkeypatch):
    provider, _binding_value, requests = _provider(
        monkeypatch,
        _start() + _thinking(0, "SIGNATURE_TEXT") + _text(1, "First answer.") + _end("end_turn"),
        _start() + _text(0, "Revised answer.") + _end("end_turn"),
    )
    loop, _effects = _loop(
        provider, tools=False, completion_feedback=lambda: "Check the answer once more.",
    )

    result = await loop.run(messages=(PublicMessage(role="USER", content="Answer."),),
                            deadline_monotonic=10_000_000_000.0)

    assert result.output == "Revised answer."
    first, second = (_body(request) for request in requests)
    _assert_append_only(first, second)
    assert second["messages"][1]["content"][0] == {
        "type": "thinking", "thinking": "", "signature": "SIGNATURE_TEXT",
    }
    assert second["messages"][2] == {"role": "user", "content": "Check the answer once more."}


@pytest.mark.asyncio
async def test_foreign_history_with_unparseable_arguments_is_refused(monkeypatch):
    provider, binding, requests = _provider(monkeypatch, DONE)
    history = (
        PublicMessage(role="USER", content="Run."),
        PublicMessage(role="ASSISTANT", tool_calls=(
            ToolCall(call_id="toolu_a", name="inspect", arguments_json="{not json"),
        )),
        PublicMessage(role="TOOL", content="{}", tool_call_id="toolu_a"),
    )

    with pytest.raises(ProviderProtocolError) as caught:
        await provider.generate(_request(binding, history))

    assert caught.value.validation_fields == ("history.tool_use.input",)
    assert requests == []


def test_adapter_accepts_only_its_transport():
    with pytest.raises(ValueError, match="transport"):
        AnthropicMessagesProvider(
            binding=ProviderBinding(
                provider="p", model="m", endpoint="https://p.invalid/v1",
                transport="OPENAI_COMPATIBLE_CHAT", thinking_mode="ENABLED",
            ),
            api_key=KEY,
        )
    with pytest.raises(ValueError, match="api_key"):
        AnthropicMessagesProvider(binding=_binding(), api_key="")
