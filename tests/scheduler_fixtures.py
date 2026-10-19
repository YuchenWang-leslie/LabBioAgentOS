"""Fake scheduler clients that really run generated job scripts with bash.

No scheduler is needed. A small `timeout` stand-in replaces GNU coreutils,
which development machines may lack.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

from labbioagentos import ProcessOutcome

TIMEOUT_SHIM = f"""#!{sys.executable}
import subprocess, sys
args = sys.argv[1:]
kill_after = 0.0
while args and args[0].startswith("--kill-after="):
    kill_after = float(args.pop(0).split("=", 1)[1])
limit, command = float(args[0]), args[1:]
child = subprocess.Popen(command)
try:
    sys.exit(child.wait(timeout=limit))
except subprocess.TimeoutExpired:
    child.terminate()
    try:
        child.wait(timeout=kill_after)
        sys.exit(124)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()
        sys.exit(137)
"""


def timeout_shim(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    shim = directory / "timeout"
    shim.write_text(TIMEOUT_SHIM, encoding="utf-8")
    shim.chmod(0o755)
    return directory


def outcome(exit_code=0, stdout=b"", stderr=b""):
    return ProcessOutcome(exit_code=exit_code, stdout=stdout, stderr=stderr, duration_seconds=0.01)


def run_job_script(script: Path, shim_dir: Path, extra_environment=None) -> subprocess.Popen:
    """Start a job script the way a batch system would, in its own process group."""

    environment = {"PATH": f"{shim_dir}:/usr/bin:/bin", "HOME": os.environ.get("HOME", "/")}
    environment.update(extra_environment or {})
    return subprocess.Popen(["/bin/bash", str(script)], start_new_session=True, env=environment,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class _Job:
    def __init__(self, script: str):
        self.script = Path(script)
        self.process: subprocess.Popen | None = None
        self.cancelled = False


class FakeSlurm:
    """sbatch / squeue / scancel over real bash processes.

    `job_environment` imitates a scheduler that loads the user's login
    environment into the job, as `sbatch --export=NONE` can.
    """

    def __init__(self, tmp_path: Path, *, hold=False, refuse=False, job_environment=None):
        self.shim_dir = timeout_shim(tmp_path / "shim")
        self.hold, self.refuse = hold, refuse
        self.job_environment = job_environment or {}
        self.jobs: dict[str, _Job] = {}
        self.commands: list[tuple[str, ...]] = []
        self.calls: list[str] = []  # One entry per accepted submission.
        self._next = 4000

    def run(self, argv, *, stdin=None):
        self.commands.append(tuple(argv))
        if argv[0] == "sbatch":
            if self.refuse:
                return outcome(1, stderr=b"sbatch: error: invalid partition specified")
            job_id, self._next = str(self._next), self._next + 1
            job = self.jobs[job_id] = _Job(argv[-1])
            self.calls.append(job_id)
            if not self.hold:
                job.process = run_job_script(job.script, self.shim_dir, self.job_environment)
            return outcome(stdout=f"{job_id};cluster\n".encode())
        if argv[0] == "squeue":
            job = self.jobs.get(argv[argv.index("--jobs") + 1])
            if job is None:
                return outcome(1, stderr=b"slurm_load_jobs error: Invalid job id specified\n")
            if job.cancelled:
                return outcome()
            if job.process is None:
                return outcome(stdout=b"PENDING\n")
            return outcome(stdout=b"RUNNING\n" if job.process.poll() is None else b"")
        if argv[0] == "scancel":
            job = self.jobs[argv[1]]
            job.cancelled = True
            if job.process is not None and job.process.poll() is None:
                os.killpg(job.process.pid, signal.SIGTERM)
                job.process.wait(timeout=10)
            return outcome()
        raise AssertionError(f"unexpected scheduler command: {argv}")
