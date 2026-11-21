"""Explicit original programs are inherited as inspectable inputs, never results."""
import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

import pytest

from labbioagentos import ExecutionPlanDraft, MountResolver, RequestedResources, WorkflowStage, cli
from labbioagentos.local_config import build_application
from labbioagentos.execution.errors import ContainerStartError
from labbioagentos.execution.submission import ExecutionSubmissionPersistenceError
from labbioagentos.local_continuation import result_refs
from labbioagentos.local_conversations import read_run_record
from test_local_cli import local_cli  # noqa: F401
from test_local_revision_cli import revision_cli, _args  # noqa: F401
from test_phase6_docker_execution import FakeDockerRunner


@pytest.fixture
def failed_program(revision_cli, request):
    settings, directory, _, _, record, *_ = revision_cli
    app = build_application(settings, directory, load_provider=False)
    source = "# retained fixture source 🧪\nraise ValueError('fixture')\n"
    try:
        executor = app.container_executor
        executor.backend_check = lambda: {}
        executor.launcher = None
        executor.process_runner = FakeDockerRunner(exit_code=1)
        if getattr(request, "param", None) == "start_failure":
            def reject_start(*args, **kwargs):
                raise ContainerStartError("fixture launch rejected")
            executor.process_runner.run = reject_start
        executor.minimum_queryable_output_count = 0
        receipt = asyncio.run(app.execution_submission.submit(
            ExecutionPlanDraft(image_key='python', script_content=source,
                resources=RequestedResources(cpus=1.0, memory_mb=512, pids_limit=32, timeout_seconds=30.0)),
            principal=settings.principal, workspace=settings.workspace, run_id=record.run_id,
            stage_id=WorkflowStage.EXECUTE, invocation_id=uuid4(),
        ))
        assert receipt.status.value == "FAILED" and not receipt.registered_outputs
        ref, _, _ = app.execution_submission.replay_binding(receipt.execution_id,
            principal=settings.principal, workspace=settings.workspace, run_id=record.run_id)
        return ref, receipt.execution_id, source
    finally:
        cli._close(app)


@pytest.mark.parametrize("failed_program, expected_exit_code", [(None, 1), ("start_failure", None)],
    indirect=["failed_program"])
def test_failed_program_explicit_revision_preserves_source_and_reader(revision_cli, failed_program, expected_exit_code):
    settings, directory, _, _, parent, *_ = revision_cli
    original, execution_id, source = failed_program
    assert original.artifact_id not in {ref.artifact_id for ref in result_refs(directory, parent)}
    assert cli.main(_args(revision_cli, selected=original.artifact_id)) == 2
    successor = settings.result_root / 'revision'
    record = read_run_record(successor)
    assert set(record.input_artifact_ids) == {*parent.input_artifact_ids, original.artifact_id}
    revision = json.loads((successor / 'REVISION.json').read_text())
    assert revision['selected_artifact_ids'] == [str(original.artifact_id)]
    entry = next(row for row in revision['imports'] if row['artifact_id'] == str(original.artifact_id))
    assert entry['producer_execution_status'] == 'FAILED'
    assert entry['producer_exit_code'] == expected_exit_code
    assert entry['producer_execution_id'] == str(execution_id)
    assert read_run_record(directory) == parent
    app = build_application(settings, successor, load_provider=False)
    try:
        inspected = app.execution_submission.inspect(execution_id, principal=settings.principal,
            workspace=settings.workspace, run_id=record.run_id, imported_source_artifact_ids=record.input_artifact_ids)
        assert inspected['producer_run_id'] == str(parent.run_id)
        assert inspected['submitted_program']['source'] == source
        assert inspected['receipt'] is None
        mount = MountResolver(app.artifact_store, approved_input_roots=(app.artifact_store.root,)).resolve_inputs((original.artifact_id,))[0]
        assert mount.source.read_bytes() == source.encode()
        assert mount.source != Path(original.storage_locator)
        assert hashlib.sha256(mount.source.read_bytes()).hexdigest() == original.metadata['sha256']
    finally:
        cli._close(app)


def test_default_revision_does_not_select_failed_program(revision_cli, failed_program):
    settings, _, observed, *_ = revision_cli
    assert cli.main(_args(revision_cli)) == 1
    assert not (settings.result_root / 'revision').exists()
    assert not observed['explicit_provider_loads']


def test_unrelated_unconfirmed_job_does_not_invalidate_specific_program_receipt(revision_cli, failed_program):
    settings, source_directory, _, _, parent, *_ = revision_cli
    original, execution_id, source = failed_program
    app = build_application(settings, source_directory, load_provider=False)
    try:
        app.configuration.job_ledger.record_intent(run_id=parent.run_id, execution_id=uuid4(),
            launcher="local", workspace_root=source_directory)
        scope = dict(principal=settings.principal, workspace=settings.workspace, run_id=parent.run_id)
        ref, draft, receipt = app.execution_submission.original_program_binding(execution_id, **scope)
        assert ref == original and draft.script_content == source and receipt.exit_code == 1
        with pytest.raises(ExecutionSubmissionPersistenceError):
            app.execution_submission.replay_binding(execution_id, **scope)
    finally:
        cli._close(app)
    assert cli.main(_args(revision_cli, selected=original.artifact_id)) == 2
    assert original.artifact_id in read_run_record(settings.result_root / 'revision').input_artifact_ids


@pytest.mark.parametrize('condition', [
    'unreceipted_start', 'unknown_execution', 'foreign_run', 'foreign_tenant',
    'forged_execution_artifact', 'changed_bytes', 'symlinked_payload',
    'symlinked_envelope', 'symlinked_job_ledger', 'hardlinked_payload', 'outside_blob',
])
def test_explicit_program_selection_fails_closed(revision_cli, failed_program, condition, tmp_path):
    settings, directory, observed, _, parent, *_ = revision_cli
    ref, execution_id, _ = failed_program
    selected = ref.artifact_id
    envelope = directory / 'artifacts' / f'{selected}.json'
    stored = json.loads(envelope.read_text())
    if condition == 'unreceipted_start':
        # Existing host plan/start events and an intact blob, but no durable terminal submission.
        with sqlite3.connect(directory / 'jobs.sqlite') as ledger:
            ledger.execute('DELETE FROM execution_submissions WHERE execution_id = ?', (str(execution_id),))
    elif condition in ('unknown_execution', 'foreign_run', 'foreign_tenant', 'outside_blob'):
        if condition == 'unknown_execution':
            stored['ref']['metadata']['execution_id'] = str(uuid4())
        elif condition == 'foreign_run':
            stored['ref']['run_id'] = str(uuid4())
        elif condition == 'outside_blob':
            outside = tmp_path / 'outside'
            outside.write_bytes(Path(ref.storage_locator).read_bytes())
            stored['ref']['storage_locator'] = str(outside)
        else:
            stored['ref']['lab_id'] = 'another-lab'
        envelope.write_text(json.dumps(stored))
    elif condition == 'forged_execution_artifact':
        # Valid tenant, exact execution, valid digest and identical payload, but a different Artifact.
        app = build_application(settings, directory, load_provider=False)
        try:
            lookalike = app.artifact_store.register_file(Path(ref.storage_locator),
                artifact_type=ref.artifact_type, exposure_class=ref.exposure_class,
                representation=app.artifact_store.load_for_view(selected).representation,
                owner_user_id=ref.owner_user_id, project_id=ref.project_id, lab_id=ref.lab_id,
                run_id=parent.run_id, stage_id=ref.stage_id, producer_invocation_id=ref.producer_invocation_id,
                metadata=ref.metadata)
            selected = lookalike.artifact_id
        finally:
            cli._close(app)
    elif condition == 'changed_bytes':
        Path(ref.storage_locator).write_bytes(b'changed')
    elif condition == 'hardlinked_payload':
        (tmp_path / 'alias').hardlink_to(Path(ref.storage_locator))
    else:
        path = {'symlinked_payload': Path(ref.storage_locator), 'symlinked_envelope': envelope,
                'symlinked_job_ledger': directory / 'jobs.sqlite'}[condition]
        moved = tmp_path / 'outside-original'
        path.rename(moved)
        path.symlink_to(moved)
    assert cli.main(_args(revision_cli, selected=selected)) == 1
    assert not observed['explicit_provider_loads']
    assert not (settings.result_root / 'revision').exists()
