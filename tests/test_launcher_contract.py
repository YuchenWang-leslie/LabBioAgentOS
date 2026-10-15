"""C13 phase 3a: every JobLauncher honours one submit/status/cancel/attach/collect contract."""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from uuid import uuid4

import pytest

from labbioagentos import ContainerStartError, ProcessOutcome, ProcessRunner, RequestedResources
from labbioagentos.execution import (
    JobHandle,
    JobLauncherError,
    JobNotFoundError,
    JobSpec,
    JobState,
    LocalLauncher,
)
from labbioagentos.execution.launchers import (
    BUILTIN_TEMPLATES,
    TERMINAL_STATES,
    SchedulerLauncher,
    SchedulerPolicy,
)

from scheduler_fixtures import FakeSlurm


class ScriptedRunner(ProcessRunner):
    """Thread-safe stand-in for a container process."""

    def __init__(self, *, delay=0.0, exit_code=0, error=None, cancellable=True):
        self.delay, self.exit_code, self.error = delay, exit_code, error
        self.stopped = threading.Event()
        self.calls = []
        if cancellable:
            self.cancel = lambda argv: self.stopped.set()

    def run(self, argv, *, timeout_seconds):
        self.calls.append((argv, timeout_seconds))
        if self.error is not None:
            raise self.error
        self.stopped.wait(self.delay)
        exit_code = 137 if self.stopped.is_set() else self.exit_code
        return ProcessOutcome(exit_code=exit_code, stdout=b"job output", stderr=b"",
                              duration_seconds=0.01)


def _spec(tmp_path: Path) -> JobSpec:
    return JobSpec(
        execution_id=uuid4(), argv=("runtime", "exec", "image", "python", "/labbio/script.py"),
        timeout_seconds=30.0, resources=RequestedResources(), workspace_root=tmp_path,
    )


def _local(tmp_path, **behaviour):
    runner = ScriptedRunner(**behaviour)
    return LocalLauncher(runner), runner, lambda: _spec(tmp_path)


def _scheduler(tmp_path, *, delay=0.0, exit_code=0):
    scheduler = FakeSlurm(tmp_path)
    root = tmp_path / "executions"
    launcher = SchedulerLauncher(
        template=BUILTIN_TEMPLATES["slurm"],
        policy=SchedulerPolicy(max_output_file_bytes=1 << 20, poll_interval_seconds=0.05,
                               completion_grace_seconds=0.5),
        workspace_root=root, command_runner=scheduler,
    )
    program = (f"import sys, time; time.sleep({delay}); "
               f"sys.stdout.write('job output'); sys.exit({exit_code})")

    def make_spec():
        execution_id = uuid4()
        workspace = root / str(execution_id)
        workspace.mkdir(parents=True)
        return JobSpec(execution_id=execution_id, argv=(sys.executable, "-c", program),
                       timeout_seconds=60.0, resources=RequestedResources(),
                       workspace_root=workspace)

    return launcher, scheduler, make_spec


LAUNCHERS = {"local": _local, "scheduler": _scheduler}


@pytest.fixture(params=sorted(LAUNCHERS))
def make_launcher(request, tmp_path):
    return lambda **behaviour: LAUNCHERS[request.param](tmp_path, **behaviour)


@pytest.mark.asyncio
async def test_completed_job_reports_its_exit_outcome(make_launcher, tmp_path):
    launcher, _, make_spec = make_launcher(exit_code=3)
    spec = make_spec()

    handle = await launcher.submit(spec)

    assert handle.execution_id == spec.execution_id and handle.launcher == launcher.kind
    assert await launcher.wait(handle) is JobState.COMPLETED
    assert await launcher.status(handle) is JobState.COMPLETED
    outcome = await launcher.collect(handle)
    assert outcome.exit_code == 3 and outcome.stdout == b"job output" and not outcome.timed_out


@pytest.mark.asyncio
async def test_an_execution_is_never_submitted_twice(make_launcher, tmp_path):
    launcher, runner, make_spec = make_launcher()
    spec = make_spec()
    handle = await launcher.submit(spec)

    with pytest.raises(JobLauncherError):
        await launcher.submit(spec)
    await launcher.wait(handle)
    assert len(runner.calls) == 1


@pytest.mark.asyncio
async def test_unknown_jobs_are_reported_not_invented(make_launcher, tmp_path):
    launcher, _, make_spec = make_launcher()
    stranger = JobHandle(launcher=launcher.kind, job_id="no-such-job", execution_id=uuid4())

    assert await launcher.status(stranger) is JobState.UNKNOWN
    with pytest.raises(JobNotFoundError):
        await launcher.collect(stranger)
    with pytest.raises(JobNotFoundError):
        await launcher.attach("no-such-job", execution_id=uuid4())


@pytest.mark.asyncio
async def test_attach_reacquires_the_same_job_without_resubmitting(make_launcher, tmp_path):
    launcher, runner, make_spec = make_launcher()
    handle = await launcher.submit(make_spec())

    attached = await launcher.attach(handle.job_id, execution_id=handle.execution_id)

    assert attached == handle
    await launcher.wait(attached)
    assert len(runner.calls) == 1


@pytest.mark.asyncio
async def test_running_job_cannot_be_collected_and_can_be_cancelled(make_launcher, tmp_path):
    launcher, runner, make_spec = make_launcher(delay=30)
    handle = await launcher.submit(make_spec())

    assert await launcher.status(handle) in {JobState.PENDING, JobState.RUNNING}
    with pytest.raises(JobLauncherError):
        await launcher.collect(handle)
    await launcher.cancel(handle)

    assert await launcher.wait(handle) in TERMINAL_STATES
    assert len(runner.calls) == 1


@pytest.mark.asyncio
async def test_cancelling_a_finished_job_changes_nothing(make_launcher, tmp_path):
    launcher, _, make_spec = make_launcher(exit_code=0)
    handle = await launcher.submit(make_spec())
    await launcher.wait(handle)

    await launcher.cancel(handle)

    assert await launcher.status(handle) is JobState.COMPLETED
    assert (await launcher.collect(handle)).exit_code == 0


# Local placement specifics.

@pytest.mark.asyncio
async def test_local_start_failure_is_failed_and_reraised(tmp_path):
    launcher, _, _ = _local(tmp_path, error=ContainerStartError("runtime missing"))
    handle = await launcher.submit(_spec(tmp_path))

    assert await launcher.wait(handle) is JobState.FAILED
    with pytest.raises(ContainerStartError):
        await launcher.collect(handle)


@pytest.mark.asyncio
async def test_local_cancel_requires_runner_support(tmp_path):
    launcher, runner, _ = _local(tmp_path, delay=0.5, cancellable=False)
    handle = await launcher.submit(_spec(tmp_path))

    with pytest.raises(JobLauncherError):
        await launcher.cancel(handle)
    assert await launcher.wait(handle) is JobState.COMPLETED


@pytest.mark.asyncio
async def test_local_jobs_do_not_outlive_their_launcher(tmp_path):
    launcher, _, _ = _local(tmp_path)
    handle = await launcher.submit(_spec(tmp_path))
    await launcher.wait(handle)

    restarted, _, _ = _local(tmp_path)
    with pytest.raises(JobNotFoundError):
        await restarted.attach(handle.job_id, execution_id=handle.execution_id)
