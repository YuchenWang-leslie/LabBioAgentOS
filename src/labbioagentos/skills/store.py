"""In-memory immutable Gold Skill version and usage storage."""

from __future__ import annotations

from threading import Lock
from uuid import UUID, uuid4

from .models import (
    GoldSkill,
    SkillProposal,
    SkillScope,
    SkillSearchContext,
    SkillSourceBundle,
    SkillStatus,
    SkillUsageOutcome,
    SkillUsageRecord,
    SkillUseAuthorization,
    SkillUseMode,
    SkillUseProposal,
    SkillUserDecision,
)


class SkillStoreError(RuntimeError):
    """Base deterministic Gold Skill storage failure."""


class SkillNotFoundError(SkillStoreError):
    """A requested bundle, proposal, Skill, or use record does not exist."""


class SkillDecisionError(SkillStoreError):
    """A decision does not match the pending proposal/gate."""


class SkillApprovalRequiredError(SkillStoreError):
    """An operation requires an explicit approved user decision."""


class SkillVersionConflictError(SkillStoreError):
    """An immutable proposal or Gold version would be overwritten."""


class InMemorySkillStore:
    """Process-local development store with locked writes and immutable values."""

    def __init__(self):
        self._bundles: dict[UUID, SkillSourceBundle] = {}
        self._proposals: dict[UUID, SkillProposal] = {}
        self._proposal_decisions: dict[UUID, SkillUserDecision] = {}
        self._gold: dict[tuple[UUID, int], GoldSkill] = {}
        self._use_proposals: dict[UUID, SkillUseProposal] = {}
        self._use_authorizations: dict[UUID, SkillUseAuthorization] = {}
        self._use_decisions: dict[UUID, SkillUserDecision] = {}
        self._usage: dict[UUID, SkillUsageRecord] = {}
        self._lock = Lock()

    def save_source_bundle(self, bundle: SkillSourceBundle) -> None:
        with self._lock:
            if bundle.bundle_id in self._bundles:
                raise SkillVersionConflictError(
                    f"Skill source bundle already exists: {bundle.bundle_id}"
                )
            self._bundles[bundle.bundle_id] = bundle

    def get_source_bundle(self, bundle_id: UUID) -> SkillSourceBundle:
        try:
            return self._bundles[bundle_id]
        except KeyError as exc:
            raise SkillNotFoundError(f"Skill source bundle not found: {bundle_id}") from exc

    def save_proposal(self, proposal: SkillProposal) -> None:
        with self._lock:
            if proposal.proposal_id in self._proposals:
                raise SkillVersionConflictError(
                    f"Skill proposal already exists: {proposal.proposal_id}"
                )
            bundle = self._bundles.get(proposal.source_bundle_id)
            if bundle is None:
                raise SkillNotFoundError(
                    f"Skill source bundle not found: {proposal.source_bundle_id}"
                )
            if bundle.source_run_id != proposal.source_run_id:
                raise SkillStoreError(
                    "Skill proposal source_run_id does not match its source bundle"
                )
            if proposal.parent_skill_id is not None:
                self._require_gold(proposal.parent_skill_id, proposal.parent_version)
            if proposal.source_usage_record_id is not None:
                usage = self._usage.get(proposal.source_usage_record_id)
                if usage is None:
                    raise SkillNotFoundError(
                        f"Source usage record not found: {proposal.source_usage_record_id}"
                    )
                if (
                    usage.proposed_mode is not SkillUseMode.ADAPT
                    or usage.outcome is not SkillUsageOutcome.SUCCEEDED
                    or usage.run_id != proposal.source_run_id
                ):
                    raise SkillStoreError(
                        "A version proposal source usage must be a successful ADAPT "
                        "record for the source run"
                    )
            self._proposals[proposal.proposal_id] = proposal

    def get_proposal(self, proposal_id: UUID) -> SkillProposal:
        try:
            return self._proposals[proposal_id]
        except KeyError as exc:
            raise SkillNotFoundError(f"Skill proposal not found: {proposal_id}") from exc

    def decide_proposal(
        self,
        proposal_id: UUID,
        decision: SkillUserDecision,
    ) -> GoldSkill | None:
        with self._lock:
            proposal = self._proposals.get(proposal_id)
            if proposal is None:
                raise SkillNotFoundError(f"Skill proposal not found: {proposal_id}")
            self._validate_decision(
                decision,
                subject_id=proposal.proposal_id,
                gate_id=proposal.approval_gate_id,
            )
            if proposal_id in self._proposal_decisions:
                raise SkillDecisionError("Skill proposal already has a user decision")
            if not decision.approved:
                self._proposal_decisions[proposal_id] = decision
                return None

            if proposal.parent_skill_id is None:
                skill_id = uuid4()
                version = 1
                parent = None
            else:
                parent = self._require_gold(
                    proposal.parent_skill_id,
                    proposal.parent_version,
                )
                skill_id = parent.skill_id
                version = parent.version + 1
                if (
                    proposal.scope is not parent.scope
                    or proposal.owner_user_id != parent.owner_user_id
                    or proposal.project_id != parent.project_id
                ):
                    raise SkillStoreError(
                        "A new Gold Skill version must preserve scope and ownership"
                    )
            key = (skill_id, version)
            if key in self._gold:
                raise SkillVersionConflictError(
                    f"Gold Skill version already exists: {skill_id} v{version}"
                )
            gold = GoldSkill(
                skill_id=skill_id,
                version=version,
                status=SkillStatus.GOLD,
                name=proposal.proposed_name,
                description=proposal.description,
                scope=proposal.scope,
                source_run_id=proposal.source_run_id,
                source_bundle_id=proposal.source_bundle_id,
                source_proposal_id=proposal.proposal_id,
                parent_skill_id=parent.skill_id if parent is not None else None,
                parent_version=parent.version if parent is not None else None,
                source_usage_record_id=proposal.source_usage_record_id,
                owner_user_id=proposal.owner_user_id,
                project_id=proposal.project_id,
                procedure=proposal.procedure,
                approved_by=decision.decided_by,
                approved_at=decision.decided_at,
            )
            self._proposal_decisions[proposal_id] = decision
            self._gold[key] = gold
            return gold

    def get_gold(self, skill_id: UUID, version: int) -> GoldSkill:
        return self._require_gold(skill_id, version)

    def lineage(self, skill_id: UUID) -> tuple[GoldSkill, ...]:
        return tuple(
            skill
            for (candidate_id, _), skill in sorted(
                self._gold.items(),
                key=lambda item: (str(item[0][0]), item[0][1]),
            )
            if candidate_id == skill_id
        )

    def search(self, context: SkillSearchContext) -> tuple[GoldSkill, ...]:
        candidates = tuple(
            skill
            for skill in self._gold.values()
            if self._eligible(skill, context)
        )
        return tuple(
            sorted(
                candidates,
                key=lambda skill: (
                    skill.name.casefold(),
                    str(skill.skill_id),
                    skill.version,
                ),
            )
        )

    def save_use_proposal(self, proposal: SkillUseProposal) -> None:
        with self._lock:
            if proposal.proposal_id in self._use_proposals:
                raise SkillVersionConflictError(
                    f"Skill use proposal already exists: {proposal.proposal_id}"
                )
            self._require_gold(proposal.skill_id, proposal.skill_version)
            self._use_proposals[proposal.proposal_id] = proposal

    def get_use_proposal(self, proposal_id: UUID) -> SkillUseProposal:
        try:
            return self._use_proposals[proposal_id]
        except KeyError as exc:
            raise SkillNotFoundError(
                f"Skill use proposal not found: {proposal_id}"
            ) from exc

    def decide_use(
        self,
        proposal_id: UUID,
        decision: SkillUserDecision,
    ) -> SkillUseAuthorization:
        with self._lock:
            proposal = self._use_proposals.get(proposal_id)
            if proposal is None:
                raise SkillNotFoundError(
                    f"Skill use proposal not found: {proposal_id}"
                )
            self._validate_decision(
                decision,
                subject_id=proposal.proposal_id,
                gate_id=proposal.approval_gate_id,
            )
            if proposal_id in self._use_decisions:
                raise SkillDecisionError("Skill use proposal already has a user decision")
            authorization = SkillUseAuthorization(
                use_proposal_id=proposal.proposal_id,
                run_id=proposal.run_id,
                skill_id=proposal.skill_id,
                skill_version=proposal.skill_version,
                approved=decision.approved,
                decided_by=decision.decided_by,
                decided_at=decision.decided_at,
            )
            self._use_decisions[proposal_id] = decision
            self._use_authorizations[authorization.authorization_id] = authorization
            return authorization

    def record_usage(
        self,
        authorization_id: UUID,
        outcome: SkillUsageOutcome,
        *,
        resulting_proposal_id: UUID | None = None,
    ) -> SkillUsageRecord:
        with self._lock:
            authorization = self._use_authorizations.get(authorization_id)
            if authorization is None:
                raise SkillNotFoundError(
                    f"Skill use authorization not found: {authorization_id}"
                )
            if not authorization.approved:
                raise SkillApprovalRequiredError(
                    "Rejected Skill use cannot produce a usage record"
                )
            if any(
                record.authorization_id == authorization_id
                for record in self._usage.values()
            ):
                raise SkillVersionConflictError(
                    "Skill use authorization already has a usage record"
                )
            proposal = self._use_proposals[authorization.use_proposal_id]
            if resulting_proposal_id is not None and resulting_proposal_id not in self._proposals:
                raise SkillNotFoundError(
                    f"Resulting proposal not found: {resulting_proposal_id}"
                )
            usage = SkillUsageRecord(
                authorization_id=authorization.authorization_id,
                run_id=proposal.run_id,
                skill_id=proposal.skill_id,
                skill_version=proposal.skill_version,
                proposed_mode=proposal.proposed_mode,
                user_approved=True,
                runtime_provided_deviations=proposal.proposed_deviations,
                outcome=outcome,
                resulting_proposal_id=resulting_proposal_id,
            )
            self._usage[usage.usage_id] = usage
            return usage

    def get_usage(self, usage_id: UUID) -> SkillUsageRecord:
        try:
            return self._usage[usage_id]
        except KeyError as exc:
            raise SkillNotFoundError(f"Skill usage record not found: {usage_id}") from exc

    def _require_gold(self, skill_id: UUID, version: int | None) -> GoldSkill:
        if version is None:
            raise SkillNotFoundError("Gold Skill version is required")
        try:
            return self._gold[(skill_id, version)]
        except KeyError as exc:
            raise SkillNotFoundError(
                f"Gold Skill not found: {skill_id} v{version}"
            ) from exc

    @staticmethod
    def _validate_decision(
        decision: SkillUserDecision,
        *,
        subject_id: UUID,
        gate_id: str,
    ) -> None:
        if decision.subject_id != subject_id or decision.gate_id != gate_id:
            raise SkillDecisionError(
                "User decision does not match the pending Skill gate"
            )

    @staticmethod
    def _eligible(skill: GoldSkill, context: SkillSearchContext) -> bool:
        if skill.status is not SkillStatus.GOLD:
            return False
        if skill.scope is SkillScope.PERSONAL:
            if context.user_id is None or skill.owner_user_id != context.user_id:
                return False
        elif skill.scope is SkillScope.PROJECT:
            if context.project_id is None or skill.project_id != context.project_id:
                return False
        elif not context.include_lab:
            return False
        if context.required_tags and not context.required_tags.issubset(
            skill.procedure.tags
        ):
            return False
        if context.artifact_types and not context.artifact_types.issubset(
            skill.procedure.artifact_types
        ):
            return False
        if context.query_text is not None:
            needle = context.query_text.casefold()
            searchable = " ".join(
                (
                    skill.name,
                    skill.description,
                    skill.procedure.applicability,
                    *sorted(skill.procedure.tags),
                )
            ).casefold()
            if needle not in searchable:
                return False
        return True
