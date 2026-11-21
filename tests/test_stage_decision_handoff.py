"""Investigation owns its action; tool-free formatting cannot replace it."""

from dataclasses import replace
import json

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from labbioagentos import NextActionProposal, RuntimeIntegrationError, WorkflowStage
from labbioagentos.kernel import FinishReason, ModelTurn, TokenUsage
from labbioagentos.kernel import ToolBatchRejectedError, ToolCall
from labbioagentos.runtime import handoff
from labbioagentos.runtime.assembly import RuntimeStageAssemblySpec
from labbioagentos.runtime.contracts import (
    CapabilityEvidenceBundle, RuntimeClarificationView, RuntimeGateDecisionView, RuntimeWorkflowControlView,
)
from labbioagentos.runtime.native_stage import NativeKernelStageInvoker, NativeRuntimeFactory
from labbioagentos.runtime.native_tools import NativeRuntimeCapabilityHost, build_labbio_tool_registry
from labbioagentos.runtime.tooling import LabBioRuntimeToolSet
from test_clarification_continuation import _question
from test_native_stage_application import WorkflowProvider, _body, _capability_catalog, _stage_input, _toolset


CONTINUE = {"action": "transition", "target_stage": "UNDERSTAND", "reason": "Agent chose further investigation."}


class Finalizer(WorkflowProvider):
    def __init__(self, calls, actions):
        super().__init__(calls)
        self.actions = iter(actions)

    async def generate(self, request):
        self.calls.append(request)
        content = json.dumps({"stage_id": "INTAKE", "summary": "Synthetic handoff.",
            "body": _body(WorkflowStage.INTAKE).model_dump(mode="json"),
            "next_action": next(self.actions)})
        return ModelTurn(content=content, finish_reason=FinishReason.STOP,
            usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2))


async def decision_case(tmp_path, action=CONTINUE, *, outputs=None, after_gate=False):
    control = RuntimeWorkflowControlView(current_stage=WorkflowStage.INTAKE,
        transition_targets=(WorkflowStage.UNDERSTAND,), request_user_input_available=after_gate,
        clarification_available=True, clarification_rounds_remaining=3,
        retry_available=False, finish_available=False)
    gate = RuntimeGateDecisionView(gate_id="test-gate", source_stage=WorkflowStage.INTAKE,
        approved=True, domain_reference_id="skill-use:test") if after_gate else None
    stage = _stage_input().model_copy(update={"workflow_control": control, "gate_decisions": (gate,) if gate else (),
        "allowed_capabilities": ("artifact_list", "prepare_handoff")})
    fixture = _toolset(tmp_path, stage)
    binding = replace(fixture.binding, capability_allowlist=stage.allowed_capabilities,
        workflow_control=control, decision_context_sha256=handoff.decision_context_sha256(stage))
    tools = LabBioRuntimeToolSet(binding, fixture.services)
    original = await tools.artifact_list()
    tools.mark_feedback_delivered((original,))
    selected = await tools.prepare_handoff([original['capability_invocation_id']],
        "Investigation completed; the Agent selected the next action.", next_action=action)
    assert selected['success'], selected
    assert selected['data']['status'] == 'READY'
    evidence = CapabilityEvidenceBundle(run_id=stage.run_id, stage_id=stage.stage_id,
        invocation_id=stage.invocation_id, items=tools.evidence_items(),
        prepared_handoff=tools.prepared_handoff,
        decision_context_sha256=binding.decision_context_sha256)
    calls = []
    catalog = _capability_catalog()
    catalog.capabilities['coordinator-capabilities'] = catalog.capabilities[
        'coordinator-capabilities'].model_copy(update={'capability_allowlist': stage.allowed_capabilities})
    invoker = NativeKernelStageInvoker(assembly=RuntimeStageAssemblySpec(
        stage_id=stage.stage_id, root_profile_key='coordinator',
        prompt_template_key='runtime-generic', capability_allowlist=stage.allowed_capabilities),
        factory=NativeRuntimeFactory(catalog, lambda _model: Finalizer(calls, outputs or [action])),
        principal=binding.principal, workspace=binding.workspace, services=tools.services)
    return stage, evidence, invoker, calls, tools


@pytest.mark.asyncio
async def test_finalizer_cannot_replace_investigation_transition_with_generic_question(tmp_path):
    question = _question().model_dump(mode='json')
    question['question']['prompt'] = 'PRIVATE_REJECTED_QUESTION'
    stage, evidence, invoker, calls, _tools = await decision_case(
        tmp_path, outputs=[question, CONTINUE])
    original = evidence.model_dump_json()
    result = await invoker.finalize_recovered(stage, evidence)
    assert result.next_action.model_dump(mode='json') == NextActionProposal(**CONTINUE).model_dump(mode='json')
    assert len(calls) == 2 and all(not call.tools for call in calls)
    feedback = calls[1].messages[-1].content
    assert 'stage_decision_mismatch' in feedback and 'next_action' in feedback
    assert 'PRIVATE_REJECTED_QUESTION' not in feedback
    assert evidence.model_dump_json() == original
    schema = calls[0].response_schema
    value = {'stage_id': 'INTAKE', 'summary': 'Synthetic.',
        'body': _body(WorkflowStage.INTAKE).model_dump(mode='json'), 'next_action': question}
    assert list(Draft202012Validator(schema).iter_errors(value))


@pytest.mark.asyncio
async def test_real_missing_user_fact_still_produces_exact_agent_question(tmp_path):
    question = _question().model_dump(mode='json')
    stage, evidence, invoker, calls, _tools = await decision_case(tmp_path, question)
    result = await invoker.finalize_recovered(stage, evidence)
    assert result.next_action.question == _question().question
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_repeated_formatter_disagreement_is_bounded_not_silently_overwritten(tmp_path):
    wrong = _question().model_dump(mode='json')
    stage, evidence, invoker, calls, _tools = await decision_case(
        tmp_path, outputs=[wrong, wrong, CONTINUE])
    with pytest.raises(RuntimeIntegrationError) as caught:
        await invoker.finalize_recovered(stage, evidence)
    assert caught.value.error_code == 'MALFORMED_RUNTIME_RESULT'
    assert len(calls) == 2
    assert evidence.prepared_handoff.stage_decision.next_action.action.value == 'transition'


@pytest.mark.asyncio
async def test_tool_declaration_and_direct_host_reject_illegal_actions(tmp_path):
    stage, _evidence, _invoker, _calls, tools = await decision_case(tmp_path)
    host = NativeRuntimeCapabilityHost(tools.binding, tools.services)
    definition = build_labbio_tool_registry(host).definition('prepare_handoff')
    args = {'observation_ids': [str(tools.prepared_handoff.observations[0].observation_id)],
        'summary': 'Agent scope.', 'next_action': CONTINUE}
    definition.arguments_model.model_validate_json(json.dumps(args))
    for illegal in ({'action': 'transition', 'target_stage': 'REPORT'}, {'action': 'finish'}):
        with pytest.raises(ValidationError):
            definition.arguments_model.model_validate_json(json.dumps({**args, 'next_action': illegal}))
        result = await tools.prepare_handoff(args['observation_ids'], args['summary'], next_action=illegal)
        assert not result['success'] and tools.prepared_handoff is None
    with pytest.raises(ValidationError):
        definition.arguments_model.model_validate_json(json.dumps({k: v for k, v in args.items() if k != 'next_action'}))


@pytest.mark.asyncio
async def test_decision_tool_schema_explicitly_declares_an_object_not_a_string(tmp_path):
    _stage, _evidence, _invoker, _calls, tools = await decision_case(tmp_path)
    registry = build_labbio_tool_registry(NativeRuntimeCapabilityHost(tools.binding, tools.services))
    schema = registry.definition('prepare_handoff').spec.parameters['properties']['next_action']
    assert schema['type'] == 'object'
    assert 'not a string' in schema['description']


@pytest.mark.asyncio
@pytest.mark.parametrize('next_action', ('transition', json.dumps(CONTINUE)))
async def test_string_decision_is_rejected_with_safe_object_correction(tmp_path, next_action):
    _stage, _evidence, _invoker, _calls, tools = await decision_case(tmp_path)
    registry = build_labbio_tool_registry(NativeRuntimeCapabilityHost(tools.binding, tools.services))
    args = {'observation_ids': [str(tools.prepared_handoff.observations[0].observation_id)],
        'summary': 'Agent scope.', 'next_action': next_action}
    with pytest.raises(ToolBatchRejectedError) as caught:
        registry.validate_batch((ToolCall(call_id='string-decision', name='prepare_handoff',
            arguments_json=json.dumps(args)),), finish_reason=FinishReason.TOOL_CALLS, max_calls=1)
    assert caught.value.validation_fields == ('next_action',)
    assert caught.value.validation_type == 'dict_type'
    assert 'JSON object' in str(caught.value)
    assert next_action not in str(caught.value)


@pytest.mark.asyncio
async def test_new_protocol_missing_decision_does_not_invent_a_default(tmp_path):
    stage, evidence, invoker, calls, _tools = await decision_case(tmp_path)
    evidence = evidence.model_copy(update={'prepared_handoff': None})
    with pytest.raises(RuntimeIntegrationError) as caught:
        await invoker.finalize_recovered(stage, evidence)
    assert caught.value.error_code == 'HANDOFF_REQUIRED'
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize(("action", "binding"), [
    (CONTINUE, "BOUND"),
    ({"action": "request_user_input", "user_prompt": "Reopen this request?",
      "domain_reference_id": "skill-use:test"}, "SUPERSEDED_BY_CURRENT_INPUT"),
])
async def test_closing_already_decided_gate_only_supersedes_a_gate_action(tmp_path, action, binding):
    stage, evidence, invoker, calls, _ = await decision_case(
        tmp_path, action, outputs=[CONTINUE], after_gate=True)
    result = await invoker.finalize_recovered(stage, evidence)
    payload = json.loads(calls[0].messages[-1].content)
    assert payload["stage_input"]["workflow_control"]["request_user_input_available"] is False
    assert payload["stage_decision_control"]["status"] == binding
    assert result.next_action.action.value == "transition"


@pytest.mark.asyncio
async def test_new_authoritative_user_context_supersedes_old_action_without_tool_replay(tmp_path):
    question = _question().model_dump(mode='json')
    stage, evidence, invoker, calls, tools = await decision_case(tmp_path, question, outputs=[CONTINUE])
    archived = evidence.model_dump_json()
    changed = stage.model_copy(update={'instruction': stage.instruction + '\nNew explicit user instruction.'})
    assert handoff.decision_context_sha256(changed) != evidence.decision_context_sha256
    before = tools.evidence_items()
    result = await invoker.finalize_recovered(changed, evidence)
    assert result.next_action.action.value == 'transition'
    assert tools.evidence_items() == before and evidence.model_dump_json() == archived
    assert len(calls) == 1
    control = json.loads(calls[0].messages[-1].content)['stage_decision_control']
    assert control['status'] == 'SUPERSEDED_BY_CURRENT_INPUT'


@pytest.mark.asyncio
async def test_answer_received_after_a_persisted_question_unlocks_current_decision(tmp_path):
    question = _question().model_dump(mode='json')
    stage, evidence, invoker, calls, _tools = await decision_case(tmp_path, question, outputs=[CONTINUE])
    persisted = CapabilityEvidenceBundle.model_validate_json(evidence.model_dump_json())
    answer = RuntimeClarificationView(question_id='synthetic-question-1', source_stage=stage.stage_id,
        question=_question().question, status='ANSWERED', answered_by='user-c2',
        answer_text='The readers are new to this example.')
    updated = stage.model_copy(update={'clarifications': (answer,)})
    result = await invoker.finalize_recovered(updated, persisted)
    assert result.next_action.action.value == 'transition' and len(calls) == 1
    assert persisted.prepared_handoff.stage_decision.next_action.question == answer.question


@pytest.mark.asyncio
async def test_historical_bundle_is_read_without_inventing_a_committed_action(tmp_path):
    from test_finalization_completion_delivery import finalization_case
    stage, evidence, invoker, calls = await finalization_case(tmp_path, 'Legacy prose.', prepared=True)
    raw = evidence.model_dump(mode='json', exclude_none=True)
    assert 'decision_context_sha256' not in raw and 'stage_decision' not in raw['prepared_handoff']
    restored = CapabilityEvidenceBundle.model_validate_json(json.dumps(raw))
    result = await invoker.finalize_recovered(stage, restored)
    assert result.next_action.action.value == 'transition' and len(calls) == 1
    assert restored.model_dump(mode='json', exclude_none=True) == raw


@pytest.mark.asyncio
@pytest.mark.parametrize('action', (
    {'action': 'fail', 'reason': 'Agent reports an unresolved blocker.'},
    {'action': 'request_clarification', 'reason': None, 'question': {
        'issue_key': 'output-language', 'prompt': 'Which output language do you need?',
        'why_needed': 'The required user preference has not been provided.', 'followup_to': None}},
))
async def test_binding_preserves_agent_blockers_and_question_wording_not_just_action_kind(tmp_path, action):
    changed = json.loads(json.dumps(action))
    if 'question' in changed:
        changed['question']['prompt'] = 'PRIVATE_CHANGED_QUESTION'
    else:
        changed['reason'] = 'PRIVATE_CHANGED_BLOCKER'
    stage, evidence, invoker, calls, _tools = await decision_case(tmp_path, action, outputs=[changed, action])
    result = await invoker.finalize_recovered(stage, evidence)
    assert result.next_action.model_dump(mode='json') == NextActionProposal(**action).model_dump(mode='json')
    assert len(calls) == 2 and 'PRIVATE_CHANGED' not in calls[1].messages[-1].content


@pytest.mark.asyncio
@pytest.mark.parametrize("repeat_read", [False, True])
async def test_investigator_submits_action_after_structural_feedback_before_finalization(tmp_path, repeat_read):
    stage, _evidence, _invoker, _calls, tools = await decision_case(tmp_path)
    captured = []
    final_calls = []

    class Investigator:
        binding = WorkflowProvider.binding
        calls = 0

        async def generate(self, request):
            self.calls += 1
            if self.calls == 1 or (repeat_read and self.calls == 2):
                name, args = 'artifact_list', {}
            elif self.calls == 3:
                feedback = json.loads(request.messages[-1].content)
                if repeat_read:
                    assert feedback['reason'] == 'REPEATED_NON_RETRYABLE_REQUEST'
                    assert {t.name for t in request.tools} == {'prepare_handoff'}
                else:
                    assert feedback['reason'] == 'HANDOFF_DECISION_REQUIRED'
                source = next(json.loads(m.content)['content'] for m in request.messages if m.role == 'TOOL')
                name, args = 'prepare_handoff', {'observation_ids': [source['capability_invocation_id']],
                    'summary': 'Agent selected evidence and its next action.', 'next_action': CONTINUE}
            else:
                assert self.calls in (2, 4)
                return ModelTurn(content='The Agent intends to continue.', finish_reason=FinishReason.STOP,
                    usage=TokenUsage(total_tokens=1))
            return ModelTurn(tool_calls=(ToolCall(call_id=f'call-{self.calls}', name=name,
                arguments_json=json.dumps(args)),), finish_reason=FinishReason.TOOL_CALLS,
                usage=TokenUsage(total_tokens=1))

    investigator = Investigator()
    built = []

    def provider(_model):
        item = investigator if not built else Finalizer(final_calls, [_question().model_dump(mode='json'), CONTINUE])
        built.append(item)
        return item

    catalog = _capability_catalog()
    catalog.capabilities['coordinator-capabilities'] = catalog.capabilities[
        'coordinator-capabilities'].model_copy(update={'capability_allowlist': stage.allowed_capabilities})
    invoker = NativeKernelStageInvoker(assembly=RuntimeStageAssemblySpec(
        stage_id=stage.stage_id, root_profile_key='coordinator', prompt_template_key='runtime-generic',
        capability_allowlist=stage.allowed_capabilities, required_capabilities=('artifact_list',),
        max_capability_turns=8), factory=NativeRuntimeFactory(catalog, provider),
        principal=tools.binding.principal, workspace=tools.binding.workspace, services=tools.services,
        boundary_observer=lambda kind, value: captured.append((kind, value)))
    result = await invoker.invoke(stage)
    assert result.next_action.action.value == 'transition'
    assert investigator.calls == (3 if repeat_read else 4) and len(final_calls) == 2
    saved = next(value for kind, value in captured if kind == 'capability_evidence')
    restored = CapabilityEvidenceBundle.model_validate_json(saved.model_dump_json())
    assert restored.decision_context_sha256 == restored.prepared_handoff.stage_decision.context_sha256
    if repeat_read:
        assert restored.termination_reason == 'REPEATED_NON_RETRYABLE_REQUEST'
        assert [item.status.value for item in restored.items] == ['COMPLETED', 'FAILED', 'COMPLETED']
    assert [item.capability_name for item in restored.items if item.status.value == 'COMPLETED'] == [
        'artifact_list', 'prepare_handoff']
