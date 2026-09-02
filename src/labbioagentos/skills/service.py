"""Gold Skill lifecycle orchestration with explicit user-owned decisions."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from labbioagentos.governance import (
    AccessAction,
    AccessService,
    AuthorizationDenied,
    Principal,
)
from labbioagentos.contracts import RunStatus
from labbioagentos.trace import RunTraceRecorder, TraceEvent, TraceEventType

from .curator import SkillCuratorPort
from .models import (
    GoldSkill,
    SkillCurationSourceView,
    SkillCuratorDraft,
    SkillProposal,
    SkillProposalContext,
    SkillProcedure,
    SkillSearchContext,
    SkillSourceBundle,
    SkillUsageOutcome,
    SkillUsageRecord,
    SkillUseAuthorization,
    SkillUseProposal,
    SkillUserDecision,
)
from .source import SkillSourceProjector
from .store import SkillApprovalRequiredError, SkillStore, SkillStoreError


class GoldSkillService:
    """Coordinate evidence, user gates, and storage without scientific choices."""

    def __init__(
        self,
        store: SkillStore,
        source_projector: SkillSourceProjector,
        *,
        access_service: AccessService | None = None,
        trace_recorder: RunTraceRecorder | None = None,
    ):
        self.store = store
        self.source_projector = source_projector
        self.access_service = access_service
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

    def create_curation_view(
        self,
        bundle_id: UUID,
    ) -> SkillCurationSourceView:
        return self.source_projector.curation_view(
            self.store.get_source_bundle(bundle_id)
        )

    async def curate_proposal(
        self,
        bundle_id: UUID,
        curator: SkillCuratorPort,
        context: SkillProposalContext,
    ) -> SkillProposal:
        if not isinstance(curator, SkillCuratorPort):
            raise TypeError("curator must implement SkillCuratorPort")
        view = self.create_curation_view(bundle_id)
        draft = await curator.propose(view)
        return self.create_proposal(bundle_id, draft, context)

    def create_proposal(
        self,
        bundle_id: UUID,
        draft: SkillCuratorDraft,
        context: SkillProposalContext,
    ) -> SkillProposal:
        if not isinstance(draft, SkillCuratorDraft):
            raise TypeError("draft must be a SkillCuratorDraft")
        if not isinstance(context, SkillProposalContext):
            raise TypeError("context must be a SkillProposalContext")
        bundle = self.store.get_source_bundle(bundle_id)
        if len(bundle.instruction_refs) > 128 or len(bundle.execution_refs) > 128:
            raise SkillStoreError(
                "Skill source evidence references exceed the proposal bound"
            )
        if len(bundle.trace_event_ids) > 512:
            raise SkillStoreError("Skill source trace references exceed the proposal bound")
        proposal = SkillProposal(
            source_bundle_id=bundle.bundle_id,
            source_run_id=bundle.source_run_id,
            proposed_name=draft.proposed_name,
            description=draft.description,
            scope=context.scope,
            owner_user_id=context.owner_user_id,
            project_id=context.project_id,
            lab_id=context.lab_id,
            parent_skill_id=context.parent_skill_id,
            parent_version=context.parent_version,
            source_usage_record_id=context.source_usage_record_id,
            procedure=SkillProcedure(
                **draft.procedure.model_dump(),
                important_instruction_ids=tuple(
                    item.instruction_id for item in bundle.instruction_refs
                ),
                script_artifact_ids=tuple(
                    item.script_artifact_id
                    for item in bundle.execution_refs
                    if item.script_artifact_id is not None
                ),
                source_trace_event_ids=bundle.trace_event_ids,
            ),
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
        *,
        principal: Principal | None = None,
    ) -> GoldSkill | None:
        proposal = self.store.get_proposal(proposal_id)
        if self.access_service is not None:
            actor = self._require_principal(principal)
            if decision.decided_by != actor.user_id:
                raise AuthorizationDenied(
                    "Skill decision identity does not match the Principal"
                )
            action = {
                "PERSONAL": AccessAction.APPROVE_PERSONAL_SKILL,
                "PROJECT": AccessAction.APPROVE_PROJECT_SKILL,
                "LAB": AccessAction.APPROVE_LAB_SKILL,
            }[proposal.scope.value]
            self.access_service.require_skill_scope(
                actor,
                scope=proposal.scope.value,
                owner_user_id=proposal.owner_user_id,
                project_id=proposal.project_id,
                lab_id=proposal.lab_id,
                action=action,
                resource_id=str(proposal.proposal_id),
                run_id=proposal.source_run_id,
            )
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

    def pending_proposal(self, proposal_id: UUID) -> SkillProposal:
        """Return one trusted pending creation proposal for gate composition."""

        return self.store.get_proposal(proposal_id)

    def submit_use_proposal(
        self,
        proposal: SkillUseProposal,
        *,
        principal: Principal | None = None,
    ) -> None:
        if principal is not None and (
            proposal.requesting_user_id != principal.user_id
            or proposal.lab_id != principal.lab_id
        ):
            raise AuthorizationDenied(
                "Skill use proposal scope does not match the Principal"
            )
        if self.access_service is not None:
            skill = self.store.get_gold(proposal.skill_id, proposal.skill_version)
            actor = self._require_principal(principal)
            self.access_service.require_skill(
                actor,
                skill,
                self._use_action(skill.scope.value),
                run_id=proposal.run_id,
            )
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
        *,
        principal: Principal | None = None,
    ) -> SkillUseAuthorization:
        proposal = self.store.get_use_proposal(proposal_id)
        if principal is not None and (
            proposal.requesting_user_id != principal.user_id
            or proposal.lab_id != principal.lab_id
        ):
            raise AuthorizationDenied(
                "Skill use decision scope does not match the Principal"
            )
        if self.access_service is not None:
            actor = self._require_principal(principal)
            if decision.decided_by != actor.user_id:
                raise AuthorizationDenied(
                    "Skill use decision identity does not match the Principal"
                )
            skill = self.store.get_gold(proposal.skill_id, proposal.skill_version)
            self.access_service.require_skill(
                actor,
                skill,
                self._use_action(skill.scope.value),
                run_id=proposal.run_id,
            )
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

    def pending_use_proposal(self, proposal_id: UUID) -> SkillUseProposal:
        """Return one trusted pending use proposal for gate composition."""

        return self.store.get_use_proposal(proposal_id)

    def get_authorized_context(
        self,
        authorization_id: UUID,
        *,
        run_id: UUID,
        project_id: str,
        principal: Principal,
    ) -> GoldSkill:
        """Return full procedural context only for one exact approved use."""

        authorization = self.store.get_authorization(authorization_id)
        expected = (
            authorization.run_id,
            authorization.authorized_user_id,
            authorization.project_id,
            authorization.lab_id,
        )
        supplied = (
            run_id,
            principal.user_id,
            project_id,
            principal.lab_id,
        )
        if not authorization.approved or supplied != expected:
            raise SkillApprovalRequiredError(
                "Skill context access requires an exact approved authorization"
            )
        skill = self.store.get_gold(
            authorization.skill_id,
            authorization.skill_version,
        )
        if self.access_service is not None:
            self.access_service.require_skill(
                principal,
                skill,
                self._use_action(skill.scope.value),
                run_id=run_id,
            )
        access = self.store.record_context_access(
            authorization_id,
            run_id=run_id,
            skill_id=authorization.skill_id,
            skill_version=authorization.skill_version,
            accessed_by=principal.user_id,
        )
        self._emit(
            run_id,
            TraceEventType.SKILL_CONTEXT_ACCESSED,
            "ACCESSED",
            {
                "access_id": str(access.access_id),
                "authorization_id": str(authorization.authorization_id),
                "skill_id": str(skill.skill_id),
                "skill_version": skill.version,
            },
        )
        return skill

    def finalize_run_usage(
        self,
        run_id: UUID,
        status: RunStatus,
    ) -> tuple[SkillUsageRecord, ...]:
        """Idempotently finalize only Skills whose full context was accessed."""

        outcome = {
            RunStatus.COMPLETED: SkillUsageOutcome.SUCCEEDED,
            RunStatus.FAILED: SkillUsageOutcome.FAILED,
            RunStatus.CANCELLED: SkillUsageOutcome.CANCELLED,
        }.get(status)
        if outcome is None:
            return ()
        finalized: list[SkillUsageRecord] = []
        for access in self.store.accesses_for_run(run_id):
            existing = self.store.get_usage_for_authorization(
                access.authorization_id
            )
            if existing is not None:
                finalized.append(existing)
                continue
            finalized.append(self.record_usage(access.authorization_id, outcome))
        return tuple(finalized)

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

    def get_gold(
        self,
        skill_id: UUID,
        version: int,
        *,
        principal: Principal | None = None,
    ) -> GoldSkill:
        skill = self.store.get_gold(skill_id, version)
        if self.access_service is not None:
            actor = self._require_principal(principal)
            self.access_service.require_skill(
                actor,
                skill,
                self._use_action(skill.scope.value),
            )
        return skill

    def search(
        self,
        context: SkillSearchContext,
        *,
        principal: Principal | None = None,
    ) -> tuple[GoldSkill, ...]:
        if self.access_service is None:
            return self.store.search(context)
        actor = self._require_principal(principal)
        if context.user_id is not None and context.user_id != actor.user_id:
            raise AuthorizationDenied("Skill search cannot impersonate another user")
        if context.lab_id is not None and context.lab_id != actor.lab_id:
            raise AuthorizationDenied("Skill search cannot cross lab scope")
        secured_context = context.model_copy(
            update={"user_id": actor.user_id, "lab_id": actor.lab_id}
        )
        visible: list[GoldSkill] = []
        for skill in self.store.search(secured_context):
            try:
                self.access_service.require_skill(
                    actor,
                    skill,
                    self._use_action(skill.scope.value),
                )
            except AuthorizationDenied:
                continue
            visible.append(skill)
        return tuple(visible)

    @staticmethod
    def _use_action(scope: str) -> AccessAction:
        return {
            "PERSONAL": AccessAction.USE_PERSONAL_SKILL,
            "PROJECT": AccessAction.USE_PROJECT_SKILL,
            "LAB": AccessAction.USE_LAB_SKILL,
        }[scope]

    @staticmethod
    def _require_principal(principal: Principal | None) -> Principal:
        if principal is None:
            raise AuthorizationDenied("A Principal is required for governed Skill access")
        return principal

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
