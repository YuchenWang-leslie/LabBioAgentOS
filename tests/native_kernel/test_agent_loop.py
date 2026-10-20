"""P3 acceptance for the native single-Agent provider loop."""

from __future__ import annotations

import asyncio
import json
from time import monotonic
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
    KernelContractError,
    KernelInterruptedError,
    ModelResultProjector,
    ModelTurn,
    NoObservableProgressError,
    ProviderBinding,
    ProviderTransportError,
    ProviderProtocolError,
    PublicMessage,
    TokenUsage,
    ToolCall,
    ToolDispatcher,
    ToolRegistry,
    ToolResult,
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
async def test_capability_tool_feedback_exposes_remaining_shared_budget():
    loop, provider, effects = _tool_loop((
        ModelTurn(
            tool_calls=(ToolCall(
                call_id="accepted",
                name="record_value",
                arguments_json='{"value":"kept"}',
            ),),
            finish_reason=FinishReason.TOOL_CALLS,
        ),
        ModelTurn(content="done", finish_reason=FinishReason.STOP),
    ), tool_calls=1)

    result = await loop.run(
        messages=(PublicMessage(role="USER", content="Run within the bound."),),
        deadline_monotonic=10_000_000_000.0,
    )

    assert result.output == "done"
    assert effects == [{"value": "kept", "nested": None}]
    assert provider.requests[1].messages[-1].role == "TOOL"
    assert json.loads(provider.requests[1].messages[-1].content)["control_state"] == {
        "authority": "CONTROL_STATE",
        "kernel_budget": {
            "tool_calls_remaining": 0,
            "evidence_entries_remaining": 7,
            "provider_turns_remaining": 7,
            "tokens_remaining": None,
            "wall_time_ms_remaining": 60_000 - result.observations[0].elapsed_ms,
        },
    }


@pytest.mark.asyncio
async def test_rejected_tool_batch_exposes_consumed_evidence_budget():
    loop, provider, effects = _tool_loop((
        ModelTurn(
            tool_calls=(ToolCall(
                call_id="rejected",
                name="record_value",
                arguments_json='{"value":42}',
            ),),
            finish_reason=FinishReason.TOOL_CALLS,
        ),
        ModelTurn(content="done", finish_reason=FinishReason.STOP),
    ))

    result = await loop.run(
        messages=(PublicMessage(role="USER", content="Run within the bound."),),
        deadline_monotonic=10_000_000_000.0,
    )

    assert result.output == "done"
    assert effects == []
    assert provider.requests[1].messages[-1].role == "TOOL"
    assert json.loads(provider.requests[1].messages[-1].content)["control_state"] == {
        "authority": "CONTROL_STATE",
        "kernel_budget": {
            "tool_calls_remaining": 8,
            "evidence_entries_remaining": 7,
            "provider_turns_remaining": 7,
            "tokens_remaining": None,
            "wall_time_ms_remaining": 60_000 - result.observations[0].elapsed_ms,
        },
    }


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
    assert result.budget_usage.tokens == 11
    assert result.events[-1].status_code == "STOPPED"
    assert result.events[-1].reason_code == "PROVIDER_TOKEN_LIMIT"
    turns = [event for event in result.events if event.kind == "MODEL_TURN_OBSERVED"]
    assert [event.numeric_facts["total_tokens"] for event in turns] == [4, 7]
    assert all(event.numeric_facts["input_tokens"] is None for event in turns)
    assert all(event.numeric_facts["output_tokens"] is None for event in turns)


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
@pytest.mark.parametrize("failure,stop_reason", [
    (ProviderTransportError, "PROVIDER_TRANSPORT_STOP"),
    (ProviderProtocolError, "PROVIDER_PROTOCOL_STOP"),
])
async def test_capability_loop_preserves_prior_evidence_on_later_provider_failure(
    failure, stop_reason,
):
    class LaterProviderFailure:
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
            raise failure()

    provider = LaterProviderFailure()
    loop, _fixture_provider, effects = _tool_loop(())
    loop.provider = provider

    result = await loop.run(
        messages=(PublicMessage(role="USER", content="Preserve completed evidence."),),
        deadline_monotonic=10_000_000_000.0,
    )

    assert result.termination == stop_reason
    assert result.output == {
        "capability_stop_reason": stop_reason
    }
    assert effects == [{"value": "kept", "nested": None}]
    assert [item.call_id for item in result.tool_results] == [
        "persisted-before-transport-failure"
    ]
    assert result.events[-1].status_code == "STOPPED"
    assert result.events[-1].reason_code == failure().code
    assert result.budget_usage.tokens is None


@pytest.mark.asyncio
async def test_capability_loop_without_evidence_rejects_protocol_failure():
    class ImmediateProtocolFailure:
        binding = _binding()

        async def generate(self, _request):
            raise ProviderProtocolError(validation_type="too_long", validation_fields=("model_turn", "content"))

    loop, _fixture_provider, _effects = _tool_loop(())
    loop.provider = ImmediateProtocolFailure()

    with pytest.raises(ProviderProtocolError):
        await loop.run(
            messages=(PublicMessage(role="USER", content="No evidence yet."),),
            deadline_monotonic=10_000_000_000.0,
        )


@pytest.mark.asyncio
async def test_provider_call_obeys_absolute_model_deadline():
    class HangingProvider:
        binding = _binding()
        cancelled = False

        async def generate(self, _request):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    loop, _fixture_provider, _effects = _tool_loop(())
    provider = HangingProvider()
    loop.provider = provider

    with pytest.raises(KernelDeadlineExceededError):
        await asyncio.wait_for(loop.run(
            messages=(PublicMessage(role="USER", content="Wait for the provider."),),
            deadline_monotonic=monotonic() + 0.02,
        ), timeout=0.2)

    assert provider.cancelled


@pytest.mark.asyncio
async def test_provider_deadline_after_tool_preserves_completed_evidence():
    class ToolThenHang:
        binding = _binding()
        calls = 0

        async def generate(self, _request):
            self.calls += 1
            if self.calls == 1:
                return ModelTurn(tool_calls=(ToolCall(
                    call_id="before-deadline", name="record_value",
                    arguments_json='{"value":"kept"}',
                ),), finish_reason=FinishReason.TOOL_CALLS)
            await asyncio.Event().wait()

    loop, _fixture_provider, effects = _tool_loop(())
    loop.provider = ToolThenHang()

    result = await asyncio.wait_for(loop.run(
        messages=(PublicMessage(role="USER", content="Preserve the completed tool."),),
        deadline_monotonic=monotonic() + 0.05,
    ), timeout=0.2)

    assert result.termination == "PROVIDER_DEADLINE_STOP"
    assert result.events[-1].reason_code == "KERNEL_DEADLINE_EXCEEDED"
    assert effects == [{"value": "kept", "nested": None}]
    assert [item.call_id for item in result.tool_results] == ["before-deadline"]


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
    assert "int_type" in provider.requests[1].messages[-1].content
    assert "result.count" in provider.requests[1].messages[-1].content
    assert "wrong" not in repr(provider.requests[1].messages)
    assert result.budget_usage.finalization_attempts == 2


@pytest.mark.asyncio
async def test_interrupt_between_batch_tools_preserves_completed_and_not_started():
    token = InterruptToken()
    loop, _provider, effects = _tool_loop(
        (ModelTurn(
            tool_calls=tuple(ToolCall(
                call_id=f"call-{number}", name="record_value",
                arguments_json=json.dumps({"value": str(number)}),
            ) for number in (1, 2)),
            finish_reason=FinishReason.TOOL_CALLS,
        ),),
        on_record=lambda: token.request(
            InterruptReason.USER_REQUESTED, reason_code="TEST_STOP"
        ),
    )
    loop.interrupt_token = token
    with pytest.raises(KernelInterruptedError):
        await loop.run(
            messages=(PublicMessage(role="USER", content="Stop inside batch."),),
            deadline_monotonic=10_000_000_000.0,
        )
    assert effects == [{"value": "1", "nested": None}]
    assert loop.tool_results[0].status == "SUCCEEDED"
    assert loop.tool_results[1].error.code == "TOOL_NOT_STARTED_INTERRUPTED"
    starts = [event for event in loop.events if event.kind == "TOOL_CALL_STARTED"]
    assert [event.references for event in starts] == [("call-1",)]


@pytest.mark.asyncio
async def test_interrupt_after_provider_return_prevents_first_tool():
    token = InterruptToken()
    loop, provider, effects = _tool_loop((ModelTurn(
        tool_calls=(ToolCall(
            call_id="not-started", name="record_value",
            arguments_json='{"value":"must-not-run"}',
        ),),
        finish_reason=FinishReason.TOOL_CALLS,
        usage=TokenUsage(total_tokens=4),
    ),))
    original = provider.generate

    async def interrupted_generate(request):
        turn = await original(request)
        token.request(InterruptReason.USER_REQUESTED, reason_code="TEST_STOP")
        return turn

    provider.generate = interrupted_generate
    loop.interrupt_token = token
    with pytest.raises(KernelInterruptedError):
        await loop.run(
            messages=(PublicMessage(role="USER", content="Stop before dispatch."),),
            deadline_monotonic=10_000_000_000.0,
        )
    assert effects == []
    assert loop.budget_ledger.usage.tokens == 4
    assert not [event for event in loop.events if event.kind == "TOOL_CALL_STARTED"]


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments,tool_budget,observed_tokens", [
    ('{"value":"not-started"}', 8, 4),
    ('{"value":', 8, 4),
    ('{"value":7}', 8, 4),
    ('{"value":"not-started"}', 0, 4),
    ('{"value":"not-started"}', 8, 11),
])
async def test_interrupt_after_provider_return_audits_raw_calls_without_validation(
    monkeypatch, arguments, tool_budget, observed_tokens,
):
    token = InterruptToken()
    calls = tuple(ToolCall(
        call_id=f"unstarted-{number}", name="record_value", arguments_json=arguments,
    ) for number in range(2))
    loop, provider, effects = _tool_loop((ModelTurn(
        tool_calls=calls, finish_reason=FinishReason.TOOL_CALLS,
        usage=TokenUsage(total_tokens=observed_tokens),
    ),), tool_calls=tool_budget, tokens=10)
    original_generate = provider.generate
    original_validate = loop.dispatcher.registry.validate_batch
    validations = []

    async def interrupted_generate(request):
        turn = await original_generate(request)
        token.request(InterruptReason.USER_REQUESTED, reason_code="TEST_STOP")
        return turn

    def validate(*args, **kwargs):
        validations.append(True)
        return original_validate(*args, **kwargs)

    monkeypatch.setattr(provider, "generate", interrupted_generate)
    monkeypatch.setattr(loop.dispatcher.registry, "validate_batch", validate)
    loop.interrupt_token = token
    with pytest.raises(KernelInterruptedError):
        await loop.run(
            messages=(PublicMessage(role="USER", content="Stop before dispatch."),),
            deadline_monotonic=10_000_000_000.0,
        )
    assert effects == []
    assert validations == []
    assert loop.budget_ledger.usage.tokens == observed_tokens
    assert loop.budget_ledger.usage.tool_calls == 0
    assert loop.budget_ledger.usage.evidence_entries == 0
    assert len(provider.requests) == 1
    assert [result.call_id for result in loop.tool_results] == [call.call_id for call in calls]
    assert all(result.error.code == "TOOL_NOT_STARTED_INTERRUPTED" for result in loop.tool_results)
    assert not [event for event in loop.events if event.kind == "TOOL_CALL_STARTED"]
    finished = [event for event in loop.events if event.kind == "TOOL_CALL_FINISHED"]
    assert len(finished) == 2
    assert all(event.reason_code == "TOOL_NOT_STARTED_INTERRUPTED" for event in finished)
    assert any(event.kind == "INTERRUPT_OBSERVED" for event in loop.events)


@pytest.mark.asyncio
async def test_finalization_feedback_does_not_echo_unrecognized_keys_or_values():
    provider = FakeProvider(binding=_binding(), turns=(
        ModelTurn(
            content='{"result":{"count":1},"private-secret-key":"private-secret-value"}',
            finish_reason=FinishReason.STOP,
        ),
        ModelTurn(content='{"result":{"count":1}}', finish_reason=FinishReason.STOP),
    ))
    loop = AgentLoop(
        provider=provider, invocation=_invocation(final=True),
        budget_ledger=BudgetLedger(_limits()),
    )
    await loop.run(
        messages=(PublicMessage(role="USER", content="Finalize."),),
        deadline_monotonic=10_000_000_000.0, response_model=FinalEnvelope,
    )
    feedback = provider.requests[1].messages[-1].content
    assert "extra_forbidden" in feedback
    assert "unknown_field" in feedback
    assert "private-secret" not in feedback


@pytest.mark.asyncio
async def test_aggregate_context_limit_blocks_66_large_messages_before_provider():
    loop, provider, _effects = _tool_loop((
        ModelTurn(content="Must not run.", finish_reason=FinishReason.STOP),
    ))
    with pytest.raises(KernelContractError, match="capacity") as raised:
        await loop.run(
            messages=tuple(PublicMessage(role="USER", content="x" * 50_000)
                           for _ in range(66)),
            deadline_monotonic=10_000_000_000.0,
        )
    assert raised.value.code == "CONTEXT_CAPACITY_EXCEEDED"
    assert provider.requests == ()
    assert loop.budget_ledger.usage.provider_turns == 0


def _observed_batch(number, *, failed=False, has_reference=True, archived=True):
    observation_id = str(uuid4())
    call = ToolCall(
        call_id=f"history-{number}", name="record_value",
        arguments_json='{"value":"requested"}',
    )
    payload = {
        "tool_invocation_id": str(uuid4()), "call_id": call.call_id,
        "name": call.name, "status": "SUCCEEDED",
        "content": {
            "success": not failed,
            "observation_archived": archived,
            "data": "x" * 48_000,
            **({"capability_invocation_id": observation_id} if has_reference else {}),
            **({"error": {"error_code": "KNOWN_FAILURE"}} if failed else {}),
        },
        "evidence_references": ["artifact:kept"],
    }
    return observation_id, (
        PublicMessage(role="ASSISTANT", content="a" * 48_000, tool_calls=(call,)),
        AgentLoop._tool_feedback(ToolResult.model_validate_json(json.dumps(payload)), {
            "authority": "CONTROL_STATE",
            "kernel_budget": {"tool_calls_remaining": 8, "evidence_entries_remaining": 8},
        }),
    )


def _enable_evidence_reader(loop):
    loop.dispatcher.registry.register(
        name="evidence_read", description="Reread a persisted observation.",
        arguments_model=RecordArguments, handler=lambda _args: {},
    )
    loop.invocation = loop.invocation.model_copy(update={
        "tool_names": (*loop.invocation.tool_names, "evidence_read"),
    })


@pytest.mark.asyncio
async def test_context_replaces_only_old_completed_batches_with_reread_references():
    roots = (
        PublicMessage(role="SYSTEM", content="Trusted system."),
        PublicMessage(role="USER", content="Original task."),
    )
    observations = [_observed_batch(number) for number in range(32)]
    messages = roots + tuple(message for _reference, batch in observations
                             for message in batch)
    loop, provider, _effects = _tool_loop((
        ModelTurn(content="Done.", finish_reason=FinishReason.STOP),
    ))
    _enable_evidence_reader(loop)
    discarded = []
    provider.discard_tool_batch = lambda run_id, invocation_id, calls, **_kwargs: discarded.append(
        (run_id, invocation_id, calls)
    )
    await loop.run(messages=messages, deadline_monotonic=10_000_000_000.0)
    request = provider.requests[0]
    assert len(request.model_dump_json().encode("utf-8")) <= 256_000
    assert request.messages[:2] == roots
    assert request.messages[-2:] == observations[-1][1]
    marker = json.loads(request.messages[2].content)
    assert marker["authority"] == "CONTROL_STATE"
    assert marker["context_replacement"] == "COMPLETED_TOOL_BATCH"
    assert marker["observations"][0]["observation_id"] == observations[0][0]
    assert marker["observations"][0]["evidence_references"] == ["artifact:kept"]
    assert marker["reread_tool"] == "evidence_read"
    assert "x" * 100 not in request.messages[2].content
    event = next(item for item in loop.events if item.kind == "MODEL_REQUEST_PREPARED")
    assert event.numeric_facts["public_request_bytes"] <= 256_000
    assert event.numeric_facts["compacted_batches"] > 0
    assert len(discarded) == event.numeric_facts["compacted_batches"]
    assert discarded[0][2] == observations[0][1][0].tool_calls
    assert all(item[2] != observations[-1][1][0].tool_calls for item in discarded)


@pytest.mark.asyncio
@pytest.mark.parametrize("remaining_tokens,compacted", [(19, True), (899, False)])
async def test_observed_token_budget_risk_compacts_rereadable_context_below_byte_limit(
    remaining_tokens, compacted,
):
    first_id, first = _observed_batch(1)
    _last_id, latest = _observed_batch(2)
    loop, provider, _effects = _tool_loop((
        ModelTurn(content="Done.", finish_reason=FinishReason.STOP,
                  usage=TokenUsage(total_tokens=1)),
    ), tokens=101 + remaining_tokens)
    _enable_evidence_reader(loop)
    observed = ModelTurn(content="Prior response.", finish_reason=FinishReason.STOP,
                         usage=TokenUsage(input_tokens=100, output_tokens=1, total_tokens=101))
    loop.observations.append(loop._observe(observed, 1, 0))
    loop.budget_ledger.record_observed_usage(tokens=101, wall_time_ms=0)

    await loop.run(
        messages=(PublicMessage(role="USER", content="Keep the archived evidence available."),
                  *first, *latest),
        deadline_monotonic=10_000_000_000.0,
    )

    request = provider.requests[0]
    assert len(request.model_dump_json().encode("utf-8")) < 256_000
    event = next(item for item in loop.events if item.kind == "MODEL_REQUEST_PREPARED")
    assert event.numeric_facts["compacted_batches"] == int(compacted)
    assert request.messages[-2:] == latest
    if compacted:
        marker = json.loads(request.messages[1].content)
        assert marker["observations"][0]["observation_id"] == first_id
        assert marker["reread_tool"] == "evidence_read"
    else:
        assert request.messages[1:3] == first


@pytest.mark.asyncio
async def test_context_compaction_handles_dispatcher_generated_feedback_end_to_end():
    effects = []
    registry = ToolRegistry()

    def archived(arguments):
        effects.append(arguments.value)
        return {"success": True, "observation_archived": True,
                "capability_invocation_id": str(uuid4()), "data": "x" * 12_000}

    registry.register(name="archive", description="Synthetic archival host.",
                      arguments_model=RecordArguments, handler=archived)
    registry.register(name="evidence_read", description="Read an archived observation.",
                      arguments_model=RecordArguments, handler=lambda _args: {})
    turns = tuple(ModelTurn(
        tool_calls=(ToolCall(call_id=f"archive-{number}", name="archive",
            arguments_json=json.dumps({"value": str(number)})),),
        finish_reason=FinishReason.TOOL_CALLS,
    ) for number in range(4)) + (ModelTurn(content="Done.", finish_reason=FinishReason.STOP),)
    provider = FakeProvider(binding=_binding(), turns=turns)
    budget = BudgetLedger(_limits())
    loop = AgentLoop(
        provider=provider, invocation=_invocation().model_copy(update={
            "tool_names": ("archive", "evidence_read")}),
        budget_ledger=budget, max_request_bytes=30_000,
        dispatcher=ToolDispatcher(registry, ModelResultProjector(max_bytes=16_000),
                                  budget_ledger=budget),
    )
    result = await loop.run(
        messages=(PublicMessage(role="USER", content="Exercise actual feedback."),),
        deadline_monotonic=10_000_000_000.0,
    )
    assert result.termination == "MODEL_COMPLETED"
    assert effects == ["0", "1", "2", "3"]
    assert len(provider.requests) == 5
    actual_feedback = next(message for message in provider.requests[1].messages if message.role == "TOOL")
    assert json.loads(actual_feedback.content)["control_state"]["authority"] == "CONTROL_STATE"
    assert any(message.role == "USER" and "COMPLETED_TOOL_BATCH" in message.content
               for message in provider.requests[-1].messages)
    assert all(len(request.model_dump_json().encode("utf-8")) <= 30_000 for request in provider.requests)


@pytest.mark.asyncio
async def test_context_does_not_advertise_unavailable_evidence_reader():
    batches = [_observed_batch(number)[1] for number in range(4)]
    loop, provider, _effects = _tool_loop((
        ModelTurn(content="Must not run.", finish_reason=FinishReason.STOP),
    ))
    with pytest.raises(KernelContractError, match="capacity"):
        await loop.run(
            messages=(PublicMessage(role="USER", content="Keep readable evidence."),
                      *(message for batch in batches for message in batch)),
            deadline_monotonic=10_000_000_000.0,
        )
    assert provider.requests == ()


@pytest.mark.asyncio
async def test_context_replacement_keeps_multi_call_batches_atomic():
    first_id, first = _observed_batch(1)
    second_id, second = _observed_batch(2)
    _last_id, latest = _observed_batch(3)
    joint_assistant = first[0].model_copy(update={
        "tool_calls": (*first[0].tool_calls, *second[0].tool_calls),
    })
    loop, provider, _effects = _tool_loop((
        ModelTurn(content="Done.", finish_reason=FinishReason.STOP),
    ))
    _enable_evidence_reader(loop)
    loop.max_request_bytes = 150_000
    await loop.run(
        messages=(PublicMessage(role="USER", content="Preserve call pairing."),
                  joint_assistant, first[1], second[1], *latest),
        deadline_monotonic=10_000_000_000.0,
    )
    sent = provider.requests[0].messages
    assert len(sent) == 4
    assert sent[-2:] == latest
    marker = json.loads(sent[1].content)
    assert [item["observation_id"] for item in marker["observations"]] == [
        first_id, second_id,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("preserve", ("error", "no_reference", "not_archived"))
async def test_context_capacity_never_drops_unreadable_or_error_batches(preserve):
    _reference, batch = _observed_batch(
        1, failed=preserve == "error", has_reference=preserve != "no_reference",
        archived=preserve != "not_archived",
    )
    _new_reference, latest = _observed_batch(2)
    loop, provider, _effects = _tool_loop((
        ModelTurn(content="Must not run.", finish_reason=FinishReason.STOP),
    ))
    loop.max_request_bytes = 110_000
    _enable_evidence_reader(loop)
    with pytest.raises(KernelContractError, match="capacity"):
        await loop.run(
            messages=(PublicMessage(role="USER", content="Keep errors."), *batch, *latest),
            deadline_monotonic=10_000_000_000.0,
        )
    assert provider.requests == ()
    assert loop.messages[1:3] == batch


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ("extra_control", "wrong_authority", "bad_counter", "extra_result"))
async def test_context_does_not_accept_arbitrary_extra_feedback_fields(corruption):
    _, batch = _observed_batch(1)
    _, latest = _observed_batch(2)
    payload = json.loads(batch[1].content)
    if corruption == "extra_control":
        payload["control_state"]["untrusted_extra"] = True
    elif corruption == "wrong_authority":
        payload["control_state"]["authority"] = "AGENT_PROSE"
    elif corruption == "bad_counter":
        payload["control_state"]["kernel_budget"]["tool_calls_remaining"] = "8"
    else:
        payload["untrusted_extra"] = True
    altered = batch[1].model_copy(update={"content": json.dumps(payload)})
    loop, provider, _ = _tool_loop((ModelTurn(content="Must not run.", finish_reason=FinishReason.STOP),))
    loop.max_request_bytes = 110_000
    _enable_evidence_reader(loop)
    with pytest.raises(KernelContractError, match="capacity"):
        await loop.run(
            messages=(PublicMessage(role="USER", content="Keep intact."), batch[0], altered, *latest),
            deadline_monotonic=10_000_000_000.0,
        )
    assert provider.requests == ()
    assert loop.messages[2] == altered


@pytest.mark.asyncio
async def test_context_capacity_accounts_for_output_schema_before_provider():
    provider = FakeProvider(binding=_binding(), turns=(
        ModelTurn(content='{"result":{"count":1}}', finish_reason=FinishReason.STOP),
    ))
    loop = AgentLoop(
        provider=provider, invocation=_invocation(final=True),
        budget_ledger=BudgetLedger(_limits()),
    )
    loop.max_request_bytes = 350
    with pytest.raises(KernelContractError, match="capacity"):
        await loop.run(
            messages=(PublicMessage(role="USER", content="Finalize."),),
            deadline_monotonic=10_000_000_000.0, response_model=FinalEnvelope,
        )
    assert provider.requests == ()


@pytest.mark.asyncio
async def test_context_capacity_includes_registered_tool_schema():
    loop, provider, _effects = _tool_loop((
        ModelTurn(content="Must not run.", finish_reason=FinishReason.STOP),
    ))
    class LargeArguments(BaseModel):
        model_config = ConfigDict(
            extra="forbid", strict=True,
            json_schema_extra={"description": "A" * 260_000},
        )

    loop.dispatcher.registry.register(
        name="large_schema", description="Large declared interface.",
        arguments_model=LargeArguments, handler=lambda _args: {},
    )
    loop.invocation = loop.invocation.model_copy(update={
        "tool_names": (*loop.invocation.tool_names, "large_schema"),
    })
    with pytest.raises(KernelContractError, match="capacity"):
        await loop.run(
            messages=(PublicMessage(role="USER", content="Small message."),),
            deadline_monotonic=10_000_000_000.0,
        )
    assert provider.requests == ()


@pytest.mark.asyncio
async def test_context_capacity_checks_provider_wire_size_without_private_contents():
    loop, provider, _effects = _tool_loop((
        ModelTurn(content="Must not run.", finish_reason=FinishReason.STOP),
    ))
    provider.request_size_bytes = lambda _request: 300_000
    with pytest.raises(KernelContractError, match="capacity"):
        await loop.run(
            messages=(PublicMessage(role="USER", content="Small public request."),),
            deadline_monotonic=10_000_000_000.0,
        )
    event = next(item for item in loop.events if item.kind == "MODEL_REQUEST_PREPARED")
    assert event.numeric_facts["request_bytes"] == 300_000
    assert event.numeric_facts["public_request_bytes"] < 256_000
    assert provider.requests == ()


@pytest.mark.asyncio
async def test_feedback_observer_receives_only_final_projected_results_after_delivery():
    loop, provider, _effects = _tool_loop((
        ModelTurn(tool_calls=(
            ToolCall(call_id="large", name="record_value",
                     arguments_json=json.dumps({"value": "x" * 500})),
            ToolCall(call_id="small", name="record_value",
                     arguments_json='{"value":"ok"}'),
        ), finish_reason=FinishReason.TOOL_CALLS),
        ModelTurn(content="Observed final feedback.", finish_reason=FinishReason.STOP),
    ))
    loop.dispatcher.projector = ModelResultProjector(max_bytes=100)
    delivered = []
    loop.dispatcher.batch_observer = lambda results=(): delivered.append(results)
    original = provider.generate

    async def verify_delivery_boundary(request):
        assert delivered == []
        return await original(request)

    provider.generate = verify_delivery_boundary
    await loop.run(
        messages=(PublicMessage(role="USER", content="Run both."),),
        deadline_monotonic=10_000_000_000.0,
    )
    assert len(delivered) == 1
    assert delivered[0][0].error.code == "MODEL_RESULT_TOO_LARGE"
    assert delivered[0][0].content is None
    assert delivered[0][1].content == {"recorded": "ok", "nested": None}


@pytest.mark.asyncio
@pytest.mark.parametrize("interrupted", (False, True))
async def test_unreceived_feedback_does_not_grant_delivery_credit(interrupted):
    token = InterruptToken()
    loop, provider, _effects = _tool_loop(
        (ModelTurn(tool_calls=(ToolCall(
            call_id="pending", name="record_value", arguments_json='{"value":"pending"}',
        ),), finish_reason=FinishReason.TOOL_CALLS),),
        on_record=(
            lambda: token.request(InterruptReason.USER_REQUESTED, reason_code="TEST_STOP")
        ) if interrupted else None,
    )
    loop.interrupt_token = token
    delivered = []
    loop.dispatcher.batch_observer = lambda results=(): delivered.append(results)
    original = provider.generate

    async def fail_after_first_turn(request):
        if request.turn_index == 2:
            raise ProviderTransportError()
        return await original(request)

    provider.generate = fail_after_first_turn
    if interrupted:
        with pytest.raises(KernelInterruptedError):
            await loop.run(
                messages=(PublicMessage(role="USER", content="Run then stop."),),
                deadline_monotonic=10_000_000_000.0,
            )
    else:
        result = await loop.run(
            messages=(PublicMessage(role="USER", content="Run then stop."),),
            deadline_monotonic=10_000_000_000.0,
        )
        assert result.termination == "PROVIDER_TRANSPORT_STOP"
    assert delivered == []


@pytest.mark.asyncio
async def test_feedback_delivery_includes_host_failure_exactly_once():
    loop, _provider, _effects = _tool_loop((
        ModelTurn(tool_calls=(ToolCall(
            call_id="host-failure", name="failed_read", arguments_json='{"value":"test"}',
        ),), finish_reason=FinishReason.TOOL_CALLS),
        ModelTurn(finish_reason=FinishReason.OTHER, private_reasoning_present=True),
        ModelTurn(content="Failure observed.", finish_reason=FinishReason.STOP),
    ))
    public_failure = {
        "success": False, "capability_invocation_id": str(uuid4()),
        "error": {"error_code": "FIXTURE_DENIED"},
    }
    loop.dispatcher.registry.register(
        name="failed_read", description="Return a bounded host failure.",
        arguments_model=RecordArguments, handler=lambda _args: public_failure,
    )
    loop.invocation = loop.invocation.model_copy(update={
        "tool_names": (*loop.invocation.tool_names, "failed_read"),
    })
    delivered = []
    loop.dispatcher.batch_observer = lambda results: delivered.append(results)
    await loop.run(
        messages=(PublicMessage(role="USER", content="Observe failure."),),
        deadline_monotonic=10_000_000_000.0,
    )
    assert len(delivered) == 1
    assert delivered[0][0].status == "SUCCEEDED"
    assert delivered[0][0].content == public_failure


@pytest.mark.asyncio
async def test_context_rejected_feedback_is_not_credited_as_delivered():
    loop, provider, _effects = _tool_loop((ModelTurn(
        tool_calls=(ToolCall(
            call_id="too-big-for-next-request", name="record_value",
            arguments_json=json.dumps({"value": "x" * 5_000}),
        ),), finish_reason=FinishReason.TOOL_CALLS,
    ),))
    loop.max_request_bytes = 3_000
    delivered = []
    loop.dispatcher.batch_observer = lambda results: delivered.append(results)
    with pytest.raises(KernelContractError, match="capacity"):
        await loop.run(
            messages=(PublicMessage(role="USER", content="Check delivery."),),
            deadline_monotonic=10_000_000_000.0,
        )
    assert len(provider.requests) == 1
    assert delivered == []


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
