"""Runtime-neutral job boundary: where an already-built container argv runs."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from ..backend import ProcessOutcome
from ..models import RequestedResources


class JobState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"  # An exit outcome exists; collect() returns it.
    FAILED = "FAILED"  # The job ended without a collectable exit outcome.
    UNKNOWN = "UNKNOWN"  # The launcher does not know this job.


TERMINAL_STATES = frozenset({JobState.COMPLETED, JobState.FAILED})


class JobLauncherError(RuntimeError):
    """A launcher operation could not be performed."""


class JobNotFoundError(JobLauncherError):
    """No job with this identity exists for the launcher."""


@dataclass(frozen=True)
class JobSpec:
    """Trusted launch request; every field comes from the validated plan or the host."""

    execution_id: UUID
    argv: tuple[str, ...]
    timeout_seconds: float
    resources: RequestedResources
    workspace_root: Path
    cleanup_paths: tuple[Path, ...] = ()  # Removed when the job exits.


@dataclass(frozen=True)
class JobHandle:
    launcher: str
    job_id: str
    execution_id: UUID


class JobLauncher(ABC):
    """submit / status / cancel / attach / collect; one implementation per placement."""

    kind: str
    poll_interval_seconds: float = 5.0

    @abstractmethod
    async def submit(self, spec: JobSpec) -> JobHandle:
        """Start the job and return its handle; never run the same execution twice."""

    @abstractmethod
    async def status(self, handle: JobHandle) -> JobState:
        """Report the current state without side effects."""

    @abstractmethod
    async def cancel(self, handle: JobHandle) -> None:
        """Stop a pending or running job; a finished job is left unchanged."""

    @abstractmethod
    async def attach(self, job_id: str, *, execution_id: UUID) -> JobHandle:
        """Re-acquire an existing job by its recorded ID; never resubmit."""

    @abstractmethod
    async def collect(self, handle: JobHandle) -> ProcessOutcome:
        """Return the exit outcome of a COMPLETED job."""

    async def find(self, execution_id: UUID) -> JobHandle | None:
        """Recover a job whose ID never reached the ledger; None when it cannot be known."""

        return None

    async def wait(self, handle: JobHandle) -> JobState:
        """Poll until the job reaches a terminal state."""

        while (state := await self.status(handle)) not in TERMINAL_STATES:
            if state is JobState.UNKNOWN:
                raise JobNotFoundError("The launcher lost track of the job")
            await asyncio.sleep(self.poll_interval_seconds)
        return state
