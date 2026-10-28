"""C14 step 6: OpenAI Responses API adapter (kernel layer only)."""

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
    KernelIdentity,
    ModelRequest,
    ModelResultProjector,
    OpenAIResponsesDialect,
    OpenAIResponsesProvider,
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
KEY = "sk-PRIVATE_KEY_CANARY"
DIALECT = OpenAIResponsesDialect(reasoning_effort_enabled="high",
                                 reasoning_effort_disabled="none")
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


# SSE fixtures: typed events; only the terminal event's response builds a turn.


def _event(kind, **fields):
    value = {"type": kind, "sequence_number": 0, **fields}
    return f"event: {kind}\ndata: " + json.dumps(value) + "\n\n"


def _message(text, item_id="msg_1"):
    return {"id": item_id, "type": "message", "status": "completed", "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": [], "logprobs": []}]}


def _call(call_id, arguments, item_id, name="record_value"):
    return {"id": item_id, "type": "function_call", "status": "completed", "call_id": call_id,
            "name": name, "arguments": json.dumps(arguments, separators=(",", ":"))}


REASONING = {"id": "rs_1", "type": "reasoning", "summary": [],
             "encrypted_content": "ENC_PRIVATE_CANARY"}
USAGE = {"input_tokens": 12, "input_tokens_details": {"cached_tokens": 4}, "output_tokens": 9,
         "output_tokens_details": {"reasoning_tokens": 6}, "total_tokens": 21}


def _response_object(*output, status="completed", incomplete=None, error=None, usage=USAGE,
                     response_id="resp_1"):
    return {"id": response_id, "object": "response", "status": status, "model": "gpt-5.1",
            "output": list(output), "incomplete_details": incomplete, "error": error,
            "usage": usage}


def _stream(*output, status="completed", **response_values):
    response = _response_object(*output, status=status, **response_values)
    started = {**response, "status": "in_progress", "output": [], "usage": None}
    return (_event("response.created", response=started)
            + _event(f"response.{status}", response=response))


DONE = _stream(_message("done"))


def _response(body):
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"},
        stream=httpx.ByteStream(body.encode("utf-8")),
    )


def _binding(*, dialect=DIALECT, thinking="ENABLED", strict=False, json_object=False,
             endpoint="https://api.openai.invalid/v1"):
    return ProviderBinding(
        provider="openai",
        model="gpt-5.1",
        endpoint=endpoint,
        transport="OPENAI_RESPONSES",
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
    provider = OpenAIResponsesProvider(
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


# Request form (criterion 12)


@pytest.mark.parametrize("endpoint", [
    "https://api.openai.invalid/v1", "https://api.openai.invalid/v1/",
    "https://api.openai.invalid/v1/responses",
])
@pytest.mark.asyncio
async def test_key_travels_only_in_its_header(monkeypatch, endpoint):
    provider, binding, requests = _provider(monkeypatch, DONE, endpoint=endpoint)

    await provider.generate(_request(binding))

    request = requests[0]
    assert str(request.url) == "https://api.openai.invalid/v1/responses"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    elsewhere = [value for name, value in request.headers.items() if name != "authorization"]
    assert KEY not in str(request.url) + request.content.decode() + "".join(elsewhere)


@pytest.mark.asyncio
async def test_request_is_stateless_and_enables_no_opt_in_features(monkeypatch):
    provider, binding, requests = _provider(monkeypatch, DONE, strict=True)

    await provider.generate(_request(binding, tools=(TOOL_SPEC,), schema=RESPONSE_SCHEMA))

    body = _body(requests[0])
    assert body["store"] is False and body["stream"] is True
    assert body["model"] == "gpt-5.1" and body["max_output_tokens"] == 4096
    assert set(body) == {"model", "input", "max_output_tokens", "stream", "store", "reasoning",
                         "include", "tools", "text"}
    assert "summary" not in body["reasoning"]
    assert [tool["type"] for tool in body["tools"]] == ["function"]


@pytest.mark.asyncio
async def test_history_mapping_keeps_every_message_in_place(monkeypatch):
    provider, binding, requests = _provider(monkeypatch, DONE)
    history = (
        PublicMessage(role="SYSTEM", content="Operator rules."),
        PublicMessage(role="USER", content="Run."),
        PublicMessage(role="ASSISTANT", content="Checking.", tool_calls=(
            ToolCall(call_id="call_a", name="inspect", arguments_json='{"view":"A"}'),
            ToolCall(call_id="call_b", name="inspect", arguments_json='{"view":"B"}'),
        )),
        PublicMessage(role="TOOL", content='{"status":"FAILED"}', tool_call_id="call_a",
                      tool_error=True),
        PublicMessage(role="TOOL", content='{"status":"SUCCEEDED"}', tool_call_id="call_b"),
        PublicMessage(role="SYSTEM", content="Late rule."),
        PublicMessage(role="USER", content="Again."),
    )

    await provider.generate(_request(binding, history, tools=(TOOL_SPEC,)))

    body = _body(requests[0])
    assert body["instructions"] == "Operator rules."
    assert body["input"] == [
        {"role": "user", "content": "Run."},
        {"role": "assistant", "content": "Checking."},
        {"type": "function_call", "call_id": "call_a", "name": "inspect",
         "arguments": '{"view":"A"}'},
        {"type": "function_call", "call_id": "call_b", "name": "inspect",
         "arguments": '{"view":"B"}'},
        {"type": "function_call_output", "call_id": "call_a", "output": '{"status":"FAILED"}'},
        {"type": "function_call_output", "call_id": "call_b",
         "output": '{"status":"SUCCEEDED"}'},
        {"role": "system", "content": "Late rule."},
        {"role": "user", "content": "Again."},
    ]


@pytest.mark.asyncio
async def test_rebuilt_call_without_arguments_is_refused(monkeypatch):
    provider, binding, requests = _provider(monkeypatch, DONE)
    history = (
        PublicMessage(role="USER", content="Run."),
        PublicMessage(role="ASSISTANT", tool_calls=(ToolCall(call_id="call_a", name="inspect"),)),
        PublicMessage(role="TOOL", content="{}", tool_call_id="call_a"),
    )

    with pytest.raises(ProviderProtocolError) as caught:
        await provider.generate(_request(binding, history))

    assert caught.value.validation_fields == ("history.function_call.arguments",)
    assert requests == []


@pytest.mark.parametrize("thinking,effort", [("ENABLED", "high"), ("DISABLED", "none")])
@pytest.mark.asyncio
async def test_reasoning_effort_follows_the_thinking_state(monkeypatch, thinking, effort):
    provider, binding, requests = _provider(monkeypatch, DONE, thinking=thinking)

    await provider.generate(_request(binding))

    body = _body(requests[0])
    assert body["reasoning"] == {"effort": effort}
    assert body["include"] == ["reasoning.encrypted_content"]


@pytest.mark.parametrize("thinking", ["ENABLED", "DISABLED"])
@pytest.mark.asyncio
async def test_dialect_without_efforts_sends_no_reasoning_fields(monkeypatch, thinking):
    provider, binding, requests = _provider(
        monkeypatch, DONE, dialect=OpenAIResponsesDialect(), thinking=thinking,
    )

    await provider.generate(_request(binding))

    body = _body(requests[0])
    assert "reasoning" not in body and "include" not in body


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.asyncio
async def test_tools_always_state_strictness_with_full_schemas(monkeypatch, strict):
    provider, binding, requests = _provider(monkeypatch, DONE, strict=strict)

    await provider.generate(_request(binding, tools=(TOOL_SPEC,)))

    assert _body(requests[0])["tools"] == [{
        "type": "function", "name": "inspect", "description": "Inspect a bounded object.",
        "parameters": TOOL_PARAMETERS, "strict": strict,
    }]


@pytest.mark.asyncio
async def test_json_schema_output_is_strict_and_unprojected(monkeypatch):
    provider, binding, requests = _provider(monkeypatch, DONE)

    await provider.generate(_request(binding, schema=RESPONSE_SCHEMA))

    body = _body(requests[0])
    assert body["text"] == {"format": {
        "type": "json_schema", "name": "structured_output", "schema": RESPONSE_SCHEMA,
        "strict": True,
    }}
    assert "instructions" not in body


@pytest.mark.parametrize("system,expected", [
    (None, schema_instruction(RESPONSE_SCHEMA)),
    ("Operator rules.", "Operator rules.\n\n" + schema_instruction(RESPONSE_SCHEMA)),
])
@pytest.mark.asyncio
async def test_json_object_mode_carries_the_schema_in_the_instructions(
    monkeypatch, system, expected,
):
    provider, binding, requests = _provider(monkeypatch, DONE, json_object=True)
    messages = (PublicMessage(role="USER", content="Run."),)
    if system is not None:
        messages = (PublicMessage(role="SYSTEM", content=system), *messages)

    await provider.generate(_request(binding, messages, schema=RESPONSE_SCHEMA))

    body = _body(requests[0])
    assert body["text"] == {"format": {"type": "json_object"}}
    assert body["instructions"] == expected
    assert body["input"] == [{"role": "user", "content": "Run."}]


# Stream reassembly


@pytest.mark.asyncio
async def test_terminal_response_is_the_only_source_of_the_turn(monkeypatch):
    output = (REASONING, _message("Let me check."),
              _call("call_1", {"view": "A"}, "fc_1", name="inspect"),
              _call("call_2", {"view": "B"}, "fc_2", name="inspect"))
    body = (
        _event("response.created", response=_response_object(status="in_progress"))
        + _event("response.output_text.delta", item_id="msg_1", delta="DELTA_ONLY")
        + _event("response.function_call_arguments.delta", item_id="fc_1", delta="{")
        + _event("response.future_event", value="ignored")
        + _event("response.completed", response=_response_object(*output))
    )
    provider, binding, _requests = _provider(monkeypatch, body)

    turn = await provider.generate(_request(binding, tools=(TOOL_SPEC,)))

    assert turn.content == "Let me check."
    assert [(c.call_id, c.name, c.arguments_json) for c in turn.tool_calls] == [
        ("call_1", "inspect", '{"view":"A"}'), ("call_2", "inspect", '{"view":"B"}'),
    ]
    assert turn.finish_reason is FinishReason.TOOL_CALLS
    assert turn.private_reasoning_present is True
    assert turn.provider_request_id == "resp_1"
    assert (turn.usage.input_tokens, turn.usage.output_tokens, turn.usage.total_tokens) == (
        12, 9, 21,
    )
    assert "ENC_PRIVATE_CANARY" not in repr(turn)


@pytest.mark.parametrize("response_id", ["_resp 1", None], ids=["other-form", "absent"])
@pytest.mark.asyncio
async def test_response_id_outside_the_identifier_form_is_unknown(monkeypatch, response_id):
    provider, binding, _requests = _provider(
        monkeypatch, _stream(_message("done"), response_id=response_id),
    )

    turn = await provider.generate(_request(binding))

    assert turn.content == "done"
    assert turn.provider_request_id is None


@pytest.mark.asyncio
async def test_non_string_response_id_is_a_protocol_error(monkeypatch):
    provider, binding, _requests = _provider(monkeypatch, _stream(_message("done"), response_id=7))

    with pytest.raises(ProviderProtocolError) as caught:
        await provider.generate(_request(binding))

    assert caught.value.validation_fields == ("id",)


@pytest.mark.asyncio
async def test_missing_usage_stays_unknown(monkeypatch):
    provider, binding, _requests = _provider(monkeypatch, _stream(_message("x"), usage=None))

    turn = await provider.generate(_request(binding))

    assert (turn.usage.input_tokens, turn.usage.output_tokens, turn.usage.total_tokens) == (
        None, None, None,
    )
    assert turn.private_reasoning_present is False


@pytest.mark.parametrize("status,details,expected", [
    ("completed", None, FinishReason.STOP),
    ("incomplete", {"reason": "max_output_tokens"}, FinishReason.LENGTH),
    ("incomplete", {"reason": "future_reason"}, FinishReason.OTHER),
    ("incomplete", None, FinishReason.OTHER),
])
@pytest.mark.asyncio
async def test_statuses_map_to_finish_reasons(monkeypatch, status, details, expected):
    provider, binding, _requests = _provider(
        monkeypatch, _stream(_message("x"), status=status, incomplete=details),
    )

    turn = await provider.generate(_request(binding))

    assert turn.finish_reason is expected
    assert turn.content == "x"


@pytest.mark.parametrize("output,status,details", [
    ((_message("PRIVATE_PARTIAL"), _call("call_1", {}, "fc_1")), "incomplete",
     {"reason": "content_filter"}),
    (({"id": "msg_1", "type": "message", "status": "completed", "role": "assistant",
       "content": [{"type": "output_text", "text": "PRIVATE_PARTIAL", "annotations": []},
                   {"type": "refusal", "refusal": "PRIVATE_REFUSAL"}]},
      _call("call_1", {}, "fc_1")), "completed", None),
], ids=["content-filter", "refusal"])
@pytest.mark.asyncio
async def test_filtered_or_refused_turn_keeps_only_its_reason(
    monkeypatch, output, status, details,
):
    provider, binding, _requests = _provider(
        monkeypatch, _stream(*output, status=status, incomplete=details),
    )

    turn = await provider.generate(_request(binding, tools=(TOOL_SPEC,)))

    assert turn.finish_reason is FinishReason.CONTENT_FILTER
    assert turn.content is None and turn.tool_calls == ()
    assert turn.refusal_category is None
    assert "PRIVATE_PARTIAL" not in repr(turn) and "PRIVATE_REFUSAL" not in repr(turn)
    assert "entries=0" in repr(provider._turns)


@pytest.mark.parametrize("item,fields", [
    ({"id": "ws_1", "type": "web_search_call", "status": "completed"},
     ("response.output.type", "web_search_call")),
    ({"id": "x_1", "type": "not a type!"}, ("response.output.type", "unrecognized")),
    ({"id": "msg_1", "type": "message", "status": "completed", "role": "assistant",
      "content": [{"type": "output_audio", "data": "AAAA"}]},
     ("response.message.content.type", "output_audio")),
], ids=["builtin-tool-item", "malformed-type", "content-part"])
@pytest.mark.asyncio
async def test_unknown_content_types_fail_and_are_named(monkeypatch, item, fields):
    provider, binding, _requests = _provider(monkeypatch, _stream(item))

    with pytest.raises(ProviderProtocolError) as caught:
        await provider.generate(_request(binding))

    assert caught.value.validation_type == "unsupported_type"
    assert caught.value.validation_fields == fields


@pytest.mark.parametrize("item,field", [
    ({"id": "fc_1", "type": "function_call", "call_id": "call_1", "name": "inspect",
      "arguments": {"view": "A"}}, "response.function_call.arguments"),
    ({"id": "msg_1", "type": "message", "role": "user", "content": []}, "response.message"),
    ({"id": "msg_1", "type": "message", "role": "assistant",
      "content": [{"type": "output_text", "text": None}]}, "response.message.content.text"),
], ids=["object-arguments", "wrong-role", "text-not-string"])
@pytest.mark.asyncio
async def test_malformed_items_are_protocol_errors(monkeypatch, item, field):
    provider, binding, _requests = _provider(monkeypatch, _stream(item))

    with pytest.raises(ProviderProtocolError) as caught:
        await provider.generate(_request(binding))

    assert caught.value.validation_fields == (field,)


@pytest.mark.asyncio
async def test_terminal_event_must_carry_its_status(monkeypatch):
    body = _event("response.completed", response=_response_object(_message("x"),
                                                                  status="incomplete"))
    provider, binding, _requests = _provider(monkeypatch, body)

    with pytest.raises(ProviderProtocolError) as caught:
        await provider.generate(_request(binding))

    assert caught.value.validation_fields == ("stream.response",)


@pytest.mark.asyncio
async def test_stream_without_terminal_event_is_incomplete(monkeypatch):
    partial = (_event("response.created", response=_response_object(status="in_progress"))
               + _event("response.output_text.delta", item_id="msg_1", delta="partial"))
    provider, binding, requests = _provider(monkeypatch, partial, DONE)

    with pytest.raises(ProviderTransportError) as caught:
        await provider.generate(_request(binding))

    assert caught.value.validation_type == "STREAM_INCOMPLETE"
    assert len(requests) == 1


RATE_LIMIT_EVENT = _event("error", code="rate_limit_exceeded", message="PRIVATE", param=None)
RATE_LIMIT_FAILED = _stream(status="failed",
                            error={"code": "rate_limit_exceeded", "message": "PRIVATE"})


@pytest.mark.parametrize("failing", [RATE_LIMIT_EVENT, RATE_LIMIT_FAILED],
                         ids=["error-event", "failed-response"])
@pytest.mark.asyncio
async def test_rate_limits_in_the_stream_are_retried(monkeypatch, failing):
    provider, binding, requests = _provider(monkeypatch, failing, DONE)

    turn = await provider.generate(_request(binding))

    assert turn.content == "done"
    assert turn.transport_retries == (ProviderRetry(category="STREAM_RATE_LIMITED",
                                                    wait_ms=500),)
    assert len(requests) == 2


@pytest.mark.parametrize("failing", [
    _event("error", code="server_error", message="PRIVATE_CANARY", param=None),
    _event("error", code=None, message="PRIVATE_CANARY", param=None),
    _stream(status="failed", error={"code": "server_error", "message": "PRIVATE_CANARY"}),
    _stream(status="failed", error={"code": "invalid_prompt", "message": "PRIVATE_CANARY"}),
    _stream(status="failed", error=None),
], ids=["error-server", "error-no-code", "failed-server", "failed-invalid", "failed-no-error"])
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
    for field in ("instructions", "tools", "reasoning", "include", "store"):
        assert second.get(field) == first.get(field)
    assert second["input"][:len(first["input"])] == first["input"]


@pytest.mark.asyncio
async def test_reasoning_and_calls_replay_verbatim_with_their_ids(monkeypatch):
    output = (REASONING, _message("I will record both."),
              _call("call_1", {"value": "a"}, "fc_1"), _call("call_2", {"value": "b"}, "fc_2"))
    provider, _binding_value, requests = _provider(monkeypatch, _stream(*output), DONE)
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
    assert first["instructions"] == "Rules."
    assert second["input"][1:5] == list(output)
    outputs = second["input"][5:]
    assert [(item["type"], item["call_id"]) for item in outputs] == [
        ("function_call_output", "call_1"), ("function_call_output", "call_2"),
    ]
    assert "entries=0" in repr(provider._turns)


@pytest.mark.asyncio
async def test_final_answer_replays_as_its_output_message(monkeypatch):
    first_output = (REASONING, _message("First answer."))
    provider, _binding_value, requests = _provider(
        monkeypatch, _stream(*first_output), _stream(_message("Revised answer.", "msg_2")),
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
    assert second["input"][1:3] == list(first_output)
    assert second["input"][3]["role"] == "user"


def test_adapter_accepts_only_its_transport():
    with pytest.raises(ValueError, match="transport"):
        OpenAIResponsesProvider(
            binding=ProviderBinding(
                provider="p", model="m", endpoint="https://p.invalid/v1",
                transport="OPENAI_COMPATIBLE_CHAT", thinking_mode="ENABLED",
            ),
            api_key=KEY,
        )
    with pytest.raises(ValueError, match="api_key"):
        OpenAIResponsesProvider(binding=_binding(), api_key="")
