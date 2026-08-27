"""Gold Skill lifecycle orchestration with explicit user-owned decisions."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from labbioagentos.trace import RunTraceRecorder, TraceEvent, TraceEventType

from .curator import SkillCuratorPort
from .models import (
    GoldSkill,
    SkillProposal,
    SkillSearchContext,
    SkillSourceBundle,
    SkillUsageOutcome,
    SkillUsageRecord,
    SkillUseAuthorization,
    SkillUseProposal,
    SkillUserDecision,
)
from .source import SkillSourceProjector
from .store import InMemorySkillStore, SkillStoreError


class GoldSkillService:
    """Coordinate evidence, user gates, and storage without scientific choices."""

    def __init__(
        self,
        store: InMemorySkillStore,
        source_projector: SkillSourceProjector,
        *,
        trace_recorder: RunTraceRecorder | None = None,
    ):
        self.store = store
        self.source_projector = source_projector
        self.trace_recorder = trace_recorder

    def create_source_bundle(
        self,
        events: tuple[TraceEvent, ...] | list[TraceEvent],
        *,
        run_id: UUID | None = None,
        task_reference: str | None = None,
    ) -> SkillSourceBundle:
        bundle = self.source_projector.project(
            events,
            run_id=run_id,
            task_reference=task_reference,
        )
        self.store.save_source_bundle(bundle)
        self._emit(
            bundle.source_run_id,
            TraceEventType.SKILL_SOURCE_CREATED,
            "CREATED",
            {
                "bundle_id": str(bundle.bundle_id),
                "source_run_id": str(bundle.source_run_id),
                "stage_count": len(bundle.workflow_stage_path),
                "instruction_ref_count": len(bundle.instruction_refs),
                "artifact_ref_count": len(bundle.artifact_ids),
            },
        )
        return bundle

    def create_proposal(
        self,
        bundle_id: UUID,
        curator: SkillCuratorPort,
    ) -> SkillProposal:
        if not isinstance(curator, SkillCuratorPort):
            raise TypeError("curator must implement SkillCuratorPort")
        bundle = self.store.get_source_bundle(bundle_id)
        proposal = curator.propose(bundle)
        if not isinstance(proposal, SkillProposal):
            raise SkillStoreError("SkillCuratorPort must return a SkillProposal")
        if (
            proposal.source_bundle_id != bundle.bundle_id
            or proposal.source_run_id != bundle.source_run_id
        ):
            raise SkillStoreError(
                "Curator proposal lineage does not match the supplied source bundle"
            )
        self.store.save_proposal(proposal)
        self._emit(
            proposal.source_run_id,
            TraceEventType.SKILL_PROPOSAL_CREATED,
            "PENDING_USER_APPROVAL",
            {
                "proposal_id": str(proposal.proposal_id),
                "bundle_id": str(proposal.source_bundle_id),
                "source_run_id": str(proposal.source_run_id),
                "approval_gate_id": proposal.approval_gate_id,
                "parent_skill_id": (
                    str(proposal.parent_skill_id)
                    if proposal.parent_skill_id is not None
                    else None
                ),
                "parent_version": proposal.parent_version,
            },
        )
        return proposal

    def decide_proposal(
        self,
        proposal_id: UUID,
        decision: SkillUserDecision,
    ) -> GoldSkill | None:
        proposal = self.store.get_proposal(proposal_id)
        gold = self.store.decide_proposal(proposal_id, decision)
        if gold is None:
            self._emit(
                proposal.source_run_id,
                TraceEventType.SKILL_PROPOSAL_REJECTED,
                "REJECTED",
                {
                    "proposal_id": str(proposal.proposal_id),
                    "decision_id": str(decision.decision_id),
                    "decided_by": decision.decided_by,
                },
            )
            return None
        self._emit(
            proposal.source_run_id,
            TraceEventType.SKILL_PROPOSAL_APPROVED,
            "GOLD",
            {
                "proposal_id": str(proposal.proposal_id),
                "decision_id": str(decision.decision_id),
                "skill_id": str(gold.skill_id),
                "skill_version": gold.version,
                "decided_by": decision.decided_by,
            },
        )
        return gold

    def submit_use_proposal(self, proposal: SkillUseProposal) -> None:
        self.store.save_use_proposal(proposal)
        self._emit(
            proposal.run_id,
            TraceEventType.SKILL_USE_PROPOSED,
            "PENDING_USER_APPROVAL",
            {
                "use_proposal_id": str(proposal.proposal_id),
                "skill_id": str(proposal.skill_id),
                "skill_version": proposal.skill_version,
                "proposed_mode": proposal.proposed_mode.value,
                "approval_gate_id": proposal.approval_gate_id,
            },
        )

    def decide_use(
        self,
        proposal_id: UUID,
        decision: SkillUserDecision,
    ) -> SkillUseAuthorization:
        proposal = self.store.get_use_proposal(proposal_id)
        authorization = self.store.decide_use(proposal_id, decision)
        event_type = (
            TraceEventType.SKILL_USE_APPROVED
            if authorization.approved
            else TraceEventType.SKILL_USE_REJECTED
        )
        self._emit(
            proposal.run_id,
            event_type,
            "APPROVED" if authorization.approved else "REJECTED",
            {
                "use_proposal_id": str(proposal.proposal_id),
                "authorization_id": str(authorization.authorization_id),
                "decision_id": str(decision.decision_id),
                "skill_id": str(proposal.skill_id),
                "skill_version": proposal.skill_version,
                "proposed_mode": proposal.proposed_mode.value,
                "decided_by": decision.decided_by,
            },
        )
        return authorization

    def record_usage(
        self,
        authorization_id: UUID,
        outcome: SkillUsageOutcome,
        *,
        resulting_proposal_id: UUID | None = None,
    ) -> SkillUsageRecord:
        usage = self.store.record_usage(
            authorization_id,
            outcome,
            resulting_proposal_id=resulting_proposal_id,
        )
        self._emit(
            usage.run_id,
            TraceEventType.SKILL_USAGE_RECORDED,
            usage.outcome.value,
            {
                "usage_id": str(usage.usage_id),
                "authorization_id": str(usage.authorization_id),
                "skill_id": str(usage.skill_id),
                "skill_version": usage.skill_version,
                "proposed_mode": usage.proposed_mode.value,
                "user_approved": usage.user_approved,
                "resulting_proposal_id": (
                    str(usage.resulting_proposal_id)
                    if usage.resulting_proposal_id is not None
                    else None
                ),
            },
        )
        return usage

    def search(self, context: SkillSearchContext) -> tuple[GoldSkill, ...]:
        return self.store.search(context)

    def _emit(
        self,
        run_id: UUID,
        event_type: TraceEventType,
        status: str,
        payload: dict[str, Any],
    ) -> None:
        if self.trace_recorder is None:
            return
        self.trace_recorder.emit(
            run_id,
            event_type,
            status=status,
            payload=payload,
        )
