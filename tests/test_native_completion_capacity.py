"""A slow work response must not consume mandatory completion capacity."""

import json

import pytest

from labbioagentos import (
    ArtifactExposureClass, ArtifactReleaseBasis, ArtifactRepresentation, CapabilityProfile,
    RuntimeStageInput, RuntimeStageResult, RuntimeWorkflowControlView,
    RuntimeWorkspaceIdentifiers, WorkflowStage,
)
from labbioagentos.kernel import KernelDeadlineExceededError
from labbioagentos.runtime.assembly import RuntimeStageAssemblySpec
from labbioagentos.runtime.native_stage import NativeKernelStageInvoker, NativeRuntimeFactory
from labbioagentos.runtime.observations import ObservationStore
from test_runtime_milestone_b import boundary, _toolset  # noqa: F401
from test_runtime_milestone_c2 import _catalog
from test_native_stage_application import WorkflowProvider, _body
from test_review_evidence_loop import turn


@pytest.mark.asyncio
@pytest.mark.parametrize('slow', (False, 'timeout', 'late'))
async def test_native_model_time_leaves_agent_owned_handoff_reachable(tmp_path, boundary, monkeypatch, slow):
    from labbioagentos.runtime import native_stage
    from labbioagentos.kernel import loop
    now = [1000.0]
    for module in (native_stage, loop):
        monkeypatch.setattr(module, 'monotonic', lambda: now[0])
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
        instruction='Read a synthetic record, then choose a legal handoff.',
        workspace=RuntimeWorkspaceIdentifiers(**tools.binding.workspace.model_dump()),
        allowed_capabilities=grants, workflow_control=RuntimeWorkflowControlView(
            current_stage=WorkflowStage.VALIDATE, transition_targets=(WorkflowStage.INTERPRET,),
            request_user_input_available=False, retry_available=False, finish_available=False))
    action = {'action':'transition', 'target_stage':'INTERPRET', 'reason':'Synthetic record checked.'}
    seen = {'work':0, 'timed_out':False, 'source':None, 'handoff':False}
    class Leader:
        binding = WorkflowProvider.binding
        async def generate(self, request):
            policy = json.loads(request.messages[1].content)['completion_capacity']
            assert policy['total_model_time_ms'] == 900000
            assert policy['reserved_within_shared_limits']['model_time_ms'] == 300000
            assert policy['reserved_within_shared_limits']['max_output_tokens'] == 8192
            assert policy['total_provider_turns'] == 16
            for message in request.messages:
                if message.role == 'TOOL':
                    data = json.loads(message.content).get('content') or {}
                    if (data.get('data') or {}).get('status') == 'READY':
                        return turn('Agent-selected evidence and action handed off.')
                    if data.get('success') and data.get('capability_invocation_id'):
                        seen['source'] = data['capability_invocation_id']
            names = {t.name for t in request.tools}
            if 'artifact_query' in names and (slow or seen['work'] < 2):
                seen['work'] += 1
                if slow and seen['work'] == 3:
                    seen['timed_out'] = True
                    now[0] = request.deadline_monotonic + .001
                    if slow == 'timeout':
                        raise KernelDeadlineExceededError()
                    return turn(name='artifact_query', arguments={'artifact_id':str(ref.artifact_id), 'view_type':'SUMMARY'})
                now[0] += (100 if seen['work'] == 1 else 450) if slow else 1
                return turn(name='artifact_query', arguments={'artifact_id':str(ref.artifact_id), 'view_type':'SUMMARY'})
            if slow:
                assert names == {'handoff_status', 'prepare_handoff'}
                assert request.max_output_tokens == 8192
            seen['handoff'] = True
            now[0] += 1
            return turn(name='prepare_handoff', arguments={'observation_ids':[seen['source']],
                'summary':'Actually inspected synthetic record; no scientific conclusion.', 'next_action':action})
    class Finalizer:
        binding = WorkflowProvider.binding
        async def generate(self, request):
            assert not request.tools
            return turn(RuntimeStageResult(stage_id=stage.stage_id, summary='Agent handoff.',
                body=_body(stage.stage_id), next_action=action).model_dump_json(exclude={'result_id'}))
    catalog = _catalog()
    catalog.models['runtime-default'] = catalog.models['runtime-default'].model_copy(
        update={'max_output_tokens':32768})
    catalog.capabilities['coordinator-capabilities'] = CapabilityProfile(
        profile_key='coordinator-capabilities', version='1', capability_allowlist=grants)
    providers = iter((Leader(), Finalizer()))
    boundaries = []
    invoker = NativeKernelStageInvoker(assembly=RuntimeStageAssemblySpec(stage_id=stage.stage_id,
        root_profile_key='coordinator', prompt_template_key='runtime-generic', capability_allowlist=grants,
        max_capability_turns=16, max_no_progress_seconds=900),
        factory=NativeRuntimeFactory(catalog, lambda _:next(providers)),
        principal=tools.binding.principal, workspace=tools.binding.workspace, services=tools.services,
        boundary_observer=lambda kind, value: boundaries.append((kind, value)))
    result = await invoker.invoke(stage)
    assert result.next_action.model_dump(mode='json') == action
    assert seen['handoff'] and seen['work'] == (3 if slow else 2)
    assert now[0] - 1000 < 900
    assert result.handoff_context is not None
    evidence = next(v for k,v in boundaries if k == 'capability_evidence')
    assert len([i for i in evidence.items if i.capability_name == 'artifact_query']) == 2
    assert seen['timed_out'] is bool(slow)


@pytest.mark.asyncio
async def test_report_can_register_required_delivery_inside_completion_window(tmp_path, boundary, monkeypatch):
    from labbioagentos import ReportSubmissionService
    from labbioagentos.kernel import BudgetDelta, BudgetLedger
    from labbioagentos.runtime import native_stage
    _, recorder, access, _, _, store, _ = boundary
    reports = ReportSubmissionService(store, access, trace_recorder=recorder)
    grants = ('artifact_query', 'report_submit', 'prepare_handoff', 'handoff_status')
    tools = _toolset(boundary, WorkflowStage.REPORT, grants, report_submission=reports,
        observation_store=ObservationStore(tmp_path/'observations'))
    ref = store.register(artifact_type='synthetic-table',
        exposure_class=ArtifactExposureClass.DERIVED,
        release_basis=ArtifactReleaseBasis.TRUSTED_EXECUTION_DECLASSIFICATION,
        representation=ArtifactRepresentation(records=({'record_type':'engineering'},), record_count=1),
        owner_user_id=tools.binding.principal.user_id, project_id=tools.binding.workspace.project_id,
        lab_id=tools.binding.workspace.lab_id, run_id=tools.binding.run_id)
    seen = {}
    stage = RuntimeStageInput(run_id=tools.binding.run_id, stage_id=WorkflowStage.REPORT,
        instruction='Register a report-only engineering delivery. No scientific claims.',
        workspace=RuntimeWorkspaceIdentifiers(**tools.binding.workspace.model_dump()),
        allowed_capabilities=grants, workflow_control=RuntimeWorkflowControlView(
            current_stage=WorkflowStage.REPORT, transition_targets=(WorkflowStage.LEARN,),
            request_user_input_available=False, retry_available=False, finish_available=False))
    def used(limits):
        ledger = BudgetLedger(limits)
        if limits.tool_calls:
            seen['ledger'] = ledger
        return ledger
    monkeypatch.setattr(native_stage, 'BudgetLedger', used)
    query = tools.services.artifact_exposure.artifact_query
    def shared_consumption(*args, **kwargs):
        value = query(*args, **kwargs)
        if not seen.get('consumed'):
            # Synthetic already-incurred shared cost, after a real governed read.
            seen['ledger'].reserve(BudgetDelta(wall_time_ms=610000))
            seen['consumed'] = True
        return value
    monkeypatch.setattr(tools.services.artifact_exposure, 'artifact_query', shared_consumption)
    action = {'action':'transition', 'target_stage':'LEARN'}
    class Leader:
        binding = WorkflowProvider.binding
        async def generate(self, request):
            responses = [json.loads(m.content).get('content') for m in request.messages if m.role == 'TOOL']
            if not responses:
                assert 'artifact_query' in {t.name for t in request.tools}
                return turn(name='artifact_query', arguments={'artifact_id':str(ref.artifact_id), 'view_type':'SUMMARY'})
            assert {t.name for t in request.tools} == {'report_submit','prepare_handoff','handoff_status'}
            last = responses[-1]
            assert last and last['success'], last
            if 'report_artifact_id' not in last['data'] and last['data'].get('status') != 'READY':
                return turn(name='report_submit', arguments={'title':'Engineering',
                    'report_text':'No scientific claims.', 'delivery_artifact_ids':[],
                    'evidence_references':[{'artifact_id':str(ref.artifact_id),
                        'observation_ids':[last['capability_invocation_id']]}]})
            if last['data'].get('status') == 'READY':
                return turn('Registered report and Agent-selected handoff complete.')
            seen['report'] = last['data']['report_artifact_id']
            return turn(name='prepare_handoff', arguments={'observation_ids':[last['capability_invocation_id']],
                'summary':'An actual report-only delivery was registered.', 'next_action':action})
    class Finalizer:
        binding = WorkflowProvider.binding
        async def generate(self, request):
            assert not request.tools
            selected = json.loads(request.messages[-1].content)['stage_decision_control']['next_action']
            return turn(json.dumps({'summary':'Registered.', 'report_artifact_id':seen['report'],
                'next_action':selected}))
    catalog = _catalog()
    catalog.capabilities['coordinator-capabilities'] = CapabilityProfile(
        profile_key='coordinator-capabilities', version='1', capability_allowlist=grants)
    providers = iter((Leader(),Finalizer()))
    invoker = NativeKernelStageInvoker(assembly=RuntimeStageAssemblySpec(stage_id=stage.stage_id,
        root_profile_key='coordinator', prompt_template_key='runtime-generic', capability_allowlist=grants,
        required_capabilities=('report_submit',), max_no_progress_seconds=900),
        factory=NativeRuntimeFactory(catalog, lambda _:next(providers)),
        principal=tools.binding.principal, workspace=tools.binding.workspace, services=tools.services)
    outcome = await invoker.invoke(stage)
    assert outcome.next_action.target_stage is WorkflowStage.LEARN
    assert outcome.body.report_reference.reference_id == seen['report']
    report = store.load_for_view(seen['report'])
    assert report.representation.stored_content == 'No scientific claims.'
    assert report.representation.summary['delivery_manifest']['outputs'] == []
