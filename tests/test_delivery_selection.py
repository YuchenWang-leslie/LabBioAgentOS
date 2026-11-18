"""Engineering-only counterexample: a check must not hide the analysis producer."""

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from labbioagentos import (
    ArtifactExposureClass, ArtifactReleaseBasis, ArtifactRepresentation,
    ExecutionPlan, OutputArtifactSpec, OutputCollector, ReportSubmissionService,
    TraceEventType, WorkflowStage,
)
from test_local_delivery import local_run, _export  # noqa: F401
from test_local_cli import local_cli  # noqa: F401
from test_local_revision_cli import revision_cli, _args  # noqa: F401
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401


def producer(local_run, tmp_path, label):
    app, handle, principal, workspace = local_run
    plan = ExecutionPlan(run_id=handle.run_id, stage_id=WorkflowStage.EXECUTE,
        invocation_id=uuid4(), owner_user_id=principal.user_id,
        project_id=workspace.project_id, lab_id=workspace.lab_id,
        image_key='synthetic', script_content=f"print({label!r})\n",
        requested_outputs=(OutputArtifactSpec(relative_path='result.tsv',
            artifact_type='synthetic-table'),))
    root = tmp_path / label
    root.mkdir()
    (root / 'result.tsv').write_bytes(b'item\tvalue\nsynthetic\t1\n')
    source = root / 'program.py'
    source.write_text(plan.script_content)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    program = app.artifact_store.register_file(source, artifact_type='execution-script',
        exposure_class=ArtifactExposureClass.RAW,
        release_basis=ArtifactReleaseBasis.INTERNAL_ONLY,
        representation=ArtifactRepresentation(), owner_user_id=principal.user_id,
        project_id=workspace.project_id, lab_id=workspace.lab_id,
        run_id=handle.run_id, stage_id=plan.stage_id,
        producer_invocation_id=plan.invocation_id,
        metadata={'execution_id':str(plan.execution_id), 'sha256':digest})
    app.trace_recorder.emit(handle.run_id, TraceEventType.EXECUTION_PLANNED,
        stage_id=plan.stage_id, invocation_id=plan.invocation_id,
        payload={'execution_id':str(plan.execution_id),
            'script_artifact_id':str(program.artifact_id), 'script_hash':digest})
    output = OutputCollector(app.artifact_store, app.registration_policy,
        trace_recorder=app.trace_recorder).collect(plan, root)[0].ref
    return output, program


def submit(local_run, selected):
    app, handle, principal, workspace = local_run
    return ReportSubmissionService(app.artifact_store, app.access_service,
        trace_recorder=app.trace_recorder).submit(title='Synthetic engineering report',
        report_text='No scientific analysis in this fixture.', evidence_artifact_ids=(),
        delivery_artifact_ids=selected, principal=principal, workspace=workspace,
        run_id=handle.run_id, stage_id=WorkflowStage.REPORT, invocation_id=uuid4())


def test_multi_producer_selection_exports_both_originals_and_not_unselected(local_run, tmp_path, monkeypatch):
    analysis, original = producer(local_run, tmp_path, 'analysis')
    check, checker = producer(local_run, tmp_path, 'independent-check')
    discarded, _ = producer(local_run, tmp_path, 'unselected')
    receipt = submit(local_run, (analysis.artifact_id, check.artifact_id))
    monkeypatch.setattr('labbioagentos.local_delivery.selected_execution_id',
        lambda _record: check.metadata['execution_id'])
    monkeypatch.setattr('labbioagentos.local_delivery.selected_report_id',
        lambda _record: receipt.report_artifact_id)
    app = local_run[0]
    actual = app.result
    def result(handle):
        return actual(handle).model_copy(update={'report_artifact_ids':(receipt.report_artifact_id,)})
    monkeypatch.setattr(app, 'result', result)
    directory = tmp_path / 'delivery'
    manifest = _export(local_run, directory)
    assert {item['artifact_id'] for item in manifest['outputs']} == {
        str(analysis.artifact_id), str(check.artifact_id)}
    assert {item['artifact_id'] for item in manifest['programs']} == {
        str(original.artifact_id), str(checker.artifact_id)}
    assert {item['artifact_id'] for item in manifest['historical_outputs']} == {str(discarded.artifact_id)}
    for item in (*manifest['outputs'], *manifest['programs']):
        assert hashlib.sha256((directory/item['file']).read_bytes()).hexdigest() == item['sha256']
    assert _export(local_run, directory) == manifest


@pytest.mark.parametrize('invalid', ['duplicate', 'program', 'raw-input', 'unknown', 'tampered'])
def test_selection_rejects_invalid_sources_without_registering_report(local_run, tmp_path, invalid):
    output, original = producer(local_run, tmp_path, 'analysis')
    selected = (output.artifact_id,)
    if invalid == 'duplicate':
        selected *= 2
    elif invalid == 'program':
        selected = (original.artifact_id,)
    elif invalid == 'raw-input':
        app, handle, principal, workspace = local_run
        raw = app.artifact_store.register_file(Path(original.storage_locator), artifact_type='input',
            exposure_class=ArtifactExposureClass.RAW, representation=ArtifactRepresentation(),
            owner_user_id=principal.user_id, project_id=workspace.project_id,
            lab_id=workspace.lab_id, run_id=handle.run_id)
        selected = (raw.artifact_id,)
    elif invalid == 'unknown':
        selected = (uuid4(),)
    else:
        Path(output.storage_locator).write_bytes(b'changed')
    before = len(local_run[0].artifact_store.list_refs())
    with pytest.raises(ValueError):
        submit(local_run, selected)
    assert len(local_run[0].artifact_store.list_refs()) == before


def test_default_revision_uses_report_selection_across_producers(revision_cli, tmp_path, monkeypatch):
    from labbioagentos import cli, ApplicationRunHandle
    from labbioagentos.local_config import build_application
    from labbioagentos.local_conversations import read_run_record
    settings, directory, _observed, _old_report, record, *_ = revision_cli
    app = build_application(settings, directory, load_provider=False)
    fixture = (app, ApplicationRunHandle(run_id=record.run_id), settings.principal, settings.workspace)
    main, program = producer(fixture, tmp_path, 'main')
    check, checker = producer(fixture, tmp_path, 'check')
    unused, _ = producer(fixture, tmp_path, 'unused')
    receipt = submit(fixture, (main.artifact_id, check.artifact_id))
    cli._close(app)
    monkeypatch.setattr('labbioagentos.local_delivery.selected_report_id',
        lambda _record: receipt.report_artifact_id if _record.run_id == record.run_id else None)
    monkeypatch.setattr('labbioagentos.local_continuation.selected_execution_id',
        lambda _record: check.metadata['execution_id'])
    assert cli.main(_args(revision_cli)) == 2
    revised = read_run_record(settings.result_root/'revision')
    imported = {*revised.input_artifact_ids, *revised.context_artifact_ids}
    assert {main.artifact_id, check.artifact_id, program.artifact_id, checker.artifact_id}.issubset(imported)
    assert unused.artifact_id not in imported
    assert receipt.report_artifact_id not in imported


@pytest.mark.parametrize('capability', ['report_check', 'report_submit'])
def test_native_reports_require_explicit_delivery_selection_without_inventing_files(capability):
    import json
    from labbioagentos.runtime.native_tools import build_labbio_tool_registry
    from labbioagentos.kernel.contracts import ToolCall, FinishReason
    from labbioagentos.kernel.tooling import ToolBatchRejectedError
    from native_kernel.test_labbio_native_tools import RecordingHost, VALID_ARGUMENTS
    registry = build_labbio_tool_registry(RecordingHost())
    spec = next(spec for spec in registry.specs if spec.name == capability)
    assert 'delivery_artifact_ids' in spec.parameters['required']
    arguments = {**VALID_ARGUMENTS[capability]}
    arguments.pop('delivery_artifact_ids', None)
    with pytest.raises(ToolBatchRejectedError):
        registry.validate_batch((ToolCall(call_id='no-selection', name=capability,
            arguments_json=json.dumps(arguments)),), finish_reason=FinishReason.TOOL_CALLS, max_calls=1)
    arguments['delivery_artifact_ids'] = []
    registry.validate_batch((ToolCall(call_id='explicit-empty', name=capability,
        arguments_json=json.dumps(arguments)),), finish_reason=FinishReason.TOOL_CALLS, max_calls=1)


@pytest.mark.asyncio
async def test_check_and_submit_share_delivery_validation_without_giving_read_credit(artifact_query_boundary, tmp_path):
    from types import SimpleNamespace
    from labbioagentos import ArtifactRegistrationPolicy, InMemoryTraceSink, RunTraceRecorder
    from test_report_scoped_evidence import report_tools, reference
    tools, citation, _ = report_tools(artifact_query_boundary, real_service=True)
    recorder = RunTraceRecorder(InMemoryTraceSink())
    tools.services.report_submission.trace_recorder = recorder
    app = SimpleNamespace(artifact_store=tools.services.artifact_store,
        registration_policy=ArtifactRegistrationPolicy(), trace_recorder=recorder)
    fixture = (app, SimpleNamespace(run_id=tools.binding.run_id), tools.binding.principal, tools.binding.workspace)
    first, _ = producer(fixture, tmp_path, 'main')
    second, original = producer(fixture, tmp_path, 'check')
    page = await tools.artifact_query(str(citation.artifact_id), 'TOP_N', 2)
    tools.mark_feedback_delivered((page,))
    references = reference(citation, page)
    ids = [str(first.artifact_id), str(second.artifact_id)]
    checked = await tools.report_check(evidence_references=references, delivery_artifact_ids=ids)
    assert checked['success'] and checked['data']['ready']
    assert len(checked['data']['delivery_manifest']['programs']) == 2
    submitted = await tools.report_submit('Synthetic', 'Two outputs archived, two rows cited.',
        evidence_references=references, delivery_artifact_ids=ids)
    assert submitted['success'], submitted
    report = tools.services.artifact_store.load_for_view(submitted['data']['report_artifact_id'])
    assert report.representation.summary['delivery_manifest'] == checked['data']['delivery_manifest']
    assert report.representation.summary['evidence_artifact_ids'] == [str(citation.artifact_id)]
    assert report.representation.summary['evidence_read_receipts'][0]['scope']['end_offset'] == 2
    before = len(tools.services.artifact_store.list_refs())
    for operation in (tools.report_check, tools.report_submit):
        kwargs = dict(evidence_references=references,
            delivery_artifact_ids=[ids[0], str(original.artifact_id)])
        if operation == tools.report_submit:
            kwargs.update(title='Synthetic', report_text='Rejected selection.')
        rejected = await operation(**kwargs)
        assert not rejected['success']
        assert rejected['error']['error_code'] == 'INVALID_DELIVERY_SELECTION'
        assert 'delivery_artifact_ids[1]' in rejected['error']['safe_message']
        assert 'NOT_REGISTERED_OUTPUT' in rejected['error']['safe_message']
    assert len(tools.services.artifact_store.list_refs()) == before
