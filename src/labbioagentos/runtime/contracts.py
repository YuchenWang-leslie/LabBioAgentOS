"""Strict, model-safe contracts for the LabBio runtime stage boundary."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal, TypeAlias
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    StringConstraints,
    model_validator,
)

from labbioagentos.contracts import (
    GateDecisionRecord,
    NextActionProposal,
    WorkflowStage,
)


ShortText = Annotated[
    StrictStr,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=1000),
]
LongText = Annotated[
    StrictStr,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=8000),
]
SafeIdentifier = Annotated[
    StrictStr,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    ),
]


class RuntimeReferenceKind(StrEnum):
    """Structural reference categories with no relevance or ranking meaning."""

    ARTIFACT = "ARTIFACT"
    RESULT = "RESULT"
    MEMORY = "MEMORY"
    GOLD_SKILL = "GOLD_SKILL"
    DOMAIN_PROPOSAL = "DOMAIN_PROPOSAL"
    INSTRUCTION = "INSTRUCTION"
    EXECUTION = "EXECUTION"
    REPORT = "REPORT"
    OTHER = "OTHER"


class RuntimeReference(BaseModel):
    """Opaque model-facing reference; it is never a host locator."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reference_id: SafeIdentifier
    kind: RuntimeReferenceKind
    label: ShortText | None = None


class RuntimeWorkspaceIdentifiers(BaseModel):
    """Read-only presentation of workspace IDs, containing no authority/path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    user_id: SafeIdentifier
    project_id: SafeIdentifier
    lab_id: SafeIdentifier


class RuntimeGateDecisionView(BaseModel):
    """Bounded decision correlation presented after a source-stage resume."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    gate_id: SafeIdentifier
    source_stage: WorkflowStage
    approved: bool
    domain_reference_id: SafeIdentifier | None = None
    decision_reference_id: SafeIdentifier | None = None

    @classmethod
    def from_record(cls, record: GateDecisionRecord) -> "RuntimeGateDecisionView":
        return cls(
            gate_id=record.gate_id,
            source_stage=record.source_stage,
            approved=record.approved,
            domain_reference_id=record.domain_reference_id,
            decision_reference_id=record.decision_reference_id,
        )


class RuntimeInputBody(BaseModel):
    """Generic typed presentation body; arbitrary dictionaries are excluded."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    context_references: tuple[RuntimeReference, ...] = Field(
        default=(),
        max_length=64,
    )
    notes: tuple[ShortText, ...] = Field(default=(), max_length=32)


class RuntimeStageInput(BaseModel):
    """Bounded value passed to a stage runtime instead of mutable domain state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    stage_id: WorkflowStage
    invocation_id: UUID = Field(default_factory=uuid4)
    instruction: LongText
    goal_reference: RuntimeReference | None = None
    workspace: RuntimeWorkspaceIdentifiers
    prior_result_references: tuple[RuntimeReference, ...] = Field(
        default=(),
        max_length=64,
    )
    artifact_references: tuple[RuntimeReference, ...] = Field(
        default=(),
        max_length=128,
    )
    memory_candidate_references: tuple[RuntimeReference, ...] = Field(
        default=(),
        max_length=64,
    )
    gold_candidate_references: tuple[RuntimeReference, ...] = Field(
        default=(),
        max_length=64,
    )
    allowed_capabilities: tuple[SafeIdentifier, ...] = Field(
        default=(),
        max_length=64,
    )
    gate_decisions: tuple[RuntimeGateDecisionView, ...] = Field(
        default=(),
        max_length=32,
    )
    body: RuntimeInputBody = Field(default_factory=RuntimeInputBody)

    @model_validator(mode="after")
    def reject_non_runtime_stage(self) -> "RuntimeStageInput":
        if self.stage_id in {
            WorkflowStage.USER_GATE,
            WorkflowStage.SEARCH,
            WorkflowStage.DEBUG,
        }:
            raise ValueError(
                f"{self.stage_id.value} is not a configured main runtime stage"
            )
        return self


class _StageBody(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class IntakeStageBody(_StageBody):
    kind: Literal["INTAKE"] = "INTAKE"
    interpreted_goal: LongText
    constraints: tuple[ShortText, ...] = Field(default=(), max_length=64)
    unresolved_questions: tuple[ShortText, ...] = Field(default=(), max_length=32)
    input_references: tuple[RuntimeReference, ...] = Field(default=(), max_length=64)


class UnderstandStageBody(_StageBody):
    kind: Literal["UNDERSTAND"] = "UNDERSTAND"
    requirements: tuple[LongText, ...] = Field(min_length=1, max_length=64)
    assumptions: tuple[ShortText, ...] = Field(default=(), max_length=64)
    uncertainties: tuple[ShortText, ...] = Field(default=(), max_length=64)
    evidence_references: tuple[RuntimeReference, ...] = Field(default=(), max_length=64)


class PlanStageBody(_StageBody):
    kind: Literal["PLAN"] = "PLAN"
    procedure_steps: tuple[LongText, ...] = Field(min_length=1, max_length=128)
    required_input_references: tuple[RuntimeReference, ...] = Field(
        default=(),
        max_length=64,
    )
    requested_capabilities: tuple[SafeIdentifier, ...] = Field(
        default=(),
        max_length=32,
    )
    validation_expectations: tuple[LongText, ...] = Field(
        default=(),
        max_length=64,
    )


class PreflightStageBody(_StageBody):
    kind: Literal["PREFLIGHT"] = "PREFLIGHT"
    structurally_valid: bool
    issues: tuple[LongText, ...] = Field(default=(), max_length=64)
    approved_references: tuple[RuntimeReference, ...] = Field(default=(), max_length=64)
    required_action_references: tuple[RuntimeReference, ...] = Field(
        default=(),
        max_length=32,
    )


class ExecuteStageBody(_StageBody):
    kind: Literal["EXECUTE"] = "EXECUTE"
    execution_status: ShortText
    execution_reference: RuntimeReference | None = None
    output_artifact_references: tuple[RuntimeReference, ...] = Field(
        default=(),
        max_length=128,
    )
    issue_references: tuple[RuntimeReference, ...] = Field(default=(), max_length=64)


class ValidateStageBody(_StageBody):
    kind: Literal["VALIDATE"] = "VALIDATE"
    technical_status: ShortText
    runtime_assessment: LongText
    evidence_references: tuple[RuntimeReference, ...] = Field(default=(), max_length=64)
    limitations: tuple[LongText, ...] = Field(default=(), max_length=64)


class InterpretStageBody(_StageBody):
    kind: Literal["INTERPRET"] = "INTERPRET"
    findings: tuple[LongText, ...] = Field(min_length=1, max_length=128)
    evidence_references: tuple[RuntimeReference, ...] = Field(default=(), max_length=64)
    limitations: tuple[LongText, ...] = Field(default=(), max_length=64)
    hypotheses: tuple[LongText, ...] = Field(default=(), max_length=64)


class ReportStageBody(_StageBody):
    kind: Literal["REPORT"] = "REPORT"
    report_summary: LongText
    report_reference: RuntimeReference | None = None
    cited_references: tuple[RuntimeReference, ...] = Field(default=(), max_length=128)


class LearnStageBody(_StageBody):
    kind: Literal["LEARN"] = "LEARN"
    learning_summary: LongText
    source_bundle_reference: RuntimeReference | None = None
    proposal_references: tuple[RuntimeReference, ...] = Field(default=(), max_length=64)


RuntimeStageBody: TypeAlias = Annotated[
    IntakeStageBody
    | UnderstandStageBody
    | PlanStageBody
    | PreflightStageBody
    | ExecuteStageBody
    | ValidateStageBody
    | InterpretStageBody
    | ReportStageBody
    | LearnStageBody,
    Field(discriminator="kind"),
]


_BODY_STAGE = {
    "INTAKE": WorkflowStage.INTAKE,
    "UNDERSTAND": WorkflowStage.UNDERSTAND,
    "PLAN": WorkflowStage.PLAN,
    "PREFLIGHT": WorkflowStage.PREFLIGHT,
    "EXECUTE": WorkflowStage.EXECUTE,
    "VALIDATE": WorkflowStage.VALIDATE,
    "INTERPRET": WorkflowStage.INTERPRET,
    "REPORT": WorkflowStage.REPORT,
    "LEARN": WorkflowStage.LEARN,
}


class RuntimeStageResult(BaseModel):
    """Common result envelope with a discriminated body and explicit proposal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    result_id: UUID = Field(default_factory=uuid4)
    stage_id: WorkflowStage
    summary: LongText
    body: RuntimeStageBody
    references: tuple[RuntimeReference, ...] = Field(default=(), max_length=128)
    next_action: NextActionProposal

    @model_validator(mode="after")
    def body_must_match_stage(self) -> "RuntimeStageResult":
        body_stage = _BODY_STAGE[self.body.kind]
        if self.stage_id is not body_stage:
            raise ValueError(
                f"Result body {self.body.kind!r} does not match stage "
                f"{self.stage_id.value!r}"
            )
        return self
