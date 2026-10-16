"""Cleanup of jobs orphaned by a lost control-plane process (DECIDED-C13-4).

Re-attachment is cleanup only: a running or queued orphan is cancelled, a
finished one is reported and its files are deleted, nothing is resubmitted,
and the run itself stays BLOCKED. The caller must hold the run writer lease,
which proves the process that launched the jobs is gone.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from .job_ledger import JobLedger, JobLedgerEntry, JobLedgerState
from .launchers.base import TERMINAL_STATES, JobHandle, JobLauncher, JobNotFoundError, JobState


class OrphanAction(StrEnum):
    CANCELLED = "CANCELLED"  # Was queued or running; stopped and its files deleted.
    DISCARDED = "DISCARDED"  # Had finished; its files deleted, the result not used.
    LOST = "LOST"  # The launcher no longer knows the job; left for the operator.
    UNCONFIRMED = "UNCONFIRMED"  # The job ID never became known; left for the operator.
    CANCEL_PENDING = "CANCEL_PENDING"  # Cancel requested but not yet confirmed; retried later.
    LAUNCHER_MISMATCH = "LAUNCHER_MISMATCH"  # Configured launcher differs; left open.


@dataclass(frozen=True)
class ReconciledJob:
    execution_id: UUID
    job_id: str | None
    launcher: str
    action: OrphanAction


async def reconcile_orphan_jobs(
    *,
    ledger: JobLedger,
    launcher: JobLauncher,
    run_id: UUID,
    cleanup_paths: Callable[[UUID], tuple[Path, ...]] = lambda _execution_id: (),
    settle_seconds: float = 120.0,
) -> tuple[ReconciledJob, ...]:
    """Resolve every open ledger entry of one run; never submit anything."""

    results = []
    for entry in ledger.open_jobs(run_id):
        action, job_id = await _reconcile(entry, ledger, launcher, cleanup_paths, settle_seconds)
        results.append(ReconciledJob(entry.execution_id, job_id, entry.launcher, action))
    return tuple(results)


async def _reconcile(entry: JobLedgerEntry, ledger: JobLedger, launcher: JobLauncher,
                     cleanup_paths, settle_seconds: float) -> tuple[OrphanAction, str | None]:
    if entry.launcher != launcher.kind:
        return OrphanAction.LAUNCHER_MISMATCH, entry.job_id
    handle: JobHandle | None
    if entry.job_id is None:
        handle = await launcher.find(entry.execution_id)
        if handle is None:
            ledger.resolve(entry.execution_id, JobLedgerState.UNCONFIRMED)
            return OrphanAction.UNCONFIRMED, None
    else:
        try:
            handle = await launcher.attach(entry.job_id, execution_id=entry.execution_id)
        except JobNotFoundError:
            ledger.resolve(entry.execution_id, JobLedgerState.LOST)
            return OrphanAction.LOST, entry.job_id

    state = await launcher.status(handle)
    if state is JobState.UNKNOWN:
        ledger.resolve(entry.execution_id, JobLedgerState.LOST, job_id=handle.job_id)
        return OrphanAction.LOST, handle.job_id
    if state not in TERMINAL_STATES:
        await launcher.cancel(handle)
        try:
            await asyncio.wait_for(launcher.wait(handle), settle_seconds)
        except (asyncio.TimeoutError, JobNotFoundError):
            # Files stay until the job is confirmed stopped; the entry stays open.
            return OrphanAction.CANCEL_PENDING, handle.job_id
        _discard(entry, cleanup_paths)
        ledger.resolve(entry.execution_id, JobLedgerState.CANCELLED, job_id=handle.job_id)
        return OrphanAction.CANCELLED, handle.job_id
    _discard(entry, cleanup_paths)
    ledger.resolve(entry.execution_id, JobLedgerState.DISCARDED, job_id=handle.job_id)
    return OrphanAction.DISCARDED, handle.job_id


def _discard(entry: JobLedgerEntry, cleanup_paths) -> None:
    """Delete the execution's workspace (outputs, logs, job files) and its scratch."""

    paths = [entry.workspace_root, *cleanup_paths(entry.execution_id)]
    if entry.workspace_root.name != str(entry.execution_id):
        raise ValueError("Ledger workspace does not belong to its execution")
    for path in paths:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
