"""C14 step 6b: Gemini Interactions API adapter (kernel layer only)."""

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
    BudgetLedger,
    FinishReason,
    GeminiInteractionsDialect,
    GeminiInteractionsProvider,
    KernelIdentity,
    ModelRequest,
    ModelResultProjector,
    ProviderBinding,
    ProviderProtocolError,
    ProviderRetry,
    ProviderTransportError,
    PublicMessage,
    ToolCall,
    ToolDispatcher,
    ToolRegistry,
    ToolSpec,
)
from labbioagentos.kernel.http_stream import HttpStreamTransport
from labbioagentos.kernel.schema_projection import schema_instruction

REAL_ASYNC_CLIENT = httpx.AsyncClient
KEY = "AIza-PRIVATE_KEY_CANARY"
DIALECT = GeminiInteractionsDialect(thinking_level_enabled="high", thinking_level_disabled="low")
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {"view": {"type": "string", "maxLength": 16}},
    "required": ["view"],
    "additionalProperties": False,
}
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"result": {"type": "string", "maxLength": 100}},
    "required": ["result"],
    "additionalProperties": False,
}
TOOL_SPEC = ToolSpec(name="inspect", description="Inspect a bounded object.",
                     parameters=TOOL_PARAMETERS)


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def denied(*_args, **_kwargs):
        pytest.fail("Adapter tests must not contact a network")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


# SSE fixtures: steps stream as start, deltas and stop; the completed event
# carries status and usage but no steps.


def _event(kind, **fields):
    return f"event: {kind}\ndata: " + json.dumps({"event_type": kind, **fields}) + "\n\n"


DONE_MARKER = "event: done\ndata: [DONE]\n\n"
USAGE = {"total_input_tokens": 10, "total_tool_use_tokens": 2, "total_output_tokens": 5,
         "total_thought_tokens": 7, "total_cached_tokens": 3, "total_tokens": 24}


def _created(interaction_id="v1_int1"):
    # With store: false the live service sends no id at all; None omits it.
    interaction = {"model": "gemini-3.8-flash", "status": "in_progress", "object": "interaction"}
    if interaction_id is not None:
        interaction["id"] = interaction_id
    return _event("interaction.created", interaction=interaction)


def _completed(status="completed", *, usage=USAGE, interaction_id="v1_int1", **extra):
    interaction = {"status": status, **extra}
    if interaction_id is not None:
        interaction["id"] = interaction_id
    if usage is not None:
        interaction["usage"] = usage
    return _event("interaction.completed", interaction=interaction) + DONE_MARKER


def _text_step(index, *texts):
    return (_event("step.start", index=index, step={"type": "model_output"})
            + "".join(_event("step.delta", index=index, delta={"type": "text", "text": text})
                      for text in texts)
            + _event("step.stop", index=index))


def _thought_step(index, signature="SIG_PRIVATE"):
    return (_event("step.start", index=index, step={"type": "thought", "signature": ""})
            + _event("step.delta", index=index,
                     delta={"type": "thought_signature", "signature": signature})
            + _event("step.stop", index=index))


def _call_step(index, call_id, name, *fragments):
    return (_event("step.start", index=index, step={"type": "function_call", "id": call_id,
                                                   "name": name, "arguments": {}})
            + "".join(_event("step.delta", index=index,
                             delta={"type": "arguments_delta", "arguments": fragment})
                      for fragment in fragments)
            + _event("step.stop", index=index))


def _stream(*steps, status="completed", **completed):
    return _created() + "".join(steps) + _completed(status, **completed)


DONE = _stream(_text_step(0, "done"))


def _response(body):
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"},
        stream=httpx.ByteStream(body.encode("utf-8")),
    )


def _binding(*, dialect=DIALECT, thinking="ENABLED", strict=False, json_object=False,
             endpoint="https://generativelanguage.googleapis.invalid/v1beta"):
    return ProviderBinding(
        provider="google",
        model="gemini-3.8-flash",
        endpoint=endpoint,
        transport="GEMINI_INTERACTIONS",
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
        return outcome if isinstance(outcome, httpx.Response) else _response(outcome)

    monkeypatch.setattr(
        "labbioagentos.kernel.http_stream.httpx.AsyncClient",
        lambda **kwargs: REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs),
    )

    async def no_wait(_seconds):
        return None

    binding = _binding(**binding_values)
    provider = GeminiInteractionsProvider(
        binding=binding, api_key=KEY,
        transport=HttpStreamTransport(sleep=no_wait, random_fraction=lambda: 0.0),
    )
    return provider, binding, requests


def _request(binding, messages=None, *, tools=(), schema=None):
    return ModelRequest(
        binding=binding,
        messages=messages or (PublicMessage(role="USER", content="Run."),),
        tools=tools,
        response_schema=schema,
        max_output_tokens=4096,
        deadline_monotonic=10_000_000_000.0,
        kernel_run_id=uuid4(),
        invocation_id=uuid4(),
        turn_index=1,
    )


def _body(request):
    return json.loads(request.content)


# Request form (criterion 12b)


@pytest.mark.parametrize("endpoint", [
    "https://generativelanguage.googleapis.invalid/v1beta",
    "https://generativelanguage.googleapis.invalid/v1beta/",
    "https://generativelanguage.googleapis.invalid/v1beta/interactions",
])
@pytest.mark.asyncio
async def test_key_travels_only_in_its_header(monkeypatch, endpoint):
    provider, binding, requests = _provider(monkeypatch, DONE, endpoint=endpoint)

    await provider.generate(_request(binding))

    request = requests[0]
    assert str(request.url) == "https://generativelanguage.googleapis.invalid/v1beta/interactions"
    assert request.headers["x-goog-api-key"] == KEY
    assert "authorization" not in request.headers
    elsewhere = [value for name, value in request.headers.items() if name != "x-goog-api-key"]
    assert KEY not in str(request.url) + request.content.decode() + "".join(elsewhere)


@pytest.mark.asyncio
async def test_request_is_stateless_and_enables_no_opt_in_features(monkeypatch):
    provider, binding, requests = _provider(monkeypatch, DONE, strict=True)

    await provider.generate(_request(binding, tools=(TOOL_SPEC,), schema=RESPONSE_SCHEMA))

    body = _body(requests[0])
    assert body["store"] is False and body["stream"] is True
    assert body["model"] == "gemini-3.8-flash"
    assert set(body) == {"model", "input", "tools", "generation_config", "response_format",
                         "stream", "store"}
    text = json.dumps(body)
    for absent in ("previous_interaction_id", "background", "agent", "environment",
                   "service_tier", "webhook_config", "safety_settings", "labels",
                   "thinking_summaries", "google_search", "code_execution", "url_context"):
        assert absent not in text


@pytest.mark.asyncio
async def test_history_mapping_keeps_steps_in_order(monkeypatch):
    provider, binding, requests = _provider(monkeypatch, DONE)
    history = (
        PublicMessage(role="SYSTEM", content="Operator rules."),
        PublicMessage(role="SYSTEM", content="Second rule."),
        PublicMessage(role="USER", content="Run."),
        PublicMessage(role="ASSISTANT", content="Checking.", tool_calls=(
            ToolCall(call_id="fc-a", name="inspect", arguments_json='{"view":"A"}'),
            ToolCall(call_id="fc-b", name="inspect", arguments_json='{"view":"B"}'),
        )),
        PublicMessage(role="TOOL", content='{"status":"FAILED"}', tool_call_id="fc-a",
                      tool_error=True),
        PublicMessage(role="TOOL", content='{"status":"SUCCEEDED"}', tool_call_id="fc-b"),
        PublicMessage(role="USER", content="Again."),
    )

    await provider.generate(_request(binding, history, tools=(TOOL_SPEC,)))

    body = _body(requests[0])
    assert body["system_instruction"] == "Operator rules.\n\nSecond rule."
    assert body["input"] == [
        {"type": "user_input", "content": [{"type": "text", "text": "Run."}]},
        {"type": "model_output", "content": [{"type": "text", "text": "Checking."}]},
        {"type": "function_call", "id": "fc-a", "name": "inspect", "arguments": {"view": "A"}},
        {"type": "function_call", "id": "fc-b", "name": "inspect", "arguments": {"view": "B"}},
        {"type": "function_result", "call_id": "fc-a", "name": "inspect",
         "result": [{"type": "text", "text": '{"status":"FAILED"}'}], "is_error": True},
        {"type": "function_result", "call_id": "fc-b", "name": "inspect",
         "result": [{"type": "text", "text": '{"status":"SUCCEEDED"}'}]},
        {"type": "user_input", "content": [{"type": "text", "text": "Again."}]},
    ]


@pytest.mark.asyncio
async def test_mid_conversation_system_message_is_refused(monkeypatch):
    provider, binding, requests = _provider(monkeypatch, DONE)
    history = (
        PublicMessage(role="USER", content="Run."),
        PublicMessage(role="SYSTEM", content="Late rule."),
    )

    with pytest.raises(ProviderProtocolError) as caught:
        await provider.generate(_request(binding, history))

    assert caught.value.validation_fields == ("history.system",)
    assert requests == []


@pytest.mark.parametrize("arguments_json", [None, "[1]", "{bad"])
@pytest.mark.asyncio
async def test_rebuilt_call_needs_object_arguments(monkeypatch, arguments_json):
    provider, binding, requests = _provider(monkeypatch, DONE)
    history = (
        PublicMessage(role="USER", content="Run."),
        PublicMessage(role="ASSISTANT", tool_calls=(
            ToolCall(call_id="fc-a", name="inspect", arguments_json=arguments_json),
        )),
        PublicMessage(role="TOOL", content="{}", tool_call_id="fc-a"),
    )

    with pytest.raises(ProviderProtocolError) as caught:
        await provider.generate(_request(binding, history))

    assert caught.value.validation_fields == ("history.function_call.arguments",)
    assert requests == []


@pytest.mark.parametrize("thinking,level", [("ENABLED", "high"), ("DISABLED", "low")])
@pytest.mark.asyncio
async def test_thinking_level_follows_the_thinking_state(monkeypatch, thinking, level):
    provider, binding, requests = _provider(monkeypatch, DONE, thinking=thinking)

    await provider.generate(_request(binding))

    assert _body(requests[0])["generation_config"] == {
        "max_output_tokens": 4096, "thinking_level": level,
    }


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.asyncio
async def test_tools_and_json_output_send_full_json_schemas(monkeypatch, strict):
    provider, binding, requests = _provider(monkeypatch, DONE, strict=strict)

    await provider.generate(_request(binding, tools=(TOOL_SPEC,), schema=RESPONSE_SCHEMA))

    body = _body(requests[0])
    assert body["tools"] == [{
        "type": "function", "name": "inspect", "description": "Inspect a bounded object.",
        "parameters": TOOL_PARAMETERS,
    }]
    if strict:
        assert body["generation_config"]["tool_choice"] == "validated"
    else:
        assert "tool_choice" not in body["generation_config"]
    assert body["response_format"] == {
        "type": "text", "mime_type": "application/json", "schema": RESPONSE_SCHEMA,
    }


@pytest.mark.asyncio
async def test_strict_mode_without_tools_sends_no_tool_choice(monkeypatch):
    provider, binding, requests = _provider(monkeypatch, DONE, strict=True)

    await provider.generate(_request(binding, schema=RESPONSE_SCHEMA))

    body = _body(requests[0])
    assert "tools" not in body and "tool_choice" not in body["generation_config"]


@pytest.mark.parametrize("system,expected", [
    (None, schema_instruction(RESPONSE_SCHEMA)),
    ("Operator rules.", "Operator rules.\n\n" + schema_instruction(RESPONSE_SCHEMA)),
])
@pytest.mark.asyncio
async def test_json_object_mode_carries_the_schema_in_the_system_instruction(
    monkeypatch, system, expected,
):
    provider, binding, requests = _provider(monkeypatch, DONE, json_object=True)
    messages = (PublicMessage(role="USER", content="Run."),)
    if system is not None:
        messages = (PublicMessage(role="SYSTEM", content=system), *messages)

    await provider.generate(_request(binding, messages, schema=RESPONSE_SCHEMA))

    body = _body(requests[0])
    assert body["response_format"] == {"type": "text", "mime_type": "application/json"}
    assert body["system_instruction"] == expected


# Stream reassembly


@pytest.mark.asyncio
async def test_stream_reassembles_text_calls_signatures_and_usage(monkeypatch):
    body = _stream(
        _thought_step(0),
        _event("interaction.status_update", interaction_id="v1_int1", status="in_progress"),
        _text_step(1, "Let me ", "check."),
        _call_step(2, "fc-1", "inspect", '{"view":', '"A"}'),
        _event("future.event", value="ignored"),
        _call_step(3, "fc-2", "inspect"),
        status="requires_action",
    )
    provider, binding, _requests = _provider(monkeypatch, body)

    turn = await provider.generate(_request(binding, tools=(TOOL_SPEC,)))

    assert turn.content == "Let me check."
    assert [(c.call_id, c.name, c.arguments_json) for c in turn.tool_calls] == [
        ("fc-1", "inspect", '{"view":"A"}'), ("fc-2", "inspect", "{}"),
    ]
    assert turn.finish_reason is FinishReason.TOOL_CALLS
    assert turn.private_reasoning_present is True
    assert turn.provider_request_id == "v1_int1"
    assert (turn.usage.input_tokens, turn.usage.output_tokens, turn.usage.total_tokens) == (
        12, 12, 24,
    )
    assert "SIG_PRIVATE" not in repr(turn)


@pytest.mark.asyncio
async def test_stream_without_interaction_ids_completes(monkeypatch):
    # The live shape under store: false: neither event carries an id.
    body = (_created(None) + _text_step(0, "Let me check.")
            + _call_step(1, "fc-1", "inspect", '{"view":"A"}')
            + _completed("requires_action", interaction_id=None))
    provider, binding, _requests = _provider(monkeypatch, body)

    turn = await provider.generate(_request(binding, tools=(TOOL_SPEC,)))

    assert turn.content == "Let me check."
    assert [(c.call_id, c.arguments_json) for c in turn.tool_calls] == [("fc-1", '{"view":"A"}')]
    assert turn.finish_reason is FinishReason.TOOL_CALLS
    assert turn.provider_request_id is None


@pytest.mark.asyncio
async def test_completed_id_is_kept_when_created_has_none(monkeypatch):
    body = _created(None) + _text_step(0, "done") + _completed(interaction_id="v1_int1")
    provider, binding, _requests = _provider(monkeypatch, body)

    turn = await provider.generate(_request(binding))

    assert turn.provider_request_id == "v1_int1"


@pytest.mark.asyncio
async def test_interaction_id_outside_the_identifier_form_is_unknown(monkeypatch):
    body = (_created("_int 1") + _text_step(0, "done")
            + _completed(interaction_id="_int 1"))
    provider, binding, _requests = _provider(monkeypatch, body)

    turn = await provider.generate(_request(binding))

    assert turn.content == "done"
    assert turn.provider_request_id is None


@pytest.mark.parametrize("status,expected", [
    ("completed", FinishReason.STOP),
    ("incomplete", FinishReason.LENGTH),
    ("cancelled", FinishReason.OTHER),
    ("future_status", FinishReason.OTHER),
])
@pytest.mark.asyncio
async def test_statuses_map_to_finish_reasons(monkeypatch, status, expected):
    provider, binding, _requests = _provider(monkeypatch, _stream(_text_step(0, "x"),
                                                                  status=status))

    turn = await provider.generate(_request(binding))

    assert turn.finish_reason is expected
    assert turn.content == "x"


@pytest.mark.asyncio
async def test_completed_status_with_calls_is_a_tool_turn(monkeypatch):
    provider, binding, _requests = _provider(
        monkeypatch, _stream(_call_step(0, "fc-1", "inspect", '{"view":"A"}')),
    )

    turn = await provider.generate(_request(binding, tools=(TOOL_SPEC,)))

    assert turn.finish_reason is FinishReason.TOOL_CALLS


@pytest.mark.asyncio
async def test_requires_action_without_calls_is_refused(monkeypatch):
    provider, binding, _requests = _provider(
        monkeypatch, _stream(_text_step(0, "x"), status="requires_action"),
    )

    with pytest.raises(ProviderProtocolError) as caught:
        await provider.generate(_request(binding))

    assert caught.value.validation_fields == ("stream.status",)


@pytest.mark.parametrize("code", ["safety", "jailbreak", "prohibited_content", "blocklist"])
@pytest.mark.asyncio
async def test_safety_error_event_is_a_content_filter(monkeypatch, code):
    body = (_created() + _text_step(0, "PRIVATE_PARTIAL")
            + _event("step.start", index=1, step={"type": "function_call", "id": "fc-1",
                                                  "name": "inspect", "arguments": {}})
            + _event("error", error={"code": code, "message": "PRIVATE_MESSAGE"}))
    provider, binding, requests = _provider(monkeypatch, body, DONE)

    turn = await provider.generate(_request(binding, tools=(TOOL_SPEC,)))

    assert turn.finish_reason is FinishReason.CONTENT_FILTER
    assert turn.refusal_category == code
    assert turn.content is None and turn.tool_calls == ()
    assert turn.provider_request_id == "v1_int1"
    assert turn.usage.output_tokens is None
    assert "PRIVATE_PARTIAL" not in repr(turn) and "PRIVATE_MESSAGE" not in repr(turn)
    assert len(requests) == 1
    assert "entries=0" in repr(provider._turns)


@pytest.mark.asyncio
async def test_failed_interaction_with_a_safety_error_is_a_content_filter(monkeypatch):
    body = _stream(_text_step(0, "PRIVATE_PARTIAL"), status="failed",
                   errors=[{"code": "safety", "message": "PRIVATE_MESSAGE"}])
    provider, binding, _requests = _provider(monkeypatch, body)

    turn = await provider.generate(_request(binding))

    assert turn.finish_reason is FinishReason.CONTENT_FILTER
    assert turn.refusal_category == "safety"
    assert turn.content is None
    assert turn.usage.total_tokens == 24


@pytest.mark.parametrize("body,fields", [
    (_stream(_event("step.start", index=0, step={"type": "google_search_call", "id": "gs-1"})
             + _event("step.stop", index=0)), ("stream.step.type", "google_search_call")),
    (_stream(_event("step.start", index=0, step={"type": "not a type!"})),
     ("stream.step.type", "unrecognized")),
    (_stream(_event("step.start", index=0, step={"type": "model_output"})
             + _event("step.delta", index=0, delta={"type": "image", "data": "AAAA"})
             + _event("step.stop", index=0)), ("stream.delta.type", "image")),
    (_stream(_event("step.start", index=0, step={"type": "model_output", "content": [
        {"type": "image", "data": "AAAA"}]}) + _event("step.stop", index=0)),
     ("stream.step.content.type", "image")),
], ids=["builtin-step", "malformed-type", "image-delta", "image-content"])
@pytest.mark.asyncio
async def test_unknown_content_types_fail_and_are_named(monkeypatch, body, fields):
    provider, binding, _requests = _provider(monkeypatch, body)

    with pytest.raises(ProviderProtocolError) as caught:
        await provider.generate(_request(binding))

    assert caught.value.validation_type == "unsupported_type"
    assert caught.value.validation_fields == fields


@pytest.mark.parametrize("body,field", [
    (_stream(_text_step(1, "x")), "stream.step.start"),
    (_stream(_text_step(0, "x") + _event("step.delta", index=0,
                                         delta={"type": "text", "text": "late"})),
     "stream.step.delta"),
    (_stream(_event("step.start", index=0, step={"type": "function_call", "id": "fc-1",
                                                "name": "inspect", "arguments": {}})
             + _event("step.delta", index=0, delta={"type": "text", "text": "x"})),
     "stream.delta"),
    (_stream(_text_step(0, "x"), interaction_id="v1_other"), "stream.interaction.id"),
    (_stream(_event("step.start", index=0, step={"type": "model_output"})),
     "stream.incomplete"),
    (_stream(_event("step.start", index=0, step={"type": "function_call", "name": "inspect",
                                                "arguments": {}})),
     "stream.function_call.id"),
    (_stream(_call_step(0, "fc-1", "inspect", "[1]")), "stream.function_call.arguments"),
    (_created() + _created(), "stream.interaction.created"),
    (_created(None) + _created(None), "stream.interaction.created"),
    (_created(7), "stream.interaction.created"),
], ids=["skipped-index", "closed-step", "mismatched-delta", "changed-id", "open-step",
        "call-without-id", "array-arguments", "two-created", "two-created-without-id",
        "non-string-id"])
@pytest.mark.asyncio
async def test_malformed_streams_are_protocol_errors(monkeypatch, body, field):
    provider, binding, _requests = _provider(monkeypatch, body)

    with pytest.raises(ProviderProtocolError) as caught:
        await provider.generate(_request(binding))

    assert caught.value.validation_fields == (field,)


@pytest.mark.parametrize("partial", [
    _created() + _text_step(0, "partial") + DONE_MARKER,
    _created() + _text_step(0, "partial"),
], ids=["done-marker", "eof"])
@pytest.mark.asyncio
async def test_stream_without_completion_is_incomplete(monkeypatch, partial):
    provider, binding, requests = _provider(monkeypatch, partial, DONE)

    with pytest.raises(ProviderTransportError) as caught:
        await provider.generate(_request(binding))

    assert caught.value.validation_type == "STREAM_INCOMPLETE"
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_missing_usage_stays_unknown(monkeypatch):
    provider, binding, _requests = _provider(monkeypatch, _stream(_text_step(0, "x"),
                                                                  usage=None))

    turn = await provider.generate(_request(binding))

    assert (turn.usage.input_tokens, turn.usage.output_tokens, turn.usage.total_tokens) == (
        None, None, None,
    )
    assert turn.private_reasoning_present is False


@pytest.mark.parametrize("code,category", [
    ("unavailable", "STREAM_OVERLOADED"), ("resource_exhausted", "STREAM_RATE_LIMITED"),
])
@pytest.mark.parametrize("form", ["error-event", "failed-interaction"])
@pytest.mark.asyncio
async def test_overload_and_quota_errors_in_the_stream_are_retried(
    monkeypatch, code, category, form,
):
    if form == "error-event":
        failing = _created() + _event("error", error={"code": code, "message": "PRIVATE"})
    else:
        failing = _stream(status="failed", errors=[{"code": code, "message": "PRIVATE"}])
    provider, binding, requests = _provider(monkeypatch, failing, DONE)

    turn = await provider.generate(_request(binding))

    assert turn.content == "done"
    assert turn.transport_retries == (ProviderRetry(category=category, wait_ms=500),)
    assert len(requests) == 2


@pytest.mark.parametrize("failing", [
    _event("error", error={"code": "gateway_timeout", "message": "PRIVATE_CANARY"}),
    _event("error", error={"code": "not_found", "message": "PRIVATE_CANARY"}),
    _event("error", error={"message": "PRIVATE_CANARY"}),
    _stream(status="failed", errors=[{"code": "internal", "message": "PRIVATE_CANARY"}]),
    _stream(status="failed"),
], ids=["gateway-timeout", "not-found", "no-code", "failed-internal", "failed-no-errors"])
@pytest.mark.asyncio
async def test_other_stream_failures_fail_without_retry_or_text(monkeypatch, failing):
    provider, binding, requests = _provider(monkeypatch, failing, DONE)

    with pytest.raises(ProviderTransportError) as caught:
        await provider.generate(_request(binding))

    assert caught.value.validation_type == "STREAM_ERROR"
    assert "PRIVATE_CANARY" not in str(caught.value)
    assert len(requests) == 1


# Verbatim replay through the real loop


class RecordArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    value: str


def _loop(provider, *, completion_feedback=None, tools=True):
    from test_agent_loop import _limits

    budget = BudgetLedger(_limits())
    registry = ToolRegistry()
    effects = []

    async def record(arguments: RecordArguments):
        effects.append(arguments.value)
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
    for field in ("system_instruction", "tools", "generation_config", "store"):
        assert second.get(field) == first.get(field)
    assert second["input"][:len(first["input"])] == first["input"]


@pytest.mark.asyncio
async def test_thought_steps_and_parallel_calls_replay_as_received(monkeypatch):
    first_response = _stream(
        _thought_step(0, "SIG_ONE"),
        _text_step(1, "I will ", "record both."),
        _call_step(2, "fc-1", "record_value", '{"value":"a"}'),
        _call_step(3, "fc-2", "record_value", '{"value":', '"b"}'),
        status="requires_action",
    )
    provider, _binding_value, requests = _provider(monkeypatch, first_response, DONE)
    loop, effects = _loop(provider)

    result = await loop.run(
        messages=(PublicMessage(role="SYSTEM", content="Rules."),
                  PublicMessage(role="USER", content="Record both.")),
        deadline_monotonic=10_000_000_000.0,
    )

    assert result.output == "done"
    assert effects == ["a", "b"]
    first, second = (_body(request) for request in requests)
    _assert_append_only(first, second)
    assert first["system_instruction"] == "Rules."
    assert second["input"][1:5] == [
        {"type": "thought", "signature": "SIG_ONE"},
        {"type": "model_output", "content": [{"type": "text", "text": "I will record both."}]},
        {"type": "function_call", "id": "fc-1", "name": "record_value",
         "arguments": {"value": "a"}},
        {"type": "function_call", "id": "fc-2", "name": "record_value",
         "arguments": {"value": "b"}},
    ]
    results = second["input"][5:]
    assert [(step["type"], step["call_id"], step["name"]) for step in results] == [
        ("function_result", "fc-1", "record_value"), ("function_result", "fc-2", "record_value"),
    ]
    assert "entries=0" in repr(provider._turns)


@pytest.mark.asyncio
async def test_final_answer_replays_with_its_thought_step(monkeypatch):
    provider, _binding_value, requests = _provider(
        monkeypatch,
        _stream(_thought_step(0, "SIG_TAIL"), _text_step(1, "First answer.")),
        _stream(_text_step(0, "Revised answer.")),
    )
    loop, _effects = _loop(
        provider, tools=False, completion_feedback=lambda answer: (
            "Check the answer once more." if answer == "First answer." else None
        ),
    )

    result = await loop.run(messages=(PublicMessage(role="USER", content="Answer."),),
                            deadline_monotonic=10_000_000_000.0)

    assert result.output == "Revised answer."
    first, second = (_body(request) for request in requests)
    _assert_append_only(first, second)
    assert second["input"][1:3] == [
        {"type": "thought", "signature": "SIG_TAIL"},
        {"type": "model_output", "content": [{"type": "text", "text": "First answer."}]},
    ]
    assert second["input"][3]["type"] == "user_input"


def test_adapter_accepts_only_its_transport():
    with pytest.raises(ValueError, match="transport"):
        GeminiInteractionsProvider(
            binding=ProviderBinding(
                provider="p", model="m", endpoint="https://p.invalid/v1",
                transport="OPENAI_COMPATIBLE_CHAT", thinking_mode="ENABLED",
            ),
            api_key=KEY,
        )
    with pytest.raises(ValueError, match="api_key"):
        GeminiInteractionsProvider(binding=_binding(), api_key="")
