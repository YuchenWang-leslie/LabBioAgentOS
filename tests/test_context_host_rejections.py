"""Host rejections must not become model-authored, untraceable history."""

from dataclasses import replace
import json
from uuid import UUID, uuid4

import pytest

from labbioagentos.kernel import FinishReason, KernelEvent, KernelEventKind, ModelTurn, ToolCall
from labbioagentos.runtime import context
from labbioagentos.runtime.contracts import RuntimePriorResultView, RuntimeStageResult, runtime_stage_result_format
from labbioagentos.runtime.native_tools import NativeRuntimeCapabilityHost
from labbioagentos.runtime.native_stage import NativeKernelStageInvoker, NativeRuntimeFactory
from labbioagentos.runtime.observations import ObservationStore
from labbioagentos.runtime.tooling import LabBioRuntimeToolSet
from test_stage_decision_handoff import CONTINUE, decision_case
from test_native_stage_application import WorkflowProvider, _body, _capability_catalog


def rejection(identity, *, kind=KernelEventKind.TOOL_BATCH_VALIDATED, status='REJECTED', name='execution_submit'):
    return KernelEvent(kernel_run_id=uuid4(), invocation_id=identity, sequence=1,
        kind=kind, status_code=status, reason_code='INVALID_TOOL_ARGUMENTS',
        tool_name=name, validation_type='model_type', validation_fields=('review',))


@pytest.mark.asyncio
async def test_exact_kernel_rejection_counts_survive_finalizer_and_later_stage(tmp_path):
    stage, evidence, invoker, calls, tools = await decision_case(tmp_path)
    events = tuple(rejection(stage.invocation_id) for _ in range(4))
    groups = context.kernel_tool_rejections((*events, events[0],
        rejection(stage.invocation_id, status='ACCEPTED')))
    assert len(groups) == 1 and len(groups[0].event_ids) == 4
    assert groups[0].validation_fields == ('review',)
    evidence = evidence.model_copy(update={'tool_rejections': groups})
    result = await invoker.finalize_recovered(stage, evidence)
    assert any('tool_rejections' in message.content for message in calls[0].messages)
    assert result.tool_rejections == groups
    restored = RuntimeStageResult.model_validate_json(result.model_dump_json())
    prior = RuntimePriorResultView.from_result(restored)
    current = stage.model_copy(update={'allowed_capabilities': ('stage_context_read',), 'prior_results': (prior,)})
    projection = context.stage_context_projection(current)
    assert projection['prior_tool_rejections'][0]['rejected_batches'] == 4
    assert projection['prior_tool_rejections'][0]['authority'] == 'CONTROL_STATE'
    assert projection['prior_tool_rejections'][0]['source_result_id'] == str(result.result_id)
    assert 'tool_rejections' not in runtime_stage_result_format(stage.stage_id).model_json_schema()['properties']
    legacy = stage.model_copy(update={'prior_results': (prior,)})
    assert 'prior_tool_rejections' not in context.stage_context_projection(legacy)
    assert 'tool_rejections' not in context.stage_context_projection(legacy)['prior_results'][0]


@pytest.mark.asyncio
async def test_kernel_rejections_survive_without_a_prepared_handoff(tmp_path):
    stage, evidence, invoker, calls, tools = await decision_case(tmp_path)
    result = await invoker.finalize_recovered(stage, evidence)
    groups = context.kernel_tool_rejections((rejection(stage.invocation_id),))
    host_only = context.attach_handoff_context(result.model_copy(update={'handoff_context': None}),
        stage, evidence.model_copy(update={'prepared_handoff': None, 'tool_rejections': groups}))
    assert host_only.handoff_context is None and host_only.tool_rejections == groups


@pytest.mark.asyncio
async def test_native_counts_actual_rejected_batches_without_copying_rejected_values(tmp_path):
    stage, evidence, invoker, calls, tools = await decision_case(tmp_path)

    class Provider(WorkflowProvider):
        turns = 0

        async def generate(self, request):
            if not request.tools:
                control = next(json.loads(m.content) for m in request.messages
                    if m.content and m.content.startswith('{') and 'capability_evidence' in m.content)
                assert len(control['capability_evidence']['tool_rejections'][0]['event_ids']) == 4
                assert 'PRIVATE_PARAMETER_VALUE' not in json.dumps(control)
                return ModelTurn(finish_reason=FinishReason.STOP, content=json.dumps({
                    'stage_id': stage.stage_id.value, 'summary': 'Host facts retained.',
                    'body': _body(stage.stage_id).model_dump(mode='json'), 'next_action': CONTINUE}))
            self.turns += 1
            if self.turns <= 4:
                name, args = 'artifact_list', {'private_unknown_field': 'PRIVATE_PARAMETER_VALUE'}
            elif self.turns == 5:
                name, args = 'artifact_list', {}
            elif self.turns == 6:
                response = json.loads(request.messages[-1].content)['content']
                name, args = 'prepare_handoff', {'observation_ids': [response['capability_invocation_id']],
                    'summary': 'The Agent assessed the actual feedback.', 'next_action': CONTINUE}
            else:
                return ModelTurn(content='Done.', finish_reason=FinishReason.STOP)
            return ModelTurn(tool_calls=(ToolCall(call_id=str(self.turns), name=name,
                arguments_json=json.dumps(args)),), finish_reason=FinishReason.TOOL_CALLS)

    provider = Provider([])
    catalog = _capability_catalog()
    catalog.capabilities['coordinator-capabilities'] = catalog.capabilities['coordinator-capabilities'].model_copy(
        update={'capability_allowlist': stage.allowed_capabilities})
    native = NativeKernelStageInvoker(assembly=invoker.assembly,
        factory=NativeRuntimeFactory(catalog, lambda _model: provider),
        principal=tools.binding.principal, workspace=tools.binding.workspace, services=tools.services)
    completed = await native.invoke(stage)
    assert len(completed.tool_rejections[0].event_ids) == 4 and provider.turns == 7
    assert completed.tool_rejections[0].validation_fields == ('unknown_field',)


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['none', 'revoke', 'result_removed'])
async def test_selected_context_page_is_reauthorized_without_source_read_credit(tmp_path, change):
    stage, evidence, invoker, calls, tools = await decision_case(tmp_path)
    result = await invoker.finalize_recovered(stage, evidence)
    services = replace(tools.services, observation_store=ObservationStore(tmp_path / 'observations'))
    binding = replace(tools.binding, invocation_id=uuid4(), workflow_control=None,
        decision_context_sha256=None, prior_results=(RuntimePriorResultView.from_result(result),),
        capability_allowlist=('stage_context_read', 'evidence_read', 'prepare_handoff'))
    author = LabBioRuntimeToolSet(binding, services)
    page = await author.stage_context_read(str(result.result_id), limit=40)
    author.mark_feedback_delivered((page,))
    prepared = await author.prepare_handoff([page['capability_invocation_id']], 'Selected original model context page.')
    assert prepared['success']
    carried = context.attach_handoff_context(result, stage,
        evidence.model_copy(update={'prepared_handoff': author.prepared_handoff})).handoff_context
    grants = ('evidence_read',) if change == 'revoke' else binding.capability_allowlist
    reader = NativeRuntimeCapabilityHost(replace(binding, invocation_id=uuid4(), capability_allowlist=grants,
        prior_results=() if change == 'result_removed' else binding.prior_results), services)
    messages, unavailable = reader.prepare_context_handoff(carried)
    if change != 'none':
        assert not messages and unavailable[0]['read_credit'] is False
        return
    assert not unavailable and json.loads(messages[0].content)['observation'] == page
    reader.mark_context_delivered(messages)
    item = reader._host._delivered_observations[UUID(page['capability_invocation_id'])]
    assert item.artifact_read_receipt is None and item.information_authority.value == 'MODEL_CONTEXT'
