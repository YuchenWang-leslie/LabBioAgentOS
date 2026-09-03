"""Immutable in-memory and transactional SQLite Memory storage."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from threading import RLock
from typing import Protocol, runtime_checkable
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, ValidationError

from .models import (
    MemoryDecision,
    MemoryEntry,
    MemoryProposalAction,
    MemoryStatus,
    MemoryUpdateProposal,
)


class MemoryStoreError(RuntimeError):
    pass


class MemoryNotFoundError(MemoryStoreError):
    pass


class MemoryConflictError(MemoryStoreError):
    pass


class MemoryStaleUpdateError(MemoryConflictError):
    """A proposal no longer targets the current immutable Memory version."""


@runtime_checkable
class MemoryStore(Protocol):
    """Persistence contract used by MemoryGovernanceService."""

    def save_proposal(self, proposal: MemoryUpdateProposal) -> None: ...
    def get_proposal(self, proposal_id: UUID) -> MemoryUpdateProposal: ...
    def decide_proposal(
        self, proposal_id: UUID, decision: MemoryDecision
    ) -> MemoryEntry | None: ...
    def get_decision(self, proposal_id: UUID) -> MemoryDecision | None: ...
    def get(self, memory_id: UUID, version: int) -> MemoryEntry: ...
    def latest(self, memory_id: UUID) -> MemoryEntry: ...
    def lineage(self, memory_id: UUID) -> tuple[MemoryEntry, ...]: ...
    def entries(self) -> tuple[MemoryEntry, ...]: ...
    def active_latest(self) -> tuple[MemoryEntry, ...]: ...


class _MemoryStoreSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    proposals: tuple[MemoryUpdateProposal, ...] = ()
    decisions: tuple[MemoryDecision, ...] = ()
    entries: tuple[MemoryEntry, ...] = ()


class InMemoryMemoryStore:
    """Trusted store with no public direct Memory write/update operation."""

    def __init__(self):
        self._proposals: dict[UUID, MemoryUpdateProposal] = {}
        self._decisions: dict[UUID, MemoryDecision] = {}
        self._entries: dict[tuple[UUID, int], MemoryEntry] = {}
        self._lock = RLock()

    def save_proposal(self, proposal: MemoryUpdateProposal) -> None:
        with self._lock:
            if proposal.proposal_id in self._proposals:
                raise MemoryConflictError(
                    f"Memory proposal already exists: {proposal.proposal_id}"
                )
            self._proposals[proposal.proposal_id] = proposal

    def get_proposal(self, proposal_id: UUID) -> MemoryUpdateProposal:
        try:
            return self._proposals[proposal_id]
        except KeyError as exc:
            raise MemoryNotFoundError(
                f"Memory proposal not found: {proposal_id}"
            ) from exc

    def decide_proposal(
        self,
        proposal_id: UUID,
        decision: MemoryDecision,
    ) -> MemoryEntry | None:
        with self._lock:
            proposal = self._proposals.get(proposal_id)
            if proposal is None:
                raise MemoryNotFoundError(f"Memory proposal not found: {proposal_id}")
            if (
                decision.proposal_id != proposal.proposal_id
                or decision.gate_id != proposal.approval_gate_id
            ):
                raise MemoryConflictError(
                    "Memory decision does not match the pending proposal gate"
                )
            if proposal_id in self._decisions:
                raise MemoryConflictError("Memory proposal already has a decision")
            if not decision.approved:
                self._decisions[proposal_id] = decision
                return None

            parent = None
            if proposal.target_memory_id is None:
                memory_id, version = uuid4(), 1
                kind = proposal.proposed_kind
                content = proposal.proposed_content
                evidence_run_ids = proposal.evidence_run_ids
                evidence_artifact_ids = proposal.evidence_artifact_ids
            else:
                lineage = self.lineage(proposal.target_memory_id)
                if not lineage:
                    raise MemoryNotFoundError(
                        f"Memory entry not found: {proposal.target_memory_id}"
                    )
                parent = lineage[-1]
                if parent.version != proposal.target_version:
                    raise MemoryStaleUpdateError(
                        "Memory proposal does not target the latest version"
                    )
                if parent.status is not MemoryStatus.ACTIVE:
                    raise MemoryStaleUpdateError(
                        "Retired Memory cannot be updated or retired again"
                    )
                self._validate_update_scope(proposal, parent)
                memory_id, version = parent.memory_id, parent.version + 1
                if proposal.action is MemoryProposalAction.RETIRE:
                    kind = parent.kind
                    content = parent.content
                    evidence_run_ids = parent.evidence_run_ids
                    evidence_artifact_ids = parent.evidence_artifact_ids
                else:
                    kind = proposal.proposed_kind
                    content = proposal.proposed_content
                    evidence_run_ids = proposal.evidence_run_ids
                    evidence_artifact_ids = proposal.evidence_artifact_ids
            if kind is None or content is None:
                raise MemoryStoreError("Approved Memory content is incomplete")
            entry = MemoryEntry(
                memory_id=memory_id,
                version=version,
                status=(
                    MemoryStatus.RETIRED
                    if proposal.action is MemoryProposalAction.RETIRE
                    else MemoryStatus.ACTIVE
                ),
                scope=proposal.target_scope,
                owner_user_id=proposal.owner_user_id,
                project_id=proposal.project_id,
                lab_id=proposal.lab_id,
                kind=kind,
                content=content,
                evidence_run_ids=evidence_run_ids,
                evidence_artifact_ids=evidence_artifact_ids,
                source_proposal_id=proposal.proposal_id,
                previous_version=parent.version if parent is not None else None,
                approved_by=decision.decided_by,
                approved_at=decision.decided_at,
            )
            key = (entry.memory_id, entry.version)
            if key in self._entries:
                raise MemoryConflictError(
                    f"Memory version already exists: {entry.memory_id} v{entry.version}"
                )
            self._decisions[proposal_id] = decision
            self._entries[key] = entry
            return entry

    def get_decision(self, proposal_id: UUID) -> MemoryDecision | None:
        return self._decisions.get(proposal_id)

    def get(self, memory_id: UUID, version: int) -> MemoryEntry:
        try:
            return self._entries[(memory_id, version)]
        except KeyError as exc:
            raise MemoryNotFoundError(
                f"Memory entry not found: {memory_id} v{version}"
            ) from exc

    def latest(self, memory_id: UUID) -> MemoryEntry:
        lineage = self.lineage(memory_id)
        if not lineage:
            raise MemoryNotFoundError(f"Memory entry not found: {memory_id}")
        return lineage[-1]

    def lineage(self, memory_id: UUID) -> tuple[MemoryEntry, ...]:
        return tuple(
            entry
            for (candidate_id, _), entry in sorted(
                self._entries.items(), key=lambda item: (str(item[0][0]), item[0][1])
            )
            if candidate_id == memory_id
        )

    def entries(self) -> tuple[MemoryEntry, ...]:
        return tuple(
            entry
            for _, entry in sorted(
                self._entries.items(), key=lambda item: (str(item[0][0]), item[0][1])
            )
        )

    def active_latest(self) -> tuple[MemoryEntry, ...]:
        latest = {}
        for entry in self._entries.values():
            current = latest.get(entry.memory_id)
            if current is None or entry.version > current.version:
                latest[entry.memory_id] = entry
        return tuple(
            sorted(
                (
                    entry
                    for entry in latest.values()
                    if entry.status is MemoryStatus.ACTIVE
                ),
                key=lambda entry: (entry.scope.value, entry.kind.value, str(entry.memory_id)),
            )
        )

    def _snapshot(self) -> _MemoryStoreSnapshot:
        return _MemoryStoreSnapshot(
            proposals=tuple(
                self._proposals[key] for key in sorted(self._proposals, key=str)
            ),
            decisions=tuple(
                self._decisions[key] for key in sorted(self._decisions, key=str)
            ),
            entries=self.entries(),
        )

    @classmethod
    def _from_snapshot(cls, snapshot: _MemoryStoreSnapshot) -> "InMemoryMemoryStore":
        store = cls()
        store._proposals = _unique_index(
            snapshot.proposals, lambda item: item.proposal_id, "proposal"
        )
        store._decisions = _unique_index(
            snapshot.decisions, lambda item: item.proposal_id, "decision"
        )
        store._entries = _unique_index(
            snapshot.entries,
            lambda item: (item.memory_id, item.version),
            "Memory version",
        )
        return store

    @staticmethod
    def _validate_update_scope(
        proposal: MemoryUpdateProposal,
        parent: MemoryEntry,
    ) -> None:
        if (
            proposal.target_scope is not parent.scope
            or proposal.owner_user_id != parent.owner_user_id
            or proposal.project_id != parent.project_id
            or proposal.lab_id != parent.lab_id
        ):
            raise MemoryConflictError(
                "A Memory update must preserve scope and ownership"
            )


def _unique_index(values, key, label: str):
    indexed = {}
    for value in values:
        identifier = key(value)
        if identifier in indexed:
            raise MemoryConflictError(
                f"Memory store snapshot contains duplicate {label}"
            )
        indexed[identifier] = value
    return indexed


class SQLiteMemoryStore:
    """Transactional Pydantic-JSON Memory store for one local process."""

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
            CREATE TABLE IF NOT EXISTS memory_store_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                payload TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            "INSERT OR IGNORE INTO memory_store_state(singleton, payload) VALUES (1, ?)",
            (_MemoryStoreSnapshot().model_dump_json(),),
        )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def save_proposal(self, proposal: MemoryUpdateProposal) -> None:
        self._write("save_proposal", proposal)

    def get_proposal(self, proposal_id: UUID) -> MemoryUpdateProposal:
        return self._read("get_proposal", proposal_id)

    def decide_proposal(
        self, proposal_id: UUID, decision: MemoryDecision
    ) -> MemoryEntry | None:
        return self._write("decide_proposal", proposal_id, decision)

    def get_decision(self, proposal_id: UUID) -> MemoryDecision | None:
        return self._read("get_decision", proposal_id)

    def get(self, memory_id: UUID, version: int) -> MemoryEntry:
        return self._read("get", memory_id, version)

    def latest(self, memory_id: UUID) -> MemoryEntry:
        return self._read("latest", memory_id)

    def lineage(self, memory_id: UUID) -> tuple[MemoryEntry, ...]:
        return self._read("lineage", memory_id)

    def entries(self) -> tuple[MemoryEntry, ...]:
        return self._read("entries")

    def active_latest(self) -> tuple[MemoryEntry, ...]:
        return self._read("active_latest")

    def _read(self, operation: str, *args):
        with self._lock:
            return getattr(self._load(), operation)(*args)

    def _write(self, operation: str, *args):
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                store = self._load()
                result = getattr(store, operation)(*args)
                payload = store._snapshot().model_dump_json()
                self._connection.execute(
                    "UPDATE memory_store_state SET payload = ? WHERE singleton = 1",
                    (payload,),
                )
                self._connection.execute("COMMIT")
                return result
            except Exception:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def _load(self) -> InMemoryMemoryStore:
        row = self._connection.execute(
            "SELECT payload FROM memory_store_state WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise MemoryStoreError("SQLite Memory store state is missing")
        try:
            snapshot = _MemoryStoreSnapshot.model_validate_json(row[0])
        except (ValidationError, ValueError, TypeError) as exc:
            raise MemoryStoreError("SQLite Memory store state is invalid") from exc
        return InMemoryMemoryStore._from_snapshot(snapshot)
