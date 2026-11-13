"""Image admission attempts and their crash cleanup (DECIDED-C15-E2).

Each admission works in its own attempt directory and holds the attempt's
lease until it ends. Its jobs (an Apptainer conversion, or a lock build's
download and build) are recorded in the member's job ledger with the attempt
as their run. Cleanup takes the lease of every other attempt; holding it
proves the process that started the attempt is gone. The attempt's open jobs
are then resolved by the launcher that owns them, its other leftovers are
removed, and its directory is deleted. Nothing is ever resubmitted.

An attempt is kept while one of its jobs may still be running: the next
cleanup tries again.
"""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import stat
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from .job_ledger import JobLedger, JobPurpose
from .job_reconciliation import OrphanAction, ReconciledJob, reconcile_admission_jobs
from .launchers.base import JobLauncher

ATTEMPT_LEASE = ".admission.lease"
_ATTEMPT_NAME = re.compile(r"[0-9a-f]{32}")
_NEW_PREFIX = ".new-"
_UNRESOLVED = frozenset({OrphanAction.CANCEL_PENDING, OrphanAction.LAUNCHER_MISMATCH})


@dataclass(frozen=True)
class AdmissionAttempt:
    id: UUID
    root: Path


@dataclass(frozen=True)
class ReconciledAttempt:
    attempt_id: UUID
    jobs: tuple[ReconciledJob, ...]
    removed: bool  # False while a job may still run or a leftover could not be removed.


class AdmissionAttempts:
    """The attempt directories of one member area.

    `launchers` maps each job root to the launcher that owns the jobs under
    it. `discard_leftovers` removes what a lost attempt may have left outside
    its directory, such as Docker build containers and images, and returns
    True once nothing is left.
    """

    def __init__(self, root: Path, *, ledger: JobLedger | None = None,
                 launchers: Mapping[Path, JobLauncher] | None = None,
                 discard_leftovers: Callable[[str], bool] | None = None, settle_seconds: float = 120.0):
        self.root, self.ledger = Path(root), ledger
        self.launchers = dict(launchers or {})
        self.discard_leftovers = discard_leftovers or (lambda _attempt: True)
        self.settle_seconds = settle_seconds

    @contextmanager
    def attempt(self) -> Iterator[AdmissionAttempt]:
        """A new attempt, leased until it ends; it is deleted then unless a job may still run."""

        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        attempt_id = uuid4()
        # The lease exists before the attempt has its final name, so cleanup never sees it unleased.
        pending = self.root / (_NEW_PREFIX + attempt_id.hex)
        pending.mkdir(mode=0o700)
        descriptor = os.open(pending / ATTEMPT_LEASE, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            path = self.root / attempt_id.hex
            os.rename(pending, path)
            try:
                yield AdmissionAttempt(attempt_id, path)
            finally:
                if not self._open_jobs(attempt_id) and self.discard_leftovers(attempt_id.hex):
                    shutil.rmtree(path, ignore_errors=True)
        finally:
            os.close(descriptor)
            if pending.is_dir() and not pending.is_symlink():  # The rename never happened.
                shutil.rmtree(pending, ignore_errors=True)

    async def reconcile(self) -> tuple[ReconciledAttempt, ...]:
        """Clean up every attempt whose process is gone; attempts in use are not touched."""

        results = []
        seen: set[UUID] = set()
        if self.root.is_dir() and not self.root.is_symlink():
            for path in sorted(self.root.iterdir()):
                name = path.name.removeprefix(_NEW_PREFIX)
                if path.is_symlink() or not path.is_dir() or _ATTEMPT_NAME.fullmatch(name) is None:
                    continue
                descriptor = _take_lease(path / ATTEMPT_LEASE)
                if descriptor is None:
                    continue  # In use, or the lease cannot be trusted: never delete it.
                try:
                    attempt_id = UUID(hex=name)
                    seen.add(attempt_id)
                    results.append(await self._resolve(attempt_id, path))
                finally:
                    os.close(descriptor)
        # An attempt directory exists while its process runs and is deleted only after its
        # jobs are resolved, so open jobs without a directory belong to a lost process.
        for attempt_id in self._open_runs():
            if attempt_id not in seen and not (self.root / attempt_id.hex).exists():
                results.append(await self._resolve(attempt_id, None))
        return tuple(results)

    async def _resolve(self, attempt_id: UUID, path: Path | None) -> ReconciledAttempt:
        jobs: tuple[ReconciledJob, ...] = ()
        if self.ledger is not None:
            jobs = await reconcile_admission_jobs(ledger=self.ledger, run_id=attempt_id, launchers=self.launchers,
                                                  settle_seconds=self.settle_seconds)
        removed = False
        if not any(job.action in _UNRESOLVED for job in jobs) and self.discard_leftovers(attempt_id.hex):
            if path is not None:
                shutil.rmtree(path)
            removed = True
        return ReconciledAttempt(attempt_id, jobs, removed)

    def _open_jobs(self, attempt_id: UUID) -> bool:
        return self.ledger is not None and bool(self.ledger.open_jobs(attempt_id))

    def _open_runs(self) -> tuple[UUID, ...]:
        return self.ledger.open_runs(JobPurpose.ADMISSION) if self.ledger is not None else ()


def _take_lease(path: Path) -> int | None:
    """The attempt's lease, or None when it is held, missing or not a private regular file."""

    try:
        descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077
                or info.st_uid != os.geteuid()):
            raise PermissionError()
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(descriptor)
        return None
    return descriptor
