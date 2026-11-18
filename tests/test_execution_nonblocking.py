"""C13 phase 3a: local execution no longer blocks the event loop (acceptance 17)."""

from __future__ import annotations

import asyncio
import subprocess
import threading
import time
from uuid import uuid4

import pytest

from labbioagentos import (
    ContainerStartError,
    ExecutionFailureClass,
    ExecutionPlanDraft,
    ExecutionStatus,
    ExecutionSubmissionService,
    ProcessOutcome,
    ProcessRunner,
    SubprocessDockerRunner,
    TraceEventType,
    WorkflowStage,
)

from test_phase6_docker_execution import FakeDockerRunner, _environment, _plan
from test_runtime_milestone_b import MockExecutor, boundary  # noqa: F401


class BlockingRunner(ProcessRunner):
    """Blocks its thread like a real container process and records overlap."""

    def __init__(self, seconds=0.6):
        self.seconds = seconds
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def run(self, argv, *, timeout_seconds):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(self.seconds)
        with self.lock:
            self.active -= 1
        return ProcessOutcome(exit_code=0, stdout=b"", stderr=b"", duration_seconds=self.seconds)


async def _ticks_during(coroutine):
    ticks = 0
    done = asyncio.Event()

    async def ticker():
        nonlocal ticks
        while not done.is_set():
            ticks += 1
            await asyncio.sleep(0.02)

    task = asyncio.create_task(ticker())
    try:
        result = await coroutine
    finally:
        done.set()
        await task
    return result, ticks


@pytest.mark.asyncio
async def test_event_loop_keeps_running_while_a_job_runs(tmp_path):
    _, executor, _ = _environment(tmp_path, BlockingRunner(seconds=0.6))

    result, ticks = await _ticks_during(executor.execute_async(_plan()))

    assert result.status is ExecutionStatus.SUCCEEDED
    assert ticks >= 10  # About 30 are possible in 0.6 s; a blocked loop allows one.


@pytest.mark.asyncio
async def test_async_and_sync_paths_produce_the_same_result_and_trace(tmp_path):
    _, sync_executor, sync_sink = _environment(tmp_path / "sync", FakeDockerRunner(exit_code=2), traced=True)
    _, async_executor, async_sink = _environment(tmp_path / "async", FakeDockerRunner(exit_code=2), traced=True)

    sync_result = sync_executor.execute(_plan())
    async_result = await async_executor.execute_async(_plan())

    for field in ("status", "exit_code", "error_class", "error_message", "diagnostics"):
        assert getattr(sync_result, field) == getattr(async_result, field)
    assert [event.event_type for event in sync_sink.read()] == [
        event.event_type for event in async_sink.read()
    ]


@pytest.mark.asyncio
async def test_executions_on_one_executor_never_overlap(tmp_path):
    runner = BlockingRunner(seconds=0.2)
    _, executor, _ = _environment(tmp_path, runner)

    results = await asyncio.gather(*(executor.execute_async(_plan()) for _ in range(3)))

    assert all(result.status is ExecutionStatus.SUCCEEDED for result in results)
    assert runner.max_active == 1


@pytest.mark.asyncio
async def test_async_start_failure_matches_the_sync_path(tmp_path):
    class MissingRuntime(ProcessRunner):
        def run(self, argv, *, timeout_seconds):
            raise ContainerStartError("runtime missing")

    _, executor, sink = _environment(tmp_path, MissingRuntime(), traced=True)

    result = await executor.execute_async(_plan())

    assert result.status is ExecutionStatus.FAILED
    assert result.error_class is ExecutionFailureClass.CONTAINER_START_FAILURE
    assert sink.read()[-1].event_type is TraceEventType.EXECUTION_FAILED


@pytest.mark.asyncio
async def test_default_placement_uses_the_current_process_runner(tmp_path):
    _, executor, _ = _environment(tmp_path, FakeDockerRunner())
    replacement = FakeDockerRunner()
    executor.process_runner = replacement  # Integration tests swap runners after wiring.

    await executor.execute_async(_plan())

    assert len(replacement.calls) == 1


@pytest.mark.asyncio
async def test_submission_service_prefers_the_non_blocking_path(boundary):  # noqa: F811
    _, recorder, access, principal, workspace, store, _ = boundary

    class AsyncOnlyExecutor(MockExecutor):
        def execute(self, plan):
            raise AssertionError("the blocking path must not be used")

        async def execute_async(self, plan):
            return MockExecutor.execute(self, plan)

    executor = AsyncOnlyExecutor(store)
    service = ExecutionSubmissionService(
        artifact_store=store, access_service=access, executor=executor, trace_recorder=recorder,
    )

    receipt = await service.submit(
        ExecutionPlanDraft(image_key="approved", script_content="pass\n"),
        principal=principal, workspace=workspace, run_id=uuid4(),
        stage_id=WorkflowStage.EXECUTE, invocation_id=uuid4(),
    )

    assert receipt.status is ExecutionStatus.SUCCEEDED and len(executor.plans) == 1


def test_docker_runner_cancel_removes_only_the_governed_container(monkeypatch):
    execution_hex = uuid4().hex
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(tuple(argv))
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    SubprocessDockerRunner().cancel(("docker", "run", "--rm", "--name", f"labbio-{execution_hex}"))

    assert calls == [("docker", "rm", "--force", f"labbio-{execution_hex}")]
    with pytest.raises(ContainerStartError):
        SubprocessDockerRunner().cancel(("docker", "run", "--name", "someone-else"))


def test_sync_execution_cannot_bypass_a_configured_launcher(tmp_path):
    from labbioagentos.execution.launchers import LocalLauncher

    runner = FakeDockerRunner()
    _, executor, _ = _environment(tmp_path, runner)
    executor.launcher = LocalLauncher(runner)
    with pytest.raises(ContainerStartError, match="execute_async"):
        executor.execute(_plan())
    assert runner.calls == []


@pytest.mark.asyncio
async def test_unknown_runner_outcome_keeps_execution_files(tmp_path):
    from labbioagentos.execution.launchers.base import JobLauncherError

    class UnknownOutcome(ProcessRunner):
        def run(self, argv, *, timeout_seconds):
            raise JobLauncherError("Container creation receipt is unknown")

    _, executor, _ = _environment(tmp_path, UnknownOutcome())
    cleaned = []
    executor._remove_cleanup_paths = lambda plan: cleaned.append(plan.execution_id)
    with pytest.raises(JobLauncherError, match="unknown"):
        await executor.execute_async(_plan())
    assert cleaned == []


@pytest.mark.asyncio
@pytest.mark.parametrize("confirm", [True, False])
async def test_execution_cancellation_preserves_files_until_stop_is_confirmed(tmp_path, confirm):
    from labbioagentos.execution.launchers.base import (
        JobHandle, JobLauncherError, JobState,
    )

    class HeldLauncher:
        kind = "test-held"
        submitted = asyncio.Event()
        stopped = asyncio.Event()
        cancel_called = False

        async def submit(self, spec):
            self.submitted.set()
            return JobHandle(self.kind, "test-job", spec.execution_id)

        async def wait(self, handle):
            await self.stopped.wait()
            return JobState.COMPLETED

        async def cancel(self, handle):
            self.cancel_called = True
            if not confirm:
                raise JobLauncherError("Stop could not be confirmed")
            self.stopped.set()

    _, executor, _ = _environment(tmp_path, FakeDockerRunner())
    launcher = HeldLauncher()
    executor.launcher = launcher
    cleanup = []
    executor._remove_cleanup_paths = lambda plan: cleanup.append(plan.execution_id)
    plan = _plan()
    task = asyncio.create_task(executor.execute_async(plan))
    await launcher.submitted.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError if confirm else JobLauncherError):
        await task
    assert launcher.cancel_called
    assert cleanup == ([plan.execution_id] if confirm else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("during_submit", [False, True])
async def test_repeated_cancel_keeps_execution_ownership_until_launch_and_stop_finish(tmp_path, during_submit):
    from labbioagentos.execution.launchers.base import JobHandle, JobState

    entered, stop_entered, release_launch, release_stop = (asyncio.Event() for _ in range(4))

    class SlowLauncher:
        kind = "test-slow-stop"
        stops = 0

        async def submit(self, spec):
            entered.set()
            if during_submit:
                await release_launch.wait()
            return JobHandle(self.kind, "test-slow", spec.execution_id)

        async def wait(self, _):
            if not stop_entered.is_set():
                await asyncio.Event().wait()
            return JobState.COMPLETED

        async def cancel(self, _):
            self.stops += 1
            stop_entered.set()
            await release_stop.wait()

    _, executor, _ = _environment(tmp_path, FakeDockerRunner())
    launcher = SlowLauncher()
    executor.launcher = launcher
    cleanup = []
    executor._remove_cleanup_paths = lambda plan: cleanup.append(plan.execution_id)
    plan = _plan()
    task = asyncio.create_task(executor.execute_async(plan))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        release_launch.set()
        await asyncio.wait_for(stop_entered.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "Repeated cancellation released the owner before stop confirmation"
        assert executor._execution_lock.locked() and cleanup == []
        release_stop.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert launcher.stops == 1 and cleanup == [plan.execution_id]
    finally:
        release_launch.set()
        release_stop.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_ack_without_terminal_state_is_not_confirmed(tmp_path):
    from labbioagentos.execution.launchers.base import JobHandle, JobLauncherError, JobState

    entered = asyncio.Event()

    class UnknownStop:
        kind = "test-unknown-stop"
        stopped = False

        async def submit(self, spec):
            entered.set()
            return JobHandle(self.kind, "test-unknown", spec.execution_id)

        async def wait(self, _):
            if not self.stopped:
                await asyncio.Event().wait()
            return JobState.UNKNOWN

        async def cancel(self, _):
            self.stopped = True

    _, executor, _ = _environment(tmp_path, FakeDockerRunner())
    executor.launcher = UnknownStop()
    cleanup = []
    executor._remove_cleanup_paths = lambda plan: cleanup.append(plan.execution_id)
    task = asyncio.create_task(executor.execute_async(_plan()))
    await entered.wait()
    task.cancel()
    with pytest.raises(JobLauncherError, match="unconfirmed"):
        await task
    assert cleanup == []


@pytest.mark.asyncio
@pytest.mark.parametrize("known_absent", [False, True])
async def test_cancelled_launch_preserves_absent_vs_unknown_acceptance(tmp_path, known_absent):
    from labbioagentos.execution.job_ledger import JobLedger, JobLedgerState
    from labbioagentos.execution.launchers.base import JobLauncherError

    entered, release = asyncio.Event(), asyncio.Event()

    class FailedLaunch:
        kind = "test-failed-launch"

        async def submit(self, _):
            entered.set()
            await release.wait()
            if known_absent:
                raise ContainerStartError("Nothing was started")
            raise JobLauncherError("Launch acceptance unconfirmed")

        async def cancel(self, _):
            pytest.fail("No confirmed job identity was returned")

    _, executor, _ = _environment(tmp_path, FakeDockerRunner())
    executor.launcher = FailedLaunch()
    ledger = JobLedger(tmp_path / "jobs.sqlite")
    executor.job_ledger = ledger
    cleanup = []
    executor._remove_cleanup_paths = lambda plan: cleanup.append(plan.execution_id)
    plan = _plan()
    task = asyncio.create_task(executor.execute_async(plan))
    try:
        await entered.wait()
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError if known_absent else JobLauncherError):
            await task
        assert cleanup == ([plan.execution_id] if known_absent else [])
        assert ledger.get(plan.execution_id).state is (
            JobLedgerState.NOT_SUBMITTED if known_absent else JobLedgerState.SUBMITTING)
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        ledger.close()
