"""A lawful evidence reread is not a fatal pagination or convergence error."""

from dataclasses import replace
import json

import pytest

from labbioagentos import WorkflowStage
from labbioagentos.kernel import FinishReason, ModelTurn, TokenUsage, ToolCall
from labbioagentos.runtime.assembly import RuntimeStageAssemblySpec
from labbioagentos.runtime.contracts import RuntimeWorkflowControlView
from labbioagentos.runtime.handoff import decision_context_sha256
from labbioagentos.runtime.native_stage import NativeKernelStageInvoker, NativeRuntimeFactory
from labbioagentos.runtime.tooling import LabBioRuntimeToolSet
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401
from test_native_stage_application import WorkflowProvider, _body, _capability_catalog, _stage_input


@pytest.mark.asyncio
async def test_repeated_partial_read_has_fresh_receipts_without_claiming_full_coverage(artifact_query_boundary):
    _sink, _binding, ref, tools = artifact_query_boundary
    responses = []
    for _ in range(3):
        response = await tools.artifact_query(str(ref.artifact_id), 'TOP_N', 2)
        assert response['success'], response
        responses.append(response)
        tools.mark_feedback_delivered((response,))
    assert len({r['capability_invocation_id'] for r in responses}) == 3
    assert all(r['data']['records'] == [{'row': 0}, {'row': 1}] for r in responses)
    assert all(r['data']['next_offset'] == 2 for r in responses)
    assert all(r['artifact_read_receipt']['scope']['offset'] == 0 for r in responses)
    assert tools.stop_reason is None
    with pytest.raises(ValueError, match='incomplete'):
        tools._require_report_evidence_coverage((ref.artifact_id,))


@pytest.mark.asyncio
async def test_reread_after_all_pages_keeps_original_cursor_and_full_read_provenance(artifact_query_boundary):
    _sink, _binding, ref, tools = artifact_query_boundary
    for offset, limit in ((0, 2), (2, 13)):
        page = await tools.artifact_query(str(ref.artifact_id), 'TOP_N', limit, offset)
        assert page['success']
        tools.mark_feedback_delivered((page,))
    tools._require_report_evidence_coverage((ref.artifact_id,))
    reread = await tools.artifact_query(str(ref.artifact_id), 'TOP_N', 2)
    assert reread['success'], reread
    assert reread['data']['next_offset'] == 2 and reread['data']['returned_count'] == 2
    assert tools.stop_reason is None
    tools.mark_feedback_delivered((reread,))
    tools._require_report_evidence_coverage((ref.artifact_id,))


@pytest.mark.asyncio
async def test_native_reviewer_can_reread_evidence_then_submit_its_own_handoff(artifact_query_boundary):
    _sink, binding, ref, original = artifact_query_boundary
    allowlist = ('artifact_query', 'prepare_handoff')
    control = RuntimeWorkflowControlView(current_stage=WorkflowStage.VALIDATE,
        transition_targets=(WorkflowStage.INTERPRET,), request_user_input_available=False,
        retry_available=False, finish_available=False)
    stage = _stage_input().model_copy(update={'run_id': binding.run_id,
        'stage_id': binding.stage_id, 'invocation_id': binding.invocation_id,
        'workspace': binding.workspace, 'body': _body(binding.stage_id),
        'allowed_capabilities': allowlist, 'workflow_control': control})
    binding = replace(binding, actor_profile_key='coordinator', actor_agent_name='CoordinatorAgent',
        capability_allowlist=allowlist, workflow_control=control,
        decision_context_sha256=decision_context_sha256(stage))
    tools = LabBioRuntimeToolSet(binding, original.services)

    class Reviewer:
        binding = WorkflowProvider.binding
        calls = 0

        async def generate(self, request):
            self.calls += 1
            if self.calls <= 3:
                name, args = 'artifact_query', {'artifact_id': str(ref.artifact_id),
                    'view_type': 'TOP_N', 'limit': 2}
            elif self.calls == 4:
                responses = [json.loads(m.content)['content'] for m in request.messages if m.role == 'TOOL']
                name, args = 'prepare_handoff', {'observation_ids': [responses[-1]['capability_invocation_id']],
                    'summary': 'Reviewed this exact two-record scope, not the full table.',
                    'next_action': {'action': 'transition', 'target_stage': 'INTERPRET'}}
            else:
                assert self.calls == 5
                return ModelTurn(content='Scoped review handed off.', finish_reason=FinishReason.STOP,
                    usage=TokenUsage(total_tokens=1))
            return ModelTurn(tool_calls=(ToolCall(call_id=f'call-{self.calls}', name=name,
                arguments_json=json.dumps(args)),), finish_reason=FinishReason.TOOL_CALLS,
                usage=TokenUsage(total_tokens=1))

    reviewer = Reviewer()
    built = []
    def provider(_model):
        result = reviewer if not built else WorkflowProvider([])
        built.append(result)
        return result

    catalog = _capability_catalog()
    catalog.capabilities['coordinator-capabilities'] = catalog.capabilities[
        'coordinator-capabilities'].model_copy(update={'capability_allowlist': allowlist})
    invoker = NativeKernelStageInvoker(assembly=RuntimeStageAssemblySpec(
        stage_id=stage.stage_id, root_profile_key='coordinator', prompt_template_key='runtime-generic',
        capability_allowlist=allowlist, required_capabilities=('artifact_query',), max_capability_turns=8),
        factory=NativeRuntimeFactory(catalog, provider), principal=binding.principal,
        workspace=binding.workspace, services=tools.services)
    result = await invoker.invoke(stage)
    assert reviewer.calls == 5
    assert result.next_action.target_stage is WorkflowStage.INTERPRET
