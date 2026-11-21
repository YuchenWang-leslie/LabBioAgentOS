"""Completion capacity is shared, bounded, and does not execute denied work."""

import asyncio
from time import monotonic
from uuid import uuid4

import pytest

from labbioagentos.kernel import (
    AgentInvocation, AgentLoop, AgentMode, BudgetDelta, BudgetLedger, FakeProvider,
    FinishReason, KernelDeadlineExceededError, ModelTurn, ProviderProtocolError,
    ProviderTransportError, PublicMessage, TokenUsage, ToolDispatcher,
)
from native_kernel.test_agent_loop import _binding, _identity
from native_kernel.test_tool_closeout_capacity import fixture, call


def result(name=None, tokens=2):
    return ModelTurn(tool_calls=(call(name),) if name else (),
        content=None if name else 'Completed from known state.',
        finish_reason=FinishReason.TOOL_CALLS if name else FinishReason.STOP,
        usage=TokenUsage(total_tokens=tokens))


def setup(provider, *, limits=None, clock=None, child=False):
    original, initial, effects = fixture(consumed=0, child=child)
    ledger = BudgetLedger(initial.limits.model_copy(update=limits or {}))
    dispatcher = ToolDispatcher(original.registry, original.projector, budget_ledger=ledger,
        closeout_tools=('status', 'finish'), closeout_reserve=2)
    identity = _identity()
    if child:
        child_id = uuid4()
        identity = identity.model_copy(update={'invocation_id':child_id, 'depth':1,
            'parent_invocation_id':identity.invocation_id, 'parent_tool_invocation_id':uuid4(),
            'chain':(*identity.chain, child_id)})
    loop = AgentLoop(provider=provider, invocation=AgentInvocation(identity=identity,
        agent_id='checker' if child else 'leader', mode=AgentMode.CAPABILITY,
        tool_names=dispatcher.registry.names), budget_ledger=ledger, dispatcher=dispatcher,
        clock=clock, reserve_completion_capacity=True)
    return loop, ledger, effects


@pytest.mark.asyncio
@pytest.mark.parametrize("last_action,expected", [
    ("finish", ["work", "finish"]), ("work", ["work"]), (None, ["work"]),
])
async def test_convergence_stop_offers_one_decision_only_turn(last_action, expected):
    provider = FakeProvider(binding=_binding(), turns=(result("work"), result(last_action)))
    loop, ledger, effects = setup(provider)
    reason = "REPEATED_NON_RETRYABLE_REQUEST"
    loop.dispatcher.stop_requested = lambda: reason if effects else None
    loop.dispatcher.stop_handoff = (reason, "finish")
    outcome = await loop.run(messages=(PublicMessage(role="USER", content="Submit your own decision."),),
                             deadline_monotonic=monotonic()+60)
    assert effects == expected
    assert ledger.usage.provider_turns == len(provider.requests) == 2
    assert {s.name for s in provider.requests[-1].tools} == {"finish"}
    assert outcome.output == {"capability_stop_reason": reason}


@pytest.mark.asyncio
async def test_unconfirmed_effect_does_not_offer_a_handoff():
    provider = FakeProvider(binding=_binding(), turns=(result("work"),))
    loop, ledger, effects = setup(provider)
    loop.dispatcher.stop_handoff = ("REPEATED_NON_RETRYABLE_REQUEST", "finish")
    loop.dispatcher.stop_requested = lambda: "ENVIRONMENT_BUILD_UNCONFIRMED" if effects else None
    outcome = await loop.run(messages=(PublicMessage(role="USER", content="Finish."),),
                             deadline_monotonic=monotonic()+60)
    assert len(provider.requests) == 1
    assert effects == ["work"]
    assert outcome.output == {"capability_stop_reason":"ENVIRONMENT_BUILD_UNCONFIRMED"}


@pytest.mark.asyncio
async def test_real_async_work_timeout_keeps_unknown_usage_and_only_closes():
    class Slow:
        binding = _binding()
        def __init__(self):
            self.requests, self.cancelled = [], False
        async def generate(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                try:
                    await asyncio.sleep(5)
                finally:
                    self.cancelled = True
                return result('work')
            assert {t.name for t in request.tools} == {'status','finish'}
            assert request.max_output_tokens == 8192
            return result('finish') if len(self.requests) == 2 else result()
    provider = Slow()
    loop, ledger, effects = setup(provider, limits={'wall_time_ms':900})
    outcome = await loop.run(messages=(PublicMessage(role='USER', content='Engineering fixture.'),),
        deadline_monotonic=monotonic()+.9, max_output_tokens=32768)
    assert outcome.termination == 'MODEL_COMPLETED'
    assert provider.cancelled and effects == ['finish']
    assert ledger.usage.tokens is None
    assert 550 <= ledger.usage.wall_time_ms <= 900
    assert ledger.usage.provider_turns == 3
    assert len([e for e in loop.events if e.kind.value == 'MODEL_USAGE_UNREPORTED']) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('dimension,consumed', [('provider_turns',5), ('tokens',70000)])
@pytest.mark.parametrize('max_output_tokens', [32768, 4096])
async def test_existing_shared_consumption_reserves_completion_before_new_work(dimension, consumed, max_output_tokens):
    provider = FakeProvider(binding=_binding(), turns=(result('finish'),result()))
    loop, ledger, effects = setup(provider)
    ledger.reserve(BudgetDelta(**{dimension:consumed}))
    outcome = await loop.run(messages=(PublicMessage(role='USER', content='Complete within budget.'),),
        deadline_monotonic=monotonic()+60, max_output_tokens=max_output_tokens)
    assert outcome.termination == 'MODEL_COMPLETED' and effects == ['finish']
    assert {t.name for t in provider.requests[0].tools} == {'status','finish'}
    assert all(request.max_output_tokens == min(8192, max_output_tokens) for request in provider.requests)
    assert loop.completion_capacity['max_output_tokens'] == min(8192, max_output_tokens)
    assert ledger.usage.provider_turns <= ledger.limits.provider_turns
    assert ledger.usage.tokens <= ledger.limits.tokens


@pytest.mark.asyncio
@pytest.mark.parametrize('dimension,consumed,spent', [('provider_turns',4,2), ('tokens',64000,4000)])
async def test_already_admitted_work_is_not_discarded_at_a_non_time_boundary(dimension, consumed, spent):
    provider = FakeProvider(binding=_binding(), turns=(result('work',spent),result('finish'),result()))
    loop, ledger, effects = setup(provider)
    ledger.reserve(BudgetDelta(**{dimension:consumed}))
    outcome = await loop.run(messages=(PublicMessage(role='USER', content='Preserve admitted work.'),),
        deadline_monotonic=monotonic()+60)
    assert outcome.termination == 'MODEL_COMPLETED' and effects == ['work','finish']
    assert 'work' in {t.name for t in provider.requests[0].tools}
    assert {t.name for t in provider.requests[1].tools} == {'status','finish'}


@pytest.mark.asyncio
@pytest.mark.parametrize('dimension,consumed', [('wall_time_ms',40001), ('provider_turns',5), ('tokens',70000)])
async def test_child_stops_without_spending_parent_completion_capacity(dimension, consumed):
    provider = FakeProvider(binding=_binding(), turns=())
    loop, ledger, effects = setup(provider, child=True, limits={'delegation_depth':1})
    ledger.reserve(BudgetDelta(**{dimension:consumed}))
    before = ledger.usage
    outcome = await loop.run(messages=(PublicMessage(role='USER', content='Bounded child assignment.'),),
        deadline_monotonic=monotonic()+60)
    assert outcome.termination == 'RUNTIME_STOPPED'
    assert outcome.output['capability_stop_reason'].startswith('WORK_')
    assert not provider.requests and not effects and ledger.usage == before


@pytest.mark.asyncio
@pytest.mark.parametrize('max_output_tokens', [32768, 8192, 4096])
async def test_child_reserves_a_public_return_inside_parent_work_allocation(max_output_tokens):
    provider = FakeProvider(binding=_binding(), turns=(result(),))
    loop, ledger, effects = setup(provider, child=True, limits={'delegation_depth':1})
    ledger.reserve(BudgetDelta(provider_turns=4))
    outcome = await loop.run(messages=(PublicMessage(role='USER', content='Bounded child review.'),),
        deadline_monotonic=monotonic()+60, max_output_tokens=max_output_tokens)
    assert outcome.termination == 'MODEL_COMPLETED'
    assert not provider.requests[0].tools
    assert provider.requests[0].max_output_tokens == min(8192, max_output_tokens)
    assert loop.completion_capacity['max_output_tokens'] == min(8192, max_output_tokens)
    assert provider.requests[0].deadline_monotonic <= monotonic()+40
    assert not effects and ledger.usage.provider_turns == 5
    assert ledger.limits.provider_turns - ledger.usage.provider_turns == 3


@pytest.mark.asyncio
@pytest.mark.parametrize('child', [False, True])
@pytest.mark.parametrize('error', [KernelDeadlineExceededError, ProviderTransportError, ProviderProtocolError])
async def test_typed_request_deadline_enters_reserved_return_before_clock_boundary(child, error):
    now = [1000.0]
    class Provider:
        binding = _binding()
        def __init__(self): self.requests = []
        async def generate(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                now[0] += 1
                return result('work')
            if len(self.requests) == 2:
                now[0] = request.deadline_monotonic - .25
                raise error()
            assert 'work' not in {tool.name for tool in request.tools}
            return result() if child or len(self.requests) == 4 else result('finish')
    provider = Provider()
    loop, ledger, effects = setup(provider, child=child, clock=lambda:now[0], limits={'delegation_depth':1})
    outcome = await loop.run(messages=(PublicMessage(role='USER', content='Request deadline fixture.'),),
        deadline_monotonic=1060, max_output_tokens=32768)
    if error is KernelDeadlineExceededError:
        assert outcome.termination == 'MODEL_COMPLETED'
        assert effects == (['work'] if child else ['work','finish'])
        assert len(provider.requests) == (3 if child else 4)
        assert provider.requests[2].max_output_tokens == 8192
        assert provider.requests[2].deadline_monotonic <= (1040 if child else 1060)
    else:
        assert outcome.termination == ('PROVIDER_TRANSPORT_STOP' if error is ProviderTransportError else 'PROVIDER_PROTOCOL_STOP')
        assert len(provider.requests) == 2 and effects == ['work']
    assert ledger.usage.tokens is None
    assert ledger.usage.provider_turns <= ledger.limits.provider_turns
    assert ledger.usage.wall_time_ms < ledger.limits.wall_time_ms
    assert len([e for e in loop.events if e.kind.value=='MODEL_USAGE_UNREPORTED']) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('child', [False, True])
async def test_return_deadline_never_allocates_another_completion_window(child):
    now = [1000.0]
    class Provider:
        binding = _binding()
        def __init__(self): self.requests = []
        async def generate(self, request):
            self.requests.append(request)
            if len(self.requests)==1:
                return result('work')
            now[0] = request.deadline_monotonic-.25
            raise KernelDeadlineExceededError()
    provider = Provider()
    loop, ledger, effects = setup(provider, child=child, clock=lambda:now[0],limits={'delegation_depth':1})
    outcome = await loop.run(messages=(PublicMessage(role='USER',content='Only one reserved window.'),),
        deadline_monotonic=1060,max_output_tokens=32768)
    assert outcome.termination=='PROVIDER_DEADLINE_STOP'
    assert len(provider.requests)==3 and effects==['work']
    assert ledger.usage.provider_turns==3 and ledger.usage.tokens is None
    assert ledger.usage.wall_time_ms < ledger.limits.wall_time_ms


@pytest.mark.asyncio
async def test_total_deadline_never_opens_a_reserved_window():
    now = [1000.0]
    class Provider:
        binding = _binding()
        def __init__(self): self.requests = []
        async def generate(self, request):
            self.requests.append(request)
            if len(self.requests)==1:
                return result('work')
            now[0] = 1060
            raise KernelDeadlineExceededError()
    provider = Provider()
    loop, ledger, effects = setup(provider,clock=lambda:now[0])
    outcome = await loop.run(messages=(PublicMessage(role='USER',content='Total budget remains binding.'),),
        deadline_monotonic=1060,max_output_tokens=32768)
    assert outcome.termination=='PROVIDER_DEADLINE_STOP'
    assert len(provider.requests)==2 and effects==['work']


@pytest.mark.asyncio
async def test_real_http_deadline_margin_keeps_kernel_closeout_reachable(monkeypatch):
    import json
    import httpx
    from labbioagentos.kernel import OpenAICompatibleChatProvider
    from native_kernel.test_http_stream import Provider, _route, _sse
    class Idle(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b': keep-alive\n\n'
            await asyncio.Event().wait()
    def response(name=None):
        delta = {'role':'assistant', 'content':None if name else 'Closed from received evidence.'}
        if name:
            delta['tool_calls'] = [{'index':0,'id':name,'type':'function',
                'function':{'name':name,'arguments':'{}'}}]
        value = {'choices':[{'index':0,'delta':delta,'finish_reason':'tool_calls' if name else 'stop'}],
            'usage':{'prompt_tokens':2,'completion_tokens':2,'total_tokens':4}}
        return _sse('data: '+json.dumps(value)+'\n\ndata: [DONE]\n\n')
    wire = Provider(response('work'), lambda:httpx.Response(200, stream=Idle()),
        response('finish'), response())
    _route(monkeypatch, wire)
    provider = OpenAICompatibleChatProvider(binding=_binding(), api_key='synthetic-key')
    loop, ledger, effects = setup(provider, limits={'wall_time_ms':3000})
    started = monotonic()
    outcome = await loop.run(messages=(PublicMessage(role='USER',content='Real HTTP deadline fixture.'),),
        deadline_monotonic=started+3, max_output_tokens=32768)
    assert outcome.termination == 'MODEL_COMPLETED' and effects == ['work','finish']
    assert len(wire.requests) == 4 and monotonic()-started < 3
    assert ledger.usage.tokens is None


@pytest.mark.asyncio
@pytest.mark.parametrize('dimension, consumed, spent', [('wall_time_ms',30000,7000), ('tokens',64000,2000)])
async def test_child_return_time_and_tokens_stay_above_parent_boundary(dimension, consumed, spent):
    now = [1000.0]
    class Provider:
        binding = _binding()
        def __init__(self): self.requests = []
        async def generate(self, request):
            self.requests.append(request)
            if len(self.requests) == 1:
                assert 'work' in {t.name for t in request.tools}
                if dimension == 'wall_time_ms': now[0] += spent/1000
                return result('work', spent if dimension == 'tokens' else 2)
            assert not request.tools
            if dimension == 'wall_time_ms':
                assert request.deadline_monotonic - now[0] <= 3
            return result()
    provider = Provider()
    loop, ledger, effects = setup(provider, child=True, limits={'delegation_depth':1}, clock=lambda:now[0])
    ledger.reserve(BudgetDelta(**{dimension:consumed}))
    outcome = await loop.run(messages=(PublicMessage(role='USER', content='Synthetic bounded review.'),),
        deadline_monotonic=now[0]+60)
    assert outcome.termination == 'MODEL_COMPLETED'
    # The time response crossed its work deadline; its tool must not dispatch.
    assert len(provider.requests) == 2
    assert effects == ([] if dimension == 'wall_time_ms' else ['work'])
    reserved = loop._parent_completion_capacity['model_time_ms' if dimension=='wall_time_ms' else 'tokens']
    assert getattr(ledger.limits,dimension) - getattr(ledger.usage,dimension) > reserved


@pytest.mark.asyncio
@pytest.mark.parametrize('dispatcher_reserve', [0, 2])
async def test_child_return_cannot_dispatch_or_spend_parent_turns(dispatcher_reserve):
    provider = FakeProvider(binding=_binding(), turns=(result('work'),))
    loop, ledger, effects = setup(provider, child=True, limits={'delegation_depth':1})
    loop.dispatcher.closeout_reserve = dispatcher_reserve
    ledger.reserve(BudgetDelta(provider_turns=4))
    outcome = await loop.run(messages=(PublicMessage(role='USER', content='Return without new work.'),),
        deadline_monotonic=monotonic()+60)
    assert outcome.termination == 'RUNTIME_STOPPED'
    assert outcome.output['capability_stop_reason'] == 'PARENT_COMPLETION_RESERVED'
    assert not effects and ledger.usage.provider_turns == 5


@pytest.mark.asyncio
@pytest.mark.parametrize('max_output_tokens', [32768, 4096])
async def test_real_collaboration_returns_child_assessment_and_keeps_parent_closeout(max_output_tokens):
    import json
    from labbioagentos.kernel import AgentRegistry, ToolCall
    from native_kernel.test_collaboration import _limits, _runtime
    registry, _, effects = fixture(consumed=0)
    delegated = ModelTurn(tool_calls=(ToolCall(call_id='delegate', name='delegate', arguments_json=json.dumps({
        'target_agent_id':'writer', 'instruction':'Bounded synthetic review.'})),), finish_reason=FinishReason.TOOL_CALLS,
        usage=TokenUsage(total_tokens=2))
    runtime, providers, ledger = _runtime(leader_tools=('finish',), tool_registry=registry.registry,
        limits=_limits().model_copy(update={'provider_turns':8}),
        leader_turns=(delegated,result()), child_turns={'writer':(result(),)})
    runtime.registry = AgentRegistry(team=runtime.registry.team,
        profiles=tuple(runtime.registry.profile(name).model_copy(
            update={'max_output_tokens':max_output_tokens})
            for name in runtime.registry.team.member_ids), providers=providers)
    runtime.reserve_completion_capacity = True
    runtime.closeout_tools, runtime.closeout_reserve = ('finish',), 2
    ledger.reserve(BudgetDelta(provider_turns=3))
    outcome = await runtime.run_leader(identity=_identity(),
        messages=(PublicMessage(role='USER', content='Engineering collaboration.'),),
        deadline_monotonic=monotonic()+60)
    assert outcome.leader_result.termination == 'MODEL_COMPLETED'
    assert outcome.delegations[0].status == 'SUCCEEDED'
    assert outcome.delegations[0].content == 'Completed from known state.'
    assert not providers['writer'].requests[0].tools
    assert providers['writer'].requests[0].max_output_tokens == min(8192, max_output_tokens)
    assert providers['leader'].requests[-1].max_output_tokens == min(8192, max_output_tokens)
    assert {t.name for t in providers['leader'].requests[-1].tools} == {'finish'}
    assert ledger.usage.provider_turns == 6 and not effects


@pytest.mark.asyncio
async def test_forbidden_mixed_batch_executes_nothing_and_does_not_spend_reserved_slots():
    mixed = ModelTurn(tool_calls=(call('work'),call('finish','two')), finish_reason=FinishReason.TOOL_CALLS,
        usage=TokenUsage(total_tokens=2))
    provider = FakeProvider(binding=_binding(), turns=(mixed,result('finish'),result()))
    loop, ledger, effects = setup(provider)
    ledger.reserve(BudgetDelta(wall_time_ms=40001))
    outcome = await loop.run(messages=(PublicMessage(role='USER', content='Finish, not new work.'),),
        deadline_monotonic=monotonic()+60)
    assert outcome.termination == 'MODEL_COMPLETED' and effects == ['finish']
    assert ledger.usage.tool_calls == ledger.usage.evidence_entries == 1
    for request in provider.requests:
        for message in request.messages:
            if message.role == 'TOOL':
                loop._feedback_result(message.content)


@pytest.mark.asyncio
async def test_governed_tool_duration_does_not_spend_model_completion_time(monkeypatch):
    now = [0.0]
    provider = FakeProvider(binding=_binding(), turns=(result('work'),result('finish'),result()))
    loop, ledger, effects = setup(provider, clock=lambda:now[0])
    dispatch = loop.dispatcher.dispatch_parsed
    async def waited(parsed, **kwargs):
        results = await dispatch(parsed, **kwargs)
        if any(item.request.name == 'work' for item in parsed):
            now[0] += 120
        return results
    monkeypatch.setattr(loop.dispatcher, 'dispatch_parsed', waited)
    outcome = await loop.run(messages=(PublicMessage(role='USER', content='A governed long tool.'),),
        deadline_monotonic=60)
    assert outcome.termination == 'MODEL_COMPLETED' and effects == ['work','finish']
    assert ledger.usage.wall_time_ms == 0
    assert provider.requests[1].deadline_monotonic == pytest.approx(160)
