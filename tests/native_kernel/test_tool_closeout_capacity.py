"""Completion control must remain reachable inside the unchanged total budget."""

from time import monotonic

import pytest
from pydantic import BaseModel, ConfigDict

from labbioagentos.kernel import (
    AgentInvocation, AgentLoop, AgentMode, BudgetDelta, BudgetLedger, BudgetLimits,
    FakeProvider, FinishReason, ModelResultProjector, ModelTurn, PublicMessage,
    ToolBatchRejectedError, ToolCall, ToolDispatcher, ToolRegistry,
)
from native_kernel.test_agent_loop import _binding, _identity


class Empty(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


def fixture(*, consumed=6, closing=True, child=False):
    effects = []
    registry = ToolRegistry()
    for name in (('work',) if child else ('work', 'status', 'finish')):
        registry.register(name=name, description='Synthetic control test.', arguments_model=Empty,
            handler=lambda _, name=name: effects.append(name) or {'action':name})
    ledger = BudgetLedger(BudgetLimits(provider_turns=8, tokens=100_000, wall_time_ms=60_000,
        tool_calls=8, evidence_entries=8, evidence_bytes=100_000, delegations=0,
        delegation_depth=0, finalization_attempts=0))
    ledger.reserve(BudgetDelta(tool_calls=consumed, evidence_entries=consumed))
    kwargs = {'closeout_tools':('status','finish'), 'closeout_reserve':2} if closing else {}
    return ToolDispatcher(registry, ModelResultProjector(max_bytes=8_000), budget_ledger=ledger,
        **kwargs), ledger, effects


def call(name, identity='one'):
    return ToolCall(call_id=identity, name=name, arguments_json='{}')


@pytest.mark.asyncio
async def test_only_closeout_is_exposed_and_work_admission_spends_nothing():
    dispatcher, ledger, effects = fixture()
    before = ledger.usage
    assert {x.name for x in dispatcher.available_specs} == {'status','finish'}
    with pytest.raises(ToolBatchRejectedError) as caught:
        await dispatcher.dispatch_batch((call('work'),), finish_reason=FinishReason.TOOL_CALLS)
    assert caught.value.code == 'CLOSEOUT_REQUIRED'
    assert ledger.usage == before and effects == []
    await dispatcher.dispatch_batch((call('status'),call('finish','two')),
        finish_reason=FinishReason.TOOL_CALLS)
    assert effects == ['status','finish'] and ledger.usage.tool_calls == 8


@pytest.mark.asyncio
async def test_mixed_independent_batch_respects_work_and_total_capacity():
    dispatcher, ledger, effects = fixture(consumed=5)
    await dispatcher.dispatch_batch((call('work'),call('finish','two')),
        finish_reason=FinishReason.TOOL_CALLS)
    assert effects == ['work','finish'] and ledger.usage.tool_calls == 7


@pytest.mark.asyncio
async def test_crossing_work_reserve_rejects_the_entire_batch():
    dispatcher, ledger, effects = fixture(consumed=5)
    before = ledger.usage
    with pytest.raises(ToolBatchRejectedError):
        await dispatcher.dispatch_batch((call('work'),call('work','two')),
            finish_reason=FinishReason.TOOL_CALLS)
    assert effects == [] and ledger.usage == before


@pytest.mark.asyncio
async def test_legacy_without_closeout_is_not_restricted():
    dispatcher, ledger, effects = fixture(closing=False)
    assert {x.name for x in dispatcher.available_specs} == {'work','status','finish'}
    await dispatcher.dispatch_batch((call('work'),), finish_reason=FinishReason.TOOL_CALLS)
    assert effects == ['work'] and ledger.usage.tool_calls == 7


@pytest.mark.asyncio
async def test_child_cannot_consume_the_parents_reserved_control_calls():
    child, ledger, effects = fixture(child=True)
    assert child.available_specs == ()
    with pytest.raises(ToolBatchRejectedError):
        await child.dispatch_batch((call('finish'),), finish_reason=FinishReason.TOOL_CALLS)
    assert effects == [] and ledger.usage.tool_calls == 6


@pytest.mark.asyncio
async def test_agent_receives_budget_denial_then_selects_its_own_closeout():
    dispatcher, ledger, effects = fixture()
    provider = FakeProvider(binding=_binding(), turns=(
        ModelTurn(tool_calls=(call('work'),), finish_reason=FinishReason.TOOL_CALLS),
        ModelTurn(tool_calls=(call('status','two'),), finish_reason=FinishReason.TOOL_CALLS),
        ModelTurn(tool_calls=(call('finish','three'),), finish_reason=FinishReason.TOOL_CALLS),
        ModelTurn(content='Closed from known state.', finish_reason=FinishReason.STOP)))
    identity = _identity()
    loop = AgentLoop(provider=provider, invocation=AgentInvocation(identity=identity,
        agent_id='leader', mode=AgentMode.CAPABILITY, tool_names=dispatcher.registry.names),
        budget_ledger=ledger, dispatcher=dispatcher)
    result = await loop.run(messages=(PublicMessage(role='USER', content='Synthetic task.'),),
        deadline_monotonic=monotonic()+30)
    assert result.termination == 'MODEL_COMPLETED'
    assert effects == ['status','finish']
    assert ledger.usage.tool_calls == ledger.usage.evidence_entries == 8
    assert {x.name for x in provider.requests[0].tools} == {'status','finish'}
    for request in provider.requests:
        for message in request.messages:
            if message.role == 'TOOL':
                loop._feedback_result(message.content)
