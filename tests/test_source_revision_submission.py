"""Explicit imported-source CAS creates a fresh submission, never an old receipt."""

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from labbioagentos import ArtifactExposureClass, ArtifactRepresentation, LabBioRuntimeToolSet, WorkflowStage
from labbioagentos.execution import models
from labbioagentos.execution import JobLedger
from labbioagentos.execution.submission import ExecutionSubmissionError
from test_execution_inspection import inspection, _inspect  # noqa: F401
from test_execution_submission_persistence import _persistent
from test_runtime_milestone_b import boundary, _toolset  # noqa: F401


SOURCE = '# synthetic-source-café\r\nVALUE = 1\r\nprint(VALUE)\r\n'


def _base(inspection, boundary, tmp_path, *, source=SOURCE, metadata=None, owner=None,
          project=None, lab=None, artifact_type='execution-script', stage=WorkflowStage.EXECUTE,
          omit_metadata=()):
    _service, _executor, _run_id = inspection
    _sink, _recorder, _access, principal, workspace, store, _policy = boundary
    producer_id = uuid4()
    path = tmp_path / f'{producer_id}.py'
    path.write_bytes(source.encode('utf-8'))
    source_metadata = {'execution_id': str(producer_id), 'sha256': sha256(source.encode()).hexdigest(),
                       **(metadata or {})}
    for field in omit_metadata:
        source_metadata.pop(field, None)
    ref = store.register_file(path, artifact_type=artifact_type,
        exposure_class=ArtifactExposureClass.RAW, representation=ArtifactRepresentation(),
        owner_user_id=owner or principal.user_id, project_id=project or workspace.project_id,
        lab_id=lab or workspace.lab_id, run_id=uuid4(), stage_id=stage, metadata=source_metadata)
    return ref, producer_id


def _payload(ref, *, edits=None):
    return {'source_revision': {'source_artifact_id': str(ref.artifact_id),
                'expected_script_hash': ref.metadata.get('sha256', 'a' * 64),
                'edits': [{'old': 'VALUE = 1', 'new': 'VALUE = 2'}] if edits is None else edits},
            'image_key': 'approved', 'resources': {}, 'input_artifact_ids': [],
            'parameters': {'chosen': 'current'}, 'requested_outputs': [], 'network_required': False}


def _intent(payload):
    # The frozen pre-feature consumer rejects this actual source-mode payload
    # through its existing canonical model, rather than failing an import.
    model = getattr(models, 'SourceRevisionExecutionDraft', models.ExecutionPlanDraft)
    return model.model_validate(payload)


async def _submit_source(service, payload, **scope):
    draft = _intent(payload)
    return await service.submit_source_revision(draft, **scope)


def _scope(inspection, boundary, ref):
    _service, _executor, run_id = inspection
    _sink, _recorder, _access, principal, workspace, _store, _policy = boundary
    return dict(principal=principal, workspace=workspace, run_id=run_id,
        stage_id=WorkflowStage.EXECUTE, invocation_id=uuid4(),
        mountable_input_artifact_ids=(ref.artifact_id,))


@pytest.mark.asyncio
@pytest.mark.parametrize('empty_edits', [False, True])
async def test_imported_source_exact_edits_preserve_bytes_and_create_fresh_intent(
        inspection, boundary, tmp_path, empty_edits):
    service, executor, _run_id = inspection
    ref, producer = _base(inspection, boundary, tmp_path)
    before = Path(ref.storage_locator).read_bytes()
    payload = _payload(ref, edits=[] if empty_edits else None)
    receipt = await _submit_source(service, payload, **_scope(inspection, boundary, ref))
    expected = SOURCE if empty_edits else SOURCE.replace('VALUE = 1', 'VALUE = 2')
    assert len(executor.plans) == 1
    plan = executor.plans[0]
    assert plan.script_content.encode() == expected.encode()
    assert receipt.execution_id != producer
    assert receipt.script_hash == sha256(expected.encode()).hexdigest()
    assert plan.input_artifact_ids == ()  # Source adoption is not an implicit mount.
    assert plan.parameters == {'chosen': 'current'}
    assert Path(ref.storage_locator).read_bytes() == before
    page = _inspect(inspection, boundary, receipt.execution_id)
    assert page['submitted_program']['source'] == expected
    origin = page['source_revision_origin']
    assert origin['source_artifact_id'] == str(ref.artifact_id)
    assert origin['source_execution_id'] == str(producer)
    assert origin['source_run_id'] == str(ref.run_id)
    assert origin['source_sha256'] == ref.metadata['sha256']
    assert origin['edit_count'] == int(not empty_edits)
    planned = [event for event in boundary[0].read(plan.run_id)
               if event.event_type.value == 'EXECUTION_PLANNED'][-1]
    assert planned.payload['source_revision_origin'] == origin
    assert 'VALUE = ' not in json.dumps(planned.payload)
    assert 'synthetic-source' not in json.dumps(planned.payload)


@pytest.mark.asyncio
@pytest.mark.parametrize('condition', ['not_imported', 'no_advertised_scope', 'declared_output',
    'wrong_owner', 'wrong_project', 'wrong_lab', 'wrong_type', 'wrong_stage',
    'missing_producer', 'missing_hash', 'unknown_uuid',
    'changed_bytes', 'symlink', 'stale_hash', 'missing_edit', 'duplicate_edit'])
async def test_source_revision_fails_closed_without_execution_or_source_mutation(
        inspection, boundary, tmp_path, condition):
    service, executor, _run_id = inspection
    source = SOURCE + 'VALUE = 1\r\n' if condition == 'duplicate_edit' else SOURCE
    ref, _producer = _base(inspection, boundary, tmp_path, source=source,
        metadata={'requested_exposure': 'RAW'} if condition == 'declared_output' else None,
        owner='foreign-owner' if condition == 'wrong_owner' else None,
        project='foreign-project' if condition == 'wrong_project' else None,
        lab='foreign-lab' if condition == 'wrong_lab' else None,
        artifact_type='ordinary-file' if condition == 'wrong_type' else 'execution-script',
        stage=WorkflowStage.REPORT if condition == 'wrong_stage' else WorkflowStage.EXECUTE,
        omit_metadata=('execution_id',) if condition == 'missing_producer' else
                      ('sha256',) if condition == 'missing_hash' else ())
    payload, scope = _payload(ref), _scope(inspection, boundary, ref)
    if condition == 'not_imported':
        scope['mountable_input_artifact_ids'] = ()
    elif condition == 'no_advertised_scope':
        scope['mountable_input_artifact_ids'] = None
    elif condition == 'changed_bytes':
        Path(ref.storage_locator).write_bytes(b'# different source\npass\n')
    elif condition == 'symlink':
        stored = Path(ref.storage_locator)
        target = tmp_path / 'target.py'
        target.write_bytes(stored.read_bytes())
        stored.unlink()
        stored.symlink_to(target)
    elif condition == 'stale_hash':
        payload['source_revision']['expected_script_hash'] = '0' * 64
    elif condition == 'missing_edit':
        payload['source_revision']['edits'][0]['old'] = 'ABSENT_PRIVATE_EDIT'
    elif condition == 'unknown_uuid':
        unknown = uuid4()
        payload['source_revision']['source_artifact_id'] = str(unknown)
        scope['mountable_input_artifact_ids'] = (unknown,)
    before = Path(ref.storage_locator).read_bytes()
    with pytest.raises(ExecutionSubmissionError) as caught:
        await _submit_source(service, payload, **scope)
    assert caught.value.code.startswith('SOURCE_REVISION_')
    assert 'ABSENT_PRIVATE_EDIT' not in str(caught.value)
    assert str(ref.storage_locator) not in str(caught.value)
    if condition == 'unknown_uuid':
        assert str(unknown) not in str(caught.value)
    assert len(executor.plans) == 0
    assert Path(ref.storage_locator).read_bytes() == before


@pytest.mark.parametrize('field', ['image_key', 'resources', 'input_artifact_ids', 'parameters', 'requested_outputs'])
def test_source_intent_requires_current_execution_fields(inspection, boundary, tmp_path, field):
    ref, _producer = _base(inspection, boundary, tmp_path)
    payload = _payload(ref)
    del payload[field]
    with pytest.raises(ValidationError) as caught:
        _intent(payload)
    assert any(issue['loc'][0] == field for issue in caught.value.errors())


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['resources', 'input_artifact_ids', 'parameters', 'requested_outputs'])
async def test_direct_toolset_source_mode_rejects_omitted_current_fields(
        inspection, boundary, tmp_path, field):
    service, executor, run_id = inspection
    ref, _producer = _base(inspection, boundary, tmp_path)
    base = _toolset(boundary, WorkflowStage.EXECUTE, ('execution_submit',), execution_submission=service)
    tools = LabBioRuntimeToolSet(replace(base.binding, run_id=run_id,
        mountable_input_artifact_ids=(ref.artifact_id,)), base.services)
    payload = _payload(ref)
    del payload[field]
    result = await tools.execution_submit(**payload)
    assert not result['success'] and result['error']['error_code'] == 'INVALID_EXECUTION_DRAFT'
    assert executor.plans == []


@pytest.mark.asyncio
@pytest.mark.parametrize('both_sources', [False, True])
async def test_complete_direct_source_call_retains_all_nine_audit_fields(
        inspection, boundary, tmp_path, both_sources):
    from labbioagentos.runtime.contracts import ExecutionDraftField
    service, executor, run_id = inspection
    ref, _producer = _base(inspection, boundary, tmp_path)
    base = _toolset(boundary, WorkflowStage.EXECUTE, ('execution_submit',), execution_submission=service)
    tools = LabBioRuntimeToolSet(replace(base.binding, run_id=run_id,
        mountable_input_artifact_ids=(ref.artifact_id,)), base.services)
    payload = _payload(ref)
    if both_sources:
        payload['script_content'] = 'pass\n'
    result = await tools.execution_submit(**payload)
    assert result['success'] is not both_sources
    if both_sources:
        assert result['error']['error_code'] == 'INVALID_EXECUTION_DRAFT'
        assert executor.plans == []
    else:
        assert result['data']['status'] == 'SUCCEEDED'
        assert len(executor.plans) == 1
        assert executor.plans[0].script_content == SOURCE.replace('VALUE = 1', 'VALUE = 2')
    audit = tools.evidence_items()[-1].execution_submit_request
    assert set(audit.known_top_level_field_presence) == set(ExecutionDraftField)
    assert len(audit.known_top_level_field_presence) == 9
    assert audit.validation_status.value == ('INVALID' if both_sources else 'VALID')
    assert 'VALUE = ' not in audit.model_dump_json()


@pytest.mark.asyncio
async def test_unconfirmed_effect_blocks_source_read_and_new_execution(
        inspection, boundary, tmp_path, monkeypatch):
    from labbioagentos.execution.submission import ExecutionSubmissionPersistenceError
    service, executor, run_id = inspection
    ref, _producer = _base(inspection, boundary, tmp_path)
    draft = _intent(_payload(ref))
    service._persistence_failures[run_id] = uuid4()
    monkeypatch.setattr(service.artifact_store, 'get_ref',
        lambda _identity: pytest.fail('Unconfirmed effect must block source access'))
    with pytest.raises(ExecutionSubmissionPersistenceError):
        await service.submit_source_revision(draft, **_scope(inspection, boundary, ref))
    assert executor.plans == []


@pytest.mark.parametrize('field', ['image_key', 'resources', 'input_artifact_ids', 'parameters', 'requested_outputs'])
def test_native_source_wire_rejects_omitted_current_fields_before_default_dump(
        inspection, boundary, tmp_path, field):
    from labbioagentos.kernel import FinishReason, ToolBatchRejectedError, ToolCall
    from labbioagentos.runtime.native_tools import build_labbio_tool_registry
    ref, _producer = _base(inspection, boundary, tmp_path)
    payload = _payload(ref)
    del payload[field]
    registry = build_labbio_tool_registry(object())
    with pytest.raises(ToolBatchRejectedError):
        registry.validate_batch((ToolCall(call_id='missing-current-field', name='execution_submit',
            arguments_json=json.dumps(payload)),), finish_reason=FinishReason.TOOL_CALLS, max_calls=1)
    assert inspection[1].plans == []


@pytest.mark.parametrize('selection', ['neither', 'both'])
def test_native_program_source_choice_is_exclusive(inspection, boundary, tmp_path, selection):
    from labbioagentos.kernel import FinishReason, ToolBatchRejectedError, ToolCall
    from labbioagentos.runtime.native_tools import build_labbio_tool_registry
    ref, _producer = _base(inspection, boundary, tmp_path)
    payload = _payload(ref)
    if selection == 'neither':
        del payload['source_revision']
    else:
        payload['script_content'] = 'pass\n'
    registry = build_labbio_tool_registry(object())
    with pytest.raises(ToolBatchRejectedError):
        registry.validate_batch((ToolCall(call_id='source-choice', name='execution_submit',
            arguments_json=json.dumps(payload)),), finish_reason=FinishReason.TOOL_CALLS, max_calls=1)
    assert inspection[1].plans == []


@pytest.mark.asyncio
async def test_source_revision_persists_full_new_source_and_bounded_lineage(inspection, boundary, tmp_path):
    ledger = JobLedger(tmp_path / 'source-revision.sqlite')
    initial = _persistent(inspection, boundary, ledger)
    service, executor, _run_id = initial
    ref, _producer = _base(initial, boundary, tmp_path)
    receipt = await _submit_source(service, _payload(ref), **_scope(initial, boundary, ref))
    expected = SOURCE.replace('VALUE = 1', 'VALUE = 2')
    ledger.close()
    reopened = JobLedger(tmp_path / 'source-revision.sqlite')
    restored = _persistent(inspection, boundary, reopened)
    page = _inspect(restored, boundary, receipt.execution_id)
    assert page['submitted_program']['source'] == expected
    assert page['source_revision_origin']['source_artifact_id'] == str(ref.artifact_id)
    assert len(executor.plans) == 1
    payload = reopened.execution_submission(restored[2], receipt.execution_id)
    assert 'VALUE = ' not in payload and 'synthetic-source' not in payload
    assert 'source_revision_origin' in payload
    reopened.close()


@pytest.mark.asyncio
async def test_materialized_source_still_passes_normal_syntax_validation(inspection, boundary, tmp_path):
    from labbioagentos import ExecutionScriptValidationError
    service, executor, _run_id = inspection
    ref, _producer = _base(inspection, boundary, tmp_path)
    payload = _payload(ref, edits=[{'old': 'VALUE = 1', 'new': 'VALUE = ('}])
    with pytest.raises(ExecutionScriptValidationError):
        await _submit_source(service, payload, **_scope(inspection, boundary, ref))
    assert executor.plans == []
    assert Path(ref.storage_locator).read_bytes() == SOURCE.encode()
