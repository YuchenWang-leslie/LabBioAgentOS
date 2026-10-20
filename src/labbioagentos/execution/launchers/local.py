"""Local + Docker placement: run argv on the control-plane host without blocking the loop."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

from ..backend import ProcessOutcome, ProcessRunner
from .base import JobHandle, JobLauncher, JobLauncherError, JobNotFoundError, JobSpec, JobState


@dataclass
class _LocalJob:
    spec: JobSpec
    task: asyncio.Task


class LocalLauncher(JobLauncher):
    """The blocking runner runs in a worker thread; only the process wait leaves the loop."""

    kind = "local"

    def __init__(self, runner: ProcessRunner):
        self.runner = runner
        self._jobs: dict[str, _LocalJob] = {}

    async def submit(self, spec: JobSpec) -> JobHandle:
        job_id = f"local-{spec.execution_id.hex}"
        if job_id in self._jobs:
            raise JobLauncherError("This execution was already submitted")
        task = asyncio.create_task(asyncio.to_thread(
            self.runner.run, spec.argv, timeout_seconds=spec.timeout_seconds,
        ))
        self._jobs[job_id] = _LocalJob(spec, task)
        return JobHandle(launcher=self.kind, job_id=job_id, execution_id=spec.execution_id)

    async def status(self, handle: JobHandle) -> JobState:
        job = self._jobs.get(handle.job_id)
        if job is None or job.spec.execution_id != handle.execution_id:
            return JobState.UNKNOWN
        if not job.task.done():
            return JobState.RUNNING
        if job.task.cancelled() or isinstance(job.task.exception(), JobLauncherError):
            return JobState.UNKNOWN
        return JobState.FAILED if job.task.exception() is not None else JobState.COMPLETED

    async def cancel(self, handle: JobHandle) -> None:
        job = self._job(handle)
        if job.task.done() and await self.status(handle) is not JobState.UNKNOWN:
            return
        cancel = getattr(self.runner, "cancel", None)
        if cancel is None:
            raise JobLauncherError(
                "This runner cannot stop a running process; its host timeout bounds it"
            )
        await asyncio.to_thread(cancel, job.spec.argv)

    async def attach(self, job_id: str, *, execution_id: UUID) -> JobHandle:
        handle = JobHandle(launcher=self.kind, job_id=job_id, execution_id=execution_id)
        if await self.status(handle) is JobState.UNKNOWN:
            # Losing in-memory identity is not proof that daemon-owned work stopped.
            raise JobNotFoundError("No local execution state exists in this control-plane process")
        return handle

    async def collect(self, handle: JobHandle) -> ProcessOutcome:
        job = self._job(handle)
        if not job.task.done():
            raise JobLauncherError("The job has not finished")
        return job.task.result()  # Re-raises the runner's own failure, e.g. ContainerStartError.

    async def wait(self, handle: JobHandle) -> JobState:
        job = self._job(handle)
        await asyncio.wait({job.task})
        return await self.status(handle)

    def _job(self, handle: JobHandle) -> _LocalJob:
        job = self._jobs.get(handle.job_id)
        if job is None or job.spec.execution_id != handle.execution_id:
            raise JobNotFoundError("Unknown local job")
        return job
