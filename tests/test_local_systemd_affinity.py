"""Integer-core local execution without privileged CPU-controller delegation."""

import errno
import json
from uuid import uuid4

import pytest

from labbioagentos import RequestedResources
from labbioagentos.execution.apptainer import SubprocessApptainerRunner
from labbioagentos.execution.launchers import _local_supervisor as supervisor
from labbioagentos.execution.launchers.base import JobLauncherError, JobSpec
from labbioagentos.execution.launchers.systemd import SystemdLocalLauncher


def _limits():
    return {"memory.max": "268435456", "memory.swap.max": "0", "pids.max": "32",
            "cpu.affinity": [2], "cpu.affinity_locked": True,
            "no_new_privs": True, "seccomp_filter": True}


def test_affinity_command_preserves_other_hard_limits(tmp_path, monkeypatch):
    monkeypatch.setattr("os.sched_getaffinity", lambda _: {2, 4, 6})
    launcher = SystemdLocalLauncher(SubprocessApptainerRunner(max_output_file_bytes=8192),
                                   resource_enforcement="systemd-affinity")
    spec = JobSpec(uuid4(), ("/bin/true",), 20,
                   RequestedResources(cpus=2, memory_mb=256, pids_limit=32), tmp_path)
    command = launcher.command(spec)
    for item in ("--property=CPUAffinity=2,4", "--property=NoNewPrivileges=yes",
                 "--property=SystemCallArchitectures=native",
                 "--property=SystemCallFilter=~sched_setaffinity",
                 "--property=SystemCallErrorNumber=EPERM", "--property=MemoryMax=268435456",
                 "--property=MemorySwapMax=0", "--property=TasksMax=32",
                 "--property=KillMode=control-group", "--property=RuntimeMaxSec=20"):
        assert item in command
    assert not any("CPUQuota=" in item for item in command)
    assert command[-3:] == ("--systemd-affinity", "2,4", "/bin/true")


@pytest.mark.parametrize("cpus", [0.5, 1.5, 4])
def test_affinity_mode_refuses_fractional_or_unavailable_cores(tmp_path, monkeypatch, cpus):
    monkeypatch.setattr("os.sched_getaffinity", lambda _: {2, 4, 6})
    launcher = SystemdLocalLauncher(SubprocessApptainerRunner(max_output_file_bytes=8192),
                                   resource_enforcement="systemd-affinity")
    spec = JobSpec(uuid4(), ("/bin/true",), 20, RequestedResources(cpus=cpus), tmp_path)
    with pytest.raises(JobLauncherError):
        launcher.command(spec)


def test_unknown_resource_owner_is_not_an_automatic_fallback():
    with pytest.raises(ValueError):
        SystemdLocalLauncher(SubprocessApptainerRunner(max_output_file_bytes=8192),
                             resource_enforcement="unknown")


def test_original_quota_mode_still_supports_fractional_cpu(tmp_path):
    launcher = SystemdLocalLauncher(SubprocessApptainerRunner(max_output_file_bytes=8192))
    spec = JobSpec(uuid4(), ("/bin/true",), 20, RequestedResources(cpus=1.5), tmp_path)
    command = launcher.command(spec)
    assert "--property=CPUQuota=150%" in command
    assert "--systemd-affinity" not in command


@pytest.mark.parametrize("key,value", [
    ("memory.max", "max"), ("memory.swap.max", "1"), ("pids.max", "33"),
    ("cpu.affinity", [2, 4]), ("cpu.affinity", [2, 2]), ("cpu.affinity", []),
    ("cpu.affinity_locked", False), ("no_new_privs", False), ("seccomp_filter", False),
])
def test_affinity_proof_refuses_incomplete_or_excessive_boundaries(key, value):
    limits = _limits()
    assert supervisor.affinity_enforced(limits, 1, 268435456, 32)
    assert not supervisor.affinity_enforced({**limits, key: value}, 1, 268435456, 32)
    assert not supervisor.affinity_enforced({k: v for k, v in limits.items() if k != key},
                                           1, 268435456, 32)


@pytest.mark.parametrize("denial", [None, errno.EPERM, errno.EACCES])
def test_observed_affinity_lock_requires_a_real_eperm(monkeypatch, denial):
    monkeypatch.setattr(supervisor, "read_limits", lambda _names: {
        "memory.max": "268435456", "memory.swap.max": "0", "pids.max": "32"})
    monkeypatch.setattr(supervisor.Path, "read_text", lambda _: "NoNewPrivs:\t1\nSeccomp:\t2\n")
    monkeypatch.setattr(supervisor.os, "sched_getaffinity", lambda _: {2})

    def set_affinity(pid, mask):
        assert pid == 0 and mask == {2}
        if denial is not None:
            raise OSError(denial, "probe")

    monkeypatch.setattr(supervisor.os, "sched_setaffinity", set_affinity)
    observed = supervisor.read_affinity_limits()
    assert observed["cpu.affinity_locked"] is (denial == errno.EPERM)
    assert supervisor.affinity_enforced(observed, 1, 268435456, 32) is (denial == errno.EPERM)


@pytest.mark.parametrize("actual", [[2], [4]])
def test_supervisor_checks_the_selected_cpu_identity_before_execution(monkeypatch, capsys, actual):
    monkeypatch.setattr("sys.argv", ["supervisor", "1", "268435456", "32",
                                     "--systemd-affinity", "2", "--probe"])
    monkeypatch.setattr(supervisor, "read_affinity_limits", lambda: {**_limits(), "cpu.affinity": actual})
    monkeypatch.setattr(supervisor.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("executed"))
    assert supervisor.main() == (0 if actual == [2] else 125)
    if actual == [2]:
        assert json.loads(capsys.readouterr().out) == _limits()


def test_failed_affinity_capability_probe_is_typed_not_replayed(monkeypatch):
    launcher = SystemdLocalLauncher(SubprocessApptainerRunner(max_output_file_bytes=8192),
                                   resource_enforcement="systemd-affinity")
    monkeypatch.setattr(launcher, "run", lambda _: (_ for _ in ()).throw(JobLauncherError("unavailable")))
    assert launcher.probe() == {"status": "REFUSED", "failures": ["local_affinity_limits"]}
