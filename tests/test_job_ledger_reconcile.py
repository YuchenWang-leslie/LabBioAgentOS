"""C13 phase 3a: job ledger and orphan reconciliation after a crash (acceptance 16)."""

from __future__ import annotations

import asyncio
import dataclasses
import stat
import sys
import time
from uuid import uuid4

import pytest

from labbioagentos import (
    ApprovedImageRegistry,
    ArtifactRegistrationPolicy,
    ContainerExecutor,
    ExecutionPolicy,
    ExecutionStatus,
    ExecutionWorkspaceManager,
    LocalArtifactStore,
    MountResolver,
    OutputCollector,
    RequestedResources,
    cli,
)
from labbioagentos.execution import (
    JobLauncherError,
    JobLedger,
    JobLedgerState,
    JobSpec,
    LocalLauncher,
    OrphanAction,
    reconcile_orphan_jobs,
)
from labbioagentos.execution.launchers import BUILTIN_TEMPLATES, SchedulerLauncher
from labbioagentos.local_conversations import read_run_record

from scheduler_fixtures import FakeSlurm, outcome
from test_local_cli import local_cli  # noqa: F401 - shared offline application fixture
from test_local_continuation_cli import (  # noqa: F401 - autouse provider guard
    _mark_uncheckpointed_stage,
    _new_run,
    _output,
    _provider_must_not_be_loaded,
)
from test_phase6_docker_execution import FakeDockerRunner, _image, _plan
from test_scheduler_launcher import PythonBuilder, _policy


def _launcher(root, scheduler):
    return SchedulerLauncher(template=BUILTIN_TEMPLATES["slurm"], policy=_policy(),
                             workspace_root=root, command_runner=scheduler)


def _executor(tmp_path, scheduler, ledger, program="print('done')"):
    store = LocalArtifactStore(tmp_path / "artifacts")
    root = tmp_path / "executions"
    return ContainerExecutor(
        store=store, image_registry=ApprovedImageRegistry((_image(),)),
        execution_policy=ExecutionPolicy(),
        mount_resolver=MountResolver(store, approved_input_roots=(store.root,)),
        workspace_manager=ExecutionWorkspaceManager(root),
        output_collector=OutputCollector(store, ArtifactRegistrationPolicy()),
        process_runner=FakeDockerRunner(),
        command_builder=PythonBuilder(tmp_path / "scratch", program),
        launcher=_launcher(root, scheduler), job_ledger=ledger,
    )


async def _crash_after_submission(executor, ledger, plan):
    """Run until the ledger shows the job as submitted, then lose the process."""

    task = asyncio.create_task(executor.execute_async(plan))
    for _ in range(500):
        entry = ledger.get(plan.execution_id)
        if entry is not None and entry.state is JobLedgerState.SUBMITTED:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return entry


def _orphan(tmp_path, scheduler, run_id, *, record_job_id=True, submit=True):
    """A job submitted by a process that died, as the ledger and job files left it."""

    root = tmp_path / "executions"
    execution_id = uuid4()
    workspace = root / str(execution_id)
    (workspace / "outputs").mkdir(parents=True)
    (workspace / "outputs" / "result.txt").write_text("partial")
    ledger = JobLedger(tmp_path / "jobs.sqlite")
    ledger.record_intent(run_id=run_id, execution_id=execution_id, launcher="scheduler",
                         workspace_root=workspace)
    job_id = None
    if submit:
        spec = JobSpec(execution_id=execution_id, argv=(sys.executable, "-c", "print('late')"),
                       timeout_seconds=60.0, resources=RequestedResources(), workspace_root=workspace)
        job_id = asyncio.run(_launcher(root, scheduler).submit(spec)).job_id
        if record_job_id:
            ledger.record_submitted(execution_id, job_id)
    ledger.close()
    return execution_id, workspace, job_id


def _reconcile(tmp_path, scheduler, run_id, **options):
    ledger = JobLedger(tmp_path / "jobs.sqlite")  # A restarted control plane reopens it.
    launcher = options.pop("launcher", None) or _launcher(tmp_path / "executions", scheduler)
    results = asyncio.run(reconcile_orphan_jobs(ledger=ledger, launcher=launcher, run_id=run_id,
                                                **options))
    return results, ledger.get


# Ledger contract.

def test_ledger_records_each_execution_once_and_guards_transitions(tmp_path):
    ledger = JobLedger(tmp_path / "jobs.sqlite")
    run_id, execution_id = uuid4(), uuid4()
    ledger.record_intent(run_id=run_id, execution_id=execution_id, launcher="scheduler",
                         workspace_root=tmp_path / str(execution_id))

    with pytest.raises(JobLauncherError):
        ledger.record_intent(run_id=run_id, execution_id=execution_id, launcher="scheduler",
                             workspace_root=tmp_path / str(execution_id))
    with pytest.raises(JobLauncherError):
        ledger.record_collected(execution_id)  # Not submitted yet.
    with pytest.raises(ValueError):
        ledger.resolve(execution_id, JobLedgerState.COLLECTED)
    ledger.record_submitted(execution_id, "4000")
    assert [entry.job_id for entry in ledger.open_jobs(run_id)] == ["4000"]
    assert ledger.open_jobs(uuid4()) == ()
    ledger.record_collected(execution_id)
    assert ledger.open_jobs(run_id) == ()
    assert stat.S_IMODE((tmp_path / "jobs.sqlite").stat().st_mode) == 0o600
    ledger.close()

    reopened = JobLedger(tmp_path / "jobs.sqlite")
    assert reopened.get(execution_id).state is JobLedgerState.COLLECTED
    reopened.close()


@pytest.mark.asyncio
async def test_successful_and_refused_executions_close_their_ledger_entry(tmp_path):
    ledger = JobLedger(tmp_path / "jobs.sqlite")
    executor = _executor(tmp_path, FakeSlurm(tmp_path), ledger)
    plan = _plan()
    assert (await executor.execute_async(plan)).status is ExecutionStatus.SUCCEEDED
    assert ledger.get(plan.execution_id).state is JobLedgerState.COLLECTED

    refused = _executor(tmp_path / "refused", FakeSlurm(tmp_path / "refused", refuse=True), ledger)
    plan = _plan()
    assert (await refused.execute_async(plan)).status is ExecutionStatus.FAILED
    assert ledger.get(plan.execution_id).state is JobLedgerState.NOT_SUBMITTED
    assert ledger.open_jobs(plan.run_id) == ()


# Orphans after a lost process.

def test_queued_orphan_is_cancelled_and_its_files_deleted_without_resubmitting(tmp_path):
    scheduler = FakeSlurm(tmp_path, hold=True)
    ledger = JobLedger(tmp_path / "jobs.sqlite")
    plan = _plan()
    entry = asyncio.run(_crash_after_submission(_executor(tmp_path, scheduler, ledger), ledger, plan))
    ledger.close()
    assert entry.state is JobLedgerState.SUBMITTED  # Persisted before the wait began.

    results, get = _reconcile(tmp_path, scheduler, plan.run_id)

    assert [(item.execution_id, item.action) for item in results] == [
        (plan.execution_id, OrphanAction.CANCELLED)]
    assert get(plan.execution_id).state is JobLedgerState.CANCELLED
    assert any(command[0] == "scancel" for command in scheduler.commands)
    assert not (tmp_path / "executions" / str(plan.execution_id)).exists()
    assert len(scheduler.calls) == 1
    assert _reconcile(tmp_path, scheduler, plan.run_id)[0] == ()  # Idempotent.


def test_finished_orphan_is_reported_and_its_outputs_deleted(tmp_path):
    scheduler = FakeSlurm(tmp_path)
    ledger = JobLedger(tmp_path / "jobs.sqlite")
    plan = _plan()
    outputs = tmp_path / "executions" / str(plan.execution_id) / "outputs"
    program = f"import time; time.sleep(0.3); open({str(outputs / 'result.txt')!r}, 'w').write('x')"
    asyncio.run(_crash_after_submission(_executor(tmp_path, scheduler, ledger, program), ledger, plan))
    ledger.close()
    exit_record = outputs.parent / "job" / "exit_code"
    deadline = time.monotonic() + 20
    while not exit_record.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert (outputs / "result.txt").exists()

    results, get = _reconcile(tmp_path, scheduler, plan.run_id)

    assert [item.action for item in results] == [OrphanAction.DISCARDED]
    assert get(plan.execution_id).state is JobLedgerState.DISCARDED
    assert not outputs.parent.exists()
    assert len(scheduler.calls) == 1


def test_job_id_missing_from_the_ledger_is_recovered_from_the_job_files(tmp_path):
    scheduler = FakeSlurm(tmp_path, hold=True)
    run_id = uuid4()
    execution_id, workspace, job_id = _orphan(tmp_path, scheduler, run_id, record_job_id=False)

    results, get = _reconcile(tmp_path, scheduler, run_id)

    assert [(item.job_id, item.action) for item in results] == [(job_id, OrphanAction.CANCELLED)]
    assert get(execution_id).job_id == job_id and not workspace.exists()


def test_crash_before_submission_is_reported_and_nothing_is_deleted(tmp_path):
    scheduler = FakeSlurm(tmp_path)
    run_id = uuid4()
    execution_id, workspace, _ = _orphan(tmp_path, scheduler, run_id, submit=False)

    results, get = _reconcile(tmp_path, scheduler, run_id)

    assert [item.action for item in results] == [OrphanAction.UNCONFIRMED]
    assert get(execution_id).state is JobLedgerState.UNCONFIRMED
    assert (workspace / "outputs" / "result.txt").exists() and scheduler.calls == []


def test_unknown_job_is_lost_and_left_for_the_operator(tmp_path):
    scheduler = FakeSlurm(tmp_path, hold=True)
    run_id = uuid4()
    execution_id, workspace, _ = _orphan(tmp_path, scheduler, run_id)
    (workspace / "job" / "job_id").write_text("someone-else")

    results, get = _reconcile(tmp_path, scheduler, run_id)

    assert [item.action for item in results] == [OrphanAction.LOST]
    assert (workspace / "outputs" / "result.txt").exists()
    assert not any(command[0] == "scancel" for command in scheduler.commands)


def test_unconfirmed_cancel_keeps_files_and_stays_open(tmp_path):
    class Stubborn:
        def __init__(self):
            self.commands, self.calls = [], []

        def run(self, argv, *, stdin=None):
            self.commands.append(tuple(argv))
            if argv[0] == "sbatch":
                self.calls.append("90")
                return outcome(stdout=b"90\n")
            return outcome(stdout=b"RUNNING\n" if argv[0] == "squeue" else b"")

    scheduler = Stubborn()
    run_id = uuid4()
    execution_id, workspace, _ = _orphan(tmp_path, scheduler, run_id)

    results, get = _reconcile(tmp_path, scheduler, run_id, settle_seconds=0.3)

    assert [item.action for item in results] == [OrphanAction.CANCEL_PENDING]
    assert get(execution_id).state is JobLedgerState.SUBMITTED
    assert workspace.exists() and len(scheduler.calls) == 1


def test_mismatched_and_local_launchers_never_act(tmp_path):
    scheduler = FakeSlurm(tmp_path, hold=True)
    run_id = uuid4()
    execution_id, workspace, _ = _orphan(tmp_path, scheduler, run_id)

    results, get = _reconcile(tmp_path, scheduler, run_id, launcher=LocalLauncher(FakeDockerRunner()))
    assert [item.action for item in results] == [OrphanAction.LAUNCHER_MISMATCH]
    assert get(execution_id).state is JobLedgerState.SUBMITTED and workspace.exists()

    ledger = JobLedger(tmp_path / "local.sqlite")
    local_id = uuid4()
    ledger.record_intent(run_id=run_id, execution_id=local_id, launcher="local",
                         workspace_root=tmp_path / str(local_id))
    ledger.record_submitted(local_id, f"local-{local_id.hex}")
    results = asyncio.run(reconcile_orphan_jobs(
        ledger=ledger, launcher=LocalLauncher(FakeDockerRunner()), run_id=run_id))
    assert [item.action for item in results] == [OrphanAction.LOST]


# Through the operator command, after a simulated crash.

def test_reconcile_command_cancels_orphans_and_the_run_stays_blocked(
    local_cli, capsys, monkeypatch, tmp_path,  # noqa: F811
):
    directory, record, _ = _new_run(local_cli, capsys)
    _mark_uncheckpointed_stage(directory)
    scheduler = FakeSlurm(tmp_path, hold=True)
    root = directory / "executions"
    root.mkdir(exist_ok=True)
    execution_id = uuid4()
    workspace = root / str(execution_id)
    workspace.mkdir()
    ledger = JobLedger(directory / "jobs.sqlite")
    ledger.record_intent(run_id=record.run_id, execution_id=execution_id, launcher="scheduler",
                         workspace_root=workspace)
    spec = JobSpec(execution_id=execution_id, argv=("true",), timeout_seconds=60.0,
                   resources=RequestedResources(), workspace_root=workspace)
    ledger.record_submitted(execution_id, asyncio.run(_launcher(root, scheduler).submit(spec)).job_id)
    ledger.close()
    before = read_run_record(directory).model_dump_json()
    offline_build = cli.build_application

    def with_jobs(settings, run_directory, **options):
        application = offline_build(settings, run_directory, **options)
        application.configuration = dataclasses.replace(
            application.configuration, job_ledger=JobLedger(run_directory / "jobs.sqlite"),
            job_launcher=_launcher(run_directory / "executions", scheduler),
        )
        return application

    monkeypatch.setattr(cli, "build_application", with_jobs)

    assert cli.main(["reconcile", "--conversation", "thread-one", "--run-id", str(record.run_id)]) == 0
    output = _output(capsys)

    jobs = [item for item in output if item.get("event") == "jobs_reconciled"]
    assert jobs[0]["jobs"] == [{"execution_id": str(execution_id), "job_id": scheduler.calls[0],
                                "launcher": "scheduler", "action": "CANCELLED"}]
    assert output[-1]["continuation_action"] == "BLOCKED" and output[-1]["uncertain_side_effects"]
    assert read_run_record(directory).model_dump_json() == before
    assert not workspace.exists() and len(scheduler.calls) == 1

    assert cli.main(["reconcile", "--conversation", "thread-one", "--run-id", str(record.run_id)]) == 0
    assert not any(item.get("event") == "jobs_reconciled" for item in _output(capsys))
