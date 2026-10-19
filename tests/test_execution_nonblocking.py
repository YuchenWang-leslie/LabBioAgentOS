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
