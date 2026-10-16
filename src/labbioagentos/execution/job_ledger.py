"""Durable record of every launched job, written before the control plane waits on it."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from .launchers.base import JobLauncherError


class JobLedgerState(StrEnum):
    SUBMITTING = "SUBMITTING"  # Recorded before submission; the job ID may be unknown.
    SUBMITTED = "SUBMITTED"  # The launcher accepted the job.
    COLLECTED = "COLLECTED"  # The control plane collected the outcome.
    NOT_SUBMITTED = "NOT_SUBMITTED"  # Submission failed; nothing runs.
    # Resolutions written only by orphan reconciliation:
    CANCELLED = "CANCELLED"
    DISCARDED = "DISCARDED"
    LOST = "LOST"
    UNCONFIRMED = "UNCONFIRMED"


OPEN_STATES = frozenset({JobLedgerState.SUBMITTING, JobLedgerState.SUBMITTED})


@dataclass(frozen=True)
class JobLedgerEntry:
    run_id: UUID
    execution_id: UUID
    launcher: str
    job_id: str | None
    state: JobLedgerState
    workspace_root: Path


class JobLedger:
    """One row per execution; an execution ID can never be recorded twice."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # A rollback journal, not WAL: run directories may live on a shared filesystem.
        self._connection = sqlite3.connect(
            self.path, timeout=30, isolation_level=None, check_same_thread=False,
        )
        os.chmod(self.path, 0o600)
        self._connection.execute("PRAGMA journal_mode=DELETE")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS jobs ("
            " execution_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, launcher TEXT NOT NULL,"
            " job_id TEXT, state TEXT NOT NULL, workspace_root TEXT NOT NULL,"
            " created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )

    def record_intent(self, *, run_id: UUID, execution_id: UUID, launcher: str,
                      workspace_root: Path) -> None:
        now = _now()
        try:
            self._connection.execute(
                "INSERT INTO jobs VALUES (?, ?, ?, NULL, ?, ?, ?, ?)",
                (str(execution_id), str(run_id), launcher, JobLedgerState.SUBMITTING.value,
                 str(workspace_root), now, now),
            )
        except sqlite3.IntegrityError:
            raise JobLauncherError("This execution already has a job record") from None

    def record_submitted(self, execution_id: UUID, job_id: str) -> None:
        self._transition(execution_id, JobLedgerState.SUBMITTED, (JobLedgerState.SUBMITTING,),
                         job_id=job_id)

    def record_not_submitted(self, execution_id: UUID) -> None:
        self._transition(execution_id, JobLedgerState.NOT_SUBMITTED, (JobLedgerState.SUBMITTING,))

    def record_collected(self, execution_id: UUID) -> None:
        self._transition(execution_id, JobLedgerState.COLLECTED, (JobLedgerState.SUBMITTED,))

    def resolve(self, execution_id: UUID, state: JobLedgerState, *, job_id: str | None = None) -> None:
        if state not in {JobLedgerState.CANCELLED, JobLedgerState.DISCARDED,
                         JobLedgerState.LOST, JobLedgerState.UNCONFIRMED}:
            raise ValueError("Only reconciliation outcomes resolve an open job")
        self._transition(execution_id, state, tuple(OPEN_STATES), job_id=job_id)

    def open_jobs(self, run_id: UUID) -> tuple[JobLedgerEntry, ...]:
        rows = self._connection.execute(
            "SELECT run_id, execution_id, launcher, job_id, state, workspace_root FROM jobs"
            " WHERE run_id = ? AND state IN (?, ?) ORDER BY created_at, execution_id",
            (str(run_id), *sorted(state.value for state in OPEN_STATES)),
        ).fetchall()
        return tuple(_entry(row) for row in rows)

    def get(self, execution_id: UUID) -> JobLedgerEntry | None:
        row = self._connection.execute(
            "SELECT run_id, execution_id, launcher, job_id, state, workspace_root FROM jobs"
            " WHERE execution_id = ?", (str(execution_id),),
        ).fetchone()
        return None if row is None else _entry(row)

    def close(self) -> None:
        self._connection.close()

    def _transition(self, execution_id: UUID, state: JobLedgerState,
                    allowed: tuple[JobLedgerState, ...], *, job_id: str | None = None) -> None:
        placeholders = ", ".join("?" for _ in allowed)
        cursor = self._connection.execute(
            f"UPDATE jobs SET state = ?, job_id = COALESCE(?, job_id), updated_at = ?"
            f" WHERE execution_id = ? AND state IN ({placeholders})",
            (state.value, job_id, _now(), str(execution_id), *(item.value for item in allowed)),
        )
        if cursor.rowcount != 1:
            raise JobLauncherError("The job record is not in a state that allows this change")


def _entry(row) -> JobLedgerEntry:
    run_id, execution_id, launcher, job_id, state, workspace_root = row
    return JobLedgerEntry(run_id=UUID(run_id), execution_id=UUID(execution_id), launcher=launcher,
                          job_id=job_id, state=JobLedgerState(state),
                          workspace_root=Path(workspace_root))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
