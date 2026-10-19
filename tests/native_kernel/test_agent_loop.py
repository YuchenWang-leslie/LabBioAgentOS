"""P3 acceptance for the native single-Agent provider loop."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import BaseModel, ConfigDict

from labbioagentos.kernel import (
    AgentInvocation,
    AgentLoop,
    AgentMode,
    BudgetExceededError,
    BudgetLedger,
    BudgetLimits,
    FakeProvider,
    FakeProviderExhaustedError,
    FinalizationProtocolError,
    FinalizationRejectedError,
    FinishReason,
    IncompleteProviderResponseError,
    InterruptReason,
    InterruptToken,
    KernelDeadlineExceededError,
    KernelIdentity,
    KernelInterruptedError,
    ModelResultProjector,
    ModelTurn,
    NoObservableProgressError,
    ProviderBinding,
    ProviderTransportError,
    PublicMessage,
    TokenUsage,
    ToolCall,
    ToolDispatcher,
    ToolRegistry,
)


class RecordArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    value: str
    nested: str | None = None


class CountResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    count: int


class FinalEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    result: CountResult


def _binding() -> ProviderBinding:
    return ProviderBinding(
        provider="fixture",
        model="fixture-model",
        endpoint="https://provider.invalid/v1",
        transport="OPENAI_COMPATIBLE_CHAT",
        thinking_mode="DISABLED",
    )


def _identity() -> KernelIdentity:
    invocation_id = uuid4()
    return KernelIdentity(
        thread_id=uuid4(),
        workflow_run_id=uuid4(),
        kernel_run_id=uuid4(),
        invocation_id=invocation_id,
        chain=(invocation_id,),
    )


def _invocation(*, final: bool = False) -> AgentInvocation:
    return AgentInvocation(
        identity=_identity(),
        agent_id="leader",
        mode=AgentMode.FINALIZE if final else AgentMode.CAPABILITY,
        tool_names=() if final else ("record_value",),
    )


def _limits(
    *,
    provider_turns: int = 8,
    tokens: int = 10_000,
    tool_calls: int = 8,
    finalization_attempts: int = 2,
) -> BudgetLimits:
    return BudgetLimits(
        provider_turns=provider_turns,
        tokens=tokens,
        wall_time_ms=60_000,
        tool_calls=tool_calls,
        evidence_entries=8,
        evidence_bytes=64_000,
        delegations=0,
        delegation_depth=0,
        finalization_attempts=finalization_attempts,
    )


def _tool_loop(
    turns, *, clock=None, no_progress_ms=30_000, provider_turns=8,
    tokens=10_000, tool_calls=8, on_record=None
):
    effects = []

    async def record(arguments: RecordArguments):
        if on_record is not None:
            on_record()
        effects.append(arguments.model_dump())
        return {"recorded": arguments.value, "nested": arguments.nested}

    registry = ToolRegistry()
    registry.register(
        name="record_value",
        description="Record one synthetic bounded value.",
        arguments_model=RecordArguments,
        handler=record,
    )
    budget = BudgetLedger(_limits(
        provider_turns=provider_turns,
        tokens=tokens,
        tool_calls=tool_calls,
    ))
    provider = FakeProvider(binding=_binding(), turns=turns)
    dispatcher = ToolDispatcher(
        registry,
        ModelResultProjector(max_bytes=16_000),
        budget_ledger=budget,
    )
    loop = AgentLoop(
        provider=provider,
        invocation=_invocation(),
        budget_ledger=budget,
        dispatcher=dispatcher,
        no_progress_ms=no_progress_ms,
        clock=clock,
    )
    return loop, provider, effects


@pytest.mark.asyncio
async def test_long_tool_wait_does_not_expire_model_phase_deadline():
    now = [0.0]
    loop, provider, effects = _tool_loop(
        (
            ModelTurn(
                tool_calls=(ToolCall(
                    call_id="scheduled-job",
                    name="record_value",
                    arguments_json='{"value":"completed"}',
                ),),
                finish_reason=FinishReason.TOOL_CALLS,
            ),
            ModelTurn(content="done", finish_reason=FinishReason.STOP),
        ),
        clock=lambda: now[0],
        on_record=lambda: now.__setitem__(0, 120.0),
    )

    result = await loop.run(
        messages=(PublicMessage(role="USER", content="Run the fixture."),),
        deadline_monotonic=60.0,
    )

    assert result.output == "done"
    assert effects[0]["value"] == "completed"
    assert len(provider.requests) == 2


@pytest.mark.asyncio
async def test_capability_loop_stops_before_batch_crosses_tool_budget():
    loop, provider, effects = _tool_loop(
        (
            ModelTurn(
                tool_calls=(ToolCall(
                    call_id="within-budget",
                    name="record_value",
                    arguments_json='{"value":"kept"}',
                ),),
                finish_reason=FinishReason.TOOL_CALLS,
            ),
            ModelTurn(
                tool_calls=(ToolCall(
                    call_id="over-budget",
                    name="record_value",
                    arguments_json='{"value":"must-not-run"}',
                ),),
                finish_reason=FinishReason.TOOL_CALLS,
            ),
        ),
        tool_calls=1,
    )

    result = await loop.run(
        messages=(PublicMessage(role="USER", content="Run within the bound."),),
        deadline_monotonic=10_000_000_000.0,
    )

    assert result.termination == "CAPABILITY_EVIDENCE_LIMIT"
    assert result.output == {
        "capability_stop_reason": "CAPABILITY_EVIDENCE_LIMIT"
    }
    assert effects == [{"value": "kept", "nested": None}]
    assert result.budget_usage.tool_calls == 1
    assert len(provider.requests) == 2
    assert result.events[-1].status_code == "STOPPED"
    assert result.events[-1].reason_code == "CAPABILITY_EVIDENCE_LIMIT"


@pytest.mark.asyncio
async def test_capability_loop_stops_before_over_budget_provider_turn_dispatches():
    loop, provider, effects = _tool_loop(
        (
            ModelTurn(
                tool_calls=(ToolCall(
                    call_id="within-token-budget",
                    name="record_value",
                    arguments_json='{"value":"kept"}',
                ),),
                finish_reason=FinishReason.TOOL_CALLS,
                usage=TokenUsage(total_tokens=4),
            ),
            ModelTurn(
                tool_calls=(ToolCall(
                    call_id="over-token-budget",
                    name="record_value",
                    arguments_json='{"value":"must-not-run"}',
                ),),
                finish_reason=FinishReason.TOOL_CALLS,
                usage=TokenUsage(total_tokens=7),
            ),
        ),
        tokens=10,
    )

    result = await loop.run(
        messages=(PublicMessage(role="USER", content="Run within the bound."),),
        deadline_monotonic=10_000_000_000.0,
    )

    assert result.termination == "PROVIDER_USAGE_LIMIT"
    assert result.output == {
        "capability_stop_reason": "PROVIDER_USAGE_LIMIT",
        "budget_dimension": "tokens",
    }
    assert effects == [{"value": "kept", "nested": None}]
    assert len(provider.requests) == 2
    assert len(result.observations) == 2
    assert [item.call_id for item in result.tool_results] == ["within-token-budget"]
    assert result.budget_usage.tokens == 4
    assert result.events[-1].status_code == "STOPPED"
    assert result.events[-1].reason_code == "PROVIDER_TOKEN_LIMIT"


@pytest.mark.asyncio
async def test_capability_loop_dispatches_turn_that_exactly_reaches_token_limit():
    loop, provider, effects = _tool_loop(
        (
            ModelTurn(
                tool_calls=(ToolCall(
                    call_id="first",
                    name="record_value",
                    arguments_json='{"value":"first"}',
                ),),
                finish_reason=FinishReason.TOOL_CALLS,
                usage=TokenUsage(total_tokens=4),
            ),
            ModelTurn(
                tool_calls=(ToolCall(
                    call_id="at-limit",
                    name="record_value",
                    arguments_json='{"value":"second"}',
                ),),
                finish_reason=FinishReason.TOOL_CALLS,
                usage=TokenUsage(total_tokens=6),
            ),
        ),
        provider_turns=2,
        tokens=10,
    )

    result = await loop.run(
        messages=(PublicMessage(role="USER", content="Reach the exact bound."),),
        deadline_monotonic=10_000_000_000.0,
    )

    assert result.termination == "PROVIDER_TURN_LIMIT"
    assert effects == [
        {"value": "first", "nested": None},
        {"value": "second", "nested": None},
    ]
    assert len(provider.requests) == 2
    assert result.budget_usage.tokens == 10


@pytest.mark.asyncio
async def test_capability_loop_preserves_prior_evidence_on_later_transport_failure():
    class LaterTransportFailure:
        binding = _binding()

        def __init__(self):
            self.calls = 0

        async def generate(self, _request):
            self.calls += 1
            if self.calls == 1:
                return ModelTurn(
                    tool_calls=(ToolCall(
                        call_id="persisted-before-transport-failure",
                        name="record_value",
                        arguments_json='{"value":"kept"}',
                    ),),
                    finish_reason=FinishReason.TOOL_CALLS,
                    usage=TokenUsage(total_tokens=4),
                )
            raise ProviderTransportError()

    provider = LaterTransportFailure()
    loop, _fixture_provider, effects = _tool_loop(())
    loop.provider = provider

    result = await loop.run(
        messages=(PublicMessage(role="USER", content="Preserve completed evidence."),),
        deadline_monotonic=10_000_000_000.0,
    )

    assert result.termination == "PROVIDER_TRANSPORT_STOP"
    assert result.output == {
        "capability_stop_reason": "PROVIDER_TRANSPORT_STOP"
    }
    assert effects == [{"value": "kept", "nested": None}]
    assert [item.call_id for item in result.tool_results] == [
        "persisted-before-transport-failure"
    ]
    assert result.events[-1].status_code == "STOPPED"
    assert result.events[-1].reason_code == "PROVIDER_TRANSPORT_ERROR"


@pytest.mark.asyncio
async def test_capability_loop_preserves_prior_evidence_on_later_incomplete_turn():
    loop, provider, effects = _tool_loop((
        ModelTurn(
            tool_calls=(ToolCall(
                call_id="persisted-before-incomplete-turn",
                name="record_value",
                arguments_json='{"value":"kept"}',
            ),),
            finish_reason=FinishReason.TOOL_CALLS,
            usage=TokenUsage(total_tokens=4),
        ),
        ModelTurn(content="partial", finish_reason=FinishReason.LENGTH),
    ))

    result = await loop.run(
        messages=(PublicMessage(role="USER", content="Preserve completed evidence."),),
        deadline_monotonic=10_000_000_000.0,
    )

    assert result.termination == "INCOMPLETE_PROVIDER_STOP"
    assert result.output == {
        "capability_stop_reason": "INCOMPLETE_PROVIDER_STOP",
        "finish_reason": "LENGTH",
    }
    assert effects == [{"value": "kept", "nested": None}]
    assert len(provider.requests) == 2
    assert [item.call_id for item in result.tool_results] == [
        "persisted-before-incomplete-turn"
    ]
    assert result.events[-1].status_code == "STOPPED"
    assert result.events[-1].reason_code == "INCOMPLETE_PROVIDER_RESPONSE"


@pytest.mark.asyncio
async def test_capability_loop_replays_canonical_calls_and_paired_results():
    loop, provider, effects = _tool_loop(
        (
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        call_id="call-1",
                        name="record_value",
                        arguments_json=' { "value" : "alpha", "nested" : null } ',
                    ),
                ),
                finish_reason=FinishReason.TOOL_CALLS,
                usage=TokenUsage(input_tokens=8, output_tokens=4, total_tokens=12),
            ),
            ModelTurn(content="complete", finish_reason=FinishReason.STOP),
        )
    )

    result = await loop.run(
        messages=(PublicMessage(role="USER", content="Run the fixture."),),
        deadline_monotonic=10_000_000_000.0,
    )

    assert result.output == "complete"
    assert result.termination == "MODEL_COMPLETED"
    assert effects == [{"value": "alpha", "nested": None}]
    replay = provider.requests[1].messages
    assistant = next(item for item in replay if item.role == "ASSISTANT")
    assert assistant.tool_calls[0].arguments_json == '{"nested":null,"value":"alpha"}'
    tool = next(item for item in replay if item.role == "TOOL")
    feedback = json.loads(tool.content)
    assert feedback["call_id"] == "call-1"
    assert feedback["status"] == "SUCCEEDED"
    assert result.tool_results[0].call_id == "call-1"
    assert result.budget_usage.provider_turns == 2
    assert result.budget_usage.tool_calls == 1
    assert [item.turn_index for item in result.observations] == [1, 2]


@pytest.mark.asyncio
async def test_capability_loop_stops_after_host_requests_convergence():
    stop_reason = None

    async def record(_arguments: RecordArguments):
        nonlocal stop_reason
        stop_reason = "REPEATED_NON_RETRYABLE_REQUEST"
        return {"recorded": "bounded"}

    registry = ToolRegistry()
    registry.register(
        name="record_value", description="Record one bounded value.",
        arguments_model=RecordArguments, handler=record,
    )
    provider = FakeProvider(binding=_binding(), turns=(ModelTurn(
        tool_calls=(ToolCall(call_id="repeat", name="record_value",
                             arguments_json='{"value":"bounded"}'),),
        finish_reason=FinishReason.TOOL_CALLS,
    ),))
    budget = BudgetLedger(_limits())
    loop = AgentLoop(
        provider=provider, invocation=_invocation(), budget_ledger=budget,
        dispatcher=ToolDispatcher(
            registry, ModelResultProjector(max_bytes=16_000), budget_ledger=budget,
            stop_requested=lambda: stop_reason,
        ),
    )

    result = await loop.run(
        messages=(PublicMessage(role="USER", content="Run the fixture."),),
        deadline_monotonic=10_000_000_000.0,
    )

    assert result.termination == "RUNTIME_STOPPED"
    assert result.output == {"capability_stop_reason": "REPEATED_NON_RETRYABLE_REQUEST"}
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_capability_loop_final_tool_batch_reaches_declared_turn_limit_cleanly():
    effects = []

    async def record(arguments: RecordArguments):
        effects.append(arguments.value)
        return {"recorded": arguments.value}

    registry = ToolRegistry()
    registry.register(
        name="record_value", description="Record one bounded value.",
        arguments_model=RecordArguments, handler=record,
    )
    provider = FakeProvider(binding=_binding(), turns=(ModelTurn(
        tool_calls=(ToolCall(call_id="last", name="record_value",
                             arguments_json='{"value":"bounded"}'),),
        finish_reason=FinishReason.TOOL_CALLS,
    ),))
    budget = BudgetLedger(_limits(provider_turns=1))
    loop = AgentLoop(
        provider=provider, invocation=_invocation(), budget_ledger=budget,
        dispatcher=ToolDispatcher(
            registry, ModelResultProjector(max_bytes=16_000), budget_ledger=budget,
        ),
    )

    result = await loop.run(
        messages=(PublicMessage(role="USER", content="Run the fixture."),),
        deadline_monotonic=10_000_000_000.0,
    )

    assert result.termination == "PROVIDER_TURN_LIMIT"
    assert result.output == {"capability_stop_reason": "PROVIDER_TURN_LIMIT"}
    assert effects == ["bounded"]
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_invalid_batch_returns_bounded_feedback_without_effect_then_recovers():
    loop, provider, effects = _tool_loop(
        (
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        call_id="bad",
                        name="record_value",
                        arguments_json='{"value":"first","value":"second"}',
                    ),
                ),
                finish_reason=FinishReason.TOOL_CALLS,
            ),
            ModelTurn(content="complete", finish_reason=FinishReason.STOP),
        )
    )

    result = await loop.run(
        messages=(PublicMessage(role="USER", content="Run the fixture."),),
        deadline_monotonic=10_000_000_000.0,
    )

    assert result.output == "complete"
    assert effects == []
    assert result.argument_observations[0][0].parse_mode == "REJECTED"
    assert result.argument_observations[0][0].rejection_reason == "INVALID_JSON_OBJECT"
    replay = provider.requests[1].messages
    assistant = next(item for item in replay if item.role == "ASSISTANT")
    assert assistant.tool_calls[0].arguments_json == (
        '{"value":"first","value":"second"}'
    )
    feedback = json.loads(next(item for item in replay if item.role == "TOOL").content)
    assert feedback["error"]["code"] == "INVALID_JSON_OBJECT"


@pytest.mark.asyncio
async def test_invalid_tool_arguments_feedback_names_only_schema_fields():
    loop, provider, effects = _tool_loop(
        (
            ModelTurn(
                tool_calls=(ToolCall(
                    call_id="bad-type",
                    name="record_value",
                    arguments_json='{"value":7}',
                ),),
                finish_reason=FinishReason.TOOL_CALLS,
            ),
            ModelTurn(content="complete", finish_reason=FinishReason.STOP),
        )
    )

    result = await loop.run(
        messages=(PublicMessage(role="USER", content="Run the fixture."),),
        deadline_monotonic=10_000_000_000.0,
    )

    assert effects == []
    observation = result.argument_observations[0][0]
    assert observation.rejection_reason == "INVALID_TOOL_ARGUMENTS"
    assert observation.validation_fields == ("value",)
    feedback = json.loads(
        next(
            item
            for item in provider.requests[1].messages
            if item.role == "TOOL"
        ).content
    )
    assert feedback["error"]["code"] == "INVALID_TOOL_ARGUMENTS"
    assert feedback["error"]["validation_fields"] == ["value"]


@pytest.mark.asyncio
async def test_incomplete_tool_turn_rejects_before_effect_and_can_recover():
    loop, _provider, effects = _tool_loop(
        (
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        call_id="truncated",
                        name="record_value",
                        arguments_json='{"value":"valid"}',
                    ),
                ),
                finish_reason=FinishReason.LENGTH,
            ),
            ModelTurn(content="complete", finish_reason=FinishReason.STOP),
        )
    )

    result = await loop.run(
        messages=(PublicMessage(role="USER", content="Run the fixture."),),
        deadline_monotonic=10_000_000_000.0,
    )

    assert effects == []
    assert result.argument_observations[0][0].rejection_reason == "RESPONSE_TRUNCATED"


@pytest.mark.asyncio
async def test_reasoning_only_turn_is_observed_but_not_replayed_publicly():
    loop, provider, _effects = _tool_loop(
        (
            ModelTurn(
                finish_reason=FinishReason.OTHER,
                private_reasoning_present=True,
            ),
            ModelTurn(content="complete", finish_reason=FinishReason.STOP),
        )
    )

    result = await loop.run(
        messages=(PublicMessage(role="USER", content="Run the fixture."),),
        deadline_monotonic=10_000_000_000.0,
    )

    assert provider.requests[1].messages == provider.requests[0].messages
    assert result.observations[0].progress_kind == "REASONING_ONLY"
    assert result.observations[0].observable_progress is False
    assert "reasoning" not in repr(result.messages).lower()


@pytest.mark.asyncio
async def test_no_progress_terminates_with_bounded_facts():
    values = iter((0.0, 0.0, 2.0, 2.0, 4.0))
    loop, _provider, _effects = _tool_loop(
        (
            ModelTurn(finish_reason=FinishReason.OTHER, private_reasoning_present=True),
            ModelTurn(finish_reason=FinishReason.OTHER, private_reasoning_present=True),
        ),
        clock=lambda: next(values),
        no_progress_ms=3_000,
    )

    with pytest.raises(NoObservableProgressError) as raised:
        await loop.run(
            messages=(PublicMessage(role="USER", content="Run the fixture."),),
            deadline_monotonic=10.0,
        )

    assert raised.value.elapsed_ms == 4_000
    assert raised.value.turn_count == 2
    assert "private" not in str(raised.value).lower()


@pytest.mark.asyncio
async def test_interrupt_deadline_and_turn_budget_stop_before_provider_effect():
    turn = ModelTurn(content="unused", finish_reason=FinishReason.STOP)

    token = InterruptToken()
    token.request(InterruptReason.USER_REQUESTED, reason_code="USER_STOP")
    loop, provider, _effects = _tool_loop((turn,))
    loop.interrupt_token = token
    with pytest.raises(KernelInterruptedError):
        await loop.run(
            messages=(PublicMessage(role="USER", content="Run."),),
            deadline_monotonic=10_000_000_000.0,
        )
    assert provider.requests == ()

    loop, provider, _effects = _tool_loop((turn,), clock=lambda: 10.0)
    with pytest.raises(KernelDeadlineExceededError):
        await loop.run(
            messages=(PublicMessage(role="USER", content="Run."),),
            deadline_monotonic=9.0,
        )
    assert provider.requests == ()

    provider = FakeProvider(binding=_binding(), turns=(turn,))
    budget = BudgetLedger(_limits(provider_turns=0))
    invocation = _invocation(final=True)
    loop = AgentLoop(
        provider=provider,
        invocation=invocation,
        budget_ledger=budget,
    )
    with pytest.raises(BudgetExceededError) as raised:
        await loop.run(
            messages=(PublicMessage(role="USER", content="Run."),),
            deadline_monotonic=10_000_000_000.0,
            response_model=FinalEnvelope,
        )
    assert raised.value.dimension == "provider_turns"
    assert provider.requests == ()


@pytest.mark.asyncio
async def test_reported_token_budget_failure_keeps_the_provider_observation():
    provider = FakeProvider(
        binding=_binding(),
        turns=(
            ModelTurn(
                content="complete",
                finish_reason=FinishReason.STOP,
                usage=TokenUsage(input_tokens=6, output_tokens=5, total_tokens=11),
            ),
        ),
    )
    budget = BudgetLedger(_limits(tokens=10))
    loop = AgentLoop(
        provider=provider,
        invocation=_invocation(final=True),
        budget_ledger=budget,
    )

    with pytest.raises(BudgetExceededError) as raised:
        await loop.run(
            messages=(PublicMessage(role="USER", content="Finalize."),),
            deadline_monotonic=10_000_000_000.0,
            response_model=FinalEnvelope,
        )

    assert raised.value.dimension == "tokens"
    assert len(loop.observations) == 1
    assert loop.observations[0].usage.total_tokens == 11
    assert loop.events[-1].reason_code == "BUDGET_EXCEEDED"


@pytest.mark.asyncio
async def test_evidence_byte_budget_failure_retains_completed_tool_result():
    effects = []

    async def record(arguments: RecordArguments):
        effects.append(arguments.value)
        return {"recorded": arguments.value}

    registry = ToolRegistry()
    registry.register(
        name="record_value",
        description="Record one synthetic bounded value.",
        arguments_model=RecordArguments,
        handler=record,
    )
    provider = FakeProvider(
        binding=_binding(),
        turns=(
            ModelTurn(
                tool_calls=(
                    ToolCall(
                        call_id="call-evidence-budget",
                        name="record_value",
                        arguments_json='{"value":"recorded"}',
                    ),
                ),
                finish_reason=FinishReason.TOOL_CALLS,
            ),
        ),
    )
    limits = _limits().model_copy(update={"evidence_bytes": 1})
    budget = BudgetLedger(limits)
    loop = AgentLoop(
        provider=provider,
        invocation=_invocation(),
        budget_ledger=budget,
        dispatcher=ToolDispatcher(
            registry,
            ModelResultProjector(max_bytes=1_000),
        ),
    )

    with pytest.raises(BudgetExceededError) as raised:
        await loop.run(
            messages=(PublicMessage(role="USER", content="Run."),),
            deadline_monotonic=10_000_000_000.0,
        )

    assert raised.value.dimension == "evidence_bytes"
    assert effects == ["recorded"]
    assert len(loop.tool_results) == 1
    assert loop.tool_results[0].status == "SUCCEEDED"
    assert any(item.kind.value == "TOOL_CALL_FINISHED" for item in loop.events)


@pytest.mark.asyncio
async def test_incomplete_completion_and_finalization_tool_call_fail_closed():
    loop, _provider, _effects = _tool_loop(
        (ModelTurn(content="partial", finish_reason=FinishReason.LENGTH),)
    )
    with pytest.raises(IncompleteProviderResponseError):
        await loop.run(
            messages=(PublicMessage(role="USER", content="Run."),),
            deadline_monotonic=10_000_000_000.0,
        )

    provider = FakeProvider(
        binding=_binding(),
        turns=(
            ModelTurn(
                tool_calls=(ToolCall(call_id="illegal", name="x", arguments_json="{}"),),
                finish_reason=FinishReason.TOOL_CALLS,
            ),
        ),
    )
    loop = AgentLoop(
        provider=provider,
        invocation=_invocation(final=True),
        budget_ledger=BudgetLedger(_limits()),
    )
    with pytest.raises(FinalizationProtocolError):
        await loop.run(
            messages=(PublicMessage(role="USER", content="Finalize."),),
            deadline_monotonic=10_000_000_000.0,
            response_model=FinalEnvelope,
        )


@pytest.mark.asyncio
async def test_finalization_is_tool_free_and_performs_one_format_correction():
    provider = FakeProvider(
        binding=_binding(),
        turns=(
            ModelTurn(
                content='{"result":{"count":"wrong"}}',
                finish_reason=FinishReason.STOP,
            ),
            ModelTurn(
                content='{"result":{"count":7}}',
                finish_reason=FinishReason.STOP,
            ),
        ),
    )
    budget = BudgetLedger(_limits(finalization_attempts=2))
    loop = AgentLoop(
        provider=provider,
        invocation=_invocation(final=True),
        budget_ledger=budget,
    )

    result = await loop.run(
        messages=(PublicMessage(role="USER", content="Finalize."),),
        deadline_monotonic=10_000_000_000.0,
        response_model=FinalEnvelope,
    )

    assert result.output == {"result": {"count": 7}}
    assert result.termination == "STRUCTURED_OUTPUT_ACCEPTED"
    assert all(request.tools == () for request in provider.requests)
    assert provider.requests[0].response_schema is not None
    assert provider.requests[1].messages[-1].role == "USER"
    assert "schema" in provider.requests[1].messages[-1].content.lower()
    assert "wrong" not in repr(provider.requests[1].messages)
    assert result.budget_usage.finalization_attempts == 2


@pytest.mark.asyncio
async def test_length_truncated_finalization_gets_one_bounded_format_correction():
    provider = FakeProvider(
        binding=_binding(),
        turns=(
            ModelTurn(
                content='{"result":{"count":',
                finish_reason=FinishReason.LENGTH,
            ),
            ModelTurn(
                content='{"result":{"count":7}}',
                finish_reason=FinishReason.STOP,
            ),
        ),
    )
    budget = BudgetLedger(_limits(finalization_attempts=2))
    loop = AgentLoop(
        provider=provider,
        invocation=_invocation(final=True),
        budget_ledger=budget,
    )

    result = await loop.run(
        messages=(PublicMessage(role="USER", content="Finalize."),),
        deadline_monotonic=10_000_000_000.0,
        response_model=FinalEnvelope,
    )

    assert result.output == {"result": {"count": 7}}
    assert len(provider.requests) == 2
    correction = provider.requests[1].messages[-1].content
    assert "short" in correction.lower()
    assert "count\":" not in repr(provider.requests[1].messages)
    assert result.budget_usage.finalization_attempts == 2


@pytest.mark.asyncio
async def test_length_truncated_finalization_fails_when_retry_budget_is_exhausted():
    provider = FakeProvider(
        binding=_binding(),
        turns=(ModelTurn(
            content='{"result":{"count":',
            finish_reason=FinishReason.LENGTH,
        ),),
    )
    loop = AgentLoop(
        provider=provider,
        invocation=_invocation(final=True),
        budget_ledger=BudgetLedger(_limits(finalization_attempts=1)),
    )

    with pytest.raises(FinalizationRejectedError):
        await loop.run(
            messages=(PublicMessage(role="USER", content="Finalize."),),
            deadline_monotonic=10_000_000_000.0,
            response_model=FinalEnvelope,
        )
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_finalization_rejects_duplicate_json_without_repair():
    provider = FakeProvider(
        binding=_binding(),
        turns=(
            ModelTurn(
                content='{"result":{"count":1},"result":{"count":2}}',
                finish_reason=FinishReason.STOP,
            ),
        ),
    )
    loop = AgentLoop(
        provider=provider,
        invocation=_invocation(final=True),
        budget_ledger=BudgetLedger(_limits(finalization_attempts=1)),
    )

    with pytest.raises(FinalizationRejectedError) as raised:
        await loop.run(
            messages=(PublicMessage(role="USER", content="Finalize."),),
            deadline_monotonic=10_000_000_000.0,
            response_model=FinalEnvelope,
        )
    assert raised.value.validation_type == "json_invalid"
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_exhausted_provider_closes_loop_with_typed_failure_event():
    loop, _provider, effects = _tool_loop(())

    with pytest.raises(FakeProviderExhaustedError):
        await loop.run(
            messages=(PublicMessage(role="USER", content="Run."),),
            deadline_monotonic=10_000_000_000.0,
        )

    assert effects == []
    assert loop.events[-1].kind.value == "KERNEL_FINISHED"
    assert loop.events[-1].status_code == "FAILED"
    assert loop.events[-1].reason_code == "FAKE_PROVIDER_EXHAUSTED"
