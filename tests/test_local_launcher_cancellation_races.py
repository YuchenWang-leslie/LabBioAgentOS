"""Regression witnesses: cancellation during launch and a stalled service manager.

No Docker or systemd processes are created. The synchronous probe uses a local
data-free Python process to exercise real pipe collection.
"""

import asyncio
import subprocess
import sys
import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest

from labbioagentos import ProcessOutcome, RequestedResources
from labbioagentos.execution.apptainer import SubprocessApptainerRunner
from labbioagentos.execution.docker import SubprocessDockerRunner
from labbioagentos.execution.launchers.base import JobSpec
from labbioagentos.execution.launchers.base import JobLauncherError
from labbioagentos.execution.launchers.local import LocalLauncher
from labbioagentos.execution.launchers.systemd import SystemdLocalLauncher


@pytest.mark.asyncio
async def test_cancel_before_docker_container_creation_prevents_late_survivor(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    state = {"container_exists": False, "running": False}

    def docker(argv, **kwargs):
        if argv[1] == "create":
            entered.set()
            assert release.wait(2)
            state.update(container_exists=True, running=False)
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if argv[1] == "start":
            state["running"] = state["container_exists"]
            return subprocess.CompletedProcess(argv, 0 if state["running"] else 1, b"", b"")
        assert argv[1:3] == ["rm", "--force"]
        if not state["container_exists"]:
            return subprocess.CompletedProcess(
                argv, 1, b"", f"Error response from daemon: No such container: {argv[-1]}".encode())
        state["running"] = False
        state["container_exists"] = False
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr("labbioagentos.execution.docker.subprocess.run", docker)
    identifier = uuid4()
    launcher = LocalLauncher(SubprocessDockerRunner())
    handle = await launcher.submit(JobSpec(
        identifier, ("docker", "run", "--name", f"labbio-{identifier.hex}", "fixture-image"),
        10, RequestedResources(), tmp_path))
    assert await asyncio.to_thread(entered.wait, 1)
    cancellation = asyncio.create_task(launcher.cancel(handle))
    # Release the simulated daemon-create window while cancellation is pending.
    await asyncio.sleep(0.02)
    release.set()
    try:
        await asyncio.wait_for(cancellation, 1)
    finally:
        release.set()
        await asyncio.wait_for(launcher.wait(handle), 1)
    assert not state["running"], "A Docker container started after cancellation and survived"


def test_docker_unknown_create_ack_is_not_started_or_replayed(monkeypatch):
    calls = []

    def create(argv, **kwargs):
        calls.append(argv)
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr("labbioagentos.execution.docker.subprocess.run", create)
    runner = SubprocessDockerRunner()
    argv = ("docker", "run", "--rm", "--name", f"labbio-{uuid4().hex}", "fixture")
    with pytest.raises(JobLauncherError, match="unconfirmed"):
        runner.run(argv, timeout_seconds=1)
    with pytest.raises(JobLauncherError, match="unconfirmed"):
        runner.cancel(argv)
    with pytest.raises(JobLauncherError, match="replayed"):
        runner.run(argv, timeout_seconds=1)
    assert len(calls) == 1 and calls[0][1] == "create"


@pytest.mark.parametrize("container_created", [True, False])
def test_docker_nonzero_create_ack_requires_confirmed_cleanup(monkeypatch, container_created):
    state = {"exists": False, "calls": []}

    def docker(argv, **kwargs):
        state["calls"].append(argv[1])
        if argv[1] == "create":
            state["exists"] = container_created
            return subprocess.CompletedProcess(argv, 1, b"", b"create response connection lost")
        assert argv[1:3] == ["rm", "--force"]
        if not state["exists"]:
            return subprocess.CompletedProcess(
                argv, 1, b"", f"Error response from daemon: No such container: {argv[-1]}".encode())
        state["exists"] = False
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    monkeypatch.setattr("labbioagentos.execution.docker.subprocess.run", docker)
    runner = SubprocessDockerRunner()
    argv = ("docker", "run", "--rm", "--name", f"labbio-{uuid4().hex}", "fixture")
    if container_created:
        outcome = runner.run(argv, timeout_seconds=1)
        assert outcome.exit_code == 1
        assert outcome.stderr == b"create response connection lost"
        runner.cancel(argv)
    else:
        # A negative lookup cannot rule out a request still accepted by the daemon.
        with pytest.raises(JobLauncherError, match="unconfirmed"):
            runner.run(argv, timeout_seconds=1)
        with pytest.raises(JobLauncherError, match="unconfirmed"):
            runner.cancel(argv)
    assert not state["exists"]
    assert state["calls"] == ["create", "rm"]
    with pytest.raises(JobLauncherError, match="replayed"):
        runner.run(argv, timeout_seconds=1)


def test_docker_failed_client_spawn_is_confirmed_not_created(monkeypatch):
    from labbioagentos.execution.errors import ContainerStartError

    calls = []

    def missing(argv, **kwargs):
        calls.append(argv[1])
        raise FileNotFoundError("docker executable unavailable")

    monkeypatch.setattr("labbioagentos.execution.docker.subprocess.run", missing)
    runner = SubprocessDockerRunner()
    argv = ("docker", "run", "--rm", "--name", f"labbio-{uuid4().hex}", "fixture")
    with pytest.raises(ContainerStartError):
        runner.run(argv, timeout_seconds=1)
    runner.cancel(argv)
    assert calls == ["create"]


def test_docker_create_preserves_all_resource_and_isolation_arguments(monkeypatch):
    calls = []
    capture_timeout = []

    def docker(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0,
                                           b"container-id" if argv[1] == "create" else b"", b"")

    def start(argv, **kwargs):
        calls.append((argv, kwargs))
        return object()

    def capture(process, *, timeout_seconds, stop):
        capture_timeout.append(timeout_seconds)
        return ProcessOutcome(7, b"analysis-output", b"", 0.01)

    monkeypatch.setattr("labbioagentos.execution.docker.subprocess.run", docker)
    monkeypatch.setattr("labbioagentos.execution.docker.subprocess.Popen", start)
    monkeypatch.setattr("labbioagentos.execution.docker.capture_process", capture)
    name = f"labbio-{uuid4().hex}"
    argv = ("docker", "run", "--rm", "--name", name, "--cpus", "4", "--memory", "4096m",
            "--pids-limit", "128", "--network", "none", "--read-only",
            "--security-opt", "no-new-privileges", "--cap-drop", "ALL", "fixture", "python", "analysis.py")
    result = SubprocessDockerRunner().run(argv, timeout_seconds=30)
    assert calls[0][0] == ["docker", "create", *argv[2:]]
    assert calls[1][0] == ["docker", "start", "--attach", name]
    assert 0 < capture_timeout[0] <= calls[0][1]["timeout"] == 30
    assert result.exit_code == 7 and result.stdout == b"analysis-output"


def test_docker_attach_client_failure_cannot_leave_running_container(monkeypatch):
    state = {"exists": False}

    def docker(argv, **kwargs):
        if argv[1] == "create":
            state["exists"] = True
            return subprocess.CompletedProcess(argv, 0, b"id", b"")
        assert argv[1:3] == ["rm", "--force"]
        state["exists"] = False
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    def start(argv, **kwargs):
        assert argv[1] == "start"
        return object()

    def capture(process, *, timeout_seconds, stop):
        return ProcessOutcome(1, b"", b"attachment transport lost", 0.01)

    monkeypatch.setattr("labbioagentos.execution.docker.subprocess.run", docker)
    monkeypatch.setattr("labbioagentos.execution.docker.subprocess.Popen", start)
    monkeypatch.setattr("labbioagentos.execution.docker.capture_process", capture)
    outcome = SubprocessDockerRunner().run(
        ("docker", "run", "--rm", "--name", f"labbio-{uuid4().hex}", "fixture"),
        timeout_seconds=10,
    )
    assert outcome.exit_code == 1
    assert not state["exists"]


@pytest.mark.asyncio
async def test_unknown_create_result_is_not_terminal(tmp_path, monkeypatch):
    from labbioagentos.execution.launchers.base import JobState

    def create(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr("labbioagentos.execution.docker.subprocess.run", create)
    identifier = uuid4()
    launcher = LocalLauncher(SubprocessDockerRunner())
    handle = await launcher.submit(JobSpec(
        identifier, ("docker", "run", "--name", f"labbio-{identifier.hex}", "fixture"),
        1, RequestedResources(), tmp_path))
    assert await launcher.wait(handle) is JobState.UNKNOWN
    with pytest.raises(JobLauncherError, match="unconfirmed"):
        await launcher.cancel(handle)


@pytest.mark.asyncio
async def test_docker_cancel_does_not_wait_on_an_attach_lock(tmp_path, monkeypatch):
    attached, removed = threading.Event(), threading.Event()

    def docker(argv, **kwargs):
        if argv[1] == "create":
            return subprocess.CompletedProcess(argv, 0, b"id", b"")
        assert argv[1:3] == ["rm", "--force"]
        removed.set()
        return subprocess.CompletedProcess(argv, 0, b"", b"")

    def start(argv, **kwargs):
        assert argv[1] == "start"
        attached.set()
        return object()

    def capture(process, *, timeout_seconds, stop):
        assert removed.wait(2)
        return ProcessOutcome(137, b"", b"", 0.01)

    monkeypatch.setattr("labbioagentos.execution.docker.subprocess.run", docker)
    monkeypatch.setattr("labbioagentos.execution.docker.subprocess.Popen", start)
    monkeypatch.setattr("labbioagentos.execution.docker.capture_process", capture)
    execution_id = uuid4()
    launcher = LocalLauncher(SubprocessDockerRunner())
    handle = await launcher.submit(JobSpec(
        execution_id, ("docker", "run", "--rm", "--name", f"labbio-{execution_id.hex}", "fixture"),
        10, RequestedResources(), tmp_path))
    assert await asyncio.to_thread(attached.wait, 1)
    await asyncio.wait_for(launcher.cancel(handle), 1)
    await launcher.wait(handle)
    assert (await launcher.collect(handle)).exit_code == 137


@pytest.mark.asyncio
@pytest.mark.parametrize("client_exit,unit_state,expected", [
    (0, "not-found", "COMPLETED"),
    (7, "failed", "COMPLETED"),
    (1, "active", "COMPLETED"),
    (1, "not-found", "UNKNOWN"),
    (1, "manager-unavailable", "UNKNOWN"),
])
async def test_systemd_client_exit_requires_service_terminal_confirmation(
    tmp_path, monkeypatch, client_exit, unit_state, expected,
):
    from labbioagentos.execution.launchers.base import JobState

    class Client:
        returncode = client_exit
        stdin = SimpleNamespace(close=lambda: None)

        def __init__(self):
            self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
            self.stdout.feed_eof()
            self.stderr.feed_eof()

        async def wait(self):
            return self.returncode

    async def spawn(*args, **kwargs):
        return Client()

    launcher = SystemdLocalLauncher(SubprocessApptainerRunner(max_output_file_bytes=8192))
    manager = {"state": unit_state, "calls": []}

    def control(*arguments):
        manager["calls"].append(arguments)
        if arguments[0] == "stop":
            manager["state"] = "failed"
            return b"", 0
        if arguments[0] == "reset-failed":
            manager["state"] = "not-found"
            return b"", 0
        assert arguments[0] == "show"
        if manager["state"] == "manager-unavailable":
            return b"", 1
        loaded = "not-found" if manager["state"] == "not-found" else "loaded"
        return f"LoadState={loaded}\nActiveState={manager['state']}\n".encode(), 0

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(launcher, "_control_sync", control, raising=False)
    handle = await launcher.submit(JobSpec(
        uuid4(), ("/bin/true",), 1, RequestedResources(), tmp_path))
    if expected == "UNKNOWN":
        with pytest.raises(JobLauncherError, match="unconfirmed"):
            await launcher.wait(handle)
        assert await launcher.status(handle) is JobState.UNKNOWN
        # A finished client cannot suppress a later real cancellation attempt.
        manager["state"] = "active"
        await launcher.cancel(handle)
        assert await launcher.status(handle) is JobState.FAILED
        assert await launcher.wait(handle) is JobState.FAILED
    else:
        assert await launcher.wait(handle) is JobState.COMPLETED
        assert (await launcher.collect(handle)).exit_code == client_exit
    assert manager["calls"]
    if unit_state == "active" or expected == "UNKNOWN":
        assert any(call[0] == "stop" for call in manager["calls"])
    assert manager["state"] in {"not-found", "inactive", "failed"}


@pytest.mark.parametrize("manager_available", [True, False])
def test_sync_systemd_probe_uses_the_same_terminal_confirmation(tmp_path, monkeypatch, manager_available):
    from labbioagentos.execution.errors import ContainerStartError

    real_popen = subprocess.Popen

    def client(argv, **kwargs):
        return real_popen(
            (sys.executable, "-I", "-c", "import sys; sys.stderr.write('client ended'); sys.exit(1)"),
            **kwargs,
        )

    launcher = SystemdLocalLauncher(SubprocessApptainerRunner(max_output_file_bytes=8192))
    manager = {"active": True, "calls": []}

    def control(*arguments):
        manager["calls"].append(arguments[0])
        if not manager_available:
            return b"", 1
        if arguments[0] == "stop":
            manager["active"] = False
            return b"", 0
        state = "active" if manager["active"] else "inactive"
        return f"LoadState=loaded\nActiveState={state}".encode(), 0

    monkeypatch.setattr("labbioagentos.execution.launchers.systemd.subprocess.Popen", client)
    monkeypatch.setattr(launcher, "_control_sync", control)
    spec = JobSpec(uuid4(), ("--probe",), 1, RequestedResources(), tmp_path)
    if manager_available:
        assert launcher.run(spec).exit_code == 1
        assert manager["calls"] == ["show", "stop", "show"] and not manager["active"]
    else:
        with pytest.raises(ContainerStartError, match="unconfirmed"):
            launcher.run(spec)
        assert launcher.probe() == {"status": "REFUSED", "failures": ["local_cgroup_limits"]}


@pytest.mark.asyncio
async def test_systemd_manager_stall_is_bounded_by_host_deadline(tmp_path, monkeypatch):
    class Process:
        returncode = None

        def __init__(self):
            self.done = asyncio.Event()
            self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
            self.stdin = SimpleNamespace(close=self.finish)
            self.terminated = False

        def finish(self):
            self.terminated = True
            self.returncode = -15
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self.done.set()

        kill = finish
        terminate = finish

        async def wait(self):
            await self.done.wait()
            return self.returncode

        async def communicate(self):
            await self.wait()
            return b"", b""

    stalled = Process()

    async def spawn(*argv, **kwargs):
        if argv[0] == "systemctl":
            stalled.finish()
            return stalled
        return stalled

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    launcher = SystemdLocalLauncher(SubprocessApptainerRunner(max_output_file_bytes=8192))
    monkeypatch.setattr(launcher, "_control_sync", lambda *_: (b"", 1))
    handle = await launcher.submit(JobSpec(
        uuid4(), ("/bin/true",), 0.01, RequestedResources(timeout_seconds=0.01), tmp_path))
    try:
        # A service never started, so its RuntimeMaxSec cannot protect this wait.
        with pytest.raises(JobLauncherError, match="unconfirmed"):
            await asyncio.wait_for(launcher.wait(handle), 0.3)
        assert stalled.terminated
    finally:
        stalled.finish()
        await asyncio.gather(launcher._jobs[handle.job_id].task, return_exceptions=True)
