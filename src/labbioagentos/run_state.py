"""Authoritative durable control state for application run reconstruction."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Protocol
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from .contracts import RunStatus, WorkflowRun, WorkflowStage
from .runtime import RuntimeReference, RuntimeStageResult


class RunStateStoreError(RuntimeError):
    """Base deterministic application run-state storage failure."""


class RunStateNotFoundError(RunStateStoreError):
    """The requested durable application run does not exist."""


class RunStateVersionConflictError(RunStateStoreError):
    """The expected optimistic record version is no longer current."""


class RunRecoveryState(StrEnum):
    """Durable knowledge about possible escaped external side effects."""

    STABLE = "STABLE"
    STAGE_IN_FLIGHT = "STAGE_IN_FLIGHT"
    GATE_DECISION_IN_FLIGHT = "GATE_DECISION_IN_FLIGHT"


class RunInflightOperation(StrEnum):
    """Bounded operation class associated with one in-flight marker."""

    RUNTIME_STAGE = "RUNTIME_STAGE"
    DOMAIN_GATE_DECISION = "DOMAIN_GATE_DECISION"


class ApplicationRunRecord(BaseModel):
    """Strict data-only snapshot needed to reconstruct one application run."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_id: UUID
    task_text: StrictStr = Field(min_length=1, max_length=32_000)
    owner_user_id: StrictStr = Field(min_length=1, max_length=256)
    project_id: StrictStr = Field(min_length=1, max_length=256)
    lab_id: StrictStr = Field(min_length=1, max_length=256)
    input_artifact_ids: tuple[UUID, ...] = Field(default=(), max_length=128)
    context_artifact_ids: tuple[UUID, ...] = Field(default=(), max_length=128)
    safe_domain_references: tuple[RuntimeReference, ...] = Field(
        default=(), max_length=64
    )
    workflow_run: WorkflowRun
    runtime_results: tuple[RuntimeStageResult, ...] = Field(
        default=(), max_length=512
    )
    runtime_revision: StrictStr = Field(min_length=1, max_length=256)
    recovery_state: RunRecoveryState = RunRecoveryState.STABLE
    inflight_stage: WorkflowStage | None = None
    inflight_invocation_id: UUID | None = None
    inflight_operation: RunInflightOperation | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    record_version: int = Field(default=1, ge=1)

    @field_validator("created_at", "updated_at")
    @classmethod
    def require_utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Run-state timestamps must be timezone-aware")
        return value

    @field_validator("runtime_revision")
    @classmethod
    def runtime_revision_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("runtime_revision cannot be blank")
        return value

    @model_validator(mode="after")
    def validate_control_state(self) -> "ApplicationRunRecord":
        run = self.workflow_run
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        if self.run_id != run.run_id:
            raise ValueError("Application run_id must match WorkflowRun")
        if (
            self.owner_user_id != run.owner_user_id
            or self.project_id != run.project_id
            or self.lab_id != run.lab_id
        ):
            raise ValueError("Application scope must match WorkflowRun scope")
        artifact_ids = (*self.input_artifact_ids, *self.context_artifact_ids)
        if len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError("Durable run Artifact IDs must be unique")
        if len(self.runtime_results) != len(run.stage_results):
            raise ValueError(
                "Runtime result history must match WorkflowRun stage result history"
            )
        for runtime_result, workflow_result in zip(
            self.runtime_results, run.stage_results, strict=True
        ):
            if runtime_result.stage_id is not workflow_result.stage:
                raise ValueError("Runtime and workflow result stages must match")
            if workflow_result.payload.get("runtime_result_id") != str(
                runtime_result.result_id
            ):
                raise ValueError("Runtime and workflow result identities must match")

        markers = (
            self.inflight_stage,
            self.inflight_invocation_id,
            self.inflight_operation,
        )
        if self.recovery_state is RunRecoveryState.STABLE:
            if any(value is not None for value in markers):
                raise ValueError("Stable run state cannot retain in-flight markers")
        elif self.recovery_state is RunRecoveryState.STAGE_IN_FLIGHT:
            if (
                run.status is not RunStatus.RUNNING
                or run.current_stage is None
                or self.inflight_stage is not run.current_stage
                or self.inflight_invocation_id is None
                or self.inflight_operation is not RunInflightOperation.RUNTIME_STAGE
            ):
                raise ValueError("Runtime stage in-flight markers are inconsistent")
        elif self.recovery_state is RunRecoveryState.GATE_DECISION_IN_FLIGHT:
            if (
                run.status is not RunStatus.WAITING_FOR_USER
                or run.current_stage is not WorkflowStage.USER_GATE
                or run.pending_user_gate is None
                or self.inflight_stage is not WorkflowStage.USER_GATE
                or self.inflight_invocation_id is None
                or self.inflight_operation
                is not RunInflightOperation.DOMAIN_GATE_DECISION
            ):
                raise ValueError("Gate-decision in-flight markers are inconsistent")
        return self


class RunStateStore(Protocol):
    """Persistence contract for authoritative application run state."""

    def create(self, record: ApplicationRunRecord) -> ApplicationRunRecord: ...
    def get(self, run_id: UUID) -> ApplicationRunRecord: ...
    def update(
        self, record: ApplicationRunRecord, *, expected_version: int
    ) -> ApplicationRunRecord: ...
    def list(self) -> tuple[ApplicationRunRecord, ...]: ...


def _copy_record(record: ApplicationRunRecord) -> ApplicationRunRecord:
    return ApplicationRunRecord.model_validate_json(record.model_dump_json())


def _next_record(
    record: ApplicationRunRecord,
    current: ApplicationRunRecord,
    *,
    expected_version: int,
) -> ApplicationRunRecord:
    if current.record_version != expected_version:
        raise RunStateVersionConflictError(
            f"Run-state version conflict for {record.run_id}"
        )
    if record.record_version != expected_version:
        raise RunStateVersionConflictError(
            "Replacement record version does not match expected version"
        )
    if record.created_at != current.created_at:
        raise RunStateStoreError("Application run created_at is immutable")
    return _copy_record(
        record.model_copy(
            update={
                "updated_at": datetime.now(timezone.utc),
                "record_version": expected_version + 1,
            }
        )
    )


class InMemoryRunStateStore:
    """Locked process-local run-state store for deterministic tests."""

    def __init__(self):
        self._records: dict[UUID, ApplicationRunRecord] = {}
        self._lock = RLock()

    def create(self, record: ApplicationRunRecord) -> ApplicationRunRecord:
        if record.record_version != 1:
            raise RunStateVersionConflictError("New run state must start at version 1")
        stored = _copy_record(record)
        with self._lock:
            if stored.run_id in self._records:
                raise RunStateVersionConflictError(
                    f"Application run already exists: {stored.run_id}"
                )
            self._records[stored.run_id] = stored
        return _copy_record(stored)

    def get(self, run_id: UUID) -> ApplicationRunRecord:
        with self._lock:
            try:
                return _copy_record(self._records[run_id])
            except KeyError as exc:
                raise RunStateNotFoundError(
                    f"Application run state not found: {run_id}"
                ) from exc

    def update(
        self, record: ApplicationRunRecord, *, expected_version: int
    ) -> ApplicationRunRecord:
        with self._lock:
            try:
                current = self._records[record.run_id]
            except KeyError as exc:
                raise RunStateNotFoundError(
                    f"Application run state not found: {record.run_id}"
                ) from exc
            stored = _next_record(
                record, current, expected_version=expected_version
            )
            self._records[stored.run_id] = stored
            return _copy_record(stored)

    def list(self) -> tuple[ApplicationRunRecord, ...]:
        with self._lock:
            return tuple(
                _copy_record(record)
                for record in sorted(
                    self._records.values(),
                    key=lambda item: (item.created_at, str(item.run_id)),
                )
            )


class SQLiteRunStateStore:
    """Transactional Pydantic-JSON run state for one local application writer."""

    def __init__(self, database_path: str | Path):
        self.path = Path(database_path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._connection = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS application_run_state (
                run_id TEXT PRIMARY KEY,
                record_version INTEGER NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def create(self, record: ApplicationRunRecord) -> ApplicationRunRecord:
        if record.record_version != 1:
            raise RunStateVersionConflictError("New run state must start at version 1")
        stored = _copy_record(record)
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._connection.execute(
                    """
                    INSERT INTO application_run_state(run_id, record_version, payload)
                    VALUES (?, ?, ?)
                    """,
                    (str(stored.run_id), stored.record_version, stored.model_dump_json()),
                )
                self._connection.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise RunStateVersionConflictError(
                    f"Application run already exists: {stored.run_id}"
                ) from exc
            except Exception:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise
        return _copy_record(stored)

    def get(self, run_id: UUID) -> ApplicationRunRecord:
        with self._lock:
            row = self._connection.execute(
                "SELECT payload FROM application_run_state WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()
        if row is None:
            raise RunStateNotFoundError(f"Application run state not found: {run_id}")
        return self._parse(row[0])

    def update(
        self, record: ApplicationRunRecord, *, expected_version: int
    ) -> ApplicationRunRecord:
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                row = self._connection.execute(
                    """
                    SELECT payload FROM application_run_state
                    WHERE run_id = ?
                    """,
                    (str(record.run_id),),
                ).fetchone()
                if row is None:
                    raise RunStateNotFoundError(
                        f"Application run state not found: {record.run_id}"
                    )
                current = self._parse(row[0])
                stored = _next_record(
                    record, current, expected_version=expected_version
                )
                cursor = self._connection.execute(
                    """
                    UPDATE application_run_state
                    SET record_version = ?, payload = ?
                    WHERE run_id = ? AND record_version = ?
                    """,
                    (
                        stored.record_version,
                        stored.model_dump_json(),
                        str(stored.run_id),
                        expected_version,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RunStateVersionConflictError(
                        f"Run-state version conflict for {stored.run_id}"
                    )
                self._connection.execute("COMMIT")
                return _copy_record(stored)
            except Exception:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def list(self) -> tuple[ApplicationRunRecord, ...]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT payload FROM application_run_state ORDER BY run_id"
            ).fetchall()
        return tuple(self._parse(row[0]) for row in rows)

    @staticmethod
    def _parse(payload: str) -> ApplicationRunRecord:
        try:
            return ApplicationRunRecord.model_validate_json(payload)
        except (ValidationError, ValueError, TypeError) as exc:
            raise RunStateStoreError("SQLite application run state is invalid") from exc


__all__ = [
    "ApplicationRunRecord",
    "InMemoryRunStateStore",
    "RunInflightOperation",
    "RunRecoveryState",
    "RunStateNotFoundError",
    "RunStateStore",
    "RunStateStoreError",
    "RunStateVersionConflictError",
    "SQLiteRunStateStore",
]
