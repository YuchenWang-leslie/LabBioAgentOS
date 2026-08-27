"""Proposal-only persistent Memory governance and immutable version creation."""

from __future__ import annotations

from uuid import UUID, uuid4

from labbioagentos.governance import (
    AccessAction,
    AccessService,
    Principal,
)
from labbioagentos.trace import RunTraceRecorder, TraceEventType

from .models import (
    MemoryDecision,
    MemoryEntry,
    MemoryScope,
    MemoryUpdateProposal,
)
from .store import InMemoryMemoryStore, MemoryConflictError


class MemoryDecisionError(PermissionError):
    pass


class MemoryGovernanceService:
    """Only public path that can turn an approved proposal into Memory."""

    def __init__(
        self,
        store: InMemoryMemoryStore,
        access: AccessService,
        *,
        trace_recorder: RunTraceRecorder | None = None,
    ):
        self.store = store
        self.access = access
        self.trace_recorder = trace_recorder

    def submit_proposal(
        self,
        principal: Principal,
        proposal: MemoryUpdateProposal,
    ) -> None:
        self._require_proposal_visibility(principal, proposal)
        self.store.save_proposal(proposal)
        self._emit(
            proposal,
            TraceEventType.MEMORY_PROPOSAL_CREATED,
            "PENDING_USER_APPROVAL",
            {"proposal_id": str(proposal.proposal_id)},
        )

    def decide(
        self,
        principal: Principal,
        proposal_id: UUID,
        decision: MemoryDecision,
    ) -> MemoryEntry | None:
        proposal = self.store.get_proposal(proposal_id)
        if (
            decision.proposal_id != proposal.proposal_id
            or decision.gate_id != proposal.approval_gate_id
            or decision.decided_by != principal.user_id
        ):
            raise MemoryDecisionError(
                "Memory decision does not match the proposal gate and principal"
            )
        if self.store.has_decision(proposal_id):
            raise MemoryConflictError("Memory proposal already has a decision")
        self._require_approval(principal, proposal)
        if not decision.approved:
            self.store._record_decision(decision)
            self._emit(
                proposal,
                TraceEventType.MEMORY_PROPOSAL_REJECTED,
                "REJECTED",
                {
                    "proposal_id": str(proposal.proposal_id),
                    "decision_id": str(decision.decision_id),
                },
            )
            return None

        parent = None
        if proposal.target_memory_id is None:
            memory_id, version = uuid4(), 1
        else:
            parent = self.store.get(
                proposal.target_memory_id,
                proposal.target_version,
            )
            self._validate_update_scope(proposal, parent)
            memory_id, version = parent.memory_id, parent.version + 1
        entry = MemoryEntry(
            memory_id=memory_id,
            version=version,
            scope=proposal.target_scope,
            owner_user_id=proposal.owner_user_id,
            project_id=proposal.project_id,
            lab_id=proposal.lab_id,
            kind=proposal.proposed_kind,
            content=proposal.proposed_content,
            evidence_run_ids=proposal.evidence_run_ids,
            evidence_artifact_ids=proposal.evidence_artifact_ids,
            source_proposal_id=proposal.proposal_id,
            previous_version=parent.version if parent is not None else None,
            approved_by=decision.decided_by,
            approved_at=decision.decided_at,
        )
        self.store._commit_entry(entry)
        self.store._record_decision(decision)
        self._emit(
            proposal,
            TraceEventType.MEMORY_PROPOSAL_APPROVED,
            "APPROVED",
            {
                "proposal_id": str(proposal.proposal_id),
                "decision_id": str(decision.decision_id),
                "memory_id": str(entry.memory_id),
                "memory_version": entry.version,
            },
        )
        self._emit(
            proposal,
            TraceEventType.MEMORY_VERSION_CREATED,
            "CREATED",
            {
                "proposal_id": str(proposal.proposal_id),
                "memory_id": str(entry.memory_id),
                "memory_version": entry.version,
                "previous_version": entry.previous_version,
            },
        )
        return entry

    def get(
        self,
        principal: Principal,
        memory_id: UUID,
        version: int,
    ) -> MemoryEntry:
        entry = self.store.get(memory_id, version)
        self._require_read(principal, entry)
        return entry

    def lineage(
        self,
        principal: Principal,
        memory_id: UUID,
    ) -> tuple[MemoryEntry, ...]:
        entries = self.store.lineage(memory_id)
        if entries:
            self._require_read(principal, entries[-1])
        return entries

    def list_visible(self, principal: Principal) -> tuple[MemoryEntry, ...]:
        visible: list[MemoryEntry] = []
        for entry in self.store.entries():
            try:
                self._require_read(principal, entry)
            except PermissionError:
                continue
            visible.append(entry)
        return tuple(visible)

    def _require_proposal_visibility(
        self,
        principal: Principal,
        proposal: MemoryUpdateProposal,
    ) -> None:
        action = {
            MemoryScope.PERSONAL: AccessAction.READ_PERSONAL_MEMORY,
            MemoryScope.PROJECT: AccessAction.READ_PROJECT_MEMORY,
            MemoryScope.LAB: AccessAction.READ_LAB_MEMORY,
        }[proposal.target_scope]
        self._require_scope(principal, proposal, action)

    def _require_approval(
        self,
        principal: Principal,
        proposal: MemoryUpdateProposal,
    ) -> None:
        action = {
            MemoryScope.PERSONAL: AccessAction.WRITE_PERSONAL_MEMORY,
            MemoryScope.PROJECT: AccessAction.WRITE_PROJECT_MEMORY,
            MemoryScope.LAB: AccessAction.APPROVE_LAB_MEMORY,
        }[proposal.target_scope]
        self._require_scope(principal, proposal, action)

    def _require_read(self, principal: Principal, entry: MemoryEntry) -> None:
        action = {
            MemoryScope.PERSONAL: AccessAction.READ_PERSONAL_MEMORY,
            MemoryScope.PROJECT: AccessAction.READ_PROJECT_MEMORY,
            MemoryScope.LAB: AccessAction.READ_LAB_MEMORY,
        }[entry.scope]
        self.access.require_memory_scope(
            principal,
            scope=entry.scope.value,
            owner_user_id=entry.owner_user_id,
            project_id=entry.project_id,
            lab_id=entry.lab_id,
            action=action,
            resource_id=str(entry.memory_id),
            run_id=entry.evidence_run_ids[0] if entry.evidence_run_ids else None,
        )

    def _require_scope(
        self,
        principal: Principal,
        proposal: MemoryUpdateProposal,
        action: AccessAction,
    ) -> None:
        self.access.require_memory_scope(
            principal,
            scope=proposal.target_scope.value,
            owner_user_id=proposal.owner_user_id,
            project_id=proposal.project_id,
            lab_id=proposal.lab_id,
            action=action,
            resource_id=str(proposal.proposal_id),
            run_id=proposal.source_run_id,
        )

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
            raise MemoryDecisionError(
                "A Memory update must preserve scope, ownership, project, and lab"
            )

    def _emit(
        self,
        proposal: MemoryUpdateProposal,
        event_type: TraceEventType,
        status: str,
        extra: dict[str, object],
    ) -> None:
        if self.trace_recorder is None or proposal.source_run_id is None:
            return
        payload = {
            "scope": proposal.target_scope.value,
            "owner_user_id": proposal.owner_user_id,
            "project_id": proposal.project_id,
            "lab_id": proposal.lab_id,
            **extra,
        }
        self.trace_recorder.emit(
            proposal.source_run_id,
            event_type,
            invocation_id=proposal.proposing_invocation_id,
            status=status,
            payload=payload,
        )
