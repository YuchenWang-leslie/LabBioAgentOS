"""Exact consumer-bound approval persistence for USER_APPROVED Artifacts."""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from pathlib import Path
from threading import Lock
from uuid import UUID

from pydantic import ValidationError

from .models import ArtifactApproval, ArtifactConsumer


class ArtifactApprovalStoreError(RuntimeError):
    """Artifact approval persistence failed."""


class ArtifactApprovalStore(ABC):
    """Trusted approval authority; never exposed as a model capability."""

    durable: bool = False

    @abstractmethod
    def record(self, approval: ArtifactApproval) -> None:
        """Persist the exact Artifact/consumer approval."""

    @abstractmethod
    def get(
        self, artifact_id: UUID, consumer: ArtifactConsumer
    ) -> ArtifactApproval | None:
        """Return the exact approval when present."""


class InMemoryArtifactApprovalStore(ArtifactApprovalStore):
    """Deterministic process-local test implementation."""

    def __init__(self):
        self._records: dict[tuple[UUID, ArtifactConsumer], ArtifactApproval] = {}
        self._lock = Lock()

    def record(self, approval: ArtifactApproval) -> None:
        with self._lock:
            self._records[(approval.artifact_id, approval.consumer)] = approval

    def get(
        self, artifact_id: UUID, consumer: ArtifactConsumer
    ) -> ArtifactApproval | None:
        with self._lock:
            return self._records.get((artifact_id, consumer))


class SQLiteArtifactApprovalStore(ArtifactApprovalStore):
    """Small durable local approval store using strict Pydantic JSON."""

    durable = True

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path).expanduser().resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._initialize()

    def record(self, approval: ArtifactApproval) -> None:
        payload = approval.model_dump_json()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO artifact_approvals (
                    artifact_id, consumer, payload
                ) VALUES (?, ?, ?)
                ON CONFLICT(artifact_id, consumer)
                DO UPDATE SET payload = excluded.payload
                """,
                (str(approval.artifact_id), approval.consumer.value, payload),
            )

    def get(
        self, artifact_id: UUID, consumer: ArtifactConsumer
    ) -> ArtifactApproval | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT payload FROM artifact_approvals
                WHERE artifact_id = ? AND consumer = ?
                """,
                (str(artifact_id), consumer.value),
            ).fetchone()
        if row is None:
            return None
        try:
            approval = ArtifactApproval.model_validate_json(row[0])
        except ValidationError as exc:
            raise ArtifactApprovalStoreError(
                "Stored Artifact approval is invalid"
            ) from exc
        if approval.artifact_id != artifact_id or approval.consumer is not consumer:
            raise ArtifactApprovalStoreError(
                "Stored Artifact approval does not match its persistence key"
            )
        return approval

    def _initialize(self) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS artifact_approvals (
                        artifact_id TEXT NOT NULL,
                        consumer TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        PRIMARY KEY (artifact_id, consumer)
                    )
                    """
                )
        except sqlite3.Error as exc:
            raise ArtifactApprovalStoreError(
                "Could not initialize Artifact approval store"
            ) from exc

    def _connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(self.database_path, timeout=30)
            connection.execute("PRAGMA foreign_keys = ON")
            return connection
        except sqlite3.Error as exc:
            raise ArtifactApprovalStoreError(
                "Could not open Artifact approval store"
            ) from exc


__all__ = [
    "ArtifactApprovalStore",
    "ArtifactApprovalStoreError",
    "InMemoryArtifactApprovalStore",
    "SQLiteArtifactApprovalStore",
]
