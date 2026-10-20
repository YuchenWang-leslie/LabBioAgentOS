"""Opt-in local process-tree tests; refusal is a failure, never a skipped limit.

LABBIO_RUN_LOCAL_SYSTEMD=1 PYTHONPATH=src python -m pytest -q \
    tests/integration/test_local_systemd_real.py

Requires a systemd user manager with CPU, memory and pids controllers delegated.
These are engineering processes, not substituted scientific analysis programs.
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from labbioagentos import RequestedResources
from labbioagentos.execution.apptainer import SubprocessApptainerRunner
from labbioagentos.execution.launchers.base import JobSpec
from labbioagentos.execution.launchers.systemd import SystemdLocalLauncher

pytestmark = pytest.mark.skipif(
    os.environ.get("LABBIO_RUN_LOCAL_SYSTEMD") != "1",
    reason="Opt in on a host with a delegated systemd user resource boundary",
)


@pytest.fixture
def launcher():
    result = SystemdLocalLauncher(SubprocessApptainerRunner(max_output_file_bytes=1048576))
    assert result.probe()["status"] == "OK"
    return result


def _spec(tmp_path, code, timeout=30):
    return JobSpec(
        uuid4(), (sys.executable, "-I", "-c", code), timeout,
        RequestedResources(cpus=1, memory_mb=256, pids_limit=32, timeout_seconds=timeout), tmp_path,
    )


async def _pid(path):
    for _ in range(100):
        if path.is_file():
            return int(path.read_text())
        await asyncio.sleep(0.1)
    pytest.fail("Child was not started")


async def _gone(pid):
    for _ in range(100):
        status = Path(f"/proc/{pid}/stat")
        if not status.exists() or status.read_text().split(") ", 1)[1].startswith("Z"):
            return
        await asyncio.sleep(0.1)
    pytest.fail("A descendant survived the service lifecycle")


def _detached_child(path):
    return (
        "import subprocess, sys, time\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)'], "
        "start_new_session=True)\n"
        f"Path({str(path)!r}).write_text(str(child.pid))\n"
        "time.sleep(120)\n"
    )


@pytest.mark.asyncio
async def test_cancel_stops_a_detached_sigterm_ignoring_descendant(launcher, tmp_path):
    path = tmp_path / "pid"
    handle = await launcher.submit(_spec(tmp_path, _detached_child(path)))
    pid = await _pid(path)
    await launcher.cancel(handle)
    await _gone(pid)
    assert (await launcher.collect(handle)).exit_code != 0


@pytest.mark.asyncio
async def test_timeout_stops_the_complete_tree(launcher, tmp_path):
    path = tmp_path / "pid"
    handle = await launcher.submit(_spec(tmp_path, _detached_child(path), timeout=2))
    pid = await _pid(path)
    await launcher.wait(handle)
    assert (await launcher.collect(handle)).timed_out
    await _gone(pid)


@pytest.mark.asyncio
async def test_control_process_death_closes_service_lifetime_pipe(launcher, tmp_path):
    path = tmp_path / "pid"
    code = (
        "import asyncio, os, sys\n"
        "from pathlib import Path\n"
        "from uuid import uuid4\n"
        "from labbioagentos import RequestedResources\n"
        "from labbioagentos.execution.apptainer import SubprocessApptainerRunner\n"
        "from labbioagentos.execution.launchers.systemd import SystemdLocalLauncher\n"
        "from labbioagentos.execution.launchers.base import JobSpec\n"
        "async def main():\n"
        "    launcher = SystemdLocalLauncher(SubprocessApptainerRunner(max_output_file_bytes=1048576))\n"
        f"    spec = JobSpec(uuid4(), (sys.executable, '-I', '-c', {_detached_child(path)!r}), "
        f"60, RequestedResources(cpus=1, memory_mb=256, pids_limit=32), Path({str(tmp_path)!r}))\n"
        "    await launcher.submit(spec)\n"
        f"    while not Path({str(path)!r}).is_file():\n"
        "        await asyncio.sleep(0.05)\n"
        "    os._exit(0)\n"
        "asyncio.run(main())\n"
    )
    process = subprocess.Popen((sys.executable, "-c", code))
    try:
        pid = await _pid(path)
        assert await asyncio.to_thread(process.wait, 15) == 0
        await _gone(pid)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
