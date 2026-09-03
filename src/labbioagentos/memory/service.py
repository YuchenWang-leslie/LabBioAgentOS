"""Governed proposal-only Memory lifecycle and provenance validation."""

from __future__ import annotations

from uuid import UUID

from labbioagentos.artifacts import ArtifactExposureClass, ArtifactStore
from labbioagentos.governance import (
    AccessAction,
    AccessService,
    AuthorizationDenied,
    Principal,
    WorkspaceContext,
)
from labbioagentos.trace import RunTraceRecorder, TraceEventType

from .models import (
    MemoryDecision,
    MemoryEntry,
    MemoryScope,
    MemoryUpdateProposal,
)
from .store import MemoryStore


class MemoryDecisionError(PermissionError):
    pass


class MemoryEvidenceError(MemoryDecisionError):
    """A proposed provenance reference is not safe to persist."""


class MemoryGovernanceService:
    """Only public path that can turn an approved proposal into Memory."""

    def __init__(
        self,
        store: MemoryStore,
        access: AccessService | None = None,
        *,
        trace_recorder: RunTraceRecorder | None = None,
        artifact_store: ArtifactStore | None = None,
    ):
        self.store = store
        self.access = access
        self.trace_recorder = trace_recorder
        self.artifact_store = artifact_store

    def bind_runtime_context(
        self,
        *,
        access_service: AccessService,
        trace_recorder: RunTraceRecorder,
        artifact_store: ArtifactStore,
    ) -> None:
        """Bind all nested effects to the application's current authorities."""

        if not isinstance(access_service, AccessService):
            raise TypeError("access_service must be an AccessService")
        if not isinstance(trace_recorder, RunTraceRecorder):
            raise TypeError("trace_recorder must be a RunTraceRecorder")
        self.access = access_service
        self.trace_recorder = trace_recorder
        self.artifact_store = artifact_store

    def submit_proposal(
        self,
        principal: Principal,
        proposal: MemoryUpdateProposal,
        *,
        workspace: WorkspaceContext | None = None,
    ) -> None:
        self._require_approval(principal, proposal)
        self._validate_source_lineage(proposal)
        self._validate_artifact_lineage(principal, proposal, workspace)
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
        self._require_approval(principal, proposal)
        entry = self.store.decide_proposal(proposal_id, decision)
        if entry is None:
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
        self._emit(
            proposal,
            TraceEventType.MEMORY_PROPOSAL_APPROVED,
            entry.status.value,
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
            except AuthorizationDenied:
                continue
            visible.append(entry)
        return tuple(visible)

    def list_candidates(self, principal: Principal) -> tuple[MemoryEntry, ...]:
        """Return only latest ACTIVE entries visible to the current Principal."""

        visible = []
        for entry in self.store.active_latest():
            try:
                self._require_read(principal, entry)
            except AuthorizationDenied:
                continue
            visible.append(entry)
        return tuple(visible)

    def pending_proposal(self, proposal_id: UUID) -> MemoryUpdateProposal:
        return self.store.get_proposal(proposal_id)

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
        self._required_access().require_memory_scope(
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
        self._required_access().require_memory_scope(
            principal,
            scope=proposal.target_scope.value,
            owner_user_id=proposal.owner_user_id,
            project_id=proposal.project_id,
            lab_id=proposal.lab_id,
            action=action,
            resource_id=str(proposal.proposal_id),
            run_id=proposal.source_run_id,
        )

    def _validate_artifact_lineage(
        self,
        principal: Principal,
        proposal: MemoryUpdateProposal,
        workspace: WorkspaceContext | None,
    ) -> None:
        if not proposal.evidence_artifact_ids:
            return
        if workspace is None or self.artifact_store is None:
            raise MemoryEvidenceError(
                "Memory Artifact evidence requires a bound governed workspace"
            )
        if (
            workspace.user_id != principal.user_id
            or workspace.lab_id != principal.lab_id
            or proposal.lab_id != workspace.lab_id
        ):
            raise AuthorizationDenied(
                "Memory evidence workspace is not bound to the Principal"
            )
        for artifact_id in proposal.evidence_artifact_ids:
            ref = self.artifact_store.get_ref(artifact_id)
            if (
                ref.project_id != workspace.project_id
                or ref.lab_id != workspace.lab_id
            ):
                raise AuthorizationDenied(
                    "Memory evidence Artifact is outside the bound workspace"
                )
            self._required_access().require_artifact(
                principal,
                ref,
                AccessAction.READ_ARTIFACT,
            )
            if ref.exposure_class is ArtifactExposureClass.RAW:
                raise MemoryEvidenceError(
                    "RAW Artifact references cannot support runtime-generated Memory"
                )

    @staticmethod
    def _validate_source_lineage(proposal: MemoryUpdateProposal) -> None:
        if proposal.evidence_run_ids not in {
            (),
            (proposal.source_run_id,),
        }:
            raise MemoryEvidenceError(
                "Memory evidence run lineage must be the current source run"
            )

    def _required_access(self) -> AccessService:
        if self.access is None:
            raise MemoryDecisionError(
                "Memory governance access service is not configured"
            )
        return self.access

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
