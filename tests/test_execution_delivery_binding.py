"""Engineering submission/executor/report boundary, without a real provider."""

from pathlib import Path
from uuid import uuid4

import pytest

from labbioagentos import (
    ExecutionPlanDraft, ExecutionSubmissionService, OutputArtifactSpec,
    ProcessOutcome, ProcessRunner, ReportSubmissionService, TraceEventType,
    WorkflowStage,
)
from labbioagentos.runtime.delivery import DeliverySelectionError
from apptainer_fixtures import apptainer_environment, bind_fields
from test_runtime_milestone_b import boundary  # noqa: F401


class OutputRunner(ProcessRunner):
    def run(self, argv, *, timeout_seconds):
        root = next(Path(source) for source, target, _ in bind_fields(argv)
            if target == '/workspace/outputs')
        (root / 'result.tsv').write_bytes(b'item\tvalue\nengineering\t1\n')
        return ProcessOutcome(exit_code=0, stdout=b'', stderr=b'', duration_seconds=0.1)


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', [None, 'missing', 'conflicting'])
async def test_delivery_validates_program_binding_not_submission_lifecycle(tmp_path, boundary, monkeypatch, fault):
    _, _, access, principal, workspace, _, _ = boundary
    store, executor, *_ = apptainer_environment(tmp_path / 'execution', OutputRunner(), traced=True, watched_scratch=True)
    recorder = executor.trace_recorder
    service = ExecutionSubmissionService(artifact_store=store, access_service=access,
        executor=executor, trace_recorder=recorder)
    run_id, invocation_id = uuid4(), uuid4()
    receipt = await service.submit(ExecutionPlanDraft(image_key='python-analysis',
        script_content="print('Engineering fixture only')\n",
        requested_outputs=(OutputArtifactSpec(relative_path='result.tsv', artifact_type='synthetic-table'),)),
        principal=principal, workspace=workspace, run_id=run_id,
        stage_id=WorkflowStage.EXECUTE, invocation_id=invocation_id)
    events = recorder.events(run_id)
    planned = [e for e in events if e.event_type is TraceEventType.EXECUTION_PLANNED]
    assert [e.status for e in planned] == ['SUBMITTED', 'PLANNED']
    assert 'script_artifact_id' not in planned[0].payload
    assert planned[1].payload['script_hash'] == receipt.script_hash
    if fault == 'missing':
        monkeypatch.setattr(recorder, 'events', lambda _run: tuple(e for e in events if e != planned[1]))
    elif fault == 'conflicting':
        recorder.emit(run_id, TraceEventType.EXECUTION_PLANNED,
            stage_id=WorkflowStage.EXECUTE, invocation_id=invocation_id, status='PLANNED',
            payload={**planned[1].payload, 'script_hash': '0' * 64})
    reports = ReportSubmissionService(store, access, trace_recorder=recorder)
    selected = tuple(receipt.registered_outputs)
    kwargs = dict(principal=principal, workspace=workspace, run_id=run_id)
    if fault:
        with pytest.raises(DeliverySelectionError, match='ORIGINAL_PROGRAM_BINDING_MISMATCH'):
            reports.prepare_delivery(selected, **kwargs)
        return
    manifest = reports.prepare_delivery(selected, **kwargs)
    assert len(manifest['outputs']) == len(manifest['programs']) == 1
    report = reports.submit(title='Engineering', report_text='No scientific claims.',
        evidence_artifact_ids=(), delivery_artifact_ids=selected,
        stage_id=WorkflowStage.REPORT, invocation_id=uuid4(), **kwargs)
    saved = store.load_for_view(report.report_artifact_id)
    assert saved.representation.summary['delivery_manifest'] == manifest
