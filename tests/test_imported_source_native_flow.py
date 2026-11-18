"""Cross-run source edits through actual native schemas, host and receipt binding.

Generic programs only. No model, network, container or scientific result claim.
"""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

import pytest

from labbioagentos import (
    ArtifactRepresentation, ExecutionPlanDraft, ExecutionSubmissionService,
    LabBioRuntimeToolSet, WorkflowStage,
)
from labbioagentos.kernel import FinishReason, ToolBatchRejectedError, ToolCall
from labbioagentos.runtime.native_tools import (
    NativeCapabilityBinding, NativeCapabilityDispatcher, NativeRuntimeCapabilityHost,
    build_labbio_tool_registry,
)
from test_execution_inspection import inspection  # noqa: F401
from test_runtime_milestone_b import boundary, _toolset  # noqa: F401

SOURCE = '# SOURCE_PRIVATE_CANARY café\r\nvalue = 11\r\nprint(value)\r\n'


async def source_case(inspection, boundary):
    service, executor, old_run = inspection
    _, recorder, access, principal, workspace, store, _ = boundary
    old = await service.submit(
        ExecutionPlanDraft(image_key='approved', script_content=SOURCE,
                           parameters={'old_only': True}),
        principal=principal, workspace=workspace, run_id=old_run,
        stage_id=WorkflowStage.EXECUTE, invocation_id=uuid4(),
    )
    original = executor.script_refs[old.execution_id]
    imported = store.register_file(
        Path(original.storage_locator), artifact_type='execution-script',
        exposure_class=original.exposure_class, representation=ArtifactRepresentation(),
        owner_user_id=principal.user_id, project_id=workspace.project_id,
        lab_id=workspace.lab_id, run_id=old_run, stage_id=WorkflowStage.EXECUTE,
        metadata={'execution_id': str(old.execution_id), 'sha256': old.script_hash},
    )
    fresh = ExecutionSubmissionService(
        artifact_store=store, access_service=access, executor=executor,
        trace_recorder=recorder,
    )
    current = uuid4()
    base = _toolset(boundary, WorkflowStage.EXECUTE,
                   ('execution_inspect', 'execution_submit', 'execution_revise'),
                   execution_submission=fresh)
    tools = LabBioRuntimeToolSet(replace(
        base.binding, run_id=current,
        mountable_input_artifact_ids=(imported.artifact_id,)), base.services)
    host = NativeRuntimeCapabilityHost(tools.binding, tools.services)
    registry = build_labbio_tool_registry(host)
    binding = NativeCapabilityBinding(
        run_id=current, stage_id=WorkflowStage.EXECUTE,
        invocation_id=tools.binding.invocation_id,
        actor_profile_key=tools.binding.actor_profile_key,
        actor_agent_name=tools.binding.actor_agent_name,
        capability_allowlist=tools.binding.capability_allowlist,
    )
    dispatcher = NativeCapabilityDispatcher(binding=binding, registry=registry)
    return fresh, executor, current, old, imported, host, registry, dispatcher


async def dispatch(dispatcher, name, payload):
    outcomes = await dispatcher.dispatch_batch((ToolCall(
        call_id=str(uuid4()), name=name,
        arguments_json=json.dumps(payload, ensure_ascii=False),
    ),), finish_reason=FinishReason.TOOL_CALLS)
    assert len(outcomes) == 1
    assert outcomes[0].result.content is not None, outcomes[0].result.model_dump(mode='json')
    return outcomes[0].result.content


def revision_payload(imported, digest):
    return {
        'image_key': 'approved', 'input_artifact_ids': [],
        'requested_outputs': [], 'parameters': {'current_only': True},
        'resources': {'cpus': 1.0, 'memory_mb': 512, 'pids_limit': 32,
                      'timeout_seconds': 30.0},
        'network_required': False,
        'source_revision': {
            'source_artifact_id': str(imported.artifact_id),
            'expected_script_hash': digest,
            'edits': [{'old': 'value = 11', 'new': 'value = 17'}],
        },
    }


@pytest.mark.asyncio
async def test_native_inspect_then_exact_source_edit_keeps_original_and_fresh_receipt(
    inspection, boundary,
):
    fresh, executor, current, old, imported, host, _, dispatcher = await source_case(
        inspection, boundary)
    inspected = await dispatch(dispatcher, 'execution_inspect',
                               {'execution_id': str(old.execution_id)})
    assert inspected['success']
    assert inspected['data']['receipt'] is None
    assert inspected['data']['submitted_contract'] is None
    assert inspected['data']['submitted_program']['source'] == SOURCE

    result = await dispatch(dispatcher, 'execution_submit',
                            revision_payload(imported, old.script_hash))
    assert result['success'], result
    receipt = result['data']
    plan = executor.plans[-1]
    expected = SOURCE.replace('value = 11', 'value = 17', 1)
    assert len(executor.plans) == 2
    assert plan.script_content.encode() == expected.encode()
    assert plan.run_id == current and plan.run_id != executor.plans[0].run_id
    assert str(plan.execution_id) == receipt['execution_id']
    assert plan.execution_id != old.execution_id
    assert receipt['script_hash'] == sha256(expected.encode()).hexdigest()
    assert plan.parameters == {'current_only': True}
    assert plan.input_artifact_ids == ()
    assert not plan.network_required
    assert Path(imported.storage_locator).read_bytes() == SOURCE.encode()
    assert Path(executor.script_refs[old.execution_id].storage_locator).read_bytes() == SOURCE.encode()

    own_page = fresh.inspect(plan.execution_id, principal=host._host.binding.principal,
                             workspace=host._host.binding.workspace, run_id=current)
    assert own_page['receipt']['execution_id'] == receipt['execution_id']
    assert own_page['submitted_program']['source'] == expected
    # Reading/patching old source never imports the old result as current success.
    receipts = [x.safe_result for x in host.evidence_items()
                if x.capability_name == 'execution_submit' and x.status.value == 'COMPLETED']
    assert len(receipts) == 1 and receipts[0]['execution_id'] == receipt['execution_id']
    persisted = json.dumps(
        [x.model_dump(mode='json') for x in host.evidence_items()] +
        [x.model_dump(mode='json') for x in boundary[0].read(current)])
    assert 'SOURCE_PRIVATE_CANARY' not in persisted
    assert 'value = 11' not in persisted and 'value = 17' not in persisted


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['unimported', 'stale_hash', 'absent_match'])
async def test_native_source_edits_reject_before_execution_without_mutating_base(
    inspection, boundary, fault,
):
    _, executor, _, old, imported, _, _, dispatcher = await source_case(inspection, boundary)
    payload = revision_payload(imported, old.script_hash)
    if fault == 'unimported':
        payload['source_revision']['source_artifact_id'] = str(uuid4())
    elif fault == 'stale_hash':
        payload['source_revision']['expected_script_hash'] = '0' * 64
    else:
        payload['source_revision']['edits'][0]['old'] = 'ABSENT_REPLACEMENT_CANARY'
    result = await dispatch(dispatcher, 'execution_submit', payload)
    assert not result['success'], result
    assert len(executor.plans) == 1
    assert Path(imported.storage_locator).read_bytes() == SOURCE.encode()
    encoded = json.dumps(result)
    assert 'SOURCE_PRIVATE_CANARY' not in encoded
    assert 'ABSENT_REPLACEMENT_CANARY' not in encoded


@pytest.mark.asyncio
async def test_native_source_and_literal_program_are_mutually_exclusive(
    inspection, boundary,
):
    _, executor, _, old, imported, _, registry, _ = await source_case(inspection, boundary)
    payload = revision_payload(imported, old.script_hash)
    payload['script_content'] = "print('literal')"
    with pytest.raises(ToolBatchRejectedError):
        registry.validate_batch((ToolCall(call_id='ambiguous-source', name='execution_submit',
            arguments_json=json.dumps(payload)),),
            finish_reason=FinishReason.TOOL_CALLS, max_calls=1)
    assert len(executor.plans) == 1


def test_published_native_inspection_and_submission_identify_source_mode():
    registry = build_labbio_tool_registry(object())
    specs = {x.name: x for x in registry.specs}
    inspect = specs['execution_inspect']
    assert 'import' in inspect.description.lower()
    assert 'source' in inspect.description.lower()
    submit = specs['execution_submit']
    assert 'source_revision' in submit.parameters['properties']
    assert 'source_revision' in submit.description

@pytest.mark.parametrize('omitted', [None, 'input_artifact_ids', 'requested_outputs'])
def test_source_mode_keeps_data_bearing_required_selections(omitted):
    from labbioagentos import ExecutionRuntime, RequestedResources, RuntimeExecutionCapabilityView
    source_id = uuid4()
    view = RuntimeExecutionCapabilityView(
        runtime=ExecutionRuntime.PYTHON, image_key='approved',
        resources=RequestedResources(), network_required=False,
        minimum_queryable_output_count=1,
        mountable_input_artifact_ids=(source_id,))
    registry = build_labbio_tool_registry(object(), view)
    payload = {
        'image_key': 'approved', 'input_artifact_ids': [str(source_id)],
        'requested_outputs': [{
            'relative_path': 'result.jsonl', 'artifact_type': 'analysis-result',
            'requested_exposure': 'DERIVED', 'output_contract_id': 'fixture-contract'}],
        'parameters': {}, 'resources': RequestedResources().model_dump(mode='json'),
        'source_revision': {'source_artifact_id': str(source_id),
                            'expected_script_hash': '0' * 64, 'edits': []},
    }
    if omitted is not None:
        payload.pop(omitted)
    call = ToolCall(call_id='data-bearing-source', name='execution_submit',
                    arguments_json=json.dumps(payload))
    if omitted is None:
        assert registry.validate_batch((call,), finish_reason=FinishReason.TOOL_CALLS,
                                       max_calls=1)
    else:
        with pytest.raises(ToolBatchRejectedError):
            registry.validate_batch((call,), finish_reason=FinishReason.TOOL_CALLS,
                                    max_calls=1)
