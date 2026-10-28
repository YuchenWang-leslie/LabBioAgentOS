"""C14 step 5: Gemini generateContent adapter (kernel layer only)."""

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
    GeminiGenerateContentDialect,
    GeminiGenerateContentProvider,
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
DIALECT = GeminiGenerateContentDialect(thinking_level_enabled="high", thinking_level_disabled="low")
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


# SSE fixtures: one GenerateContentResponse per event, no end marker.


def _chunk(*parts, finish=None, usage=None, response_id="resp-1", **extra):
    candidate = {"content": {"role": "model", "parts": list(parts)}, "index": 0}
    if finish is not None:
        candidate["finishReason"] = finish
    value = {"candidates": [candidate], "modelVersion": "gemini-3-flash", "responseId": response_id,
             **extra}
    if usage is not None:
        value["usageMetadata"] = usage
    return "data: " + json.dumps(value) + "\r\n\r\n"


USAGE = {"promptTokenCount": 10, "toolUsePromptTokenCount": 2, "candidatesTokenCount": 5,
         "thoughtsTokenCount": 7, "totalTokenCount": 24}
DONE = _chunk({"text": "done"}, finish="STOP", usage=USAGE)


def _response(body):
    return httpx.Response(
        200, headers={"content-type": "text/event-stream"},
        stream=httpx.ByteStream(body.encode("utf-8")),
    )


def _binding(*, dialect=DIALECT, thinking="ENABLED", strict=False, json_object=False,
             model="gemini-3-flash"):
    return ProviderBinding(
        provider="google",
        model=model,
        endpoint="https://generativelanguage.googleapis.invalid/v1beta",
        transport="GEMINI_GENERATE_CONTENT",
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
    provider = GeminiGenerateContentProvider(
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


# Request form (criterion 11)


@pytest.mark.parametrize("model", ["gemini-3-flash", "models/gemini-3-flash"])
@pytest.mark.asyncio
async def test_key_travels_only_in_its_header(monkeypatch, model):
    provider, binding, requests = _provider(monkeypatch, DONE, model=model)

    await provider.generate(_request(binding))

    request = requests[0]
    assert str(request.url) == (
        "https://generativelanguage.googleapis.invalid/v1beta/models/"
        "gemini-3-flash:streamGenerateContent?alt=sse"
    )
    assert request.headers["x-goog-api-key"] == KEY
    assert "authorization" not in request.headers
    elsewhere = [value for name, value in request.headers.items() if name != "x-goog-api-key"]
    assert KEY not in str(request.url) + request.content.decode() + "".join(elsewhere)


@pytest.mark.asyncio
async def test_history_mapping_groups_responses_after_their_calls(monkeypatch):
    provider, binding, requests = _provider(monkeypatch, DONE)
    history = (
        PublicMessage(role="SYSTEM", content="Operator rules."),
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
    assert body["systemInstruction"] == {"parts": [{"text": "Operator rules."}]}
    assert body["contents"] == [
        {"role": "user", "parts": [{"text": "Run."}]},
        {"role": "model", "parts": [
            {"text": "Checking."},
            {"functionCall": {"name": "inspect", "args": {"view": "A"}, "id": "fc-a"}},
            {"functionCall": {"name": "inspect", "args": {"view": "B"}, "id": "fc-b"}},
        ]},
        {"role": "user", "parts": [
            {"functionResponse": {"name": "inspect", "id": "fc-a",
                                  "response": {"error": '{"status":"FAILED"}'}}},
            {"functionResponse": {"name": "inspect", "id": "fc-b",
                                  "response": {"output": '{"status":"SUCCEEDED"}'}}},
        ]},
        {"role": "user", "parts": [{"text": "Again."}]},
    ]


@pytest.mark.parametrize("thinking,level", [("ENABLED", "high"), ("DISABLED", "low")])
@pytest.mark.asyncio
async def test_thinking_level_follows_the_thinking_state(monkeypatch, thinking, level):
    provider, binding, requests = _provider(monkeypatch, DONE, thinking=thinking)

    await provider.generate(_request(binding))

    generation = _body(requests[0])["generationConfig"]
    assert generation == {"maxOutputTokens": 4096, "thinkingConfig": {"thinkingLevel": level}}


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.asyncio
async def test_tools_and_json_output_send_full_json_schemas(monkeypatch, strict):
    provider, binding, requests = _provider(monkeypatch, DONE, strict=strict)

    await provider.generate(_request(binding, tools=(TOOL_SPEC,), schema=RESPONSE_SCHEMA))

    body = _body(requests[0])
    assert body["tools"] == [{"functionDeclarations": [{
        "name": "inspect", "description": "Inspect a bounded object.",
        "parametersJsonSchema": TOOL_PARAMETERS,
    }]}]
    if strict:
        assert body["toolConfig"] == {"functionCallingConfig": {"mode": "VALIDATED"}}
    else:
        assert "toolConfig" not in body
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["generationConfig"]["responseJsonSchema"] == RESPONSE_SCHEMA


@pytest.mark.asyncio
async def test_json_object_mode_carries_the_schema_in_the_system_instruction(monkeypatch):
    provider, binding, requests = _provider(monkeypatch, DONE, json_object=True)

    await provider.generate(_request(binding, schema=RESPONSE_SCHEMA))

    body = _body(requests[0])
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert "responseJsonSchema" not in body["generationConfig"]
    assert body["systemInstruction"] == {"parts": [{"text": schema_instruction(RESPONSE_SCHEMA)}]}


@pytest.mark.asyncio
async def test_request_enables_no_opt_in_features(monkeypatch):
    provider, binding, requests = _provider(monkeypatch, DONE, strict=True)

    await provider.generate(_request(binding, tools=(TOOL_SPEC,), schema=RESPONSE_SCHEMA))

    body = _body(requests[0])
    assert set(body) <= {"contents", "systemInstruction", "tools", "toolConfig", "generationConfig"}
    text = json.dumps(body)
    for absent in ("candidateCount", "cachedContent", "store", "safetySettings",
                   "includeThoughts", "googleSearch", "codeExecution", "urlContext"):
        assert absent not in text


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


# Stream reassembly


@pytest.mark.asyncio
async def test_stream_reassembles_text_calls_signatures_and_usage(monkeypatch):
    body = (
        _chunk({"text": "Let me "})
        + _chunk({"text": "check."})
        + _chunk({"functionCall": {"name": "inspect", "args": {"view": "A"}, "id": "fc-1"},
                  "thoughtSignature": "SIG_PRIVATE"},
                 {"functionCall": {"name": "inspect", "args": {"view": "B"}, "id": "fc-2"}},
                 finish="STOP", usage=USAGE)
    )
    provider, binding, _requests = _provider(monkeypatch, body)

    turn = await provider.generate(_request(binding, tools=(TOOL_SPEC,)))

    assert turn.content == "Let me check."
    assert [(c.call_id, c.arguments_json) for c in turn.tool_calls] == [
        ("fc-1", '{"view":"A"}'), ("fc-2", '{"view":"B"}'),
    ]
    assert turn.finish_reason is FinishReason.TOOL_CALLS
    assert turn.private_reasoning_present is True
    assert turn.provider_request_id == "resp-1"
    assert (turn.usage.input_tokens, turn.usage.output_tokens, turn.usage.total_tokens) == (
        12, 12, 24,
    )
    assert "SIG_PRIVATE" not in repr(turn)


@pytest.mark.asyncio
async def test_response_id_outside_the_identifier_form_is_unknown(monkeypatch):
    # Live response ids did not always have the kernel Identifier form.
    body = _chunk({"text": "done"}, finish="STOP", usage=USAGE, response_id="-7bQaZ_x")
    provider, binding, _requests = _provider(monkeypatch, body)

    turn = await provider.generate(_request(binding))

    assert turn.content == "done"
    assert turn.finish_reason is FinishReason.STOP
    assert turn.provider_request_id is None


@pytest.mark.parametrize("finish,expected", [
    ("STOP", FinishReason.STOP),
    ("MAX_TOKENS", FinishReason.LENGTH),
    ("MALFORMED_FUNCTION_CALL", FinishReason.OTHER),
    ("MISSING_THOUGHT_SIGNATURE", FinishReason.OTHER),
    ("CONTINUATION", FinishReason.OTHER),
    ("FUTURE_REASON", FinishReason.OTHER),
])
@pytest.mark.asyncio
async def test_finish_reasons(monkeypatch, finish, expected):
    provider, binding, _requests = _provider(monkeypatch, _chunk({"text": "x"}, finish=finish))

    turn = await provider.generate(_request(binding))

    assert turn.finish_reason is expected


@pytest.mark.parametrize("finish,category", [
    ("SAFETY", "safety"), ("RECITATION", "recitation"),
    ("PROHIBITED_CONTENT", "prohibited_content"), ("SPII", "spii"),
])
@pytest.mark.asyncio
async def test_filtered_finish_keeps_only_its_reason(monkeypatch, finish, category):
    body = _chunk({"text": "PRIVATE_PARTIAL"},
                  {"functionCall": {"name": "inspect", "args": {}, "id": "fc-1"}}, finish=finish)
    provider, binding, _requests = _provider(monkeypatch, body)

    turn = await provider.generate(_request(binding, tools=(TOOL_SPEC,)))

    assert turn.finish_reason is FinishReason.CONTENT_FILTER
    assert turn.content is None and turn.tool_calls == ()
    assert turn.refusal_category == category
    assert "PRIVATE_PARTIAL" not in repr(turn)


@pytest.mark.asyncio
async def test_blocked_prompt_is_a_content_filter(monkeypatch):
    body = "data: " + json.dumps({
        "promptFeedback": {"blockReason": "PROHIBITED_CONTENT", "safetyRatings": []},
        "usageMetadata": {"promptTokenCount": 9, "totalTokenCount": 9},
    }) + "\n\n"
    provider, binding, _requests = _provider(monkeypatch, body)

    turn = await provider.generate(_request(binding))

    assert turn.finish_reason is FinishReason.CONTENT_FILTER
    assert turn.refusal_category == "prohibited_content"
    assert turn.usage.output_tokens is None


@pytest.mark.parametrize("part,fields", [
    ({"executableCode": {"code": "x"}}, ("stream.part", "executableCode")),
    ({"inlineData": {"mimeType": "image/png", "data": "AAAA"}}, ("stream.part", "inlineData")),
    ({"thoughtSignature": "SIG"}, ("stream.part", "unrecognized")),
])
@pytest.mark.asyncio
async def test_unknown_part_kinds_fail_and_are_named(monkeypatch, part, fields):
    provider, binding, _requests = _provider(monkeypatch, _chunk(part, finish="STOP"))

    with pytest.raises(ProviderProtocolError) as caught:
        await provider.generate(_request(binding))

    assert caught.value.validation_type == "unsupported_type"
    assert caught.value.validation_fields == fields


@pytest.mark.asyncio
async def test_function_call_without_id_is_refused(monkeypatch):
    body = _chunk({"functionCall": {"name": "inspect", "args": {}}}, finish="STOP")
    provider, binding, _requests = _provider(monkeypatch, body)

    with pytest.raises(ProviderProtocolError) as caught:
        await provider.generate(_request(binding, tools=(TOOL_SPEC,)))

    assert caught.value.validation_fields == ("stream.function_call.id",)


@pytest.mark.parametrize("body,field", [
    (_chunk({"text": "x"}, finish="STOP", response_id="a") + _chunk({"text": "y"}, response_id="b"),
     "stream.responseId"),
    ("data: " + json.dumps({"candidates": [{"index": 0}, {"index": 1}]}) + "\n\n",
     "stream.candidates"),
], ids=["changed-id", "two-candidates"])
@pytest.mark.asyncio
async def test_malformed_streams_are_protocol_errors(monkeypatch, body, field):
    provider, binding, _requests = _provider(monkeypatch, body)

    with pytest.raises(ProviderProtocolError) as caught:
        await provider.generate(_request(binding))

    assert caught.value.validation_fields == (field,)


@pytest.mark.asyncio
async def test_stream_without_finish_reason_is_incomplete(monkeypatch):
    provider, binding, requests = _provider(monkeypatch, _chunk({"text": "partial"}), DONE)

    with pytest.raises(ProviderTransportError) as caught:
        await provider.generate(_request(binding))

    assert caught.value.validation_type == "STREAM_INCOMPLETE"
    assert len(requests) == 1


@pytest.mark.parametrize("status,category", [
    ("UNAVAILABLE", "STREAM_OVERLOADED"), ("RESOURCE_EXHAUSTED", "STREAM_RATE_LIMITED"),
])
@pytest.mark.asyncio
async def test_overload_and_quota_errors_in_the_stream_are_retried(monkeypatch, status, category):
    failing = "data: " + json.dumps({"error": {"code": 503, "message": "PRIVATE", "status": status}}) + "\n\n"
    provider, binding, requests = _provider(monkeypatch, failing, DONE)

    turn = await provider.generate(_request(binding))

    assert turn.content == "done"
    assert turn.transport_retries == (ProviderRetry(category=category, wait_ms=500),)
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_other_stream_errors_fail_without_retry_or_text(monkeypatch):
    failing = "data: " + json.dumps({"error": {"code": 400, "message": "PRIVATE_CANARY",
                                               "status": "INVALID_ARGUMENT"}}) + "\n\n"
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
    assert second.get("systemInstruction") == first.get("systemInstruction")
    assert second.get("tools") == first.get("tools")
    assert second["contents"][:len(first["contents"])] == first["contents"]


@pytest.mark.asyncio
async def test_parallel_calls_replay_with_signatures_in_their_parts(monkeypatch):
    first_response = (
        _chunk({"text": "I will "})
        + _chunk({"text": "record both."},
                 {"functionCall": {"name": "record_value", "args": {"value": "a"}, "id": "fc-1"},
                  "thoughtSignature": "SIG_ONE"},
                 {"functionCall": {"name": "record_value", "args": {"value": "b"}, "id": "fc-2"}},
                 finish="STOP")
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
    assert second["contents"][1] == {"role": "model", "parts": [
        {"text": "I will "},
        {"text": "record both."},
        {"functionCall": {"name": "record_value", "args": {"value": "a"}, "id": "fc-1"},
         "thoughtSignature": "SIG_ONE"},
        {"functionCall": {"name": "record_value", "args": {"value": "b"}, "id": "fc-2"}},
    ]}
    responses = second["contents"][2]["parts"]
    assert [part["functionResponse"]["id"] for part in responses] == ["fc-1", "fc-2"]
    assert "entries=0" in repr(provider._turns)


@pytest.mark.asyncio
async def test_signature_on_a_final_empty_part_is_replayed(monkeypatch):
    provider, _binding_value, requests = _provider(
        monkeypatch,
        _chunk({"text": "First answer."}) + _chunk({"text": "", "thoughtSignature": "SIG_TAIL"},
                                                   finish="STOP"),
        _chunk({"text": "Revised answer."}, finish="STOP"),
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
    assert second["contents"][1] == {"role": "model", "parts": [
        {"text": "First answer."},
        {"text": "", "thoughtSignature": "SIG_TAIL"},
    ]}


@pytest.mark.asyncio
async def test_empty_parts_without_signature_are_not_replayed(monkeypatch):
    provider, _binding_value, requests = _provider(
        monkeypatch,
        _chunk({"text": "First answer."}) + _chunk({"text": ""}, finish="STOP"),
        _chunk({"text": "Revised answer."}, finish="STOP"),
    )
    loop, _effects = _loop(provider, tools=False, completion_feedback=lambda answer: (
        "Again." if answer == "First answer." else None
    ))

    await loop.run(messages=(PublicMessage(role="USER", content="Answer."),),
                   deadline_monotonic=10_000_000_000.0)

    assert _body(requests[1])["contents"][1] == {
        "role": "model", "parts": [{"text": "First answer."}],
    }


@pytest.mark.asyncio
async def test_response_to_an_unknown_call_is_refused(monkeypatch):
    provider, binding, requests = _provider(monkeypatch, DONE)
    history = (
        PublicMessage(role="USER", content="Run."),
        PublicMessage(role="TOOL", content="{}", tool_call_id="fc-missing"),
    )

    with pytest.raises(ProviderProtocolError) as caught:
        await provider.generate(_request(binding, history))

    assert caught.value.validation_fields == ("history.function_response.name",)
    assert requests == []


def test_adapter_accepts_only_its_transport():
    with pytest.raises(ValueError, match="transport"):
        GeminiGenerateContentProvider(
            binding=ProviderBinding(
                provider="p", model="m", endpoint="https://p.invalid/v1",
                transport="OPENAI_COMPATIBLE_CHAT", thinking_mode="ENABLED",
            ),
            api_key=KEY,
        )
    with pytest.raises(ValueError, match="api_key"):
        GeminiGenerateContentProvider(binding=_binding(), api_key="")
