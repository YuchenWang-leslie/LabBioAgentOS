"""The fake scheduler cancels its whole private job group, including TERM-resistant work."""

import os
import select
import shlex
import signal
import sys
import time

import pytest

from scheduler_fixtures import FakeSlurm


@pytest.mark.parametrize("leader_trap", ["trap '' TERM", "trap 'exit 0' TERM"])
def test_fake_scancel_releases_term_resistant_workload(tmp_path, leader_trap):
    scheduler = FakeSlurm(tmp_path)
    marker = tmp_path / "ready"
    fifo = tmp_path / "liveness"
    os.mkfifo(fifo)
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    job = tmp_path / "job.sh"
    workload = (
        "import os, pathlib, signal, sys, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "writer = os.open(sys.argv[1], os.O_WRONLY); "
        "pathlib.Path(sys.argv[2]).touch(); time.sleep(30)"
    )
    command = shlex.join([sys.executable, "-c", workload, str(fifo), str(marker)])
    job.write_text(f"{leader_trap}\n{command} &\nwait\n")
    job_id = scheduler.run(("sbatch", str(job))).stdout.decode().split(";")[0]
    process = scheduler.jobs[job_id].process
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert marker.exists()
        assert scheduler.run(("scancel", job_id)).exit_code == 0
        assert process.poll() is not None
        assert scheduler.run(("squeue", "--jobs", job_id)).stdout == b""
        assert select.select([reader], [], [], 2)[0] == [reader]
        assert os.read(reader, 1) == b""
    finally:
        # If the test fails, stop only the job we created and reap its leader.
        if process.poll() is None or not select.select([reader], [], [], 0)[0]:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=2)
        os.close(reader)
