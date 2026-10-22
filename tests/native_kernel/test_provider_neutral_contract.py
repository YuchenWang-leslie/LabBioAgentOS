"""C14 step 2: neutral provider contract, continuation, projection, retry records."""

from __future__ import annotations

import asyncio
import json
from time import monotonic
from typing import get_args
from uuid import uuid4

import httpx
import jsonschema
import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from labbioagentos.kernel import (
    AgentInvocation,
    AgentLoop,
    AgentMode,
    AgentRegistry,
    AnthropicMessagesDialect,
    BudgetLedger,
    BudgetLimits,
    FakeProvider,
    FinishReason,
    GeminiGenerateContentDialect,
    HttpxChatCompletionTransport,
    IncompleteProviderResponseError,
    KernelEventKind,
    KernelIdentity,
    ModelRequest,
    ModelResultProjector,
    ModelTurn,
    OpenAIChatDialect,
    OpenAICompatibleChatProvider,
    OpenAIResponsesDialect,
    ProviderBinding,
    ProviderProtocolError,
    ProviderRetry,
    ProviderTransportError,
    PublicMessage,
    ToolCall,
    ToolDispatcher,
    ToolRegistry,
)
from labbioagentos.kernel.contracts import ProviderTransport
from labbioagentos.kernel.continuation import (
    PrivateContinuationStore,
    message_fingerprint,
)
from labbioagentos.kernel.errors import unsupported_type_error
from labbioagentos.kernel.http_stream import HttpStreamResult, HttpStreamTransport, RetryPolicy
from labbioagentos.kernel.schema_projection import project_json_schema

REAL_ASYNC_CLIENT = httpx.AsyncClient
RETRIES = (
    ProviderRetry(category="HTTP_429", wait_ms=500),
    ProviderRetry(category="NETWORK_READ", wait_ms=1000),
)


def _binding(transport="OPENAI_COMPATIBLE_CHAT", **values):
    return ProviderBinding(
        provider="fixture",
        model="fixture-model",
        endpoint="https://provider.invalid/v1",
        transport=transport,
        thinking_mode="DISABLED",
        **values,
    )


def _identity():
    invocation_id = uuid4()
    return KernelIdentity(
        thread_id=uuid4(),
        workflow_run_id=uuid4(),
        kernel_run_id=uuid4(),
        invocation_id=invocation_id,
        chain=(invocation_id,),
    )


def _limits():
    return BudgetLimits(
        provider_turns=8,
        tokens=10_000,
        wall_time_ms=60_000,
        tool_calls=8,
        evidence_entries=8,
        evidence_bytes=64_000,
        delegations=0,
        delegation_depth=0,
        finalization_attempts=2,
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


# Criterion 5: transports and dialects


def test_binding_admits_exactly_four_transports():
    assert set(get_args(ProviderTransport)) == {
        "OPENAI_COMPATIBLE_CHAT",
        "OPENAI_RESPONSES",
        "ANTHROPIC_MESSAGES",
        "GEMINI_GENERATE_CONTENT",
    }
    with pytest.raises(ValidationError):
        _binding("OPENAI_COMPATIBLE_RESPONSES")


def test_dialect_defaults_only_where_a_safe_default_exists():
    assert _binding().dialect == OpenAIChatDialect()
    assert _binding("OPENAI_RESPONSES").dialect == OpenAIResponsesDialect()
    for transport in ("ANTHROPIC_MESSAGES", "GEMINI_GENERATE_CONTENT"):
        with pytest.raises(ValidationError):
            _binding(transport)
    assert _binding(
        "ANTHROPIC_MESSAGES",
        dialect=AnthropicMessagesDialect(thinking_off="OMIT"),
    ).dialect.anthropic_version == "2023-06-01"
    assert _binding(
        "GEMINI_GENERATE_CONTENT",
        dialect=GeminiGenerateContentDialect(
            thinking_level_enabled="high", thinking_level_disabled="low",
        ),
    ).dialect.thinking_level_disabled == "low"


@pytest.mark.parametrize("transport,dialect", [
    ("OPENAI_COMPATIBLE_CHAT", OpenAIResponsesDialect()),
    ("OPENAI_RESPONSES", OpenAIChatDialect()),
    ("ANTHROPIC_MESSAGES", GeminiGenerateContentDialect(
        thinking_level_enabled="high", thinking_level_disabled="low",
    )),
    ("GEMINI_GENERATE_CONTENT", AnthropicMessagesDialect(thinking_off="DISABLED")),
])
def test_dialect_must_match_its_transport(transport, dialect):
    with pytest.raises(ValidationError):
        _binding(transport, dialect=dialect)


def test_chat_reasoning_effort_belongs_only_to_its_parameter_form():
    OpenAIChatDialect(
        thinking_parameter="REASONING_EFFORT",
        reasoning_effort_enabled="high", reasoning_effort_disabled="none",
    )
    with pytest.raises(ValidationError):
        OpenAIChatDialect(thinking_parameter="REASONING_EFFORT")
    with pytest.raises(ValidationError):
        OpenAIChatDialect(
            thinking_parameter="REASONING_EFFORT", reasoning_effort_enabled="high",
        )
    with pytest.raises(ValidationError):
        OpenAIChatDialect(reasoning_effort_enabled="high", reasoning_effort_disabled="low")


def test_team_single_binding_check_covers_the_dialect():
    from test_collaboration import _profiles, _team

    team = _team()
    vendor = _binding()
    official = _binding(dialect=OpenAIChatDialect(
        output_limit_parameter="max_completion_tokens",
    ))
    assert vendor != official
    with pytest.raises(ValueError, match="share one exact binding"):
        AgentRegistry(
            team=team,
            profiles=_profiles(team),
            providers={
                agent_id: FakeProvider(
                    binding=official if agent_id == team.leader.agent_id else vendor,
                    turns=(),
                )
                for agent_id in team.member_ids
            },
        )


def test_chat_adapter_accepts_only_its_own_transport():
    OpenAICompatibleChatProvider(
        binding=_binding(dialect=OpenAIChatDialect(stream_include_usage=True)),
        api_key="key",
    )
    with pytest.raises(ValueError, match="transport"):
        OpenAICompatibleChatProvider(binding=_binding("OPENAI_RESPONSES"), api_key="key")


# Criterion 6: generic private continuation


def _assistant(arguments='{"view":"SUMMARY"}', content=None):
    return PublicMessage(
        role="ASSISTANT",
        content=content,
        tool_calls=(ToolCall(call_id="call-1", name="inspect", arguments_json=arguments),),
    )


def test_continuation_is_keyed_by_run_invocation_and_canonical_message():
    store = PrivateContinuationStore()
    run, invocation = uuid4(), uuid4()
    store.put(run, invocation, _assistant(' { "view" : "SUMMARY" } '), "PRIVATE_SENTINEL")

    assert store.get(run, invocation, _assistant()) == "PRIVATE_SENTINEL"
    assert store.get(run, uuid4(), _assistant()) is None
    assert store.get(uuid4(), invocation, _assistant()) is None
    assert store.get(run, invocation, _assistant('{"view":"FULL"}')) is None
    assert store.get(run, invocation, _assistant(content="Visible text.")) is None
    assert "PRIVATE_SENTINEL" not in repr(store)


def test_text_only_assistant_messages_are_keyable():
    store = PrivateContinuationStore()
    run, invocation = uuid4(), uuid4()
    message = PublicMessage(role="ASSISTANT", content="Visible text.")
    store.put(run, invocation, message, "PRIVATE_SENTINEL")
    assert store.get(run, invocation, message) == "PRIVATE_SENTINEL"


def test_unparseable_tool_arguments_are_never_keyed():
    store = PrivateContinuationStore()
    run, invocation = uuid4(), uuid4()
    message = _assistant("{not json")
    assert message_fingerprint(message) is None
    store.put(run, invocation, message, "PRIVATE_SENTINEL")
    assert store.get(run, invocation, message) is None


def test_continuation_is_bounded_per_invocation_and_cleared():
    store = PrivateContinuationStore(max_bytes=10)
    run, first, second = uuid4(), uuid4(), uuid4()
    store.put(run, first, _assistant(), "x" * 10)
    store.put(run, first, _assistant(), "y" * 10)  # replacing frees the old value
    store.put(run, second, _assistant(), "z" * 10)  # each invocation has its own bound

    with pytest.raises(ProviderProtocolError) as caught:
        store.put(run, first, _assistant(content="More."), "x")
    assert caught.value.validation_type == "PRIVATE_CONTINUATION_LIMIT"

    store.clear(run, first)
    assert store.get(run, first, _assistant()) is None
    assert store.get(run, second, _assistant()) == "z" * 10
    store.put(run, first, _assistant(content="More."), "x" * 10)


def test_continuation_bound_counts_utf8_bytes():
    store = PrivateContinuationStore(max_bytes=5)
    with pytest.raises(ProviderProtocolError):
        store.put(uuid4(), uuid4(), _assistant(), "\u4e2d\u4e2d")


# Criterion 7: schema projection with local authority


class LabelArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    label: str = Field(max_length=4, pattern="^[a-z]+$")


def test_projection_removes_keywords_only_at_schema_positions():
    schema = {
        "type": "object",
        "properties": {
            "maxLength": {"type": "string", "maxLength": 3},
            "items": {"type": "array", "items": {"type": "string", "maxLength": 3}},
            "choice": {"anyOf": [{"type": "string", "maxLength": 3}, {"type": "null"}]},
            "ref": {"$ref": "#/$defs/Inner"},
        },
        "$defs": {"Inner": {"type": "string", "maxLength": 3, "default": {"maxLength": 1}}},
        "additionalProperties": False,
        "enum": [{"maxLength": 2}],
    }
    original = json.loads(json.dumps(schema))

    projected = project_json_schema(schema, removed_keywords=frozenset({"maxLength"}))

    assert schema == original
    assert "maxLength" in projected["properties"]
    assert projected["properties"]["maxLength"] == {"type": "string"}
    assert projected["properties"]["items"]["items"] == {"type": "string"}
    assert projected["properties"]["choice"]["anyOf"][0] == {"type": "string"}
    assert projected["$defs"]["Inner"] == {"type": "string", "default": {"maxLength": 1}}
    assert projected["enum"] == [{"maxLength": 2}]
    assert project_json_schema(schema, removed_keywords=frozenset()) == original


@pytest.mark.asyncio
async def test_local_validation_wins_over_a_wider_projected_schema():
    effects = []

    async def label(arguments: LabelArguments):
        effects.append(arguments.label)
        return {"labelled": arguments.label}

    registry = ToolRegistry()
    registry.register(
        name="label_value",
        description="Label one synthetic value.",
        arguments_model=LabelArguments,
        handler=label,
    )
    full = registry.specs[0].parameters
    projected = project_json_schema(
        full, removed_keywords=frozenset({"maxLength", "pattern"})
    )
    value = {"label": "TOO-LONG"}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(value, full)
    jsonschema.validate(value, projected)

    budget = BudgetLedger(_limits())
    loop = AgentLoop(
        provider=FakeProvider(binding=_binding(), turns=(
            ModelTurn(
                tool_calls=(ToolCall(
                    call_id="call-1", name="label_value", arguments_json=json.dumps(value),
                ),),
                finish_reason=FinishReason.TOOL_CALLS,
            ),
            ModelTurn(content="done", finish_reason=FinishReason.STOP),
        )),
        invocation=AgentInvocation(
            identity=_identity(), agent_id="leader",
            mode=AgentMode.CAPABILITY, tool_names=("label_value",),
        ),
        budget_ledger=budget,
        dispatcher=ToolDispatcher(
            registry, ModelResultProjector(max_bytes=16_000), budget_ledger=budget,
        ),
    )

    result = await loop.run(
        messages=(PublicMessage(role="USER", content="Label it."),),
        deadline_monotonic=10_000_000_000.0,
    )

    assert effects == []
    assert [item.status for item in result.tool_results] == ["FAILED"]
    assert result.output == "done"


# Criterion 8: retry records reach the turn, the error and the events


def _plain_loop(provider, *, tools=()):
    return AgentLoop(
        provider=provider,
        invocation=AgentInvocation(
            identity=_identity(), agent_id="leader",
            mode=AgentMode.CAPABILITY, tool_names=tools,
        ),
        budget_ledger=BudgetLedger(_limits()),
    )


def _retry_events(events):
    return [
        (event.reason_code, event.status_code, event.numeric_facts)
        for event in events
        if event.kind is KernelEventKind.PROVIDER_RETRY_OBSERVED
    ]


EXPECTED_EVENTS = [
    ("HTTP_429", "RETRIED", {"turn_index": 1, "attempt": 1, "wait_ms": 500}),
    ("NETWORK_READ", "RETRIED", {"turn_index": 1, "attempt": 2, "wait_ms": 1000}),
]


@pytest.mark.asyncio
async def test_loop_records_retries_before_the_turn_they_preceded():
    loop = _plain_loop(FakeProvider(binding=_binding(), turns=(
        ModelTurn(content="done", finish_reason=FinishReason.STOP, transport_retries=RETRIES),
    )))

    result = await loop.run(
        messages=(PublicMessage(role="USER", content="Run."),),
        deadline_monotonic=10_000_000_000.0,
    )

    assert _retry_events(result.events) == EXPECTED_EVENTS
    kinds = [event.kind for event in result.events]
    assert kinds.index(KernelEventKind.PROVIDER_RETRY_OBSERVED) < kinds.index(
        KernelEventKind.MODEL_TURN_OBSERVED
    )


class FailingProvider:
    def __init__(self, error, *, first=None):
        self.binding = _binding()
        self.error = error
        self.first = first

    async def generate(self, request):
        if self.first is not None:
            turn, self.first = self.first, None
            return turn
        raise self.error


@pytest.mark.parametrize("error", [
    ProviderTransportError(validation_type="HTTP_503", transport_retries=RETRIES),
    ProviderProtocolError(validation_fields=("usage",), transport_retries=RETRIES),
])
@pytest.mark.asyncio
async def test_loop_records_retries_of_a_failed_turn(error):
    loop = _plain_loop(FailingProvider(error))

    with pytest.raises(type(error)):
        await loop.run(
            messages=(PublicMessage(role="USER", content="Run."),),
            deadline_monotonic=10_000_000_000.0,
        )

    assert _retry_events(loop.events) == EXPECTED_EVENTS
    assert loop.events[-1].kind is KernelEventKind.KERNEL_FINISHED
    assert loop.events[-1].status_code == "FAILED"


@pytest.mark.asyncio
async def test_loop_records_retries_before_a_capability_stop():
    from test_agent_loop import _tool_loop

    loop, _provider, _effects = _tool_loop(())
    loop.provider = FailingProvider(
        ProviderTransportError(validation_type="HTTP_503", transport_retries=RETRIES),
        first=ModelTurn(
            tool_calls=(ToolCall(
                call_id="call-1", name="record_value", arguments_json='{"value":"kept"}',
            ),),
            finish_reason=FinishReason.TOOL_CALLS,
        ),
    )

    result = await loop.run(
        messages=(PublicMessage(role="USER", content="Run."),),
        deadline_monotonic=10_000_000_000.0,
    )

    assert result.termination == "PROVIDER_TRANSPORT_STOP"
    assert [facts["turn_index"] for _code, _status, facts in _retry_events(result.events)] == [2, 2]
    assert result.events[-1].status_code == "STOPPED"


def _on_the_wire(response):
    """An unread copy of a pre-read test response, as the network delivers it."""

    if not response.is_stream_consumed:
        return response
    return httpx.Response(
        response.status_code,
        headers=response.headers,
        stream=httpx.ByteStream(response.content),
    )


def _chat_provider(monkeypatch, *responses, real_time=False):
    outcomes = list(responses)
    monkeypatch.setattr(
        "labbioagentos.kernel.http_stream.httpx.AsyncClient",
        lambda **kwargs: REAL_ASYNC_CLIENT(
            transport=httpx.MockTransport(lambda _request: _on_the_wire(outcomes.pop(0))),
            **kwargs,
        ),
    )
    clock = [1_000.0]

    async def sleep(seconds):
        clock[0] += seconds

    transport = HttpxChatCompletionTransport()
    transport._http = (
        HttpStreamTransport(random_fraction=lambda: 0.0) if real_time
        else HttpStreamTransport(
            retry=RetryPolicy(), sleep=sleep, clock=lambda: clock[0],
            random_fraction=lambda: 0.0,
        )
    )
    binding = _binding()
    return OpenAICompatibleChatProvider(
        binding=binding, api_key="key", transport=transport,
    ), binding


def _stream(*chunks):
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)


def _content_chunk(content):
    return {"id": "request-1", "choices": [{
        "index": 0, "delta": {"content": content}, "finish_reason": "stop",
    }]}


@pytest.mark.asyncio
async def test_chat_adapter_returns_retries_with_the_turn(monkeypatch):
    provider, binding = _chat_provider(
        monkeypatch, httpx.Response(503), _stream(_content_chunk("done")),
    )

    turn = await provider.generate(_request(binding))

    assert turn.content == "done"
    assert turn.transport_retries == (ProviderRetry(category="HTTP_503", wait_ms=500),)


@pytest.mark.asyncio
async def test_chat_adapter_keeps_retries_on_every_final_error(monkeypatch):
    provider, binding = _chat_provider(
        monkeypatch, httpx.Response(503), httpx.Response(503), httpx.Response(503),
    )
    with pytest.raises(ProviderTransportError) as exhausted:
        await provider.generate(_request(binding))
    assert exhausted.value.validation_type == "HTTP_503"
    assert [item.wait_ms for item in exhausted.value.transport_retries] == [500, 1000]

    provider, binding = _chat_provider(
        monkeypatch, httpx.Response(503), _stream(_content_chunk("x" * 64_001)),
    )
    with pytest.raises(ProviderProtocolError) as rejected:
        await provider.generate(_request(binding))
    assert rejected.value.validation_fields == ("model_turn", "content")
    assert rejected.value.transport_retries == (ProviderRetry(category="HTTP_503", wait_ms=500),)

    provider, binding = _chat_provider(
        monkeypatch, httpx.Response(503), _stream({"id": "request-1"}),
    )
    with pytest.raises(ProviderProtocolError) as malformed:
        await provider.generate(_request(binding))
    assert malformed.value.validation_fields == ("stream.choices",)
    assert malformed.value.transport_retries == (ProviderRetry(category="HTTP_503", wait_ms=500),)


def _raw_stream(body):
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)


@pytest.mark.parametrize("body,fields", [
    ('data: {"id":"r"}\n\ndata: [DONE]\n\n', ("stream.choices",)),
    ('data: {"choices":[{"index":1,"delta":{}}]}\n\ndata: [DONE]\n\n', ("stream.choices",)),
    ('data: {"choices":[{"index":0,"delta":{"content":7}}]}\n\ndata: [DONE]\n\n',
     ("stream.delta.content",)),
    ('data: {"choices":[{"index":0,"delta":{"content":"x"}}]}\n\ndata: [DONE]\n\n',
     ("stream.finish_reason",)),
], ids=["no-choices", "wrong-index", "content-type", "no-finish-reason"])
@pytest.mark.asyncio
async def test_stream_format_errors_are_protocol_errors(monkeypatch, body, fields):
    provider, binding = _chat_provider(monkeypatch, _raw_stream(body))

    with pytest.raises(ProviderProtocolError) as caught:
        await provider.generate(_request(binding))

    assert caught.value.validation_fields == fields


@pytest.mark.parametrize("body", [
    'data: {"choices":[{"index":0,"delta":{"content":"x"},"finish_reason":"stop"}]}\n\n',
    'data: {"choices":[{"index":0,"delta":{"content":"x"}',
], ids=["no-done", "mid-event"])
@pytest.mark.asyncio
async def test_truncated_stream_is_a_transport_error_and_not_retried(monkeypatch, body):
    provider, binding = _chat_provider(
        monkeypatch, _raw_stream(body), _stream(_content_chunk("unreachable")),
    )

    with pytest.raises(ProviderTransportError) as caught:
        await provider.generate(_request(binding))

    assert caught.value.validation_type == "STREAM_INCOMPLETE"
    assert caught.value.transport_retries == ()


class Stalls(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'data: {"id":"request-2","choices":[]}\n\n'
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_deadline_stop_keeps_the_retries_that_preceded_it(monkeypatch):
    from test_agent_loop import _tool_loop

    loop, _provider, effects = _tool_loop(())
    loop.provider, _binding_value = _chat_provider(
        monkeypatch,
        _stream({"id": "request-1", "choices": [{"index": 0, "delta": {"tool_calls": [{
            "index": 0, "id": "call-1", "type": "function",
            "function": {"name": "record_value", "arguments": '{"value":"kept"}'},
        }]}, "finish_reason": "tool_calls"}]}),
        httpx.Response(503, headers={"retry-after": "0"}),
        httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Stalls()),
        real_time=True,
    )

    result = await asyncio.wait_for(loop.run(
        messages=(PublicMessage(role="USER", content="Run."),),
        # 1.0 s remains for the stalled attempt once the 0.5 s margin is
        # taken off, so a slow machine still reaches that attempt.
        deadline_monotonic=monotonic() + 1.5,
    ), timeout=5)

    assert effects == [{"value": "kept", "nested": None}]
    assert result.termination == "PROVIDER_DEADLINE_STOP"
    assert result.events[-1].reason_code == "KERNEL_DEADLINE_EXCEEDED"
    assert _retry_events(result.events) == [
        ("HTTP_503", "RETRIED", {"turn_index": 2, "attempt": 1, "wait_ms": 0}),
    ]


@pytest.mark.asyncio
async def test_capability_stops_on_a_stream_format_error_as_a_protocol_stop(monkeypatch):
    from test_agent_loop import _tool_loop

    loop, _provider, effects = _tool_loop(())
    loop.provider, _binding_value = _chat_provider(
        monkeypatch,
        _stream({"id": "request-1", "choices": [{"index": 0, "delta": {"tool_calls": [{
            "index": 0, "id": "call-1", "type": "function",
            "function": {"name": "record_value", "arguments": '{"value":"kept"}'},
        }]}, "finish_reason": "tool_calls"}]}),
        _raw_stream('data: {"id":"request-2"}\n\ndata: [DONE]\n\n'),
    )

    result = await loop.run(
        messages=(PublicMessage(role="USER", content="Run."),),
        deadline_monotonic=10_000_000_000.0,
    )

    assert effects == [{"value": "kept", "nested": None}]
    assert result.termination == "PROVIDER_PROTOCOL_STOP"
    assert result.events[-1].reason_code == "PROVIDER_PROTOCOL_ERROR"


# DECIDED-C14-2 as refined on 2026-10-23: unknown content-bearing types are
# named and fail; unknown events and metadata are ignored.


@pytest.mark.parametrize("value,name", [
    ("custom", "custom"),
    ("web_search.v2", "web_search.v2"),
    ("Custom Tool PRIVATE_CANARY", "unrecognized"),
    ("x" * 65, "unrecognized"),
    (7, "unrecognized"),
    (None, "unrecognized"),
])
def test_unsupported_type_error_names_only_plain_identifiers(value, name):
    error = unsupported_type_error("stream.block.type", value)

    assert error.validation_type == "unsupported_type"
    assert error.validation_fields == ("stream.block.type", name)
    assert "PRIVATE_CANARY" not in str(error)


@pytest.mark.asyncio
async def test_streamed_tool_call_of_unknown_type_is_named(monkeypatch):
    provider, binding = _chat_provider(monkeypatch, _stream({
        "id": "request-1", "choices": [{"index": 0, "delta": {"tool_calls": [{
            "index": 0, "id": "call-1", "type": "custom",
            "function": {"name": "inspect", "arguments": "{}"},
        }]}, "finish_reason": "tool_calls"}],
    }))

    with pytest.raises(ProviderProtocolError) as caught:
        await provider.generate(_request(binding))

    assert caught.value.validation_type == "unsupported_type"
    assert caught.value.validation_fields == ("stream.delta.tool_calls.type", "custom")


@pytest.mark.asyncio
async def test_returned_tool_call_of_unknown_type_is_named():
    class Recording:
        async def post_json(self, *, url, headers, payload, timeout_seconds, max_response_bytes):
            return HttpStreamResult(body={"choices": [{"finish_reason": "tool_calls", "message": {
                "tool_calls": [{"id": "call-1", "type": "custom", "custom": {"input": "x"}}],
            }}]})

    binding = _binding()
    provider = OpenAICompatibleChatProvider(binding=binding, api_key="key", transport=Recording())

    with pytest.raises(ProviderProtocolError) as caught:
        await provider.generate(_request(binding))

    assert caught.value.validation_fields == ("message.tool_calls.type", "custom")


@pytest.mark.asyncio
async def test_unknown_events_and_metadata_are_ignored(monkeypatch):
    chunk = {
        "id": "request-1", "object": "chat.completion.chunk", "obfuscation": "pad",
        "moderation": None, "service_tier": "default", "future_metadata": {"a": 1},
        "choices": [{"index": 0, "delta": {"content": "done"}, "finish_reason": "stop"}],
    }
    body = (
        "event: future_event\ndata: " + json.dumps(chunk) + "\n\n"
        + ": keep-alive comment\n\n"
        + "data: [DONE]\n\n"
    )
    provider, binding = _chat_provider(monkeypatch, _raw_stream(body))

    turn = await provider.generate(_request(binding))

    assert turn.content == "done"
    assert turn.finish_reason is FinishReason.STOP


@pytest.mark.asyncio
async def test_unknown_finish_reason_becomes_other(monkeypatch):
    provider, binding = _chat_provider(monkeypatch, _stream({
        "id": "request-1", "choices": [{
            "index": 0, "delta": {"content": "done"}, "finish_reason": "future_reason",
        }],
    }))

    turn = await provider.generate(_request(binding))

    assert turn.finish_reason is FinishReason.OTHER


# Refusals: the finish reason and the declared category reach the record.


def test_refusal_category_requires_the_content_filter_finish():
    ModelTurn(finish_reason=FinishReason.CONTENT_FILTER, refusal_category="bio")
    ModelTurn(finish_reason=FinishReason.CONTENT_FILTER)
    with pytest.raises(ValidationError):
        ModelTurn(finish_reason=FinishReason.STOP, content="done", refusal_category="bio")


@pytest.mark.asyncio
async def test_refusal_category_is_recorded_on_the_turn_event():
    loop = _plain_loop(FakeProvider(binding=_binding(), turns=(
        ModelTurn(finish_reason=FinishReason.CONTENT_FILTER, refusal_category="bio"),
    )))

    with pytest.raises(IncompleteProviderResponseError) as caught:
        await loop.run(
            messages=(PublicMessage(role="USER", content="Run."),),
            deadline_monotonic=10_000_000_000.0,
        )

    assert caught.value.validation_type == "CONTENT_FILTER"
    observed = [
        (event.status_code, event.reason_code) for event in loop.events
        if event.kind is KernelEventKind.MODEL_TURN_OBSERVED
    ]
    assert observed == [("CONTENT_FILTER", "bio")]
