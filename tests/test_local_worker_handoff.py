"""Terminal service state alone is not permission to replace a Native writer."""

from pathlib import Path
from uuid import uuid4

import pytest

from labbioagentos.execution.apptainer import SubprocessApptainerRunner
from labbioagentos.execution.launchers.base import JobLauncherError, JobState
from labbioagentos.execution.launchers.systemd import SystemdLocalLauncher


def launcher():
    return SystemdLocalLauncher(SubprocessApptainerRunner(max_output_file_bytes=8192))


@pytest.mark.asyncio
@pytest.mark.parametrize("output,code,expected", [
    (b"", 0, JobState.UNKNOWN), (b"unrecognized", 0, JobState.UNKNOWN),
    (b"active", 1, JobState.UNKNOWN),
    (b"active", 0, JobState.RUNNING), (b"activating", 0, JobState.RUNNING),
    (b"deactivating", 0, JobState.RUNNING), (b"reloading", 0, JobState.RUNNING),
    (b"inactive", 0, JobState.FAILED), (b"failed", 0, JobState.FAILED),
])
async def test_reconnected_status_does_not_turn_unknown_into_running(monkeypatch, output, code, expected):
    owner = launcher()

    async def show(_, property_name):
        return (b"loaded", 0) if property_name == "LoadState" else (output, code)

    monkeypatch.setattr(owner, "_show", show)
    handle = await owner.find(uuid4())
    assert await owner.status(handle) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("output,code", [(b"", 1), (b"", 0), (b"loaded", 1), (b"masked", 0)])
async def test_lookup_uncertainty_is_not_an_absent_worker(monkeypatch, output, code):
    owner = launcher()

    async def show(*_):
        return output, code

    monkeypatch.setattr(owner, "_show", show)
    with pytest.raises(JobLauncherError, match="unconfirmed"):
        await owner.find(uuid4())


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [0, 1])
async def test_explicit_absent_unit_is_not_a_live_worker(monkeypatch, code):
    owner = launcher()

    async def show(*_):
        return b"not-found", code

    monkeypatch.setattr(owner, "_show", show)
    assert await owner.find(uuid4()) is None


@pytest.mark.parametrize("state,pid,group,events", [
    ("failed", "0", "/r2-fixture", "populated 1\nfrozen 0\n"),
    ("inactive", "0", "/r2-fixture", "populated 1\nfrozen 0\n"),
    ("inactive", "17", "", None),
    ("inactive", "", "", None),
    ("inactive", "0", "/r2-fixture", "frozen 0\n"),
    ("inactive", "0", "/r2-fixture", PermissionError()),
    ("inactive", "0", "/../outside", None),
    ("inactive", "0", "/", None),
])
def test_terminal_ack_requires_no_remaining_writer(monkeypatch, state, pid, group, events):
    owner = launcher()
    name = owner._name(uuid4())
    calls = []

    def control(*args):
        calls.append(args)
        return f"LoadState=loaded\nActiveState={state}\nMainPID={pid}\nControlGroup={group}".encode(), 0

    def read(path, *_, **__):
        assert path == Path("/sys/fs/cgroup/r2-fixture/cgroup.events")
        if isinstance(events, Exception):
            raise events
        return events

    monkeypatch.setattr(owner, "_control_sync", control)
    monkeypatch.setattr(Path, "read_text", read)
    with pytest.raises(JobLauncherError, match="unconfirmed"):
        owner._confirm_terminal(name, 0)
    assert name not in owner._confirmed_terminal
    assert all(args[0] != "reset-failed" for args in calls)


@pytest.mark.parametrize("group,events", [
    ("", None), ("/r2-fixture", "populated 0\nfrozen 0\n"),
    ("/r2-fixture", FileNotFoundError()),
])
def test_empty_or_removed_cgroup_allows_confirmed_terminal(monkeypatch, group, events):
    owner = launcher()
    name = owner._name(uuid4())

    def control(*_):
        return f"LoadState=loaded\nActiveState=inactive\nMainPID=0\nControlGroup={group}".encode(), 0

    def read(*_, **__):
        if isinstance(events, Exception):
            raise events
        return events

    monkeypatch.setattr(owner, "_control_sync", control)
    monkeypatch.setattr(Path, "read_text", read)
    owner._confirm_terminal(name, 0)
    assert name in owner._confirmed_terminal
