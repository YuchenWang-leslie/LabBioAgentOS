"""Real Native assembly counterexample and closure under the same total limit."""

import pytest

from labbioagentos import (
    ArtifactExposureClass, ArtifactReleaseBasis, ArtifactRepresentation, CapabilityProfile,
    RuntimeStageInput, RuntimeStageResult, RuntimeWorkflowControlView,
    RuntimeWorkspaceIdentifiers, WorkflowStage,
)
from labbioagentos.runtime.assembly import RuntimeStageAssemblySpec
from labbioagentos.runtime.errors import RuntimeIntegrationError
from labbioagentos.runtime.native_stage import NativeKernelStageInvoker, NativeRuntimeFactory
from labbioagentos.runtime.observations import ObservationStore
from test_runtime_milestone_b import boundary, _toolset  # noqa: F401
from test_runtime_milestone_c2 import _catalog
from test_native_stage_application import WorkflowProvider, _body
from test_review_evidence_loop import turn


@pytest.mark.asyncio
@pytest.mark.parametrize('reserved', [False, True])
async def test_native_work_limit_does_not_make_mandatory_handoff_unreachable(tmp_path, boundary, monkeypatch, reserved):
    import json
    from labbioagentos.runtime import native_stage
    if not reserved:
        original = native_stage.CollaborationRuntime
        def legacy(**kwargs):
            return original(**{**kwargs, 'closeout_tools':(), 'closeout_reserve':0})
        monkeypatch.setattr(native_stage, 'CollaborationRuntime', legacy)
    grants = ('artifact_query', 'handoff_status', 'prepare_handoff')
    tools = _toolset(boundary, WorkflowStage.VALIDATE, grants,
        observation_store=ObservationStore(tmp_path/'observations'))
    ref = tools.services.artifact_store.register(artifact_type='synthetic-table',
        exposure_class=ArtifactExposureClass.DERIVED,
        release_basis=ArtifactReleaseBasis.TRUSTED_EXECUTION_DECLASSIFICATION,
        representation=ArtifactRepresentation(records=({'record_type':'engineering'},), record_count=1),
        owner_user_id=tools.binding.principal.user_id, project_id=tools.binding.workspace.project_id,
        lab_id=tools.binding.workspace.lab_id, run_id=tools.binding.run_id)
    stage = RuntimeStageInput(run_id=tools.binding.run_id, stage_id=WorkflowStage.VALIDATE,
        instruction='Inspect a synthetic record and choose a legal next action.',
        workspace=RuntimeWorkspaceIdentifiers(**tools.binding.workspace.model_dump()),
        allowed_capabilities=grants, workflow_control=RuntimeWorkflowControlView(
            current_stage=WorkflowStage.VALIDATE, transition_targets=(WorkflowStage.INTERPRET,),
            request_user_input_available=False, retry_available=False, finish_available=False))
    action = {'action':'transition', 'target_stage':'INTERPRET'}
    seen = {'work':0, 'source':None}
    class Leader:
        binding = WorkflowProvider.binding
        async def generate(self, request):
            if request.messages[-1].role == 'TOOL':
                response = json.loads(request.messages[-1].content)['content']
                if response.get('data',{}).get('status') == 'READY':
                    return turn('Agent-authored bounded handoff complete.')
                if response['success']:
                    seen['source'] = response['capability_invocation_id']
            if 'artifact_query' in {t.name for t in request.tools}:
                seen['work'] += 1
                return turn(name='artifact_query', arguments={'artifact_id':str(ref.artifact_id), 'view_type':'SUMMARY'})
            return turn(name='prepare_handoff', arguments={'observation_ids':[seen['source']],
                'summary':'Synthetic record inspected; no scientific claim.', 'next_action':action})
    class Finalizer:
        binding = WorkflowProvider.binding
        async def generate(self, request):
            assert not request.tools
            return turn(RuntimeStageResult(stage_id=stage.stage_id, summary='Selected current handoff.',
                body=_body(stage.stage_id), next_action=action).model_dump_json(exclude={'result_id'}))
    catalog = _catalog()
    catalog.capabilities['coordinator-capabilities'] = CapabilityProfile(
        profile_key='coordinator-capabilities', version='1', capability_allowlist=grants)
    providers = iter((Leader(), Finalizer()))
    invoker = NativeKernelStageInvoker(assembly=RuntimeStageAssemblySpec(stage_id=stage.stage_id,
        root_profile_key='coordinator', prompt_template_key='runtime-generic', capability_allowlist=grants,
        max_capability_turns=16), factory=NativeRuntimeFactory(catalog, lambda _:next(providers)),
        principal=tools.binding.principal, workspace=tools.binding.workspace, services=tools.services)
    limits = invoker._capability_budget_limits().model_copy(update={'tool_calls':8, 'evidence_entries':8})
    monkeypatch.setattr(invoker, '_capability_budget_limits', lambda:limits)
    if not reserved:
        with pytest.raises(RuntimeIntegrationError) as error:
            await invoker.invoke(stage)
        assert error.value.error_code == 'HANDOFF_REQUIRED'
        assert seen['work'] == 9  # Ninth work request is denied before execution; no slot remains to close.
    else:
        result = await invoker.invoke(stage)
        assert result.next_action.target_stage is WorkflowStage.INTERPRET
        assert seen['work'] == 6
        assert result.handoff_context is not None


@pytest.mark.asyncio
async def test_execute_closeout_rephrasing_does_not_reopen_required_work(boundary, monkeypatch):
    grants = ('artifact_list', 'execution_submit', 'execution_revise', 'prepare_handoff', 'handoff_status')
    required = ('execution_submit', 'execution_revise')
    tools = _toolset(boundary, WorkflowStage.EXECUTE, grants)
    stage = RuntimeStageInput(run_id=tools.binding.run_id, stage_id=WorkflowStage.EXECUTE,
        instruction='Exercise the governed execution boundary.',
        workspace=RuntimeWorkspaceIdentifiers(**tools.binding.workspace.model_dump()),
        allowed_capabilities=grants, workflow_control=RuntimeWorkflowControlView(
            current_stage=WorkflowStage.EXECUTE, transition_targets=(WorkflowStage.VALIDATE,),
            request_user_input_available=False, retry_available=False, finish_available=False))
    work, stops = [], []

    class Leader:
        binding = WorkflowProvider.binding

        async def generate(self, request):
            if 'artifact_list' in {spec.name for spec in request.tools}:
                work.append(True)
                return turn(name='artifact_list', arguments={})
            stops.append(len(stops) + 1)
            return turn(f'My revised closing assessment {stops[-1]}.')

    catalog = _catalog()
    catalog.capabilities['coordinator-capabilities'] = CapabilityProfile(
        profile_key='coordinator-capabilities', version='1', capability_allowlist=grants)
    boundary_events = []
    invoker = NativeKernelStageInvoker(assembly=RuntimeStageAssemblySpec(stage_id=stage.stage_id,
        root_profile_key='coordinator', prompt_template_key='runtime-generic', capability_allowlist=grants,
        required_any_capabilities=required, max_capability_turns=16),
        factory=NativeRuntimeFactory(catalog, lambda _: Leader()), principal=tools.binding.principal,
        workspace=tools.binding.workspace, services=tools.services, trace_recorder=tools.services.trace_recorder,
        boundary_observer=lambda kind, value: boundary_events.append((kind, value)))
    limits = invoker._capability_budget_limits().model_copy(update={'tool_calls': 3, 'evidence_entries': 3})
    monkeypatch.setattr(invoker, '_capability_budget_limits', lambda: limits)
    with pytest.raises(RuntimeIntegrationError) as failure:
        await invoker.invoke(stage)
    assert failure.value.error_code == 'REQUIRED_CAPABILITY_INCOMPLETE'
    evidence = next(v for k, v in boundary_events if k == 'capability_evidence')
    assert [item.capability_name for item in evidence.items] == ['artifact_list']
    assert len(work) == 1 and len(stops) == 3
    assert evidence.termination_reason == 'COMPLETION_NO_PROGRESS'
    assert evidence.explicit_completion is None
