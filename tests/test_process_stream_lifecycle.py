"""Data-free witnesses for bounded execution logs and owner-side cleanup."""

import asyncio
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from labbioagentos import (
    ExecutionFailureClass, ExecutionReceipt, ExecutionStatus, ProcessOutcome, RequestedResources,
)
from labbioagentos.execution.docker import SubprocessDockerRunner
from labbioagentos.execution.launchers import BUILTIN_TEMPLATES, SchedulerLauncher, SchedulerPolicy
from labbioagentos.execution.launchers.base import JobHandle, JobLauncherError, JobSpec
from labbioagentos.execution.launchers.systemd import SystemdLocalLauncher
from labbioagentos.execution.apptainer import SubprocessApptainerRunner
from labbioagentos.execution.process_streams import MAX_CAPTURED_STREAM_BYTES as LIMIT
from labbioagentos.execution.process_streams import capture_process

from test_phase6_docker_execution import FakeDockerRunner, _environment, _plan


def _local_docker_client(monkeypatch, code, *, cleanup_succeeds=True):
    real_popen = subprocess.Popen
    state = {"removed": 0, "client": None, "created_name": None}

    def docker(argv, **kwargs):
        if argv[1] == "create":
            state["created_name"] = argv[argv.index("--name") + 1]
            return subprocess.CompletedProcess(argv, 0, b"id", b"")
        assert argv[1:3] == ["rm", "--force"]
        assert argv[-1] == state["created_name"]
        state["removed"] += 1
        if cleanup_succeeds:
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        return subprocess.CompletedProcess(argv, 1, b"", b"cleanup refused")

    def start(argv, **kwargs):
        assert argv[1:3] == ["start", "--attach"]
        state["client"] = real_popen((sys.executable, "-I", "-c", code), **kwargs)
        return state["client"]

    monkeypatch.setattr("labbioagentos.execution.docker.subprocess.run", docker)
    monkeypatch.setattr("labbioagentos.execution.docker.subprocess.Popen", start)
    return state


def _docker_argv():
    return ("docker", "run", "--name", f"labbio-{uuid4().hex}", "fixture")


@pytest.mark.parametrize("stream", [1, 2])
def test_docker_stream_overflow_stops_exact_container_and_client(monkeypatch, stream):
    state = _local_docker_client(
        monkeypatch, f"import os; os.write({stream}, b'x' * {LIMIT + 1})")
    outcome = SubprocessDockerRunner().run(_docker_argv(), timeout_seconds=10)

    assert outcome.output_limit_exceeded and not outcome.timed_out
    assert outcome.exit_code is None
    assert len(outcome.stdout if stream == 1 else outcome.stderr) == LIMIT
    assert state["removed"] >= 1
    assert state["client"].poll() is not None


def test_docker_dual_continuous_streams_converge_after_overflow(monkeypatch):
    state = _local_docker_client(monkeypatch,
        "import os\nchunk = b'x' * 65536\nwhile True:\n"
        "    os.write(1, chunk)\n    os.write(2, chunk)")
    outcome = SubprocessDockerRunner().run(_docker_argv(), timeout_seconds=10)

    assert outcome.output_limit_exceeded and not outcome.timed_out
    assert len(outcome.stdout) <= LIMIT and len(outcome.stderr) <= LIMIT
    assert state["removed"] >= 1 and state["client"].poll() is not None


def test_failed_docker_owner_stop_does_not_become_a_terminal_outcome(monkeypatch):
    state = _local_docker_client(
        monkeypatch, f"import os; os.write(1, b'x' * {LIMIT + 1})",
        cleanup_succeeds=False)
    with pytest.raises(JobLauncherError, match="could not be removed"):
        SubprocessDockerRunner().run(_docker_argv(), timeout_seconds=10)

    assert state["removed"] == 1
    assert state["client"].poll() is not None


@pytest.mark.parametrize("confirmed_absent", [True, False])
def test_docker_auto_removal_in_progress_needs_exact_read_only_confirmation(
    monkeypatch, confirmed_absent,
):
    argv = _docker_argv()
    name = argv[argv.index("--name") + 1]
    calls = []

    def docker(command, **kwargs):
        calls.append(command)
        if command[1:3] == ["rm", "--force"]:
            return subprocess.CompletedProcess(command, 1, b"", (
                f"Error response from daemon: removal of container {name} is already in progress\n"
            ).encode())
        assert command[1:3] == ["container", "inspect"] and command[-1] == name
        if confirmed_absent:
            return subprocess.CompletedProcess(command, 1, b"[]\n", (
                f"Error response from daemon: No such container: {name}\n"
            ).encode())
        return subprocess.CompletedProcess(command, 1, b"", b"daemon unavailable")

    monkeypatch.setattr("labbioagentos.execution.docker.subprocess.run", docker)
    if confirmed_absent:
        SubprocessDockerRunner._remove_timed_out_container(argv, allow_missing=True)
    else:
        with pytest.raises(JobLauncherError, match="could not be removed"):
            SubprocessDockerRunner._remove_timed_out_container(argv, allow_missing=True)
    assert [call[1:3] for call in calls] == [["rm", "--force"], ["container", "inspect"]]


def test_owner_stop_transport_failure_is_not_automatically_retried():
    process = subprocess.Popen(
        (sys.executable, "-I", "-c", f"import os; os.write(1, b'x' * {LIMIT + 1})"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    attempts = []

    def stop():
        attempts.append(True)
        raise OSError("controlled stop failure")

    with pytest.raises(OSError, match="controlled stop failure"):
        capture_process(process, timeout_seconds=10, stop=stop)
    assert len(attempts) == 1
    assert process.poll() is not None


@pytest.mark.asyncio
async def test_scheduler_collect_bounds_memory_without_rewriting_original_logs(tmp_path):
    identifier = uuid4()
    job = tmp_path / str(identifier) / "job"
    job.mkdir(parents=True)
    (job / "job_id").write_text("777", encoding="ascii")
    (job / "exit_code").write_text("0 1 0\n", encoding="ascii")
    log = job / "stdout.log"
    log.write_bytes(b"x" * LIMIT)
    launcher = SchedulerLauncher(
        template=BUILTIN_TEMPLATES["slurm"],
        policy=SchedulerPolicy(max_output_file_bytes=LIMIT + 1024),
        workspace_root=tmp_path,
    )
    handle = JobHandle("scheduler", "777", identifier)

    exact = await launcher.collect(handle)
    assert len(exact.stdout) == LIMIT and not exact.output_limit_exceeded
    with log.open("ab") as stream:
        stream.write(b"y")
    exceeded = await launcher.collect(handle)
    assert len(exceeded.stdout) == LIMIT and exceeded.output_limit_exceeded
    assert exceeded.exit_code is None and not exceeded.timed_out
    assert log.stat().st_size == LIMIT + 1
    with log.open("rb") as stream:
        stream.seek(-1, 2)
        assert stream.read() == b"y"


def test_log_limit_receipt_retains_only_incomplete_raw_stream_prefixes(tmp_path):
    class LimitedRunner(FakeDockerRunner):
        def run(self, argv, *, timeout_seconds):
            return ProcessOutcome(None, b"stdout prefix", b"stderr prefix", 0.1,
                                  output_limit_exceeded=True)

    _, executor, _ = _environment(tmp_path, LimitedRunner())
    result = executor.execute(_plan())
    receipt = ExecutionReceipt.from_result(result)

    assert result.status is ExecutionStatus.FAILED
    assert result.error_class is ExecutionFailureClass.LOG_LIMIT
    assert receipt.issue_codes == (ExecutionFailureClass.LOG_LIMIT,)
    assert not receipt.retryable and result.output_artifact_refs == ()
    for ref, content in ((result.stdout_ref, b"stdout prefix"),
                         (result.stderr_ref, b"stderr prefix")):
        assert ref.metadata["capture_complete"] is False
        assert Path(ref.storage_locator).read_bytes() == content


def test_timeout_and_log_limit_are_both_retained_without_scientific_outputs(tmp_path):
    class LimitedRunner(FakeDockerRunner):
        def run(self, argv, *, timeout_seconds):
            return ProcessOutcome(None, b"stdout prefix", b"stderr prefix", 0.1,
                                  timed_out=True, output_limit_exceeded=True)

    _, executor, _ = _environment(tmp_path, LimitedRunner())
    result = executor.execute(_plan())
    receipt = ExecutionReceipt.from_result(result)

    assert result.status is ExecutionStatus.TIMED_OUT
    assert result.error_class is ExecutionFailureClass.TIMEOUT
    assert set(receipt.issue_codes) == {
        ExecutionFailureClass.TIMEOUT, ExecutionFailureClass.LOG_LIMIT,
    }
    assert result.output_artifact_refs == ()
    assert result.stdout_ref.metadata["capture_complete"] is False
    assert result.stderr_ref.metadata["capture_complete"] is False


@pytest.mark.asyncio
async def test_systemd_dual_continuous_streams_stop_when_limit_reached(monkeypatch):
    launcher = SystemdLocalLauncher(SubprocessApptainerRunner(max_output_file_bytes=1_048_576))
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-I", "-c",
        "import os\nchunk = b'x' * 65536\nwhile True:\n"
        "    os.write(1, chunk)\n    os.write(2, chunk)",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    loop = asyncio.get_running_loop()
    confirmations = []

    def confirm(name, code):
        confirmations.append((name, code))
        if process.returncode is None:
            loop.call_soon_threadsafe(lambda: process.kill() if process.returncode is None else None)

    monkeypatch.setattr(launcher, "_confirm_terminal", confirm)
    outcome = await launcher._outcome(process, 10, "synthetic-no-service")

    assert outcome.output_limit_exceeded and not outcome.timed_out
    assert len(outcome.stdout) <= LIMIT and len(outcome.stderr) <= LIMIT
    assert confirmations and process.returncode is not None


@pytest.mark.asyncio
async def test_systemd_timeout_retains_late_stream_overflow(monkeypatch):
    from labbioagentos.execution.launchers import systemd

    loop = asyncio.get_running_loop()
    exited = asyncio.Event()

    class Process:
        returncode = None
        stdout = object()
        stderr = object()
        stdin = SimpleNamespace(close=lambda: None)

        async def wait(self):
            await exited.wait()
            return self.returncode

    process = Process()

    async def read_after_exit(stream, overflow):
        await exited.wait()
        if stream is process.stdout:
            overflow.set()
        return b"bounded prefix"

    def confirm(_name, _code):
        def finish():
            process.returncode = 1
            exited.set()
        loop.call_soon_threadsafe(finish)

    launcher = SystemdLocalLauncher(SubprocessApptainerRunner(max_output_file_bytes=1_048_576))
    monkeypatch.setattr(systemd, "read_bounded_stream", read_after_exit)
    monkeypatch.setattr(launcher, "_confirm_terminal", confirm)

    outcome = await launcher._outcome(process, 0.01, "synthetic-timeout")

    assert outcome.timed_out and outcome.output_limit_exceeded
    assert outcome.exit_code is None


def test_systemd_synchronous_probe_preserves_both_terminal_flags(monkeypatch, tmp_path):
    from labbioagentos.execution.launchers import systemd

    class Process:
        returncode = 1
        stdin = SimpleNamespace(close=lambda: None)

    launcher = SystemdLocalLauncher(SubprocessApptainerRunner(max_output_file_bytes=1_048_576))
    monkeypatch.setattr(systemd.subprocess, "Popen", lambda *args, **kwargs: Process())
    monkeypatch.setattr(systemd, "capture_process", lambda *args, **kwargs: ProcessOutcome(
        None, b"bounded prefix", b"", 0.1, timed_out=True, output_limit_exceeded=True,
    ))
    monkeypatch.setattr(launcher, "_confirm_terminal", lambda *_args: None)

    outcome = launcher.run(JobSpec(uuid4(), ("fixture",), 10, RequestedResources(), tmp_path))

    assert outcome.timed_out and outcome.output_limit_exceeded
    assert outcome.exit_code is None


@pytest.mark.asyncio
async def test_systemd_cleanup_does_not_kill_client_that_exited_during_confirmation(monkeypatch):
    from labbioagentos.execution.launchers import systemd

    loop = asyncio.get_running_loop()
    exited = asyncio.Event()

    class Process:
        returncode = None
        stdout = object()
        stderr = object()
        stdin = SimpleNamespace(close=lambda: None)

        async def wait(self):
            await exited.wait()
            return self.returncode

        def kill(self):
            raise AssertionError("already exited client must not be killed")

    process = Process()

    async def blocked_reader(_stream, _overflow):
        await asyncio.Event().wait()

    def confirm(_name, _code):
        def finish():
            process.returncode = 0
            exited.set()
        loop.call_soon_threadsafe(finish)

    launcher = SystemdLocalLauncher(SubprocessApptainerRunner(max_output_file_bytes=1_048_576))
    monkeypatch.setattr(systemd, "read_bounded_stream", blocked_reader)
    monkeypatch.setattr(launcher, "_confirm_terminal", confirm)
    task = asyncio.create_task(launcher._outcome(process, 10, "synthetic-cancel"))
    await asyncio.sleep(0)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.returncode == 0
