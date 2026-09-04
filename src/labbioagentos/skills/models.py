"""Typed procedural-memory contracts with no executable Gold Skill behavior."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
import re
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    StringConstraints,
    field_validator,
    model_validator,
)

from labbioagentos.artifacts import ArtifactExposureClass, ArtifactRef, ArtifactView
from labbioagentos.contracts import RunStatus, WorkflowStage
from labbioagentos.trace import DelegationProjection, InvocationProjection, InstructionKind


BoundedText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=4000),
]
ShortText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=256),
]


def _text_values(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _text_values(item)
    elif isinstance(value, (tuple, list, set, frozenset)):
        for item in value:
            yield from _text_values(item)


_UNSAFE_REMOTE_TEXT_PATTERNS = (
    r"(?:^|\s)/(?:home|media|mnt|tmp|var|run)/",
    r"\bstorage[_ -]?locator\b",
    r"\breasoning[_ -]?content\b",
    r"\bprovider[_ -]?(?:request|response|raw)[_ -]?body\b",
    r"\b(?:api[_ -]?key|authorization[_ -]?secret)\b",
    r"\bbearer\s+[A-Za-z0-9._~-]{8,}",
    r"-----BEGIN [A-Z ]+ PRIVATE KEY-----",
)


def _reject_unsafe_remote_text(value, *, label: str) -> None:
    for text in _text_values(value):
        if any(
            re.search(pattern, text, flags=re.IGNORECASE)
            for pattern in _UNSAFE_REMOTE_TEXT_PATTERNS
        ):
            raise ValueError(f"{label} contains prohibited unsafe text")


class SkillScope(StrEnum):
    PERSONAL = "PERSONAL"
    PROJECT = "PROJECT"
    LAB = "LAB"


class SkillStatus(StrEnum):
    GOLD = "GOLD"


class SkillUseMode(StrEnum):
    REUSE = "REUSE"
    ADAPT = "ADAPT"
    REFERENCE = "REFERENCE"


class SkillCuratorAuditCategory(StrEnum):
    """Generic defects an Agent auditor can identify before user review."""

    UNSUPPORTED_BY_SOURCE = "UNSUPPORTED_BY_SOURCE"
    SOURCE_FACT_AS_FUTURE_DEFAULT = "SOURCE_FACT_AS_FUTURE_DEFAULT"
    PRESCRIPTIVE_FUTURE_CHOICE = "PRESCRIPTIVE_FUTURE_CHOICE"
    IDENTIFIER_KIND_MISUSE = "IDENTIFIER_KIND_MISUSE"
    RAW_CONTENT_ACCESS = "RAW_CONTENT_ACCESS"
    HIDDEN_FALLBACK = "HIDDEN_FALLBACK"
    OVERSTATED_FAILURE_CAUSE = "OVERSTATED_FAILURE_CAUSE"


class SkillUsageOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class SkillTraceRef(BaseModel):
    """Reference to one trace fact without copying its payload."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    event_id: UUID
    sequence: int = Field(ge=0)
    event_type: StrictStr = Field(min_length=1)
    stage_id: WorkflowStage | None = None
    invocation_id: UUID | None = None
    status: StrictStr | None = Field(default=None, min_length=1)


class SkillInstructionRef(BaseModel):
    """Explicitly marked, sanitized instruction evidence; never hidden reasoning."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    instruction_id: UUID
    trace_event_id: UUID
    stage_id: WorkflowStage
    invocation_id: UUID | None = None
    kind: InstructionKind
    template_id: StrictStr | None = Field(default=None, min_length=1)
    template_version: StrictStr | None = Field(default=None, min_length=1)
    template_hash: StrictStr | None = Field(default=None, min_length=1)
    sanitized_instruction: StrictStr = Field(min_length=1, max_length=16_000)


class SkillExecutionRef(BaseModel):
    """Safe execution lineage reconstructed from execution trace metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    execution_id: UUID
    planned_event_id: UUID | None = None
    terminal_event_id: UUID | None = None
    image_key: StrictStr | None = Field(default=None, min_length=1)
    resolved_image: StrictStr | None = Field(default=None, min_length=1)
    script_hash: StrictStr | None = Field(default=None, min_length=1)
    script_artifact_id: UUID | None = None
    input_artifact_ids: tuple[UUID, ...] = ()
    output_artifact_ids: tuple[UUID, ...] = ()
    status: StrictStr
    exit_code: int | None = None


class SkillInvocationSummary(BaseModel):
    """Bounded model-facing invocation fact without transport payloads."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    invocation_id: UUID
    parent_invocation_id: UUID | None = None
    agent_name: ShortText | None = None
    stage_id: WorkflowStage | None = None
    status: ShortText


class SkillDelegationSummary(BaseModel):
    """Bounded model-facing delegation fact without conversation content."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    invocation_id: UUID
    parent_invocation_id: UUID | None = None
    caller: ShortText
    target: ShortText
    stage_id: WorkflowStage | None = None
    status: ShortText


class SkillArtifactDescriptor(BaseModel):
    """Whitelist-only Artifact identity and shape; no locator or metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    artifact_id: UUID
    artifact_type: ShortText
    exposure_class: ArtifactExposureClass
    run_id: UUID | None = None
    stage_id: WorkflowStage | None = None
    producer_invocation_id: UUID | None = None
    shape: tuple[int, ...] | None = Field(default=None, max_length=16)
    column_count: int = Field(default=0, ge=0)
    dtype_field_count: int = Field(default=0, ge=0)


class SkillCapabilityUsageRef(BaseModel):
    """Safe actor-attributed capability fact with references only."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    capability_invocation_id: UUID
    actor_profile_key: ShortText
    actor_agent_name: ShortText
    capability_name: ShortText
    status: ShortText
    reference_ids: tuple[UUID, ...] = Field(default=(), max_length=128)


class SkillCurationSourceView(BaseModel):
    """The only Skill source representation permitted at a curator boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_bundle_id: UUID
    source_run_id: UUID
    task_reference: StrictStr | None = Field(default=None, min_length=1, max_length=2000)
    final_status: RunStatus
    workflow_stage_path: tuple[WorkflowStage, ...] = Field(max_length=64)
    invocations: tuple[SkillInvocationSummary, ...] = Field(default=(), max_length=128)
    delegations: tuple[SkillDelegationSummary, ...] = Field(default=(), max_length=128)
    instruction_refs: tuple[SkillInstructionRef, ...] = Field(default=(), max_length=128)
    execution_refs: tuple[SkillExecutionRef, ...] = Field(default=(), max_length=128)
    artifact_descriptors: tuple[SkillArtifactDescriptor, ...] = Field(
        default=(), max_length=256
    )
    artifact_evidence_views: tuple[ArtifactView, ...] = Field(
        default=(),
        max_length=32,
        description=(
            "Bounded policy-controlled views of terminal run outputs; source-run "
            "facts are evidence for abstraction, not fixed future-task choices."
        ),
    )
    failure_refs: tuple[SkillTraceRef, ...] = Field(default=(), max_length=128)
    retry_refs: tuple[SkillTraceRef, ...] = Field(default=(), max_length=128)
    validation_refs: tuple[SkillTraceRef, ...] = Field(default=(), max_length=128)
    capability_usage_refs: tuple[SkillCapabilityUsageRef, ...] = Field(
        default=(), max_length=256
    )

    @model_validator(mode="after")
    def reject_explicit_unsafe_text(self) -> "SkillCurationSourceView":
        _reject_unsafe_remote_text(
            self.model_dump(mode="python"), label="Skill curation source view"
        )
        if len(self.model_dump_json().encode("utf-8")) > 512_000:
            raise ValueError("Skill curation source view exceeds the size bound")
        return self


class SkillSourceBundle(BaseModel):
    """Deterministic evidence projection; it is not a curated or executable Skill."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    bundle_id: UUID = Field(default_factory=uuid4)
    source_run_id: UUID
    task_reference: StrictStr | None = Field(default=None, min_length=1, max_length=2000)
    final_status: RunStatus
    workflow_stage_path: tuple[WorkflowStage, ...]
    invocations: tuple[InvocationProjection, ...] = ()
    delegations: tuple[DelegationProjection, ...] = ()
    instruction_refs: tuple[SkillInstructionRef, ...] = ()
    execution_refs: tuple[SkillExecutionRef, ...] = ()
    artifact_ids: tuple[UUID, ...] = ()
    artifact_refs: tuple[ArtifactRef, ...] = ()
    failure_refs: tuple[SkillTraceRef, ...] = ()
    retry_refs: tuple[SkillTraceRef, ...] = ()
    validation_refs: tuple[SkillTraceRef, ...] = ()
    capability_usage_refs: tuple[SkillCapabilityUsageRef, ...] = ()
    trace_event_ids: tuple[UUID, ...]
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("created_at")
    @classmethod
    def require_utc_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("SkillSourceBundle created_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class SkillAdaptationPoint(BaseModel):
    """A future-task choice that Gold deliberately leaves unresolved."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    decision: BoundedText = Field(
        description="Decision the future task must make; never a fixed answer or value."
    )
    evidence_requirements: tuple[BoundedText, ...] = Field(
        min_length=1,
        max_length=16,
        description="Current-task evidence needed before making the decision.",
    )
    selection_considerations: tuple[BoundedText, ...] = Field(
        min_length=1,
        max_length=16,
        description="Criteria for the future Agent, without a prescribed choice.",
    )
    revalidation_requirements: tuple[BoundedText, ...] = Field(
        min_length=1,
        max_length=16,
        description="Checks required after the future Agent makes its choice.",
    )
    modifiable: Literal[True] = True


class SkillProcedureDraft(BaseModel):
    """Untrusted curator-authored procedure content without evidence authority."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    applicability: BoundedText
    workflow_outline: tuple[BoundedText, ...] = Field(min_length=1, max_length=100)
    agent_collaboration_guidance: tuple[BoundedText, ...] = Field(
        default_factory=tuple,
        max_length=100,
    )
    execution_guidance: tuple[BoundedText, ...] = Field(
        default_factory=tuple,
        max_length=100,
    )
    parameter_guidance: tuple[BoundedText, ...] = Field(
        default_factory=tuple,
        max_length=100,
        description=(
            "Future-task decision considerations; never fixed scientific methods, "
            "parameter values, code, or tool order."
        ),
    )
    input_contract_ids: tuple[ShortText, ...] = Field(
        default_factory=tuple,
        max_length=100,
        description=(
            "Exact reusable input contract identifiers explicitly present in the "
            "safe source view; Artifact UUIDs are not contract identifiers."
        ),
    )
    output_contract_ids: tuple[ShortText, ...] = Field(
        default_factory=tuple,
        max_length=100,
        description=(
            "Exact reusable output contract identifiers explicitly present in the "
            "safe source view; Artifact UUIDs are not contract identifiers."
        ),
    )
    validation_expectations: tuple[BoundedText, ...] = Field(
        default_factory=tuple,
        max_length=100,
    )
    known_failure_modes: tuple[BoundedText, ...] = Field(
        default_factory=tuple,
        max_length=100,
    )
    debug_lessons: tuple[BoundedText, ...] = Field(
        default_factory=tuple,
        max_length=100,
    )
    known_limitations: tuple[BoundedText, ...] = Field(
        default_factory=tuple,
        max_length=100,
    )
    tags: frozenset[ShortText] = Field(default_factory=frozenset, max_length=100)
    artifact_types: frozenset[ShortText] = Field(
        default_factory=frozenset,
        max_length=100,
    )
    reusable_principles: tuple[BoundedText, ...] = Field(
        default_factory=tuple,
        max_length=32,
    )
    adaptation_points: tuple[SkillAdaptationPoint, ...] = Field(
        default_factory=tuple,
        max_length=32,
    )


class SkillProcedure(SkillProcedureDraft):
    """Approved procedure plus host-assembled source evidence references."""

    important_instruction_ids: tuple[UUID, ...] = Field(default=(), max_length=128)
    script_artifact_ids: tuple[UUID, ...] = Field(default=(), max_length=128)
    source_trace_event_ids: tuple[UUID, ...] = Field(default=(), max_length=512)


class SkillCuratorDraft(BaseModel):
    """Strict remote-curator output; it cannot set trusted proposal fields."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    proposed_name: ShortText
    description: BoundedText
    procedure: SkillProcedureDraft

    @model_validator(mode="after")
    def reject_explicit_unsafe_text(self) -> "SkillCuratorDraft":
        _reject_unsafe_remote_text(
            self.model_dump(mode="python"), label="Skill curator draft"
        )
        return self


class SkillAdaptiveProcedureDraft(BaseModel):
    """Agent-authored procedure that separates stable guidance from choices."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    applicability: BoundedText
    workflow_guidance: tuple[BoundedText, ...] = Field(min_length=1, max_length=32)
    reusable_principles: tuple[BoundedText, ...] = Field(min_length=1, max_length=32)
    adaptation_points: tuple[SkillAdaptationPoint, ...] = Field(
        min_length=1,
        max_length=32,
    )
    validation_expectations: tuple[BoundedText, ...] = Field(
        default=(), max_length=32
    )
    known_failure_modes: tuple[BoundedText, ...] = Field(default=(), max_length=32)
    known_limitations: tuple[BoundedText, ...] = Field(default=(), max_length=32)
    tags: frozenset[ShortText] = Field(default_factory=frozenset, max_length=32)
    artifact_types: frozenset[ShortText] = Field(
        default_factory=frozenset,
        max_length=32,
    )


class SkillAdaptiveCuratorDraft(BaseModel):
    """Strict Agent output for an adaptable, non-executable Gold proposal."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    proposed_name: ShortText
    description: BoundedText
    procedure: SkillAdaptiveProcedureDraft

    @model_validator(mode="after")
    def reject_explicit_unsafe_text(self) -> "SkillAdaptiveCuratorDraft":
        _reject_unsafe_remote_text(
            self.model_dump(mode="python"), label="Adaptive Skill curator draft"
        )
        return self

    def to_curator_draft(self) -> SkillCuratorDraft:
        """Mechanically preserve Agent content in the durable procedure model."""

        procedure = self.procedure
        return SkillCuratorDraft(
            proposed_name=self.proposed_name,
            description=self.description,
            procedure=SkillProcedureDraft(
                applicability=procedure.applicability,
                workflow_outline=procedure.workflow_guidance,
                validation_expectations=procedure.validation_expectations,
                known_failure_modes=procedure.known_failure_modes,
                known_limitations=procedure.known_limitations,
                tags=procedure.tags,
                artifact_types=procedure.artifact_types,
                reusable_principles=procedure.reusable_principles,
                adaptation_points=procedure.adaptation_points,
            ),
        )


class SkillCuratorAuditFinding(BaseModel):
    """One Agent-authored curation defect tied to a draft field."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    category: SkillCuratorAuditCategory
    draft_field: ShortText
    statement: BoundedText
    rationale: BoundedText


class SkillCuratorAudit(BaseModel):
    """Strict untrusted Agent audit used to drive one bounded revision."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    findings: tuple[SkillCuratorAuditFinding, ...] = Field(max_length=64)
    summary: BoundedText

    @model_validator(mode="after")
    def reject_explicit_unsafe_text(self) -> "SkillCuratorAudit":
        _reject_unsafe_remote_text(
            self.model_dump(mode="python"), label="Skill curator audit"
        )
        return self


class SkillProposalContext(BaseModel):
    """Trusted host facts used to assemble one immutable Skill proposal."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    scope: SkillScope
    owner_user_id: StrictStr | None = Field(default=None, min_length=1)
    project_id: StrictStr | None = Field(default=None, min_length=1)
    lab_id: StrictStr = Field(default="local-lab", min_length=1)
    parent_skill_id: UUID | None = None
    parent_version: int | None = Field(default=None, ge=1)
    source_usage_record_id: UUID | None = None

    @model_validator(mode="after")
    def validate_scope_and_parent(self) -> "SkillProposalContext":
        if self.scope is SkillScope.PERSONAL and self.owner_user_id is None:
            raise ValueError("PERSONAL Skill proposals require owner_user_id")
        if self.scope is SkillScope.PROJECT and self.project_id is None:
            raise ValueError("PROJECT Skill proposals require project_id")
        if (self.parent_skill_id is None) != (self.parent_version is None):
            raise ValueError("parent_skill_id and parent_version must be set together")
        if self.source_usage_record_id is not None and self.parent_skill_id is None:
            raise ValueError("A source usage record requires parent Skill lineage")
        return self


class SkillProposal(BaseModel):
    """Runtime-curator proposal that remains non-Gold until user approval."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    proposal_id: UUID = Field(default_factory=uuid4)
    approval_gate_id: StrictStr = Field(
        default_factory=lambda: f"skill-proposal:{uuid4()}",
        min_length=1,
    )
    source_bundle_id: UUID
    source_run_id: UUID
    proposed_name: ShortText
    description: BoundedText
    scope: SkillScope
    owner_user_id: StrictStr | None = Field(default=None, min_length=1)
    project_id: StrictStr | None = Field(default=None, min_length=1)
    lab_id: StrictStr = Field(default="local-lab", min_length=1)
    procedure: SkillProcedure
    parent_skill_id: UUID | None = None
    parent_version: int | None = Field(default=None, ge=1)
    source_usage_record_id: UUID | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_scope_and_parent(self) -> "SkillProposal":
        if self.scope is SkillScope.PERSONAL and self.owner_user_id is None:
            raise ValueError("PERSONAL Skill proposals require owner_user_id")
        if self.scope is SkillScope.PROJECT and self.project_id is None:
            raise ValueError("PROJECT Skill proposals require project_id")
        if (self.parent_skill_id is None) != (self.parent_version is None):
            raise ValueError("parent_skill_id and parent_version must be set together")
        return self

    @field_validator("created_at")
    @classmethod
    def require_utc_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("SkillProposal created_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class SkillUserDecision(BaseModel):
    """Explicit LabBio-owned approval/rejection matching one pending gate."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    decision_id: UUID = Field(default_factory=uuid4)
    subject_id: UUID
    gate_id: StrictStr = Field(min_length=1)
    approved: bool
    decided_by: StrictStr = Field(min_length=1)
    decided_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("decided_at")
    @classmethod
    def require_utc_decided_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("SkillUserDecision decided_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class GoldSkill(BaseModel):
    """Immutable approved procedural memory; deliberately has no run/apply API."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    skill_id: UUID
    version: int = Field(ge=1)
    status: SkillStatus = SkillStatus.GOLD
    name: StrictStr = Field(min_length=1, max_length=256)
    description: StrictStr = Field(min_length=1, max_length=4000)
    scope: SkillScope
    source_run_id: UUID
    source_bundle_id: UUID
    source_proposal_id: UUID
    parent_skill_id: UUID | None = None
    parent_version: int | None = Field(default=None, ge=1)
    source_usage_record_id: UUID | None = None
    owner_user_id: StrictStr | None = Field(default=None, min_length=1)
    project_id: StrictStr | None = Field(default=None, min_length=1)
    lab_id: StrictStr = Field(default="local-lab", min_length=1)
    procedure: SkillProcedure
    approved_by: StrictStr = Field(min_length=1)
    approved_at: datetime

    @model_validator(mode="after")
    def validate_scope_and_lineage(self) -> "GoldSkill":
        if self.scope is SkillScope.PERSONAL and self.owner_user_id is None:
            raise ValueError("PERSONAL Gold Skills require owner_user_id")
        if self.scope is SkillScope.PROJECT and self.project_id is None:
            raise ValueError("PROJECT Gold Skills require project_id")
        if (self.parent_skill_id is None) != (self.parent_version is None):
            raise ValueError("parent_skill_id and parent_version must be set together")
        if self.version == 1 and self.parent_skill_id is not None:
            raise ValueError("Gold Skill v1 cannot have parent version lineage")
        if self.version > 1:
            if self.parent_skill_id != self.skill_id:
                raise ValueError("A later version must preserve its parent skill_id")
            if self.parent_version != self.version - 1:
                raise ValueError("A later version must reference the preceding version")
        return self

    @field_validator("approved_at")
    @classmethod
    def require_utc_approved_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("GoldSkill approved_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class SkillSearchContext(BaseModel):
    """Deterministic eligibility filters, never a scientific similarity query."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    user_id: StrictStr | None = Field(default=None, min_length=1)
    project_id: StrictStr | None = Field(default=None, min_length=1)
    lab_id: StrictStr | None = Field(default=None, min_length=1)
    include_lab: bool = True
    query_text: StrictStr | None = Field(default=None, min_length=1, max_length=500)
    required_tags: frozenset[ShortText] = Field(default_factory=frozenset)
    artifact_types: frozenset[ShortText] = Field(default_factory=frozenset)


class SkillUseProposal(BaseModel):
    """Runtime-selected use mode; the Skill service never generates this choice."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    proposal_id: UUID = Field(default_factory=uuid4)
    approval_gate_id: StrictStr = Field(
        default_factory=lambda: f"skill-use:{uuid4()}",
        min_length=1,
    )
    run_id: UUID
    requesting_user_id: StrictStr = Field(min_length=1)
    project_id: StrictStr = Field(min_length=1)
    lab_id: StrictStr = Field(min_length=1)
    skill_id: UUID
    skill_version: int = Field(ge=1)
    proposed_mode: SkillUseMode
    reason: BoundedText
    proposed_deviations: tuple[BoundedText, ...] = Field(
        default_factory=tuple,
        max_length=100,
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("created_at")
    @classmethod
    def require_utc_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("SkillUseProposal created_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class SkillUseAuthorization(BaseModel):
    """Recorded user decision; only approved records authorize contextual use."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    authorization_id: UUID = Field(default_factory=uuid4)
    use_proposal_id: UUID
    approval_gate_id: StrictStr = Field(min_length=1)
    decision_id: UUID
    run_id: UUID
    authorized_user_id: StrictStr = Field(min_length=1)
    project_id: StrictStr = Field(min_length=1)
    lab_id: StrictStr = Field(min_length=1)
    skill_id: UUID
    skill_version: int = Field(ge=1)
    approved: bool
    decided_by: StrictStr = Field(min_length=1)
    decided_at: datetime

    @field_validator("decided_at")
    @classmethod
    def require_utc_decided_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("SkillUseAuthorization decided_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class SkillContextAccess(BaseModel):
    """Proof that an approved full procedure reached its exact authorized run."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    access_id: UUID = Field(default_factory=uuid4)
    authorization_id: UUID
    run_id: UUID
    skill_id: UUID
    skill_version: int = Field(ge=1)
    accessed_by: StrictStr = Field(min_length=1)
    accessed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("accessed_at")
    @classmethod
    def require_utc_accessed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("SkillContextAccess accessed_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class SkillUsageRecord(BaseModel):
    """Evidence linking one run to the exact approved Skill version used."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    usage_id: UUID = Field(default_factory=uuid4)
    authorization_id: UUID
    run_id: UUID
    skill_id: UUID
    skill_version: int = Field(ge=1)
    proposed_mode: SkillUseMode
    user_approved: bool
    runtime_provided_deviations: tuple[BoundedText, ...] = Field(
        default_factory=tuple,
        max_length=100,
    )
    outcome: SkillUsageOutcome
    resulting_proposal_id: UUID | None = None
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("recorded_at")
    @classmethod
    def require_utc_recorded_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("SkillUsageRecord recorded_at must be timezone-aware")
        return value.astimezone(timezone.utc)
