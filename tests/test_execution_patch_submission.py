"""Model-authored exact edits reuse a verified contract, not an inferred repair."""
from dataclasses import replace
from uuid import uuid4

import pytest

from labbioagentos.execution.models import ExecutionSourceEdit
from labbioagentos.execution.submission import ExecutionInspectionError
from test_execution_inspection import inspection, _submit, _inspect  # noqa: F401
from test_runtime_milestone_b import boundary  # noqa: F401


@pytest.mark.asyncio
async def test_revision_uses_explicit_edits_and_preserves_original(inspection, boundary):
    service, executor, run_id = inspection
    _, _, _, principal, workspace, _, _ = boundary
    first = await _submit(inspection, boundary, 'x = 1\nprint(x)\n')
    second = await service.revise(first.execution_id, first.script_hash,
        (ExecutionSourceEdit(old='x = 1', new='x = 2'),),
        principal=principal, workspace=workspace, run_id=run_id,
        stage_id=__import__('labbioagentos').WorkflowStage.EXECUTE, invocation_id=uuid4())
    assert executor.plans[-1].script_content == 'x = 2\nprint(x)\n'
    assert _inspect(inspection, boundary, first.execution_id)['submitted_program']['source'] == 'x = 1\nprint(x)\n'
    assert second.script_hash != first.script_hash
    assert _inspect(inspection, boundary, second.execution_id)['submitted_contract']['image_key'] == 'approved'


@pytest.mark.asyncio
@pytest.mark.parametrize('old,hash_override', [('missing', None), ('x', None), ('x = 1', '0'*64)])
async def test_bad_patch_never_runs_or_guesses(inspection, boundary, old, hash_override):
    service, executor, run_id = inspection
    _, _, _, principal, workspace, _, _ = boundary
    first = await _submit(inspection, boundary, 'x = 1\nprint(x)\n')
    with pytest.raises(ExecutionInspectionError):
        await service.revise(first.execution_id, hash_override or first.script_hash,
            (ExecutionSourceEdit(old=old, new='z'),), principal=principal,
            workspace=workspace, run_id=run_id,
            stage_id=__import__('labbioagentos').WorkflowStage.EXECUTE, invocation_id=uuid4())
    assert len(executor.plans) == 1


@pytest.mark.asyncio
async def test_registered_checkpoint_is_explicitly_reusable_only_in_its_own_run(inspection, boundary):
    from labbioagentos import ExecutionPlanDraft, WorkflowStage
    from labbioagentos.execution.errors import ExecutionInputSelectionError
    service, executor, run_id = inspection
    _, _, _, principal, workspace, _, _ = boundary
    first = await _submit(inspection, boundary)
    checkpoint_id = next(iter(first.registered_outputs))
    draft = ExecutionPlanDraft(image_key='approved', script_content='pass\n',
                               input_artifact_ids=(checkpoint_id,))
    args = dict(principal=principal, workspace=workspace, stage_id=WorkflowStage.EXECUTE,
                invocation_id=uuid4(), mountable_input_artifact_ids=())
    await service.submit(draft, run_id=run_id, **args)
    assert executor.plans[-1].input_artifact_ids == (checkpoint_id,)
    with pytest.raises(ExecutionInputSelectionError):
        await service.submit(draft, run_id=uuid4(), **args)
    assert len(executor.plans) == 2


@pytest.mark.asyncio
async def test_real_tool_schema_revision_receipt_grounding_and_source_free_audit(inspection, boundary):
    from native_test_provider import LocalProvider
    from labbioagentos import LabBioRuntimeToolSet, WorkflowStage
    from labbioagentos.runtime.contracts import CapabilityEvidenceBundle, RuntimeExecutionActivity
    from labbioagentos.runtime.execution_grounding import execution_result_control
    from test_runtime_milestone_b import _toolset
    from labbioagentos.runtime.contracts import RuntimeStageInput, RuntimeWorkspaceIdentifiers

    service, executor, run_id = inspection
    original = _toolset(boundary, WorkflowStage.EXECUTE,
        ('execution_submit', 'execution_revise', 'execution_inspect'), execution_submission=service)
    tools = LabBioRuntimeToolSet(replace(original.binding, run_id=run_id), original.services)
    provider = LocalProvider(tools)
    await provider.initialize()
    schemas = {item.name: item for item in await provider.list_tools()}
    assert 'execution_revise' in schemas
    first = await provider.call_tool('execution_submit', {
        'image_key': 'approved', 'script_content': 'value = 1\nprint(value)\n'})
    receipt = first['data']
    revised = await provider.call_tool('execution_revise', {
        'execution_id': receipt['execution_id'], 'expected_script_hash': receipt['script_hash'],
        'edits': [{'old': 'value = 1', 'new': 'value = 2'}]})
    assert revised['success'] and revised['data']['status'] == 'SUCCEEDED'
    evidence = CapabilityEvidenceBundle(run_id=run_id, stage_id=WorkflowStage.EXECUTE,
        invocation_id=tools.binding.invocation_id, items=tools.evidence_items())
    assert len(RuntimeExecutionActivity.from_evidence(evidence).receipts) == 2
    stage = RuntimeStageInput(run_id=run_id, stage_id=WorkflowStage.EXECUTE,
        invocation_id=tools.binding.invocation_id, instruction='Fixture revision.',
        workspace=RuntimeWorkspaceIdentifiers(**tools.binding.workspace.model_dump()),
        allowed_capabilities=tools.binding.capability_allowlist)
    assert len(execution_result_control(stage, evidence)['completed_execution_receipts']) == 2
    assert 'value = ' not in evidence.model_dump_json()
    events = boundary[0].read(run_id)
    planned = [e for e in events if e.event_type.value == 'EXECUTION_PLANNED']
    assert planned[-1].payload['revision_of'] == receipt['execution_id']
    denied = await provider.call_tool('execution_revise', {
        'execution_id': revised['data']['execution_id'], 'expected_script_hash': receipt['script_hash'], 'edits': []})
    assert not denied['success'] and denied['error']['error_code'] == 'REVISION_HASH_MISMATCH'
    assert len(executor.plans) == 2
