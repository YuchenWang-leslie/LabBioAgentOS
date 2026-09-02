"""Immutable in-memory and transactional SQLite Gold Skill storage."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from threading import Lock, RLock
from typing import Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, ValidationError

from .models import (
    GoldSkill,
    SkillContextAccess,
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


class SkillStore(Protocol):
    """Persistence contract used by GoldSkillService."""

    def save_source_bundle(self, bundle: SkillSourceBundle) -> None: ...
    def get_source_bundle(self, bundle_id: UUID) -> SkillSourceBundle: ...
    def save_proposal(self, proposal: SkillProposal) -> None: ...
    def get_proposal(self, proposal_id: UUID) -> SkillProposal: ...
    def decide_proposal(
        self, proposal_id: UUID, decision: SkillUserDecision
    ) -> GoldSkill | None: ...
    def get_gold(self, skill_id: UUID, version: int) -> GoldSkill: ...
    def lineage(self, skill_id: UUID) -> tuple[GoldSkill, ...]: ...
    def search(self, context: SkillSearchContext) -> tuple[GoldSkill, ...]: ...
    def save_use_proposal(self, proposal: SkillUseProposal) -> None: ...
    def get_use_proposal(self, proposal_id: UUID) -> SkillUseProposal: ...
    def decide_use(
        self, proposal_id: UUID, decision: SkillUserDecision
    ) -> SkillUseAuthorization: ...
    def get_authorization(self, authorization_id: UUID) -> SkillUseAuthorization: ...
    def get_authorization_for_proposal(
        self, proposal_id: UUID
    ) -> SkillUseAuthorization | None: ...
    def record_context_access(
        self,
        authorization_id: UUID,
        *,
        run_id: UUID,
        skill_id: UUID,
        skill_version: int,
        accessed_by: str,
    ) -> SkillContextAccess: ...
    def get_context_access(self, authorization_id: UUID) -> SkillContextAccess: ...
    def accesses_for_run(self, run_id: UUID) -> tuple[SkillContextAccess, ...]: ...
    def record_usage(
        self,
        authorization_id: UUID,
        outcome: SkillUsageOutcome,
        *,
        resulting_proposal_id: UUID | None = None,
    ) -> SkillUsageRecord: ...
    def get_usage(self, usage_id: UUID) -> SkillUsageRecord: ...
    def get_usage_for_authorization(
        self, authorization_id: UUID
    ) -> SkillUsageRecord | None: ...


class _SkillStoreSnapshot(BaseModel):
    """Complete JSON-serializable store state used by the SQLite backend."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    bundles: tuple[SkillSourceBundle, ...] = ()
    proposals: tuple[SkillProposal, ...] = ()
    proposal_decisions: tuple[SkillUserDecision, ...] = ()
    gold: tuple[GoldSkill, ...] = ()
    use_proposals: tuple[SkillUseProposal, ...] = ()
    use_authorizations: tuple[SkillUseAuthorization, ...] = ()
    use_decisions: tuple[SkillUserDecision, ...] = ()
    context_accesses: tuple[SkillContextAccess, ...] = ()
    usage: tuple[SkillUsageRecord, ...] = ()


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
        self._context_accesses: dict[UUID, SkillContextAccess] = {}
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
                    or proposal.lab_id != parent.lab_id
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
                lab_id=proposal.lab_id,
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
                approval_gate_id=proposal.approval_gate_id,
                decision_id=decision.decision_id,
                run_id=proposal.run_id,
                authorized_user_id=proposal.requesting_user_id,
                project_id=proposal.project_id,
                lab_id=proposal.lab_id,
                skill_id=proposal.skill_id,
                skill_version=proposal.skill_version,
                approved=decision.approved,
                decided_by=decision.decided_by,
                decided_at=decision.decided_at,
            )
            self._use_decisions[proposal_id] = decision
            self._use_authorizations[authorization.authorization_id] = authorization
            return authorization

    def get_authorization(self, authorization_id: UUID) -> SkillUseAuthorization:
        try:
            return self._use_authorizations[authorization_id]
        except KeyError as exc:
            raise SkillNotFoundError(
                f"Skill use authorization not found: {authorization_id}"
            ) from exc

    def get_authorization_for_proposal(
        self, proposal_id: UUID
    ) -> SkillUseAuthorization | None:
        return next(
            (
                authorization
                for authorization in self._use_authorizations.values()
                if authorization.use_proposal_id == proposal_id
            ),
            None,
        )

    def record_context_access(
        self,
        authorization_id: UUID,
        *,
        run_id: UUID,
        skill_id: UUID,
        skill_version: int,
        accessed_by: str,
    ) -> SkillContextAccess:
        with self._lock:
            authorization = self._use_authorizations.get(authorization_id)
            if authorization is None:
                raise SkillNotFoundError(
                    f"Skill use authorization not found: {authorization_id}"
                )
            if not authorization.approved:
                raise SkillApprovalRequiredError(
                    "Rejected Skill use cannot reveal procedural context"
                )
            expected = (
                authorization.run_id,
                authorization.skill_id,
                authorization.skill_version,
                authorization.authorized_user_id,
            )
            supplied = (run_id, skill_id, skill_version, accessed_by)
            if supplied != expected:
                raise SkillApprovalRequiredError(
                    "Skill context access does not match its exact authorization"
                )
            existing = self._context_accesses.get(authorization_id)
            if existing is not None:
                return existing
            access = SkillContextAccess(
                authorization_id=authorization_id,
                run_id=run_id,
                skill_id=skill_id,
                skill_version=skill_version,
                accessed_by=accessed_by,
            )
            self._context_accesses[authorization_id] = access
            return access

    def get_context_access(self, authorization_id: UUID) -> SkillContextAccess:
        try:
            return self._context_accesses[authorization_id]
        except KeyError as exc:
            raise SkillNotFoundError(
                f"Skill context access not found: {authorization_id}"
            ) from exc

    def accesses_for_run(self, run_id: UUID) -> tuple[SkillContextAccess, ...]:
        return tuple(
            sorted(
                (
                    access
                    for access in self._context_accesses.values()
                    if access.run_id == run_id
                ),
                key=lambda access: (access.accessed_at, str(access.access_id)),
            )
        )

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
            if authorization_id not in self._context_accesses:
                raise SkillApprovalRequiredError(
                    "Skill usage requires an approved procedural-context access"
                )
            existing = self.get_usage_for_authorization(authorization_id)
            if existing is not None:
                if (
                    existing.outcome is outcome
                    and existing.resulting_proposal_id == resulting_proposal_id
                ):
                    return existing
                raise SkillVersionConflictError(
                    "Skill use authorization already has a different usage record"
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

    def get_usage_for_authorization(
        self, authorization_id: UUID
    ) -> SkillUsageRecord | None:
        return next(
            (
                record
                for record in self._usage.values()
                if record.authorization_id == authorization_id
            ),
            None,
        )

    def _snapshot(self) -> _SkillStoreSnapshot:
        return _SkillStoreSnapshot(
            bundles=tuple(self._bundles.values()),
            proposals=tuple(self._proposals.values()),
            proposal_decisions=tuple(self._proposal_decisions.values()),
            gold=tuple(
                value for _, value in sorted(self._gold.items(), key=lambda item: item[0])
            ),
            use_proposals=tuple(self._use_proposals.values()),
            use_authorizations=tuple(self._use_authorizations.values()),
            use_decisions=tuple(self._use_decisions.values()),
            context_accesses=tuple(self._context_accesses.values()),
            usage=tuple(self._usage.values()),
        )

    @classmethod
    def _from_snapshot(cls, snapshot: _SkillStoreSnapshot) -> "InMemorySkillStore":
        store = cls()
        store._bundles = _unique_index(
            snapshot.bundles, lambda item: item.bundle_id, "source bundle"
        )
        store._proposals = _unique_index(
            snapshot.proposals, lambda item: item.proposal_id, "proposal"
        )
        store._proposal_decisions = _unique_index(
            snapshot.proposal_decisions,
            lambda item: item.subject_id,
            "proposal decision",
        )
        store._gold = _unique_index(
            snapshot.gold,
            lambda item: (item.skill_id, item.version),
            "Gold version",
        )
        store._use_proposals = _unique_index(
            snapshot.use_proposals, lambda item: item.proposal_id, "use proposal"
        )
        store._use_authorizations = _unique_index(
            snapshot.use_authorizations,
            lambda item: item.authorization_id,
            "use authorization",
        )
        store._use_decisions = _unique_index(
            snapshot.use_decisions,
            lambda item: item.subject_id,
            "use decision",
        )
        store._context_accesses = _unique_index(
            snapshot.context_accesses,
            lambda item: item.authorization_id,
            "context access",
        )
        store._usage = _unique_index(
            snapshot.usage, lambda item: item.usage_id, "usage record"
        )
        if len({item.authorization_id for item in snapshot.usage}) != len(
            snapshot.usage
        ):
            raise SkillVersionConflictError(
                "Skill store snapshot contains duplicate usage authorization"
            )
        return store

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
        if context.lab_id is not None and skill.lab_id != context.lab_id:
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


def _unique_index(values, key, label: str):
    indexed = {}
    for value in values:
        identifier = key(value)
        if identifier in indexed:
            raise SkillVersionConflictError(
                f"Skill store snapshot contains duplicate {label}"
            )
        indexed[identifier] = value
    return indexed


class SQLiteSkillStore:
    """Small transactional Pydantic-JSON store for one local LabBio process."""

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
            CREATE TABLE IF NOT EXISTS skill_store_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                payload TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            "INSERT OR IGNORE INTO skill_store_state(singleton, payload) VALUES (1, ?)",
            (_SkillStoreSnapshot().model_dump_json(),),
        )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def save_source_bundle(self, bundle: SkillSourceBundle) -> None:
        self._write("save_source_bundle", bundle)

    def get_source_bundle(self, bundle_id: UUID) -> SkillSourceBundle:
        return self._read("get_source_bundle", bundle_id)

    def save_proposal(self, proposal: SkillProposal) -> None:
        self._write("save_proposal", proposal)

    def get_proposal(self, proposal_id: UUID) -> SkillProposal:
        return self._read("get_proposal", proposal_id)

    def decide_proposal(
        self, proposal_id: UUID, decision: SkillUserDecision
    ) -> GoldSkill | None:
        return self._write("decide_proposal", proposal_id, decision)

    def get_gold(self, skill_id: UUID, version: int) -> GoldSkill:
        return self._read("get_gold", skill_id, version)

    def lineage(self, skill_id: UUID) -> tuple[GoldSkill, ...]:
        return self._read("lineage", skill_id)

    def search(self, context: SkillSearchContext) -> tuple[GoldSkill, ...]:
        return self._read("search", context)

    def save_use_proposal(self, proposal: SkillUseProposal) -> None:
        self._write("save_use_proposal", proposal)

    def get_use_proposal(self, proposal_id: UUID) -> SkillUseProposal:
        return self._read("get_use_proposal", proposal_id)

    def decide_use(
        self, proposal_id: UUID, decision: SkillUserDecision
    ) -> SkillUseAuthorization:
        return self._write("decide_use", proposal_id, decision)

    def get_authorization(self, authorization_id: UUID) -> SkillUseAuthorization:
        return self._read("get_authorization", authorization_id)

    def get_authorization_for_proposal(
        self, proposal_id: UUID
    ) -> SkillUseAuthorization | None:
        return self._read("get_authorization_for_proposal", proposal_id)

    def record_context_access(
        self,
        authorization_id: UUID,
        *,
        run_id: UUID,
        skill_id: UUID,
        skill_version: int,
        accessed_by: str,
    ) -> SkillContextAccess:
        return self._write(
            "record_context_access",
            authorization_id,
            run_id=run_id,
            skill_id=skill_id,
            skill_version=skill_version,
            accessed_by=accessed_by,
        )

    def get_context_access(self, authorization_id: UUID) -> SkillContextAccess:
        return self._read("get_context_access", authorization_id)

    def accesses_for_run(self, run_id: UUID) -> tuple[SkillContextAccess, ...]:
        return self._read("accesses_for_run", run_id)

    def record_usage(
        self,
        authorization_id: UUID,
        outcome: SkillUsageOutcome,
        *,
        resulting_proposal_id: UUID | None = None,
    ) -> SkillUsageRecord:
        return self._write(
            "record_usage",
            authorization_id,
            outcome,
            resulting_proposal_id=resulting_proposal_id,
        )

    def get_usage(self, usage_id: UUID) -> SkillUsageRecord:
        return self._read("get_usage", usage_id)

    def get_usage_for_authorization(
        self, authorization_id: UUID
    ) -> SkillUsageRecord | None:
        return self._read("get_usage_for_authorization", authorization_id)

    def _read(self, operation: str, *args, **kwargs):
        with self._lock:
            store = self._load()
            return getattr(store, operation)(*args, **kwargs)

    def _write(self, operation: str, *args, **kwargs):
        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                store = self._load()
                result = getattr(store, operation)(*args, **kwargs)
                payload = store._snapshot().model_dump_json()
                self._connection.execute(
                    "UPDATE skill_store_state SET payload = ? WHERE singleton = 1",
                    (payload,),
                )
                self._connection.execute("COMMIT")
                return result
            except Exception:
                if self._connection.in_transaction:
                    self._connection.execute("ROLLBACK")
                raise

    def _load(self) -> InMemorySkillStore:
        row = self._connection.execute(
            "SELECT payload FROM skill_store_state WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise SkillStoreError("SQLite Skill store state is missing")
        try:
            snapshot = _SkillStoreSnapshot.model_validate_json(row[0])
        except (ValidationError, ValueError, TypeError) as exc:
            raise SkillStoreError("SQLite Skill store state is invalid") from exc
        return InMemorySkillStore._from_snapshot(snapshot)
