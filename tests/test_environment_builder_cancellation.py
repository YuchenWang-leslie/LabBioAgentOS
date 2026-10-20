"""Cancellation/resource counterexamples; no daemon, packages, or scientific data."""

import asyncio
import json
import subprocess
import sys
import threading

import pytest

from labbioagentos.execution.environment_builder import DockerEnvironmentBuilder
from labbioagentos.execution.environments import EnvironmentBuildUnconfirmedError
from test_environment_builder import IMAGE_ID, backend, build


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["build.log", "verification.log"])
@pytest.mark.parametrize("outcome", ["cancel", "complete", "timeout"])
async def test_build_wait_owns_fixture_process_until_exit(
    backend, monkeypatch, phase, outcome,
):
    """A real local fixture process exposes cancellation of only the thread wait."""
    backend.builder.timeout_seconds = 1 if outcome == "timeout" else 10
    started, finished = threading.Event(), threading.Event()
    children = []
    original_popen = subprocess.Popen
    original_run = backend.builder._run
    original_build = backend.builder._build

    def popen(argv, **kwargs):
        child = original_popen(argv, **kwargs)
        children.append(child)
        started.set()
        return child

    def run(argv, path, deadline):
        if path.name != phase:
            return original_run(argv, path, deadline)
        # The actual builder's _run owns this harmless child; no Docker call occurs.
        seconds = 0 if outcome == "complete" else 30
        inventory = json.dumps({
            "installed_packages": {"examplepkg": "2.0"}, "failed_imports": [],
        })
        code, error = DockerEnvironmentBuilder._run(
            backend.builder,
            [sys.executable, "-c",
             f"import time; time.sleep({seconds}); print({inventory!r})"],
            path, deadline,
        )
        if phase == "build.log" and code == 0 and error is None:
            (backend.root / "image-id.txt").write_text(IMAGE_ID + "\n")
        return code, error

    def tracked_build(*args, **kwargs):
        try:
            return original_build(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(backend.builder, "_run", run)
    monkeypatch.setattr(backend.builder, "_build", tracked_build)
    task = asyncio.create_task(build(backend))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        if outcome == "cancel":
            task.cancel()
            with pytest.raises(EnvironmentBuildUnconfirmedError) as failed:
                await asyncio.wait_for(task, 2)
            assert failed.value.failure_code == "BUILD_CANCELLED"
            observed = {
                "thread_finished": finished.is_set(),
                "process_running": children[0].poll() is None,
            }
            assert observed == {"thread_finished": True, "process_running": False}
        elif outcome == "timeout":
            with pytest.raises(EnvironmentBuildUnconfirmedError) as failed:
                await asyncio.wait_for(task, 3)
            assert failed.value.failure_code == "BUILD_TIMEOUT"
            assert children[0].poll() is not None
            assert finished.is_set()
        else:
            result = await asyncio.wait_for(task, 3)
            assert result.failure_code is None
            assert children[0].poll() is not None
            assert finished.is_set()
    finally:
        # Kill only processes explicitly created by this fixture, including on failure.
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=2)
        if not task.done():
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert await asyncio.to_thread(finished.wait, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("command_index", [1, 2], ids=["build", "probe"])
async def test_environment_commands_declare_a_hard_cpu_limit(backend, command_index):
    backend.builder.cpus = 1.5
    await build(backend)
    command = backend.calls[command_index]
    if command_index == 1:
        assert command[command.index("--cpu-period") + 1] == "100000"
        assert command[command.index("--cpu-quota") + 1] == "150000"
    else:
        assert command[command.index("--cpus") + 1] == "1.5"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_step", ["cleanup-list.log", "build-cleanup.log"])
async def test_unconfirmed_cleanup_is_not_an_ordinary_confirmed_failure(
    backend, failure_step,
):
    backend.responses["build.log"] = (None, "BUILD_TIMEOUT", "")
    backend.responses["cleanup-list.log"] = (0, None, "0123456789ab\n")
    with pytest.raises(EnvironmentBuildUnconfirmedError) as interrupted:
        await build(backend)
    assert interrupted.value.reason_code == "DAEMON_TERMINATION_UNCONFIRMED"
    assert interrupted.value.failure_code == "BUILD_TIMEOUT"
    backend.root = backend.root.with_name("b" * 32)
    backend.root.mkdir(mode=0o700)
    backend.responses[failure_step] = (1, None, "")
    with pytest.raises(EnvironmentBuildUnconfirmedError) as failed:
        await build(backend)
    assert failed.value.reason_code == (
        "CLEANUP_LIST_FAILED" if failure_step == "cleanup-list.log" else "CLEANUP_REMOVE_FAILED"
    )
    assert failed.value.failure_code == "BUILD_TIMEOUT"


@pytest.mark.asyncio
async def test_cancel_before_build_command_starts_no_later_process(backend, monkeypatch):
    inspected, release = threading.Event(), threading.Event()
    original_run = backend.builder._run

    def run(argv, path, deadline):
        result = original_run(argv, path, deadline)
        if path.name == "base-inspect.log":
            inspected.set()
            assert release.wait(2)
        return result

    monkeypatch.setattr(backend.builder, "_run", run)
    task = asyncio.create_task(build(backend))
    try:
        assert await asyncio.to_thread(inspected.wait, 2)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
        assert len(backend.calls) == 1
        assert not (backend.root / "context").exists()
    finally:
        release.set()


@pytest.mark.asyncio
async def test_cleanup_confirmation_failure_is_unconfirmed(backend):
    backend.responses["build.log"] = (None, "BUILD_TIMEOUT", "")
    backend.responses["cleanup-list.log"] = (0, None, "0123456789ab\n")
    backend.responses["cleanup-confirm.log"] = (0, None, "0123456789ab\n")
    with pytest.raises(EnvironmentBuildUnconfirmedError) as failed:
        await build(backend)
    assert failed.value.reason_code == "CLEANUP_CONFIRM_FAILED"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["build.log", "verification.log"])
@pytest.mark.parametrize("cause", ["BUILD_TIMEOUT", "BUILD_LOG_LIMIT", "BUILD_CANCELLED"])
async def test_interrupted_daemon_operation_is_unknown_even_after_empty_cleanup(backend, phase, cause):
    backend.responses[phase] = (None, cause, "")
    with pytest.raises(EnvironmentBuildUnconfirmedError) as failed:
        await build(backend)
    assert failed.value.reason_code == "DAEMON_TERMINATION_UNCONFIRMED"
    assert failed.value.failure_code == cause


def test_fast_successful_process_still_checks_bounded_log(tmp_path, monkeypatch):
    class Process:
        def wait(self, timeout):
            return 0

        def kill(self):
            pytest.fail("completed process must not be killed")

    def popen(argv, **kwargs):
        kwargs["stdout"].write(b"x" * 16_777_217)
        return Process()

    monkeypatch.setattr(subprocess, "Popen", popen)
    result = DockerEnvironmentBuilder()._run(
        ["docker", "build", "synthetic-context"], tmp_path / "build.log", deadline=1e12,
    )
    assert result == (0, "BUILD_LOG_LIMIT")


@pytest.mark.asyncio
async def test_cancellation_of_one_attempt_does_not_cancel_another(backend, monkeypatch):
    first_root = backend.root
    second_root = first_root.with_name("b" * 32)
    second_root.mkdir(mode=0o700)
    started = threading.Event()
    children = []
    original_run = backend.builder._run
    original_popen = subprocess.Popen

    def popen(argv, **kwargs):
        child = original_popen(argv, **kwargs)
        children.append(child)
        started.set()
        return child

    def run(argv, path, deadline):
        if path.parent == first_root and path.name == "build.log":
            return DockerEnvironmentBuilder._run(
                backend.builder, [sys.executable, "-c", "import time; time.sleep(30)"],
                path, deadline,
            )
        result = original_run(argv, path, deadline)
        if path.parent == second_root and path.name == "build.log":
            (second_root / "image-id.txt").write_text(IMAGE_ID + "\n")
        return result

    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(backend.builder, "_run", run)
    first = asyncio.create_task(build(backend))
    try:
        assert await asyncio.to_thread(started.wait, 2)
        second = await backend.builder.build(
            base_image=backend.base, requirements=("ExamplePkg == 2.0",),
            import_modules=("examplepkg",), build_root=second_root,
            request_hash="d" * 64,
        )
        assert second.image_id == IMAGE_ID
        first.cancel()
        with pytest.raises(EnvironmentBuildUnconfirmedError) as failed:
            await asyncio.wait_for(first, 2)
        assert failed.value.failure_code == "BUILD_CANCELLED"
        assert children[0].poll() is not None
    finally:
        if not first.done():
            first.cancel()
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=2)


@pytest.mark.asyncio
async def test_repeated_cancel_waits_for_attempt_cleanup(backend, monkeypatch):
    backend.responses["build.log"] = (None, "BUILD_TIMEOUT", "")
    cleanup_started, release_cleanup = threading.Event(), threading.Event()
    original_run = backend.builder._run

    def run(argv, path, deadline):
        if path.name == "cleanup-list.log":
            cleanup_started.set()
            assert release_cleanup.wait(2)
        return original_run(argv, path, deadline)

    monkeypatch.setattr(backend.builder, "_run", run)
    task = asyncio.create_task(build(backend))
    try:
        assert await asyncio.to_thread(cleanup_started.wait, 2)
        task.cancel()
        task.cancel()
        release_cleanup.set()
        with pytest.raises(EnvironmentBuildUnconfirmedError) as failed:
            await asyncio.wait_for(task, 2)
        assert failed.value.failure_code == "BUILD_TIMEOUT"
        assert not any(argv[:2] == ["docker", "run"] for argv in backend.calls)
        assert [argv[1] for argv in backend.calls][-1] == "ps"
    finally:
        release_cleanup.set()
