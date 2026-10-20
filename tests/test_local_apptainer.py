"""Local Apptainer is explicit, process-tree bounded, and fails closed."""

import asyncio
from contextlib import closing
from uuid import uuid4

import pytest

from labbioagentos import cli
from labbioagentos.execution.apptainer import SubprocessApptainerRunner
from labbioagentos.execution.launchers.base import JobSpec
from labbioagentos.local_config import build_application, load_settings
from test_apptainer_configuration import _write
from labbioagentos import RequestedResources


def test_local_apptainer_configuration_and_wiring(tmp_path):
    from labbioagentos.execution.launchers.systemd import SystemdLocalLauncher

    settings = load_settings(_write(tmp_path, launcher="", enforcement="systemd-cgroups"))
    app = build_application(settings, tmp_path / "run", load_provider=False)
    try:
        assert isinstance(app.container_executor.launcher, SystemdLocalLauncher)
        assert app.container_executor.command_builder.resource_enforcement == "systemd-cgroups"
    finally:
        cli._close(app)


def test_local_environment_builds_use_the_same_bounded_job_contract(tmp_path):
    from labbioagentos.execution.job_ledger import JobLedger
    from labbioagentos.execution.launchers.systemd import SystemdLocalLauncher
    from labbioagentos import ApprovedImageRegistry
    from labbioagentos.local_config import _environment_wiring

    settings = load_settings(_write(
        tmp_path, launcher="", enforcement="systemd-cgroups",
        extra='[environment]\nroot = "envs"\n[environment.local_apptainer]\nmachine = "x86_64"\n',
    ))
    run = tmp_path / "run"
    with closing(JobLedger(run / "jobs.sqlite")) as ledger:
        wiring = _environment_wiring(settings, run, ledger)
        service = wiring["environment_service_factory"](ApprovedImageRegistry(()))
        builder = service.builder
        assert isinstance(builder.build_launcher, SystemdLocalLauncher)
        assert builder.download_launcher is builder.build_launcher
        assert builder.download_runner is None
        assert builder.workspace_root == run / "environment-jobs"


@pytest.mark.parametrize("enforcement", ["scheduler", "apptainer-cgroups"])
def test_local_apptainer_refuses_incomplete_process_tree_enforcement(tmp_path, enforcement):
    with pytest.raises(ValueError):
        load_settings(_write(tmp_path, launcher="", enforcement=enforcement))


def test_scheduler_refuses_local_resource_owner(tmp_path):
    with pytest.raises(ValueError):
        load_settings(_write(tmp_path, enforcement="systemd-cgroups"))


def test_host_dbus_environment_is_preserved_but_credentials_are_not(monkeypatch):
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("APPTAINERENV_OPENAI_API_KEY", "secret")
    env = SubprocessApptainerRunner(max_output_file_bytes=1024).environment()
    assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/1000/bus"
    assert env["XDG_RUNTIME_DIR"] == "/run/user/1000"
    assert "secret" not in env.values()


def test_systemd_command_constrains_entire_tree_and_does_not_inherit_manager_env(tmp_path):
    from labbioagentos.execution.launchers.systemd import SystemdLocalLauncher

    runner = SubprocessApptainerRunner(max_output_file_bytes=8192)
    launcher = SystemdLocalLauncher(runner)
    resources = RequestedResources(cpus=1.5, memory_mb=512, pids_limit=64, timeout_seconds=60)
    spec = JobSpec(uuid4(), ("/bin/echo", "$HOME"), 60, resources, tmp_path)
    argv = launcher.command(spec)
    for item in (
        "--property=CPUQuota=150%", "--property=MemoryMax=536870912",
        "--property=MemorySwapMax=0", "--property=TasksMax=64",
        "--property=RuntimeMaxSec=60", "--property=KillMode=control-group",
        "--property=LimitFSIZE=8192", "--pipe", "--wait",
    ):
        assert item in argv
    assert "/usr/bin/env" in argv and "-i" in argv
    # systemd performs its own variable expansion; no shell is involved.
    assert argv[-1] == "$$HOME"
    assert "--scope" not in argv
    assert "--collect" not in argv  # Failed unit status must survive until checked.


def test_cgroup_probe_rejects_unbounded_or_missing_limits():
    from labbioagentos.execution.apptainer_probe import cgroup_limits_enforced

    good = {"cpu.max": "100000 100000", "memory.max": "268435456", "pids.max": "32"}
    assert cgroup_limits_enforced(good)
    for key in good:
        assert not cgroup_limits_enforced({**good, key: "max"})
        assert not cgroup_limits_enforced({k: v for k, v in good.items() if k != key})
    assert not cgroup_limits_enforced({**good, "cpu.max": "200000 100000"})


def test_supervisor_refuses_before_starting_command_when_limits_are_missing(monkeypatch):
    from labbioagentos.execution.launchers import _local_supervisor as supervisor

    monkeypatch.setattr("sys.argv", ["supervisor", "1", "268435456", "32", "/bin/true"])
    monkeypatch.setattr(supervisor, "read_limits", lambda: {"cpu.max": "max 100000"})
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("executed"))
    assert supervisor.main() == 125


def test_local_backend_check_does_not_access_scheduler(tmp_path):
    from labbioagentos.execution.apptainer import SifStore
    from labbioagentos.local_backend import check_backend
    from test_apptainer_configuration import _capabilities, _sif

    settings = load_settings(_write(tmp_path, launcher="", enforcement="systemd-cgroups"))
    SifStore(settings.execution.apptainer.sif_store).register(_sif(tmp_path))
    report, okay = check_backend(
        settings, probe=lambda **_: _capabilities(),
        scheduler_check=lambda *_: pytest.fail("Local backend requested a scheduler"),
        local_check=lambda: {"status": "REFUSED", "failures": ["local_cgroup_limits"]},
    )
    assert not okay and report["scheduler"] is None
    assert report["configuration_failures"] == ["local_cgroup_limits"]


@pytest.mark.asyncio
async def test_reconnected_service_is_stopped_without_replaying(tmp_path, monkeypatch):
    from labbioagentos.execution.launchers.systemd import SystemdLocalLauncher
    from labbioagentos.execution.launchers.base import JobState

    launcher = SystemdLocalLauncher(SubprocessApptainerRunner(max_output_file_bytes=8192))
    execution_id = uuid4()
    queries = []

    async def show(name, property_name):
        queries.append((name, property_name))
        return (b"loaded" if property_name == "LoadState" else b"active"), 0

    manager_state = {"active": True}

    def control(*arguments):
        if arguments[0] == "stop":
            manager_state["active"] = False
            return b"", 0
        assert arguments[0] == "show" and arguments[1] == f"labbio-{execution_id.hex}.service"
        state = "active" if manager_state["active"] else "inactive"
        return f"LoadState=loaded\nActiveState={state}".encode(), 0

    monkeypatch.setattr(launcher, "_show", show)
    monkeypatch.setattr(launcher, "_control_sync", control)
    handle = await launcher.find(execution_id)
    assert await launcher.status(handle) == JobState.RUNNING
    await launcher.cancel(handle)
    assert await launcher.wait(handle) == JobState.FAILED  # No fabricated exit outcome.
    assert len(queries) == 2


@pytest.mark.asyncio
async def test_systemd_submit_cancel_and_collect_contract(tmp_path, monkeypatch):
    from labbioagentos.execution.launchers.systemd import SystemdLocalLauncher
    from labbioagentos.execution.launchers.base import JobState

    calls = []

    class Input:
        def close(self):
            pass

    class Process:
        stdin = Input()
        returncode = None

        def __init__(self):
            self.done = asyncio.Event()
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()

        async def wait(self):
            await self.done.wait()
            return self.returncode

        async def communicate(self):
            await self.done.wait()
            return b"complete", b""

        def finish(self):
            self.returncode = 0
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self.done.set()

    job = Process()
    loop = asyncio.get_running_loop()
    manager_state = {"active": True}

    async def spawn(*argv, **kwargs):
        calls.append((argv, kwargs))
        return job

    def control(*arguments):
        calls.append((arguments, {}))
        if arguments[0] == "stop":
            manager_state["active"] = False
            loop.call_soon_threadsafe(job.finish)
            return b"", 0
        state = "active" if manager_state["active"] else "inactive"
        return f"LoadState=loaded\nActiveState={state}".encode(), 0

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    launcher = SystemdLocalLauncher(SubprocessApptainerRunner(max_output_file_bytes=8192))
    monkeypatch.setattr(launcher, "_control_sync", control)
    spec = JobSpec(uuid4(), ("/bin/true",), 60, RequestedResources(), tmp_path)
    handle = await launcher.submit(spec)
    assert await launcher.status(handle) == JobState.RUNNING
    await launcher.cancel(handle)
    assert await launcher.wait(handle) == JobState.COMPLETED
    assert (await launcher.collect(handle)).exit_code == 0
    assert any(argv == ("stop", handle.job_id) for argv, _ in calls)
    assert calls[0][1]["stdin"] == asyncio.subprocess.PIPE
    assert "preexec_fn" not in calls[0][1]
