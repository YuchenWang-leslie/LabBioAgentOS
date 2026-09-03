"""Strict, model-safe contracts for the LabBio runtime stage boundary."""

from __future__ import annotations

import json
from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Literal, TypeAlias
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictStr,
    StringConstraints,
    create_model,
    field_validator,
    model_validator,
)

from labbioagentos.contracts import (
    GateDecisionRecord,
    InformationAuthority,
    NextActionProposal,
    WorkflowStage,
)
from labbioagentos.execution.images import ApprovedImageRegistry, ExecutionPolicy
from labbioagentos.execution.models import (
    ExecutionRuntime,
    OutputDeclassificationMode,
    RequestedResources,
)
from labbioagentos.execution.registration import ArtifactRegistrationPolicy
from labbioagentos.model_safety import validate_model_visible_json


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

MAX_CAPABILITY_EVIDENCE_ITEMS = 64


class CapabilityEvidenceStatus(StrEnum):
    """Technical capability outcome only; it has no scientific meaning."""

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


ArtifactQueryAuditToken = Annotated[
    StrictStr,
    StringConstraints(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z][A-Za-z0-9_]*$",
    ),
]


class ArtifactQueryLimitType(StrEnum):
    """Non-sensitive runtime type of an artifact_query limit value."""

    INTEGER = "INTEGER"
    NULL = "NULL"
    STRING = "STRING"
    FLOAT = "FLOAT"
    BOOLEAN = "BOOLEAN"
    ARRAY = "ARRAY"
    OBJECT = "OBJECT"
    OTHER = "OTHER"


class ArtifactQueryRequestAudit(BaseModel):
    """Explicit safe projection of one artifact_query request."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    artifact_id: UUID | Literal["INVALID_IDENTIFIER"]
    view_type: ArtifactQueryAuditToken
    limit: int | Literal["INVALID_VALUE"] | None = None
    limit_type: ArtifactQueryLimitType
    normalization_applied: bool = False

    @model_validator(mode="after")
    def normalization_matches_safe_projection(self) -> "ArtifactQueryRequestAudit":
        if self.normalization_applied and (
            self.limit_type is not ArtifactQueryLimitType.STRING
            or isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
        ):
            raise ValueError(
                "Artifact query normalization requires a string wire type and integer limit"
            )
        return self


class SkillSearchRequestAudit(BaseModel):
    """Non-content request and completeness audit for catalog browsing."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    offset: int
    limit: int
    required_tag_count: int = Field(ge=0, le=100)
    artifact_type_count: int = Field(ge=0, le=100)
    include_lab: bool
    returned_count: int | None = Field(default=None, ge=0, le=50)
    available_count: int | None = Field(default=None, ge=0)
    next_offset: int | None = Field(default=None, ge=0)
    truncated: bool | None = None

    @model_validator(mode="after")
    def result_completeness_is_all_or_none(self) -> "SkillSearchRequestAudit":
        if self.returned_count is None:
            if any(
                value is not None
                for value in (
                    self.available_count,
                    self.next_offset,
                    self.truncated,
                )
            ):
                raise ValueError("Incomplete Skill search result audit")
        elif self.available_count is None or self.truncated is None:
            raise ValueError("Completed Skill search audit requires result counts")
        return self


class ExecutionAuditWireType(StrEnum):
    """Non-sensitive outer wire type observed for an execution request field."""

    OBJECT = "OBJECT"
    ARRAY = "ARRAY"
    STRING = "STRING"
    INTEGER = "INTEGER"
    NUMBER = "NUMBER"
    BOOLEAN = "BOOLEAN"
    NULL = "NULL"
    OTHER = "OTHER"


class ExecutionSubmitValidationStatus(StrEnum):
    """Canonical draft-validation outcome without rejected values or messages."""

    NOT_VALIDATED = "NOT_VALIDATED"
    VALID = "VALID"
    INVALID = "INVALID"


class ExecutionDraftField(StrEnum):
    """Known canonical top-level fields whose presence is safe to audit."""

    RUNTIME = "runtime"
    IMAGE_KEY = "image_key"
    SCRIPT_CONTENT = "script_content"
    INPUT_ARTIFACT_IDS = "input_artifact_ids"
    PARAMETERS = "parameters"
    REQUESTED_OUTPUTS = "requested_outputs"
    RESOURCES = "resources"
    NETWORK_REQUIRED = "network_required"


class ExecutionSubmitRequestAudit(BaseModel):
    """Bounded shape-only projection of one execution_submit request."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    wire_type: ExecutionAuditWireType
    known_top_level_field_presence: tuple[ExecutionDraftField, ...] = Field(
        default=(), max_length=8
    )
    input_artifact_count: int | None = Field(default=None, ge=0)
    requested_output_count: int | None = Field(default=None, ge=0)
    resources_present: bool = False
    network_required_type: ExecutionAuditWireType | None = None
    network_required_value: bool | None = None
    validation_status: ExecutionSubmitValidationStatus
    validation_error_field_paths: tuple[SafeIdentifier, ...] = Field(
        default=(), max_length=16
    )
    validation_error_types: tuple[ArtifactQueryAuditToken, ...] = Field(
        default=(), max_length=16
    )

    @model_validator(mode="after")
    def validate_structural_diagnostics(self) -> "ExecutionSubmitRequestAudit":
        if (
            self.network_required_value is not None
            and self.network_required_type is not ExecutionAuditWireType.BOOLEAN
        ):
            raise ValueError("Network value is valid only for a boolean wire type")
        has_errors = bool(
            self.validation_error_field_paths or self.validation_error_types
        )
        if len(self.validation_error_field_paths) != len(
            self.validation_error_types
        ):
            raise ValueError("Execution validation diagnostics must remain paired")
        if self.validation_status is ExecutionSubmitValidationStatus.INVALID:
            if not has_errors:
                raise ValueError("Invalid execution audit requires diagnostics")
        elif has_errors:
            raise ValueError("Only invalid execution audits may contain diagnostics")
        return self


class RuntimeApprovedOutputContractView(BaseModel):
    """Model-safe trusted shape and declassification policy for one output."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    contract_id: StrictStr = Field(min_length=1, max_length=128)
    schema_id: StrictStr = Field(min_length=1, max_length=128)
    document_type: Literal["JSON_RECORDS"] = "JSON_RECORDS"
    document_required_keys: tuple[Literal["schema_id", "records"], ...] = (
        "schema_id",
        "records",
    )
    allowed_fields: tuple[StrictStr, ...] = Field(min_length=1, max_length=128)
    required_fields: tuple[StrictStr, ...] = Field(default=(), max_length=128)
    max_records: int = Field(ge=1, le=10_000)
    max_file_bytes: int = Field(ge=1, le=16_777_216)
    declassification_mode: OutputDeclassificationMode


class RuntimeExecutionCapabilityView(BaseModel):
    """Trusted script-free execution envelope visible to runtime models."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    authority: Literal[InformationAuthority.CONTROL_STATE] = (
        InformationAuthority.CONTROL_STATE
    )
    runtime: ExecutionRuntime
    image_key: StrictStr = Field(min_length=1, max_length=128)
    resources: RequestedResources
    network_required: bool
    approved_output_contracts: tuple[RuntimeApprovedOutputContractView, ...] = Field(
        default=(), max_length=128
    )

    @classmethod
    def from_trusted_configuration(
        cls,
        *,
        runtime: ExecutionRuntime,
        image_key: str,
        resources: RequestedResources,
        network_required: bool,
        output_contract_ids: tuple[str, ...],
        image_registry: ApprovedImageRegistry,
        execution_policy: ExecutionPolicy,
        registration_policy: ArtifactRegistrationPolicy,
    ) -> "RuntimeExecutionCapabilityView":
        """Project only trusted model-relevant configuration, never host identity."""

        image = image_registry.resolve(image_key, runtime=runtime)
        execution_policy.validate_request(
            resources,
            network_required=network_required,
            image=image,
            has_local_inputs=False,
        )
        contracts = tuple(
            registration_policy.resolve_contract(contract_id)
            for contract_id in output_contract_ids
        )
        return cls(
            runtime=runtime,
            image_key=image_key,
            resources=resources,
            network_required=network_required,
            approved_output_contracts=tuple(
                RuntimeApprovedOutputContractView(
                    contract_id=contract.contract_id,
                    schema_id=contract.schema_id,
                    allowed_fields=tuple(sorted(contract.allowed_fields)),
                    required_fields=tuple(sorted(contract.required_fields)),
                    max_records=contract.max_records,
                    max_file_bytes=contract.max_file_bytes,
                    declassification_mode=contract.declassification_mode,
                )
                for contract in contracts
            ),
        )


class CapabilityEvidenceItem(BaseModel):
    """One bounded governed-tool outcome, never a provider conversation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_invocation_id: UUID = Field(default_factory=uuid4)
    actor_profile_key: SafeIdentifier
    actor_agent_name: SafeIdentifier
    capability_name: SafeIdentifier
    information_authority: InformationAuthority
    status: CapabilityEvidenceStatus
    trace_event_ids: tuple[UUID, ...] = Field(default=(), max_length=4)
    reference_ids: tuple[SafeIdentifier, ...] = Field(default=(), max_length=128)
    safe_result: JsonValue | None = None
    error_code: SafeIdentifier | None = None
    correlation_id: UUID | None = None
    artifact_query_request: ArtifactQueryRequestAudit | None = None
    skill_search_request: SkillSearchRequestAudit | None = None
    execution_submit_request: ExecutionSubmitRequestAudit | None = None

    @field_validator("safe_result")
    @classmethod
    def validate_safe_result(cls, value: JsonValue | None) -> JsonValue | None:
        if value is not None:
            validate_model_visible_json(value)
        return value

    @model_validator(mode="after")
    def status_matches_fields(self) -> "CapabilityEvidenceItem":
        if self.status is CapabilityEvidenceStatus.COMPLETED and self.error_code is not None:
            raise ValueError("Completed capability evidence cannot contain an error")
        if self.status is CapabilityEvidenceStatus.FAILED and self.error_code is None:
            raise ValueError("Failed capability evidence requires an error code")
        if (
            self.artifact_query_request is not None
            and self.capability_name != "artifact_query"
        ):
            raise ValueError(
                "Artifact query request audit is valid only for artifact_query"
            )
        if (
            self.skill_search_request is not None
            and self.capability_name != "skill_search"
        ):
            raise ValueError(
                "Skill search request audit is valid only for skill_search"
            )
        if (
            self.execution_submit_request is not None
            and self.capability_name != "execution_submit"
        ):
            raise ValueError(
                "Execution submit request audit is valid only for execution_submit"
            )
        return self


class CapabilityEvidenceBundle(BaseModel):
    """Bounded mixed-authority projection passed into finalization."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: UUID = Field(default_factory=uuid4)
    authority_mode: Literal["ITEM_LEVEL"] = "ITEM_LEVEL"
    run_id: UUID
    stage_id: WorkflowStage
    invocation_id: UUID
    items: tuple[CapabilityEvidenceItem, ...] = Field(
        default=(), max_length=MAX_CAPABILITY_EVIDENCE_ITEMS
    )
    delegation_trace_event_ids: tuple[UUID, ...] = Field(default=(), max_length=128)
    explicit_completion: LongText | None = None
    explicit_completion_authority: Literal[InformationAuthority.MODEL_CONTEXT] = (
        InformationAuthority.MODEL_CONTEXT
    )
    technical_status: Literal["COMPLETED"] = "COMPLETED"


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


class RuntimeEvidenceRole(StrEnum):
    """Execution-lineage role without scientific relevance or ranking."""

    INPUT_EVIDENCE = "INPUT_EVIDENCE"
    CURRENT_ATTEMPT_EVIDENCE = "CURRENT_ATTEMPT_EVIDENCE"
    HISTORICAL_EVIDENCE = "HISTORICAL_EVIDENCE"


class RuntimeReference(BaseModel):
    """Opaque model-facing reference; it is never a host locator."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reference_id: SafeIdentifier
    kind: RuntimeReferenceKind
    label: ShortText | None = None


class RuntimeEvidenceReference(RuntimeReference):
    """Authoritative reference with a mechanically assigned evidence role."""

    evidence_role: RuntimeEvidenceRole
    producer_invocation_id: UUID | None = None


class RuntimeWorkspaceIdentifiers(BaseModel):
    """Read-only presentation of workspace IDs, containing no authority/path."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    authority: Literal[InformationAuthority.CONTROL_STATE] = (
        InformationAuthority.CONTROL_STATE
    )
    user_id: SafeIdentifier
    project_id: SafeIdentifier
    lab_id: SafeIdentifier


class RuntimeGateDecisionView(BaseModel):
    """Bounded decision correlation presented after a source-stage resume."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    authority: Literal[InformationAuthority.CONTROL_STATE] = (
        InformationAuthority.CONTROL_STATE
    )
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

    user_assertion_references: tuple[RuntimeReference, ...] = Field(
        default=(),
        max_length=64,
    )
    control_state_notes: tuple[ShortText, ...] = Field(default=(), max_length=32)


class RuntimeEvidenceGroundingControl(BaseModel):
    """Fixed authority boundary shown to every runtime stage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    authority: Literal[InformationAuthority.CONTROL_STATE] = (
        InformationAuthority.CONTROL_STATE
    )
    prior_results: Literal[InformationAuthority.MODEL_CONTEXT] = (
        InformationAuthority.MODEL_CONTEXT
    )
    authoritative_evidence_references: Literal[
        InformationAuthority.AUTHORITATIVE_EVIDENCE
    ] = InformationAuthority.AUTHORITATIVE_EVIDENCE
    capability_evidence_authority: Literal["ITEM_LEVEL"] = "ITEM_LEVEL"
    capability_authority_rule: Literal[
        "Each capability evidence item carries trusted information_authority. "
        "Artifact, execution, and report results may be authoritative evidence; "
        "Skill and Memory context is MODEL_CONTEXT; proposal outputs are "
        "CONTROL_STATE. A mixed container does not promote its items."
    ] = (
        "Each capability evidence item carries trusted information_authority. "
        "Artifact, execution, and report results may be authoritative evidence; "
        "Skill and Memory context is MODEL_CONTEXT; proposal outputs are "
        "CONTROL_STATE. A mixed container does not promote its items."
    )
    factual_claim_rule: Literal[
        "Ground factual claims in capability items marked AUTHORITATIVE_EVIDENCE. Treat prior-stage "
        "model summaries and bodies as unverified context. Do not repeat a factual "
        "or numeric claim from prior context unless current authoritative evidence "
        "supports it."
    ] = (
        "Ground factual claims in capability items marked AUTHORITATIVE_EVIDENCE. Treat prior-stage "
        "model summaries and bodies as unverified context. Do not repeat a factual "
        "or numeric claim from prior context unless current authoritative evidence "
        "supports it."
    )
    reference_rule: Literal[
        "Authoritative references identify governed sources; query an allowed view "
        "when claim content is required."
    ] = (
        "Authoritative references identify governed sources; query an allowed view "
        "when claim content is required."
    )
    execution_lineage_rule: Literal[
        "Validate CURRENT_ATTEMPT_EVIDENCE for the active execution result. "
        "HISTORICAL_EVIDENCE remains governed and queryable but does not substitute "
        "for the current attempt."
    ] = (
        "Validate CURRENT_ATTEMPT_EVIDENCE for the active execution result. "
        "HISTORICAL_EVIDENCE remains governed and queryable but does not substitute "
        "for the current attempt."
    )


class RuntimePriorResultView(BaseModel):
    """Bounded mechanical projection of one already-validated stage result.

    The summary, body, and references were supplied by a prior runtime model.
    Schema validation makes them bounded and model-safe; it does not make their
    factual claims authoritative evidence.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    authority: Literal[InformationAuthority.MODEL_CONTEXT] = (
        InformationAuthority.MODEL_CONTEXT
    )
    result_id: UUID
    stage_id: WorkflowStage
    model_summary: LongText
    body_kind: SafeIdentifier
    model_body: JsonValue
    model_references: tuple[RuntimeReference, ...] = Field(
        default=(), max_length=128
    )

    @field_validator("model_body")
    @classmethod
    def validate_model_body(cls, value: JsonValue) -> JsonValue:
        if not isinstance(value, dict):
            raise ValueError("Prior result model_body must be an object")
        validate_model_visible_json(value)
        return value

    @classmethod
    def from_result(cls, result: "RuntimeStageResult") -> "RuntimePriorResultView":
        return cls(
            result_id=result.result_id,
            stage_id=result.stage_id,
            model_summary=result.summary,
            body_kind=result.body.kind,
            model_body=result.body.model_dump(mode="json"),
            model_references=result.references,
        )


class RuntimeStageInput(BaseModel):
    """Bounded value passed to a stage runtime instead of mutable domain state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: UUID
    stage_id: WorkflowStage
    invocation_id: UUID = Field(default_factory=uuid4)
    instruction_authority: Literal[InformationAuthority.USER_ASSERTION] = (
        InformationAuthority.USER_ASSERTION
    )
    instruction: LongText
    evidence_grounding: RuntimeEvidenceGroundingControl = Field(
        default_factory=RuntimeEvidenceGroundingControl
    )
    goal_reference: RuntimeReference | None = None
    workspace: RuntimeWorkspaceIdentifiers
    model_context_references: tuple[RuntimeReference, ...] = Field(
        default=(),
        max_length=64,
    )
    prior_results: tuple[RuntimePriorResultView, ...] = Field(
        default=(),
        max_length=9,
    )
    authoritative_evidence_references: tuple[RuntimeEvidenceReference, ...] = Field(
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
    execution_capability: RuntimeExecutionCapabilityView | None = None
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
        prior_json = json.dumps(
            [item.model_dump(mode="json") for item in self.prior_results],
            separators=(",", ":"),
        )
        if len(prior_json.encode("utf-8")) > 256_000:
            raise ValueError("Prior result context exceeds 256000 bytes")
        if self.execution_capability is not None and self.stage_id not in {
            WorkflowStage.PREFLIGHT,
            WorkflowStage.EXECUTE,
        }:
            raise ValueError(
                "Execution capability state is limited to PREFLIGHT and EXECUTE"
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


_STAGE_BODY_TYPES = {
    WorkflowStage.INTAKE: IntakeStageBody,
    WorkflowStage.UNDERSTAND: UnderstandStageBody,
    WorkflowStage.PLAN: PlanStageBody,
    WorkflowStage.PREFLIGHT: PreflightStageBody,
    WorkflowStage.EXECUTE: ExecuteStageBody,
    WorkflowStage.VALIDATE: ValidateStageBody,
    WorkflowStage.INTERPRET: InterpretStageBody,
    WorkflowStage.REPORT: ReportStageBody,
    WorkflowStage.LEARN: LearnStageBody,
}


@lru_cache(maxsize=9)
def runtime_stage_result_format(
    stage_id: WorkflowStage,
) -> type[RuntimeStageResult]:
    """Constrain provider generation to the assembly's exact trusted stage."""

    try:
        body_type = _STAGE_BODY_TYPES[stage_id]
    except KeyError as exc:
        raise ValueError(f"No runtime result format for stage {stage_id.value}") from exc
    return create_model(
        f"{stage_id.value.title()}RuntimeStageResult",
        __base__=RuntimeStageResult,
        stage_id=(Literal[stage_id], stage_id),
        body=(body_type, ...),
    )
