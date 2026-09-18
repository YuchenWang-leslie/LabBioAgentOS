"""A truncated state response must not replay tools or silently invent a result."""
import json
from types import MethodType, SimpleNamespace

import pytest
from pantheon.agent import Agent, ProviderTurnObservation
from pydantic import TypeAdapter

from labbioagentos import (
    CapabilityEvidenceBundle, InMemoryTraceSink, PantheonRuntimeFactory,
    PantheonTypedStageInvoker, ResponseSchemaRef, RunTraceRecorder, RuntimeInvocationMode,
)
from labbioagentos.runtime.pantheon import PantheonRuntimeIntegrationError
from test_runtime_milestone_c2 import _catalog, _profile, _stage_input, _intake_result


@pytest.mark.asyncio
@pytest.mark.parametrize('second_valid', [True, False])
async def test_truncated_finalization_gets_one_evidenced_correction(second_valid):
    factory = PantheonRuntimeFactory(_catalog())
    team, prompts = await factory.create_team(('coordinator',), invocation_mode=RuntimeInvocationMode.FINALIZE)
    stage = _stage_input()
    evidence = CapabilityEvidenceBundle(run_id=stage.run_id, stage_id=stage.stage_id,
        invocation_id=stage.invocation_id)
    sink = InMemoryTraceSink()
    messages = []

    async def run(self, message, **kwargs):
        assert not any(agent.providers for agent in self.team_agents)
        messages.append(json.loads(message))
        kwargs['process_turn_observation'](ProviderTurnObservation(
            agent_name='CoordinatorAgent', execution_context_id=None, turn_index=1,
            progress_kind='CONTENT', observable_progress=True, elapsed_ms=1,
            total_tokens=16390, tool_names=(), finish_reason='length' if len(messages)==1 or not second_valid else 'stop',
            completion_tokens=16384 if len(messages)==1 or not second_valid else 200))
        if len(messages) == 1 or not second_valid:
            TypeAdapter(dict).validate_json('{"secret":"PRIVATE_SENTINEL"' + ' ' * 100)
        return SimpleNamespace(content=_intake_result())

    team.run = MethodType(run, team)
    invoker = PantheonTypedStageInvoker(team, profile=_profile(), prompt=prompts['coordinator'],
        response_schema=ResponseSchemaRef(), trace_recorder=RunTraceRecorder(sink))
    if second_valid:
        result = await invoker.invoke(stage, capability_evidence=evidence)
        assert result.next_action == _intake_result().next_action
    else:
        with pytest.raises(PantheonRuntimeIntegrationError):
            await invoker.invoke(stage, capability_evidence=evidence)
    assert len(messages) == 2
    feedback = messages[1].pop('finalization_feedback')
    assert feedback['error_code'] == 'RESPONSE_TRUNCATED'
    assert feedback['attempt'] == 2
    assert messages[0] == messages[1]
    assert messages[0]['capability_evidence'] == evidence.model_dump(mode='json')
    events = sink.read(stage.run_id)
    repairs = [event for event in events if event.event_type.value == 'FINALIZATION_CORRECTION_REQUESTED']
    assert len(repairs) == 1
    assert repairs[0].payload['feedback'] == feedback
    serialized = json.dumps([event.model_dump(mode='json') for event in events]) + json.dumps(messages) + json.dumps(feedback)
    assert 'PRIVATE_SENTINEL' not in serialized
    assert sum(event.event_type.value == 'FINALIZATION_PHASE_COMPLETED' for event in events) == int(second_valid)


@pytest.mark.asyncio
@pytest.mark.parametrize('finish_reason', ['content_filter', 'stop', None])
async def test_non_truncation_failures_are_not_retried(finish_reason):
    team, prompts = await PantheonRuntimeFactory(_catalog()).create_team(
        ('coordinator',), invocation_mode=RuntimeInvocationMode.FINALIZE)
    calls = []
    async def run(self, message, **kwargs):
        calls.append(message)
        kwargs['process_turn_observation'](ProviderTurnObservation(
            agent_name='CoordinatorAgent', execution_context_id=None, turn_index=1,
            progress_kind='CONTENT', observable_progress=True, elapsed_ms=1, total_tokens=10,
            tool_names=(), finish_reason=finish_reason, completion_tokens=5))
        TypeAdapter(dict).validate_json('{')
    team.run = MethodType(run, team)
    invoker = PantheonTypedStageInvoker(team, profile=_profile(), prompt=prompts['coordinator'],
        response_schema=ResponseSchemaRef())
    with pytest.raises(PantheonRuntimeIntegrationError):
        await invoker.invoke(_stage_input())
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_real_pantheon_parsing_corrects_without_rejected_history(monkeypatch):
    """Exercise Team -> Agent -> actual Response parser, not a mocked Team.run."""
    requests = []
    async def completion(self, history, tool_use, response_format, process_chunk, allow_transfer, **kwargs):
        assert response_format is not None
        requests.append(history)
        assert 'PRIVATE_SENTINEL' not in json.dumps(history)
        if len(requests) == 1:
            return {'role':'assistant', 'content':'{"result": {"summary":"PRIVATE_SENTINEL',
                    '_metadata':{'finish_reason':'length', 'completion_tokens':16384}}
        visible = json.loads(next(m['content'] for m in reversed(history) if m['role']=='user'))
        assert visible['finalization_feedback']['attempt'] == 2
        return {'role':'assistant', 'content':json.dumps({'result':_intake_result().model_dump(mode='json')}),
                '_metadata':{'finish_reason':'stop', 'completion_tokens':200}}
    monkeypatch.setattr(Agent, '_acompletion_with_models', completion)
    team, prompts = await PantheonRuntimeFactory(_catalog()).create_team(
        ('coordinator',), invocation_mode=RuntimeInvocationMode.FINALIZE)
    invoker = PantheonTypedStageInvoker(team, profile=_profile(), prompt=prompts['coordinator'],
        response_schema=ResponseSchemaRef())
    result = await invoker.invoke(_stage_input())
    assert result.next_action == _intake_result().next_action
    assert len(requests) == 2
