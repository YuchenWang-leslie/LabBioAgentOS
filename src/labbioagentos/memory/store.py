"""Process-local immutable Memory proposal and version storage."""

from __future__ import annotations

from threading import Lock
from uuid import UUID

from .models import MemoryDecision, MemoryEntry, MemoryUpdateProposal


class MemoryStoreError(RuntimeError):
    pass


class MemoryNotFoundError(MemoryStoreError):
    pass


class MemoryConflictError(MemoryStoreError):
    pass


class InMemoryMemoryStore:
    """Trusted store with no public direct Memory write/update operation."""

    def __init__(self):
        self._proposals: dict[UUID, MemoryUpdateProposal] = {}
        self._decisions: dict[UUID, MemoryDecision] = {}
        self._entries: dict[tuple[UUID, int], MemoryEntry] = {}
        self._lock = Lock()

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

    def get(self, memory_id: UUID, version: int) -> MemoryEntry:
        try:
            return self._entries[(memory_id, version)]
        except KeyError as exc:
            raise MemoryNotFoundError(
                f"Memory entry not found: {memory_id} v{version}"
            ) from exc

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

    def has_decision(self, proposal_id: UUID) -> bool:
        return proposal_id in self._decisions

    def _record_decision(self, decision: MemoryDecision) -> None:
        if decision.proposal_id in self._decisions:
            raise MemoryConflictError("Memory proposal already has a decision")
        self._decisions[decision.proposal_id] = decision

    def _commit_entry(self, entry: MemoryEntry) -> None:
        key = (entry.memory_id, entry.version)
        if key in self._entries:
            raise MemoryConflictError(
                f"Memory version already exists: {entry.memory_id} v{entry.version}"
            )
        self._entries[key] = entry
