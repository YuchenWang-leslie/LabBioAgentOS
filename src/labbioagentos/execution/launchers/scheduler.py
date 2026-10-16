"""Scheduler + Apptainer placement: template-driven submission from the login node."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from ..backend import ProcessOutcome
from ..errors import ContainerStartError
from ..models import RequestedResources
from .base import (
    TERMINAL_STATES,
    JobHandle,
    JobLauncher,
    JobLauncherError,
    JobNotFoundError,
    JobSpec,
    JobState,
)
from .job_script import EXIT_RECORD, JOB_PATH, SchedulerPolicy, render_job_script, validate_extra_directives
from .templates import SchedulerTemplate


class SchedulerCommandRunner:
    """Runs scheduler client commands with an allowlisted environment, never a shell."""

    BASE_ENVIRONMENT = ("PATH", "HOME", "USER", "LOGNAME", "LANG")

    def __init__(self, passthrough_environment: tuple[str, ...] = (), timeout_seconds: float = 60.0):
        self.passthrough_environment = passthrough_environment
        self.timeout_seconds = timeout_seconds

    def environment(self) -> dict[str, str]:
        names = (*self.BASE_ENVIRONMENT, *self.passthrough_environment)
        return {name: os.environ[name] for name in names if name in os.environ}

    def run(self, argv: tuple[str, ...], *, stdin: bytes | None = None) -> ProcessOutcome:
        started = time.monotonic()
        try:
            completed = subprocess.run(
                list(argv), shell=False, check=False, input=stdin,
                stdin=None if stdin is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=self.environment(), timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return ProcessOutcome(exit_code=None, stdout=b"", stderr=b"",
                                  duration_seconds=time.monotonic() - started, timed_out=True)
        except OSError as exc:
            raise ContainerStartError(f"Could not start the scheduler client: {exc}") from exc
        return ProcessOutcome(exit_code=completed.returncode, stdout=completed.stdout,
                              stderr=completed.stderr, duration_seconds=time.monotonic() - started)


def check_scheduler_client(
    template: SchedulerTemplate,
    policy: SchedulerPolicy,
    work_root: Path,
    command_runner: SchedulerCommandRunner | None = None,
) -> dict:
    """Operator preflight: clients on PATH and, where supported, a validation-only submit."""

    runner = command_runner or SchedulerCommandRunner(template.passthrough_environment)
    search_path = runner.environment().get("PATH", "")
    names = dict.fromkeys(command[0] for command in (template.submit, template.status, template.cancel))
    clients = {name: shutil.which(name, path=search_path) is not None for name in names}
    report = {"template": template.name, "clients": clients, "test_submission": "UNSUPPORTED"}
    if not template.test_submit or not all(clients.values()):
        return report
    directory = Path(tempfile.mkdtemp(prefix="labbio-scheduler-check-", dir=work_root))
    try:
        job_dir = directory / "job"
        job_dir.mkdir()
        spec = JobSpec(execution_id=uuid4(), argv=("true",), timeout_seconds=60.0,
                       resources=RequestedResources(), workspace_root=directory)
        script = job_dir / "job.sh"
        script.write_text(render_job_script(template, policy, spec, job_dir, {"PATH": JOB_PATH}),
                          encoding="utf-8")
        argv = tuple(item.replace("{script}", str(script)) for item in template.test_submit)
        outcome = runner.run(argv)
    finally:
        shutil.rmtree(directory, ignore_errors=True)
    accepted = outcome.exit_code == 0 and not outcome.timed_out
    report["test_submission"] = "ACCEPTED" if accepted else "REJECTED"
    if not accepted:
        lines = (outcome.stderr or outcome.stdout).decode("utf-8", "replace").strip().splitlines()
        report["detail"] = lines[0][:300] if lines else None
    return report


@dataclass
class _Tracked:
    submitted_at: float
    started: bool = False
    gone_since: float | None = None
    cancelled: bool = False
    queue_expired: bool = False


class SchedulerLauncher(JobLauncher):
    """Job files live in the shared execution workspace; the exit record decides completion."""

    kind = "scheduler"

    def __init__(
        self,
        *,
        template: SchedulerTemplate,
        policy: SchedulerPolicy,
        workspace_root: str | Path,
        command_runner=None,
    ):
        validate_extra_directives(template, policy.extra_directives)
        self.template = template
        self.policy = policy
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.command_runner = command_runner or SchedulerCommandRunner(template.passthrough_environment)
        self.poll_interval_seconds = policy.poll_interval_seconds
        self._tracked: dict[str, _Tracked] = {}

    async def submit(self, spec: JobSpec) -> JobHandle:
        job_dir = self._job_dir(spec.execution_id)
        if Path(spec.workspace_root).resolve() != job_dir.parent:
            raise JobLauncherError("The job workspace is outside this launcher's root")
        try:
            job_dir.mkdir(mode=0o700)
        except FileExistsError:
            raise JobLauncherError("This execution was already submitted") from None
        script = job_dir / "job.sh"
        text = render_job_script(self.template, self.policy, spec, job_dir, self._job_environment())
        descriptor = os.open(script, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
        argv = tuple(item.replace("{script}", str(script)) for item in self.template.submit)
        stdin = text.encode("utf-8") if self.template.submit_via_stdin else None
        outcome = await asyncio.to_thread(self.command_runner.run, argv, stdin=stdin)
        match = None
        if outcome.exit_code == 0:
            match = re.search(self.template.job_id_pattern,
                              outcome.stdout.decode("utf-8", "replace"), re.MULTILINE)
        if match is None:
            raise ContainerStartError("The scheduler did not accept the job")
        job_id = match.group(1)
        self._write_atomic(job_dir / "job_id", job_id)
        self._tracked[job_id] = _Tracked(submitted_at=time.monotonic())
        return JobHandle(launcher=self.kind, job_id=job_id, execution_id=spec.execution_id)

    async def status(self, handle: JobHandle) -> JobState:
        job_dir = self._job_dir(handle.execution_id)
        if self._recorded_job_id(job_dir) != handle.job_id:
            return JobState.UNKNOWN
        if (job_dir / "exit_code").is_file():
            return JobState.COMPLETED
        tracked = self._tracked.setdefault(handle.job_id, _Tracked(submitted_at=time.monotonic()))
        if tracked.queue_expired:
            return JobState.FAILED
        observed = await self._query(handle.job_id)
        if observed == "RUNNING":
            tracked.started, tracked.gone_since = True, None
            return JobState.RUNNING
        if observed == "PENDING":
            tracked.gone_since = None
            return JobState.PENDING
        if observed is None:  # Transient client error: keep the last known phase.
            return JobState.RUNNING if tracked.started else JobState.PENDING
        if tracked.cancelled:
            return JobState.FAILED
        # Gone from the scheduler without an exit record yet: allow shared-filesystem lag.
        now = time.monotonic()
        tracked.gone_since = tracked.gone_since or now
        if now - tracked.gone_since >= self.policy.completion_grace_seconds:
            return JobState.FAILED
        return JobState.RUNNING

    async def wait(self, handle: JobHandle) -> JobState:
        while True:
            state = await self.status(handle)
            if state in TERMINAL_STATES:
                return state
            if state is JobState.UNKNOWN:
                raise JobNotFoundError("The launcher lost track of the job")
            tracked = self._tracked[handle.job_id]
            if (state is JobState.PENDING and not tracked.started
                    and time.monotonic() - tracked.submitted_at >= self.policy.max_queue_seconds):
                await self.cancel(handle)
                tracked.queue_expired = True
                return JobState.FAILED
            await asyncio.sleep(self.poll_interval_seconds)

    async def cancel(self, handle: JobHandle) -> None:
        job_dir = self._existing_job_dir(handle)
        if (job_dir / "exit_code").is_file():
            return
        argv = tuple(item.replace("{job_id}", handle.job_id) for item in self.template.cancel)
        outcome = await asyncio.to_thread(self.command_runner.run, argv)
        if outcome.exit_code != 0 and not self._gone(outcome):
            raise JobLauncherError("The scheduler refused to cancel the job")
        self._tracked.setdefault(handle.job_id, _Tracked(submitted_at=time.monotonic())).cancelled = True

    async def attach(self, job_id: str, *, execution_id: UUID) -> JobHandle:
        handle = JobHandle(launcher=self.kind, job_id=job_id, execution_id=execution_id)
        self._existing_job_dir(handle)
        self._tracked.setdefault(job_id, _Tracked(submitted_at=time.monotonic()))
        return handle

    async def find(self, execution_id: UUID) -> JobHandle | None:
        job_id = self._recorded_job_id(self._job_dir(execution_id))
        if job_id is None:
            return None
        return await self.attach(job_id, execution_id=execution_id)

    async def collect(self, handle: JobHandle) -> ProcessOutcome:
        job_dir = self._existing_job_dir(handle)
        tracked = self._tracked.get(handle.job_id)
        if tracked is not None and tracked.queue_expired:
            return ProcessOutcome(exit_code=None, stdout=b"", stderr=b"",
                                  duration_seconds=0.0, timed_out=True)
        exit_path = job_dir / "exit_code"
        if not exit_path.is_file():
            if await self.status(handle) is JobState.FAILED:
                raise ContainerStartError("The scheduler job ended without an exit status")
            raise JobLauncherError("The job has not finished")
        record = EXIT_RECORD.fullmatch(exit_path.read_text(encoding="ascii", errors="replace"))
        if record is None:
            raise ContainerStartError("The scheduler job wrote an unreadable exit status")
        status, elapsed, timed_out = int(record.group(1)), int(record.group(2)), record.group(3) == "1"
        return ProcessOutcome(
            exit_code=None if timed_out else status,
            stdout=self._read(job_dir / "stdout.log"),
            stderr=self._read(job_dir / "stderr.log"),
            duration_seconds=float(elapsed),
            timed_out=timed_out,
        )

    async def _query(self, job_id: str) -> str | None:
        argv = tuple(item.replace("{job_id}", job_id) for item in self.template.status)
        outcome = await asyncio.to_thread(self.command_runner.run, argv)
        if self._gone(outcome):
            return "GONE"
        if outcome.exit_code != 0:
            return None
        text = outcome.stdout.decode("utf-8", "replace")
        if not text.strip():
            return "GONE"  # No longer listed.
        match = re.search(self.template.status_pattern, text, re.MULTILINE)
        return None if match is None else self.template.status_map.get(match.group(1))

    def _gone(self, outcome: ProcessOutcome) -> bool:
        if self.template.gone_pattern is None:
            return False
        text = (outcome.stdout + outcome.stderr).decode("utf-8", "replace")
        return re.search(self.template.gone_pattern, text) is not None

    def _job_environment(self) -> dict[str, str]:
        environment = {name: os.environ[name] for name in ("HOME", "USER", "LOGNAME", "LANG")
                       if name in os.environ}
        environment["PATH"] = JOB_PATH
        if self.policy.apptainer_cache_dir is not None:
            environment["APPTAINER_CACHEDIR"] = str(self.policy.apptainer_cache_dir)
        if self.policy.apptainer_tmp_dir is not None:
            environment["APPTAINER_TMPDIR"] = str(self.policy.apptainer_tmp_dir)
        return environment

    def _job_dir(self, execution_id: UUID) -> Path:
        return self.workspace_root / str(execution_id) / "job"

    def _existing_job_dir(self, handle: JobHandle) -> Path:
        job_dir = self._job_dir(handle.execution_id)
        if self._recorded_job_id(job_dir) != handle.job_id:
            raise JobNotFoundError("Unknown scheduler job")
        return job_dir

    @staticmethod
    def _recorded_job_id(job_dir: Path) -> str | None:
        path = job_dir / "job_id"
        if path.is_symlink() or not path.is_file():
            return None
        return path.read_text(encoding="utf-8").strip() or None

    @staticmethod
    def _write_atomic(path: Path, text: str) -> None:
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)

    @staticmethod
    def _read(path: Path) -> bytes:
        if path.is_symlink() or not path.is_file():
            return b""
        return path.read_bytes()
