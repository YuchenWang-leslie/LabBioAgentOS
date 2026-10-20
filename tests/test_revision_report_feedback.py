"""Pending regressions for safe correction feedback; no scientific fixtures."""
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from native_test_provider import LocalProvider
from pydantic import ValidationError

from labbioagentos import LabBioRuntimeToolSet, WorkflowStage
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401
from test_execution_inspection import inspection, _submit  # noqa: F401
from test_runtime_milestone_b import boundary, _toolset  # noqa: F401


@pytest.mark.asyncio
async def test_bad_revision_fields_are_actionable_without_source_leak(inspection, boundary):
    service, executor, run_id = inspection
    first = await _submit(inspection, boundary, 'value = 1\nprint(value)\n')
    base = _toolset(boundary, WorkflowStage.EXECUTE,
        ('execution_revise',), execution_submission=service)
    tools = LabBioRuntimeToolSet(replace(base.binding, run_id=run_id), base.services)
    provider = LocalProvider(tools)
    await provider.initialize()
    with pytest.raises(ValidationError):
        await provider.call_tool('execution_revise', {
            'execution_id': str(first.execution_id), 'expected_script_hash': first.script_hash,
            'edits': [{'old': 'PRIVATE_SOURCE', 'PRIVATE_KEY': 'PRIVATE_VALUE'}],
        })
    assert tools.evidence_items() == ()
    assert len(executor.plans) == 1
    corrected = await provider.call_tool('execution_revise', {
        'execution_id': str(first.execution_id), 'expected_script_hash': first.script_hash,
        'edits': [{'old': 'value = 1', 'new': 'value = 2'}],
    })
    assert corrected['success'] and len(executor.plans) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('override,field', [
    ({'execution_id': 'PRIVATE_NOT_UUID'}, 'execution_id'),
    ({'expected_script_hash': 'PRIVATE_NOT_HASH'}, 'expected_script_hash'),
    ({'resources': {'memory_mb': 0}}, 'resources.memory_mb'),
    ({'edits': [{'old': 'x', 'new': 'y'}] * 33}, 'edits'),
])
async def test_revision_rejects_invalid_contract_before_execution(inspection, boundary, override, field):
    service, executor, run_id = inspection
    first = await _submit(inspection, boundary)
    base = _toolset(boundary, WorkflowStage.EXECUTE,
        ('execution_revise',), execution_submission=service)
    tools = LabBioRuntimeToolSet(replace(base.binding, run_id=run_id), base.services)
    result = await tools.execution_revise(**{
        'execution_id': str(first.execution_id), 'expected_script_hash': first.script_hash, **override})
    assert result['error']['error_code'] == 'INVALID_EXECUTION_REVISION'
    assert field in {item['field'] for item in result['error']['revision_validation']['issues']}
    assert 'PRIVATE_' not in str(result) and len(executor.plans) == 1


@pytest.mark.asyncio
async def test_report_check_collects_all_reference_problems_without_registering(artifact_query_boundary):
    _, binding, ref, original = artifact_query_boundary
    calls = []
    def submit(**kwargs):
        calls.append(kwargs)
        return {'status': 'REGISTERED'}
    tools = LabBioRuntimeToolSet(replace(binding, stage_id=WorkflowStage.REPORT,
        capability_allowlist=('artifact_query', 'report_check', 'report_submit')),
        replace(original.services, report_submission=SimpleNamespace(submit=submit)))
    unknown = str(uuid4())
    result = await tools.report_check([str(ref.artifact_id), unknown, 'PRIVATE_NOT_A_UUID'])
    check = result['data']
    assert result['success'] and not check['ready'] and not calls
    assert [item['status'] for item in check['items']] == [
        'REPORT_EVIDENCE_INCOMPLETE', 'ARTIFACT_NOT_FOUND', 'INVALID_IDENTIFIER']
    assert check['items'][0]['next_offset'] == 0
    assert check['items'][0]['suggested_limit'] == check['items'][0]['record_count'] == 15
    assert check['items'][1]['suggested_limit'] is None
    assert check['items'][1]['artifact_id'] == unknown
    assert check['items'][2]['artifact_id'] is None
    assert 'PRIVATE_NOT_A_UUID' not in str(result)
    failed = await tools.report_submit('Synthetic', 'Synthetic report',
                                       [str(ref.artifact_id), unknown])
    assert len(failed['error']['report_evidence']['items']) == 2 and not calls
    assert tools.evidence_items()[-1].error_details.report_evidence is not None
    first = await tools.artifact_query(str(ref.artifact_id), 'TOP_N', 10)
    tools.mark_feedback_delivered((first,))
    partially_ready = await tools.report_check([str(ref.artifact_id)])
    assert partially_ready['data']['items'][0]['next_offset'] == 10
    assert partially_ready['data']['items'][0]['suggested_limit'] == 5
    rest = await tools.artifact_query(str(ref.artifact_id), 'TOP_N', 5, offset=10)
    tools.mark_feedback_delivered((rest,))
    ready = await tools.report_check([str(ref.artifact_id)])
    assert ready['data']['ready'] and not calls
    assert ready['data']['items'][0]['suggested_limit'] is None
    accepted = await tools.report_submit('Synthetic', 'Synthetic report', [str(ref.artifact_id)])
    assert accepted['success'] and len(calls) == 1


@pytest.mark.asyncio
async def test_report_check_preserves_reference_kind_and_duplicate_failures(artifact_query_boundary):
    from labbioagentos.runtime.contracts import RuntimeReference, RuntimeReferenceKind
    _, binding, ref, original = artifact_query_boundary
    execution_id = str(uuid4())
    tools = LabBioRuntimeToolSet(replace(binding, stage_id=WorkflowStage.REPORT,
        capability_allowlist=('report_check',), context_references=(
            RuntimeReference(kind=RuntimeReferenceKind.EXECUTION, reference_id=execution_id),)),
        original.services)
    result = await tools.report_check([execution_id, str(ref.artifact_id), str(ref.artifact_id)])
    assert result['data']['items'][0]['status'] == 'INVALID_REFERENCE_KIND'
    assert result['data']['items'][0]['reference_kind'] == 'EXECUTION'
    assert result['data']['items'][2]['status'] == 'DUPLICATE_REFERENCE'
    assert not result['data']['ready']


@pytest.mark.asyncio
async def test_report_check_does_not_disclose_foreign_record_counts(artifact_query_boundary):
    from labbioagentos import ArtifactRepresentation
    _, binding, ref, original = artifact_query_boundary
    foreign = original.services.artifact_store.register(
        artifact_type='table', exposure_class=ref.exposure_class, release_basis=ref.release_basis,
        representation=ArtifactRepresentation(records=({'private': 'PRIVATE_CONTENT'},), record_count=1),
        owner_user_id='other-user', project_id='other-project', lab_id=binding.workspace.lab_id)
    tools = LabBioRuntimeToolSet(replace(binding, stage_id=WorkflowStage.REPORT,
        capability_allowlist=('report_check',)), original.services)
    result = await tools.report_check([str(foreign.artifact_id)])
    item = result['data']['items'][0]
    assert item['status'] == 'AUTHORIZATION_DENIED'
    assert item['record_count'] is None and item['next_offset'] is None
    assert 'PRIVATE_CONTENT' not in str(result)
