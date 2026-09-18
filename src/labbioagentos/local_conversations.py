"""Scoped conversation-to-run identity catalog; run state remains authoritative."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import stat
from threading import RLock
from urllib.parse import quote
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from .governance.models import Principal, WorkspaceContext
from .local_workspace import _identifier, _private_file, _safe_path, _within
from .run_state import ApplicationRunRecord


class ConversationConflictError(ValueError):
    """An immutable conversation/run/directory binding would be changed."""


class ConversationNotFoundError(LookupError):
    """The run is not registered in the requested conversation."""


class ConversationRunLink(BaseModel):
    """Stable identity only; never a cached assertion of run completion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    conversation_id: str
    run_id: UUID
    relative_directory: str
    parent_run_id: UUID | None
    sequence: int
    created_at: datetime


def _check_persistence(path: Path, *, private: bool = False) -> None:
    _safe_path(path)
    if path.exists():
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PermissionError("Run persistence must be an unaliased regular file")
        if private:
            _private_file(path)


def read_run_record(directory: Path) -> ApplicationRunRecord:
    """Read exact durable control state without opening a writable application.

    Legacy run files need not have new catalog permissions. Aliases, oversized
    manifests, missing rows and inconsistent identities are always rejected.
    Traces and output files are deliberately not consulted.
    """
    directory = _safe_path(directory)
    if not directory.is_dir():
        raise ValueError("Run directory does not exist")
    manifest = directory / "RUN.json"
    database = directory / "state.sqlite"
    for name in ("RUN.json", "state.sqlite", "state.sqlite-journal", "state.sqlite-wal", "state.sqlite-shm"):
        _check_persistence(directory / name)
    if not manifest.is_file() or not database.is_file():
        raise ValueError("Run persistence is incomplete")
    descriptor = os.open(manifest, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 4096:
            raise ValueError("Run manifest is invalid or exceeds its bound")
        payload = stream.read(4097)
    try:
        metadata = json.loads(payload)
        run_id = UUID(metadata["run_id"])
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ValueError("Run manifest is invalid") from None
    if len(payload) > 4096:
        raise ValueError("Run manifest exceeds its bound")
    connection = sqlite3.connect(
        "file:" + quote(str(database), safe="/") + "?mode=ro", uri=True,
    )
    try:
        row = connection.execute(
            "SELECT record_version, payload FROM application_run_state WHERE run_id = ?",
            (str(run_id),),
        ).fetchone()
        if row is None:
            raise ValueError("Run manifest has no authoritative persisted state")
        record = ApplicationRunRecord.model_validate_json(row[1])
        if record.run_id != run_id or record.record_version != row[0]:
            raise ValueError("Run manifest and persisted identity/version disagree")
        return record
    except sqlite3.DatabaseError:
        raise ValueError("Run database is not valid persisted control state") from None
    finally:
        connection.close()


class ConversationStore:
    """Private SQLite catalog bound to one authenticated user/project/lab."""

    def __init__(self, root: Path, principal: Principal, workspace: WorkspaceContext):
        if principal.user_id != workspace.user_id or principal.lab_id != workspace.lab_id:
            raise PermissionError("Conversation identity does not match the workspace")
        self.root = _safe_path(root)
        if not self.root.is_dir():
            raise ValueError("Conversation result root must already exist")
        self._scope = (principal.user_id, workspace.project_id, workspace.lab_id)
        self._lock = RLock()
        database = self.root / "conversations.sqlite"
        for suffix in ("", "-journal", "-wal", "-shm"):
            _check_persistence(self.root / ("conversations.sqlite" + suffix), private=True)
        created = False
        try:
            descriptor = os.open(database, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            _private_file(database)
        else:
            os.close(descriptor)
            created = True
        self._connection = sqlite3.connect(
            "file:" + quote(str(database), safe="/") + "?mode=rw", uri=True,
            timeout=30, isolation_level=None, check_same_thread=False,
        )
        try:
            if created:
                self._initialize()
            else:
                version = self._connection.execute("PRAGMA user_version").fetchone()[0]
                if version != 1:
                    raise ValueError("Unsupported conversation catalog format")
                scope = self._connection.execute(
                    "SELECT user_id, project_id, lab_id FROM conversation_owner WHERE singleton = 1"
                ).fetchone()
                if scope != self._scope:
                    raise PermissionError("Conversation catalog belongs to another workspace")
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA foreign_keys=ON")
        except Exception:
            self.close()
            raise

    def _initialize(self) -> None:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute(
                "CREATE TABLE conversation_owner (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
                "user_id TEXT NOT NULL, project_id TEXT NOT NULL, lab_id TEXT NOT NULL)"
            )
            self._connection.execute(
                "INSERT INTO conversation_owner VALUES (1, ?, ?, ?)", self._scope,
            )
            self._connection.execute(
                "CREATE TABLE conversation_runs (run_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, "
                "relative_directory TEXT NOT NULL UNIQUE, parent_run_id TEXT REFERENCES conversation_runs(run_id), "
                "sequence INTEGER NOT NULL CHECK(sequence > 0), created_at TEXT NOT NULL, "
                "UNIQUE(conversation_id, sequence))"
            )
            self._connection.execute("PRAGMA user_version=1")
            self._connection.execute("COMMIT")
        except Exception:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            raise

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "ConversationStore":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    @staticmethod
    def _link(row) -> ConversationRunLink:
        return ConversationRunLink(
            run_id=row[0], conversation_id=row[1], relative_directory=row[2],
            parent_run_id=row[3], sequence=row[4], created_at=row[5],
        )

    def add_run(
        self, conversation_id: str, record: ApplicationRunRecord, directory: Path,
        *, parent_run_id: UUID | None = None,
    ) -> ConversationRunLink:
        conversation_id = _identifier(conversation_id)
        if (record.owner_user_id, record.project_id, record.lab_id) != self._scope:
            raise PermissionError("Run belongs to another workspace")
        directory = _within(directory, self.root)
        if not directory.is_dir():
            raise ValueError("Run directory does not exist")
        persisted = read_run_record(directory)
        if persisted.run_id != record.run_id:
            raise ConversationConflictError("Run directory contains a different run identity")
        if (persisted.owner_user_id, persisted.project_id, persisted.lab_id) != self._scope:
            raise PermissionError("Persisted run belongs to another workspace")
        relative = directory.relative_to(self.root).as_posix()
        parent = str(parent_run_id) if parent_run_id is not None else None
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                row = self._connection.execute(
                    "SELECT * FROM conversation_runs WHERE run_id = ?", (str(record.run_id),),
                ).fetchone()
                if row is not None:
                    existing = self._link(row)
                    if (existing.conversation_id, existing.relative_directory, existing.parent_run_id) != (
                        conversation_id, relative, parent_run_id,
                    ):
                        raise ConversationConflictError("Run conversation binding is immutable")
                    self._connection.execute("COMMIT")
                    return existing
                if parent is not None and self._connection.execute(
                    "SELECT 1 FROM conversation_runs WHERE run_id = ? AND conversation_id = ?",
                    (parent, conversation_id),
                ).fetchone() is None:
                    raise ConversationConflictError("Revision parent must belong to the same conversation")
                sequence = self._connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 FROM conversation_runs WHERE conversation_id = ?",
                    (conversation_id,),
                ).fetchone()[0]
                link = ConversationRunLink(
                    conversation_id=conversation_id, run_id=record.run_id,
                    relative_directory=relative, parent_run_id=parent_run_id,
                    sequence=sequence, created_at=datetime.now(timezone.utc),
                )
                self._connection.execute(
                    "INSERT INTO conversation_runs VALUES (?, ?, ?, ?, ?, ?)",
                    (str(link.run_id), conversation_id, relative, parent, sequence, link.created_at.isoformat()),
                )
                self._connection.execute("COMMIT")
                return link
            except Exception as exc:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                if isinstance(exc, sqlite3.IntegrityError):
                    raise ConversationConflictError("Run identity or directory is already registered") from None
                raise

    def get_run(self, conversation_id: str, run_id: UUID) -> ConversationRunLink:
        conversation_id = _identifier(conversation_id)
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM conversation_runs WHERE conversation_id = ? AND run_id = ?",
                (conversation_id, str(run_id)),
            ).fetchone()
        if row is None:
            raise ConversationNotFoundError("Run is not registered in this conversation")
        return self._link(row)

    def list_runs(self, conversation_id: str | None = None, *, offset=0, limit=20) -> dict:
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Conversation pagination requires offset >= 0 and limit between 1 and 100")
        where, parameters = "", ()
        if conversation_id is not None:
            where, parameters = " WHERE conversation_id = ?", (_identifier(conversation_id),)
        with self._lock:
            self._connection.execute("BEGIN")
            try:
                total = self._connection.execute(
                    "SELECT COUNT(*) FROM conversation_runs" + where, parameters,
                ).fetchone()[0]
                rows = self._connection.execute(
                    "SELECT * FROM conversation_runs" + where
                    + " ORDER BY conversation_id, sequence LIMIT ? OFFSET ?", (*parameters, limit, offset),
                ).fetchall()
                self._connection.execute("COMMIT")
            except Exception:
                self._connection.execute("ROLLBACK")
                raise
        items = [self._link(row).model_dump(mode="json") for row in rows]
        truncated = offset + len(items) < total
        return {
            "items": items, "total": total, "count": len(items), "offset": offset, "limit": limit,
            "truncated": truncated, "next_offset": offset + len(items) if truncated else None,
        }
