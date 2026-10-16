"""C13 phase 3a: SchedulerLauncher behaviour through a fake scheduler and real bash jobs."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

import pytest

from labbioagentos import (
    ApprovedImageRegistry,
    ArtifactRegistrationPolicy,
    ContainerExecutor,
    ContainerStartError,
    ExecutionFailureClass,
    ExecutionPolicy,
    ExecutionStatus,
    ExecutionWorkspaceManager,
    LocalArtifactStore,
    MountResolver,
    OutputCollector,
    RequestedResources,
)
from labbioagentos.execution import JobLauncherError, JobSpec, JobState
from labbioagentos.execution.launchers import (
    BUILTIN_TEMPLATES,
    SchedulerCommandRunner,
    SchedulerLauncher,
    SchedulerPolicy,
)

from scheduler_fixtures import FakeSlurm, outcome
from test_phase6_docker_execution import FakeDockerRunner, _image, _plan

SECRET = "sk-scheduler-sentinel-0123456789abcdef"


class PythonBuilder:
    """Stands in for ApptainerCommandBuilder: same contract, runs the host interpreter."""

    def __init__(self, scratch_root: Path, program: str):
        self.scratch_root, self.program = scratch_root, program

    def build(self, plan, image, workspace, input_mounts):
        self.cleanup_paths(plan.execution_id)[0].mkdir(parents=True)
        return (sys.executable, "-c", self.program)

    def import_probe_argv(self, image, interpreter_arguments):
        return (sys.executable, *interpreter_arguments)

    def cleanup_paths(self, execution_id):
        return (self.scratch_root / execution_id.hex,)

    def model_stderr(self, stderr):
        return stderr


def _policy(**overrides):
    values = dict(max_output_file_bytes=1 << 20, poll_interval_seconds=0.05,
                  completion_grace_seconds=0.5, kill_after_seconds=1)
    values.update(overrides)
    return SchedulerPolicy(**values)


def _executor(tmp_path, scheduler, program="print('scheduled mock task')", **policy):
    store = LocalArtifactStore(tmp_path / "artifacts")
    root = tmp_path / "executions"
    launcher = SchedulerLauncher(template=BUILTIN_TEMPLATES["slurm"], policy=_policy(**policy),
                                 workspace_root=root, command_runner=scheduler)
    executor = ContainerExecutor(
        store=store, image_registry=ApprovedImageRegistry((_image(),)),
        execution_policy=ExecutionPolicy(),
        mount_resolver=MountResolver(store, approved_input_roots=(store.root,)),
        workspace_manager=ExecutionWorkspaceManager(root),
        output_collector=OutputCollector(store, ArtifactRegistrationPolicy()),
        process_runner=FakeDockerRunner(),  # Must never run: the scheduler places the job.
        command_builder=PythonBuilder(tmp_path / "scratch", program),
        launcher=launcher,
    )
    return executor, launcher


@pytest.mark.asyncio
async def test_mock_task_runs_through_the_scheduler_end_to_end(tmp_path):
    scheduler = FakeSlurm(tmp_path)
    executor, _ = _executor(tmp_path, scheduler)
    plan = _plan()

    result = await executor.execute_async(plan)

    assert result.status is ExecutionStatus.SUCCEEDED and result.exit_code == 0
    assert Path(result.stdout_ref.storage_locator).read_text() == "scheduled mock task\n"
    assert executor.process_runner.calls == []
    assert not (tmp_path / "scratch" / plan.execution_id.hex).exists()
    job_dir = tmp_path / "executions" / str(plan.execution_id) / "job"
    assert (job_dir / "job.sh").read_text().startswith("#!/bin/bash\n")
    assert [command[0] for command in scheduler.commands][0] == "sbatch"


@pytest.mark.asyncio
async def test_execution_timeout_is_reported_as_timeout(tmp_path):
    executor, _ = _executor(tmp_path, FakeSlurm(tmp_path), program="import time; time.sleep(60)")

    result = await executor.execute_async(_plan(resources=RequestedResources(timeout_seconds=1)))

    assert result.status is ExecutionStatus.TIMED_OUT
    assert result.error_class is ExecutionFailureClass.TIMEOUT


@pytest.mark.asyncio
async def test_queue_limit_cancels_the_job_and_reports_timeout(tmp_path):
    scheduler = FakeSlurm(tmp_path, hold=True)
    executor, _ = _executor(tmp_path, scheduler, max_queue_seconds=0.3)

    result = await executor.execute_async(_plan())

    assert result.status is ExecutionStatus.TIMED_OUT
    assert any(command[0] == "scancel" for command in scheduler.commands)


@pytest.mark.asyncio
async def test_submission_refusal_is_a_start_failure(tmp_path):
    executor, _ = _executor(tmp_path, FakeSlurm(tmp_path, refuse=True))

    result = await executor.execute_async(_plan())

    assert result.status is ExecutionStatus.FAILED
    assert result.error_class is ExecutionFailureClass.CONTAINER_START_FAILURE


class LaggingScheduler:
    """Accepts jobs but never runs them; the test writes the exit record itself."""

    def __init__(self):
        self.commands = []

    def run(self, argv, *, stdin=None):
        self.commands.append(tuple(argv))
        if argv[0] == "sbatch":
            return outcome(stdout=b"77\n")
        return outcome()  # squeue: no longer listed.


def _spec(root: Path) -> JobSpec:
    execution_id = uuid4()
    (root / str(execution_id)).mkdir(parents=True)
    return JobSpec(execution_id=execution_id, argv=("true",), timeout_seconds=60.0,
                   resources=RequestedResources(), workspace_root=root / str(execution_id))


@pytest.mark.asyncio
async def test_shared_filesystem_lag_is_tolerated_within_the_grace_period(tmp_path):
    root = tmp_path / "executions"
    launcher = SchedulerLauncher(template=BUILTIN_TEMPLATES["slurm"],
                                 policy=_policy(completion_grace_seconds=5),
                                 workspace_root=root, command_runner=LaggingScheduler())
    spec = _spec(root)
    handle = await launcher.submit(spec)
    job_dir = root / str(spec.execution_id) / "job"

    def late_exit_record():
        time.sleep(0.3)
        (job_dir / "stdout.log").write_text("late")
        (job_dir / "exit_code").write_text("0 3 0\n")

    threading.Thread(target=late_exit_record).start()

    assert await launcher.wait(handle) is JobState.COMPLETED
    collected = await launcher.collect(handle)
    assert collected.exit_code == 0 and collected.stdout == b"late"


@pytest.mark.asyncio
async def test_a_job_lost_without_an_exit_record_fails_after_the_grace_period(tmp_path):
    root = tmp_path / "executions"
    launcher = SchedulerLauncher(template=BUILTIN_TEMPLATES["slurm"],
                                 policy=_policy(completion_grace_seconds=0.2),
                                 workspace_root=root, command_runner=LaggingScheduler())
    handle = await launcher.submit(_spec(root))

    assert await launcher.wait(handle) is JobState.FAILED
    with pytest.raises(ContainerStartError):
        await launcher.collect(handle)


@pytest.mark.asyncio
async def test_a_restarted_control_plane_reattaches_without_resubmitting(tmp_path):
    scheduler = FakeSlurm(tmp_path)
    root = tmp_path / "executions"
    first = SchedulerLauncher(template=BUILTIN_TEMPLATES["slurm"], policy=_policy(),
                              workspace_root=root, command_runner=scheduler)
    spec = _spec(root)
    spec = JobSpec(**{**spec.__dict__, "argv": (sys.executable, "-c", "print('done')")})
    handle = await first.submit(spec)
    await first.wait(handle)

    restarted = SchedulerLauncher(template=BUILTIN_TEMPLATES["slurm"], policy=_policy(),
                                  workspace_root=root, command_runner=scheduler)
    attached = await restarted.attach(handle.job_id, execution_id=handle.execution_id)

    assert await restarted.status(attached) is JobState.COMPLETED
    assert (await restarted.collect(attached)).stdout == b"done\n"
    assert len(scheduler.calls) == 1


@pytest.mark.asyncio
async def test_cancel_refused_by_the_scheduler_is_an_error(tmp_path):
    class Refusing(LaggingScheduler):
        def run(self, argv, *, stdin=None):
            if argv[0] == "scancel":
                return outcome(1, stderr=b"scancel: error: Access/permission denied")
            return super().run(argv, stdin=stdin)

    root = tmp_path / "executions"
    launcher = SchedulerLauncher(template=BUILTIN_TEMPLATES["slurm"], policy=_policy(),
                                 workspace_root=root, command_runner=Refusing())
    handle = await launcher.submit(_spec(root))

    with pytest.raises(JobLauncherError):
        await launcher.cancel(handle)


@pytest.mark.asyncio
async def test_jobs_outside_the_launcher_root_are_refused(tmp_path):
    launcher = SchedulerLauncher(template=BUILTIN_TEMPLATES["slurm"], policy=_policy(),
                                 workspace_root=tmp_path / "executions",
                                 command_runner=LaggingScheduler())
    with pytest.raises(JobLauncherError):
        await launcher.submit(_spec(tmp_path / "elsewhere"))


def test_scheduler_client_receives_only_allowlisted_environment(monkeypatch):
    for name in ("OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "APPTAINERENV_TOKEN"):
        monkeypatch.setenv(name, SECRET)
    monkeypatch.setenv("SLURM_CONF", "/etc/slurm/slurm.conf")
    runner = SchedulerCommandRunner(("SLURM_CONF",))

    result = runner.run((sys.executable, "-I", "-c",
                         "import json, os; print(json.dumps(dict(os.environ)))"))

    environment = json.loads(result.stdout)
    assert SECRET not in result.stdout.decode()
    assert environment["SLURM_CONF"] == "/etc/slurm/slurm.conf"
    assert set(runner.environment()) <= {*SchedulerCommandRunner.BASE_ENVIRONMENT, "SLURM_CONF"}


def test_concurrent_status_polls_do_not_block_the_event_loop(tmp_path):
    # The scheduler client runs in a worker thread, like the local process wait.
    class SlowClient(LaggingScheduler):
        def run(self, argv, *, stdin=None):
            if argv[0] == "squeue":
                time.sleep(0.3)
                return outcome(stdout=b"RUNNING\n")
            return super().run(argv, stdin=stdin)

    async def scenario():
        root = tmp_path / "executions"
        launcher = SchedulerLauncher(template=BUILTIN_TEMPLATES["slurm"], policy=_policy(),
                                     workspace_root=root, command_runner=SlowClient())
        handle = await launcher.submit(_spec(root))
        ticks = 0

        async def ticker():
            nonlocal ticks
            for _ in range(10):
                ticks += 1
                await asyncio.sleep(0.02)

        await asyncio.gather(launcher.status(handle), ticker())
        return ticks

    assert asyncio.run(scenario()) == 10
