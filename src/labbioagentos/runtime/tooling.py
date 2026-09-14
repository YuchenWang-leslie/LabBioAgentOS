"""Pantheon ToolSet exposing only host-bound, governed LabBio capabilities."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Annotated, Any, Callable, Literal
from uuid import UUID, uuid4

from pantheon.toolset import ToolSet, tool
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictStr,
    ValidationError,
    model_validator,
)

from labbioagentos.artifacts import (
    ArtifactConsumer,
    ArtifactExposureDenied,
    ArtifactExposureService,
    ArtifactQuery,
    ArtifactSchema,
    ArtifactIdentifierError,
    ArtifactNotFoundError,
    ArtifactStore,
    ArtifactViewType,
)
from labbioagentos.artifacts.store import coerce_artifact_id
from labbioagentos.artifacts.models import ArtifactQueryLimit
from labbioagentos.contracts import InformationAuthority, WorkflowStage
from labbioagentos.execution import (
    ExecutionRuntime,
    ExecutionScriptValidationError,
    ExecutionPlanDraft,
    ExecutionSubmissionService,
    RequestedResources,
)
from labbioagentos.execution.errors import (
    ExecutionInputSelectionError,
    ExecutionOutputDeclarationError,
)
from labbioagentos.execution.models import (
    ExecutionImageKey,
    ExecutionInputArtifactIds,
    ExecutionRequestedOutputs,
    ExecutionScriptContent,
)
from labbioagentos.execution.submission import ExecutionInspectionError
from labbioagentos.execution.environments import EnvironmentService, EnvironmentRequestError
from labbioagentos.governance import AuthorizationDenied, Principal, WorkspaceContext
from labbioagentos.memory import (
    MemoryConflictError,
    MemoryDecisionError,
    MemoryEvidenceError,
    MemoryGovernanceService,
    MemoryKind,
    MemoryNotFoundError,
    MemoryProposalAction,
    MemoryScope,
    MemoryStaleUpdateError,
    MemoryStatus,
    MemoryStoreError,
    MemoryUpdateProposal,
)
from labbioagentos.skills import (
    GoldSkillService,
    SkillAdaptationPoint,
    SkillApprovalRequiredError,
    SkillDecisionError,
    SkillNotFoundError,
    SkillSearchContext,
    SkillStoreError,
    SkillUseMode,
    SkillUseProposal,
)
from labbioagentos.trace import RunTraceRecorder, TraceEventType

from .contracts import (
    ArtifactQueryConstraints,
    ArtifactQueryLimitType,
    ArtifactQueryRequestAudit,
    CapabilityErrorDetails,
    CapabilityEvidenceItem,
    CapabilityEvidenceStatus,
    ExecutionAuditWireType,
    ExecutionDraftField,
    ExecutionSubmitRequestAudit,
    ExecutionSubmitValidationStatus,
    SkillSearchRequestAudit,
    ScriptValidationDetails,
)
from .reporting import ReportSubmissionService, read_report_page


CAPABILITY_CEILINGS: dict[WorkflowStage, tuple[str, ...]] = {
    WorkflowStage.INTAKE: ("artifact_list", "artifact_query", "report_read"),
    WorkflowStage.UNDERSTAND: (
        "artifact_list", "artifact_query", "report_read", "skill_search", "skill_view",
        "memory_search", "memory_view",
    ),
    WorkflowStage.PLAN: (
        "artifact_query", "report_read", "skill_search", "skill_view", "skill_propose_use",
        "memory_search", "memory_view", "environment_list",
    ),
    WorkflowStage.PREFLIGHT: ("artifact_query", "report_read"),
    WorkflowStage.EXECUTE: (
        "artifact_query", "report_read", "execution_submit", "execution_inspect",
        "environment_list", "environment_build",
    ),
    WorkflowStage.VALIDATE: ("artifact_query", "report_read"),
    WorkflowStage.INTERPRET: ("artifact_query", "report_read"),
    WorkflowStage.REPORT: ("artifact_query", "report_read", "report_submit"),
    WorkflowStage.LEARN: (
        "skill_search", "skill_view", "memory_search", "memory_view",
        "memory_propose_update",
    ),
}
ALL_CAPABILITIES = frozenset(
    capability for values in CAPABILITY_CEILINGS.values() for capability in values
)
CAPABILITY_INFORMATION_AUTHORITY: dict[str, InformationAuthority] = {
    "artifact_list": InformationAuthority.AUTHORITATIVE_EVIDENCE,
    "artifact_query": InformationAuthority.AUTHORITATIVE_EVIDENCE,
    "execution_submit": InformationAuthority.AUTHORITATIVE_EVIDENCE,
    "execution_inspect": InformationAuthority.AUTHORITATIVE_EVIDENCE,
    "environment_list": InformationAuthority.AUTHORITATIVE_EVIDENCE,
    "environment_build": InformationAuthority.AUTHORITATIVE_EVIDENCE,
    "report_submit": InformationAuthority.AUTHORITATIVE_EVIDENCE,
    "report_read": InformationAuthority.MODEL_CONTEXT,
    "skill_search": InformationAuthority.MODEL_CONTEXT,
    "skill_view": InformationAuthority.MODEL_CONTEXT,
    "memory_search": InformationAuthority.MODEL_CONTEXT,
    "memory_view": InformationAuthority.MODEL_CONTEXT,
    "skill_propose_use": InformationAuthority.CONTROL_STATE,
    "memory_propose_update": InformationAuthority.CONTROL_STATE,
}
if frozenset(CAPABILITY_INFORMATION_AUTHORITY) != ALL_CAPABILITIES:
    raise RuntimeError("Capability information-authority mapping is incomplete")
_SAFE_ARTIFACT_QUERY_TOKEN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_CANONICAL_INTEGER_WIRE_TOKEN = re.compile(r"(?:0|-?[1-9][0-9]{0,127})")
_EXECUTION_VALIDATION_PATH_PARTS = frozenset(
    {
        *(field.value for field in ExecutionDraftField),
        "relative_path",
        "artifact_type",
        "requested_exposure",
        "output_contract_id",
        "cpus",
        "memory_mb",
        "pids_limit",
        "timeout_seconds",
    }
)


def _normalize_canonical_integer_wire_value(value: Any) -> tuple[Any, bool]:
    """Normalize one bounded canonical decimal string without semantic validation."""

    if (
        isinstance(value, str)
        and _CANONICAL_INTEGER_WIRE_TOKEN.fullmatch(value) is not None
    ):
        return int(value), True
    return value, False


class _ArtifactQueryFailure(ValueError):
    """An authorized query failed, with only bounded mechanical feedback."""

    def __init__(
        self,
        error_code: Literal[
            "INVALID_ENUM_VALUE", "INVALID_QUERY_SHAPE", "ARTIFACT_EXPOSURE_DENIED"
        ],
        query_constraints: ArtifactQueryConstraints,
    ):
        super().__init__(error_code)
        self.error_code = error_code
        self.query_constraints = query_constraints


class _InvalidExecutionDraft(ValueError):
    """execution_submit received a draft rejected by its canonical model."""


class _ExecutionSourceTransportError(RuntimeError):
    """A source page cannot be transported without alteration or truncation."""


class _ReportPageTransportError(RuntimeError):
    """An original report page cannot be transported exactly."""


class ToolError(CapabilityErrorDetails):
    error_code: StrictStr = Field(min_length=1, max_length=128)
    correlation_id: UUID = Field(default_factory=uuid4)


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    success: bool
    information_authority: InformationAuthority
    data: JsonValue | None = None
    error: ToolError | None = None


class ArtifactListItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    artifact_id: UUID
    artifact_type: StrictStr
    exposure_class: StrictStr
    release_basis: StrictStr
    schema_: "ArtifactSchemaView | None" = Field(default=None, serialization_alias="schema")
    available_views: tuple[StrictStr, ...] = ()
    owner_user_id: StrictStr
    project_id: StrictStr
    lab_id: StrictStr
    run_id: UUID | None = None
    stage_id: WorkflowStage | None = None
    producer_invocation_id: UUID | None = None


class ArtifactSchemaView(BaseModel):
    """Bounded structural projection; free-form schema properties stay internal."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    shape: tuple[int, ...] | None = None
    columns: tuple[StrictStr, ...] = Field(default=(), max_length=128)
    dtypes: dict[str, StrictStr] = Field(default_factory=dict)

    @classmethod
    def from_schema(cls, schema: ArtifactSchema | None) -> "ArtifactSchemaView | None":
        if schema is None:
            return None
        keys = sorted(schema.dtypes)[:128]
        return cls(
            shape=tuple(schema.shape[:16]) if schema.shape is not None else None,
            columns=tuple(schema.columns[:128]),
            dtypes={key: schema.dtypes[key][:128] for key in keys},
        )


class SkillCandidateView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    authority: Literal[InformationAuthority.MODEL_CONTEXT] = (
        InformationAuthority.MODEL_CONTEXT
    )
    skill_id: UUID
    version: int
    name: StrictStr
    description: StrictStr
    scope: StrictStr
    tags: tuple[StrictStr, ...] = ()
    artifact_types: tuple[StrictStr, ...] = ()
    input_contract_ids: tuple[StrictStr, ...] = ()
    output_contract_ids: tuple[StrictStr, ...] = ()
    applicability_preview: StrictStr
    limitation_preview: tuple[StrictStr, ...] = ()


class SkillCandidatePage(BaseModel):
    """Stable bounded page of visible active candidates without relevance ranking."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    authority: Literal[InformationAuthority.MODEL_CONTEXT] = (
        InformationAuthority.MODEL_CONTEXT
    )
    items: tuple[SkillCandidateView, ...] = Field(default=(), max_length=50)
    returned_count: int = Field(ge=0, le=50)
    available_count: int = Field(ge=0)
    offset: int = Field(ge=0)
    effective_limit: int = Field(ge=1, le=50)
    next_offset: int | None = Field(default=None, ge=0)
    truncated: bool

    @model_validator(mode="after")
    def validate_page_completeness(self) -> "SkillCandidatePage":
        if self.returned_count != len(self.items):
            raise ValueError("Skill candidate returned_count does not match items")
        if self.returned_count > self.available_count:
            raise ValueError("Skill candidate page exceeds available_count")
        has_next = self.offset + self.returned_count < self.available_count
        if self.truncated != has_next:
            raise ValueError("Skill candidate truncated flag is inconsistent")
        expected_next = self.offset + self.returned_count if has_next else None
        if self.next_offset != expected_next:
            raise ValueError("Skill candidate next_offset is inconsistent")
        return self


class SkillDetailView(SkillCandidateView):
    source_run_id: UUID
    parent_skill_id: UUID | None = None
    parent_version: int | None = None
    applicability: StrictStr
    workflow_outline: tuple[StrictStr, ...]
    collaboration_guidance: tuple[StrictStr, ...] = ()
    execution_guidance: tuple[StrictStr, ...] = ()
    parameter_guidance: tuple[StrictStr, ...] = ()
    known_failure_modes: tuple[StrictStr, ...] = ()
    debug_lessons: tuple[StrictStr, ...] = ()
    validation_expectations: tuple[StrictStr, ...] = ()
    limitations: tuple[StrictStr, ...] = ()
    reusable_principles: tuple[StrictStr, ...] = ()
    adaptation_points: tuple[SkillAdaptationPoint, ...] = ()
    truncated_fields: tuple[StrictStr, ...] = ()


class MemoryCandidateView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    authority: Literal[InformationAuthority.MODEL_CONTEXT] = (
        InformationAuthority.MODEL_CONTEXT
    )
    memory_id: UUID
    version: int
    scope: MemoryScope
    kind: MemoryKind
    status: Literal[MemoryStatus.ACTIVE] = MemoryStatus.ACTIVE
    preview: StrictStr


class MemoryCandidatePage(BaseModel):
    """Stable bounded page of visible latest ACTIVE Memory candidates."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    authority: Literal[InformationAuthority.MODEL_CONTEXT] = (
        InformationAuthority.MODEL_CONTEXT
    )
    items: tuple[MemoryCandidateView, ...] = Field(default=(), max_length=50)
    returned_count: int = Field(ge=0, le=50)
    available_count: int = Field(ge=0)
    offset: int = Field(ge=0)
    effective_limit: int = Field(ge=1, le=50)
    next_offset: int | None = Field(default=None, ge=0)
    truncated: bool

    @model_validator(mode="after")
    def validate_page_completeness(self) -> "MemoryCandidatePage":
        if self.returned_count != len(self.items):
            raise ValueError("Memory candidate returned_count does not match items")
        if self.returned_count > self.available_count:
            raise ValueError("Memory candidate page exceeds available_count")
        has_next = self.offset + self.returned_count < self.available_count
        if self.truncated != has_next:
            raise ValueError("Memory candidate truncated flag is inconsistent")
        expected_next = self.offset + self.returned_count if has_next else None
        if self.next_offset != expected_next:
            raise ValueError("Memory candidate next_offset is inconsistent")
        return self


class MemoryDetailView(MemoryCandidateView):
    content: StrictStr
    previous_version: int | None = None
    evidence_run_count: int = Field(ge=0)
    evidence_artifact_count: int = Field(ge=0)
    has_evidence: bool


@dataclass(frozen=True)
class RuntimeCapabilityContext:
    """Trusted host binding; this object never appears in a tool signature."""

    principal: Principal
    workspace: WorkspaceContext
    run_id: UUID
    stage_id: WorkflowStage
    invocation_id: UUID
    actor_profile_key: str
    actor_agent_name: str
    capability_allowlist: tuple[str, ...]
    consumer: ArtifactConsumer = ArtifactConsumer.REMOTE_LLM
    mountable_input_artifact_ids: tuple[UUID, ...] | None = None

    @classmethod
    def from_stage_spec(
        cls,
        spec,
        *,
        principal: Principal,
        workspace: WorkspaceContext,
        run_id: UUID,
        invocation_id: UUID,
        actor_profile_key: str,
        actor_agent_name: str,
    ) -> "RuntimeCapabilityContext":
        """Bind the exact StageRuntimeSpec allowlist outside model input."""

        return cls(
            principal=principal,
            workspace=workspace,
            run_id=run_id,
            stage_id=spec.stage_id,
            invocation_id=invocation_id,
            actor_profile_key=actor_profile_key,
            actor_agent_name=actor_agent_name,
            capability_allowlist=tuple(spec.capability_allowlist),
        )

    def __post_init__(self) -> None:
        ceiling = frozenset(CAPABILITY_CEILINGS.get(self.stage_id, ()))
        requested = frozenset(self.capability_allowlist)
        if not requested.issubset(ceiling):
            raise ValueError("Stage capability allowlist exceeds its approved ceiling")
        if not self.actor_profile_key or not self.actor_agent_name:
            raise ValueError("Runtime capability actor identity is required")
        if self.consumer is not ArtifactConsumer.REMOTE_LLM:
            raise ValueError("Runtime model tools must bind REMOTE_LLM")


@dataclass(frozen=True)
class RuntimeCapabilityServices:
    artifact_store: ArtifactStore
    artifact_exposure: ArtifactExposureService
    execution_submission: ExecutionSubmissionService | None = None
    skill_service: GoldSkillService | None = None
    memory_service: MemoryGovernanceService | None = None
    report_submission: ReportSubmissionService | None = None
    trace_recorder: RunTraceRecorder | None = None
    environment_service: EnvironmentService | None = None


class LabBioRuntimeToolSet(ToolSet):
    """One filtered ToolSet; allowed tools are available but never auto-invoked."""

    def __init__(self, binding: RuntimeCapabilityContext, services: RuntimeCapabilityServices):
        self.binding = binding
        self.services = services
        self._evidence_items: list[CapabilityEvidenceItem] = []
        super().__init__(
            name=(
                f"labbio-{binding.stage_id.value.lower()}-"
                f"{binding.actor_profile_key}"
            )
        )
        allowed = frozenset(binding.capability_allowlist)
        self.functions = {
            name: value
            for name, value in self.functions.items()
            if name in allowed or name == "list_tools"
        }

    def _guard(self, capability: str) -> None:
        if capability not in self.binding.capability_allowlist:
            raise AuthorizationDenied(
                f"Capability {capability!r} is not available in this stage"
            )

    async def _call(
        self,
        capability: str,
        operation: Callable[[], Any],
        *,
        request_ids: dict[str, JsonValue] | None = None,
        artifact_query_request: ArtifactQueryRequestAudit | None = None,
        skill_search_request: SkillSearchRequestAudit | None = None,
        execution_submit_request: ExecutionSubmitRequestAudit | None = None,
    ) -> dict[str, Any]:
        capability_invocation_id = uuid4()
        information_authority = CAPABILITY_INFORMATION_AUTHORITY[capability]
        trace_event_ids: list[UUID] = []
        bounded_ids = self._bounded_identifiers(request_ids or {})
        request_audit_payload: dict[str, JsonValue] = {}
        if artifact_query_request is not None:
            request_audit_payload["artifact_query_request"] = (
                artifact_query_request.model_dump(mode="json")
            )
        if skill_search_request is not None:
            request_audit_payload["skill_search_request"] = (
                skill_search_request.model_dump(mode="json")
            )
        if execution_submit_request is not None:
            request_audit_payload["execution_submit_request"] = (
                execution_submit_request.model_dump(mode="json")
            )
        try:
            self._guard(capability)
            event = self._emit(
                TraceEventType.CAPABILITY_INVOKED,
                capability,
                "STARTED",
                {
                    "capability_invocation_id": str(capability_invocation_id),
                    **bounded_ids,
                    **request_audit_payload,
                },
            )
            if event is not None:
                trace_event_ids.append(event.event_id)
            value = operation()
            if hasattr(value, "__await__"):
                value = await value
            if skill_search_request is not None and isinstance(
                value, SkillCandidatePage
            ):
                skill_search_request = skill_search_request.model_copy(
                    update={
                        "returned_count": value.returned_count,
                        "available_count": value.available_count,
                        "next_offset": value.next_offset,
                        "truncated": value.truncated,
                    }
                )
                request_audit_payload["skill_search_request"] = (
                    skill_search_request.model_dump(mode="json")
                )
            if isinstance(value, BaseModel):
                value = value.model_dump(mode="json", by_alias=True)
            elif isinstance(value, (tuple, list)) and all(isinstance(item, BaseModel) for item in value):
                value = [item.model_dump(mode="json", by_alias=True) for item in value]
            result = ToolResult(
                success=True,
                information_authority=information_authority,
                data=value,
            )
        except Exception as exc:
            error = self._safe_error(exc)
            error_details = CapabilityErrorDetails(
                safe_message=error.safe_message,
                retryable=error.retryable,
                denied_operation=error.denied_operation,
                query_constraints=error.query_constraints,
                script_validation=error.script_validation,
            )
            failure_details = None
            if capability == "execution_submit" and isinstance(
                exc, ExecutionOutputDeclarationError
            ):
                failure_details = {
                    "execution_output_declaration": {
                        "minimum_queryable_output_count": exc.minimum_queryable_output_count,
                        "declared_queryable_output_count": exc.declared_queryable_output_count,
                    },
                }
            event = self._emit(
                TraceEventType.CAPABILITY_FAILED,
                capability,
                "FAILED",
                {
                    "capability_invocation_id": str(capability_invocation_id),
                    "error_code": error.error_code,
                    "correlation_id": str(error.correlation_id),
                    "error_details": error_details.model_dump(mode="json"),
                    **(failure_details or {}),
                    **bounded_ids,
                    **request_audit_payload,
                },
            )
            if event is not None:
                trace_event_ids.append(event.event_id)
            self._evidence_items.append(
                CapabilityEvidenceItem(
                    capability_invocation_id=capability_invocation_id,
                    actor_profile_key=self.binding.actor_profile_key,
                    actor_agent_name=self.binding.actor_agent_name,
                    capability_name=capability,
                    information_authority=information_authority,
                    status=CapabilityEvidenceStatus.FAILED,
                    trace_event_ids=tuple(trace_event_ids),
                    error_code=error.error_code,
                    correlation_id=error.correlation_id,
                    error_details=error_details,
                    safe_result=failure_details,
                    artifact_query_request=artifact_query_request,
                    skill_search_request=skill_search_request,
                    execution_submit_request=execution_submit_request,
                )
            )
            return ToolResult(
                success=False,
                information_authority=information_authority,
                error=error,
            ).model_dump(mode="json")
        event = self._emit(
            TraceEventType.CAPABILITY_COMPLETED,
            capability,
            "COMPLETED",
            {
                "capability_invocation_id": str(capability_invocation_id),
                **bounded_ids,
                **request_audit_payload,
            },
        )
        if event is not None:
            trace_event_ids.append(event.event_id)
        self._evidence_items.append(
            CapabilityEvidenceItem(
                capability_invocation_id=capability_invocation_id,
                actor_profile_key=self.binding.actor_profile_key,
                actor_agent_name=self.binding.actor_agent_name,
                capability_name=capability,
                information_authority=information_authority,
                status=CapabilityEvidenceStatus.COMPLETED,
                trace_event_ids=tuple(trace_event_ids),
                reference_ids=self._reference_ids(value),
                safe_result=value,
                artifact_query_request=artifact_query_request,
                skill_search_request=skill_search_request,
                execution_submit_request=execution_submit_request,
            )
        )
        return result.model_dump(mode="json", by_alias=True)

    def evidence_items(self) -> tuple[CapabilityEvidenceItem, ...]:
        """Return immutable bounded outcomes accumulated by model-selected calls."""

        return tuple(self._evidence_items)

    def _environment_service(self) -> EnvironmentService:
        service = self._required(self.services.environment_service, "environment")
        if service.owner_user_id != self.binding.principal.user_id:
            raise AuthorizationDenied("Environment owner does not match the bound user")
        return service

    @tool
    async def environment_list(
        self, requirements: list[str] = [], offset: int = 0, limit: int = 10,
    ) -> dict:
        """Discover approved Python images and verified package-version facts.

        The execution capability describes the default image, not every cached
        environment. Returned image keys may be used by execution_submit.
        Requirements are optional PyPI distribution names with extras/version
        constraints, not import names. An unspecified inventory does not prove
        that a package is absent. No environment is selected or built by listing.
        """
        return await self._call("environment_list", lambda: (
            self._environment_service().list_environments(
                requirements=tuple(requirements), offset=offset, limit=limit,
            )
        ))

    @tool
    async def environment_build(
        self, base_image_key: ExecutionImageKey, requirements: list[str],
        import_modules: list[str] = [],
    ) -> dict:
        """Prepare and verify a reusable Python image, without running analysis.

        Choose an approved base image key and PyPI package requirements with
        optional extras/version constraints. Only wheels are supported; URLs,
        local paths, editable installs, environment markers and pip options are
        not accepted. import_modules are optional Python import names to verify.
        Builds have no analysis data mounted. A SUCCEEDED receipt contains an
        immutable verified image key usable by execution_submit; FAILED contains
        bounded dependency diagnostics for your decision. No automatic repair or
        analysis submission occurs. The unchanged default image remains usable.
        """
        return await self._call("environment_build", lambda: (
            self._environment_service().build_environment(
                base_image_key=base_image_key, requirements=tuple(requirements),
                import_modules=tuple(import_modules),
            )
        ))

    @tool
    async def artifact_list(self, offset: int = 0, limit: int = 20) -> dict:
        """List bounded metadata for artifacts in the bound workspace."""
        return await self._call("artifact_list", lambda: self._artifact_list(offset, limit))

    @tool
    async def artifact_query(
        self,
        artifact_id: str,
        view_type: Literal["METADATA", "SCHEMA", "SUMMARY", "TOP_N"],
        limit: ArtifactQueryLimit | None = None,
    ) -> dict:
        """Request one policy-controlled view of a governed Artifact.

        Args:
            artifact_id: UUID from a RuntimeReference whose kind is ARTIFACT;
                an EXECUTION reference UUID is not an Artifact identifier.
            view_type: One of METADATA, SCHEMA, SUMMARY, or TOP_N.
            limit: Optional positive integer for TOP_N only. For every other
                view, omit limit or use JSON null, not a string. For TOP_N,
                omitting limit or using null selects the policy default; policy
                may cap the number returned below the requested limit.
        """
        canonical_limit, normalization_applied = (
            _normalize_canonical_integer_wire_value(limit)
        )
        return await self._call(
            "artifact_query",
            lambda: self._artifact_query(artifact_id, view_type, canonical_limit),
            request_ids={"artifact_id": artifact_id},
            artifact_query_request=self._artifact_query_request_audit(
                artifact_id,
                view_type,
                limit,
                canonical_limit=canonical_limit,
                normalization_applied=normalization_applied,
            ),
        )

    @tool
    async def execution_inspect(
        self, execution_id: str,
        source_offset: Annotated[int, Field(strict=True, ge=0)] = 0,
        source_limit: Annotated[int, Field(strict=True, ge=1, le=32_000)] = 12_000,
    ) -> dict:
        """Inspect a prior execution receipt and its original Agent-submitted program.

        Accepts an EXECUTION UUID from this same run and live application session,
        including an earlier stage invocation. It never reads input data, process
        streams or arbitrary RAW Artifacts, and never executes or revises code.
        The receipt retains its original technical outcome; submitted_program is
        MODEL_CONTEXT, not proof of scientific results. Revisions keep distinct
        execution identities and hashes. Source is exact UTF-8 text paginated by
        character offset; source_end is the next offset and complete marks EOF,
        not a claim that earlier pages were read. Limit must be 1 through 32000.
        Pages that would be altered by transport are rejected, not rewritten.
        A page exceeding the serialized transport bound requires a smaller limit.
        """
        page = None

        def inspect_submission():
            nonlocal page
            service = self._required(self.services.execution_submission, "execution")
            page = service.inspect(
                UUID(execution_id), principal=self.binding.principal,
                workspace=self.binding.workspace, run_id=self.binding.run_id,
                source_offset=source_offset, source_limit=source_limit,
            )
            # Use the actual pure transport filters on a copy. Never invoke the
            # truncator, which can write full tool bodies to externalized files.
            from pantheon.settings import get_settings
            from pantheon.utils.llm import filter_base64_in_tool_result, filter_tool_messages
            from pantheon.utils.token_optimization import get_per_tool_limit

            serialized = json.dumps(page, ensure_ascii=False)
            transport_limit = get_per_tool_limit(
                "execution_inspect", get_settings().max_tool_content_length,
            )
            if len(serialized) > min(40_000, transport_limit - 2_000):
                raise _ExecutionSourceTransportError("EXECUTION_SOURCE_PAGE_TOO_LARGE")
            filtered = json.dumps(
                filter_base64_in_tool_result(json.loads(serialized)), ensure_ascii=False,
            )
            filtered = filter_tool_messages([{"role": "tool", "content": filtered}])[0]["content"]
            if filtered != serialized:
                raise _ExecutionSourceTransportError("EXECUTION_SOURCE_TRANSPORT_UNSUPPORTED")
            # The provider may revisit its own source; durable capability evidence
            # keeps only the exact receipt and page identity/completeness facts.
            return {"receipt": page["receipt"], "submitted_program": {
                key: value for key, value in page["submitted_program"].items()
                if key != "source"
            }}

        result = await self._call(
            "execution_inspect", inspect_submission,
            request_ids={"execution_id": execution_id},
        )
        # Do not mutate the mapping already retained by the evidence bundle.
        if result["success"]:
            return {**result, "data": page}
        return result

    @tool
    async def execution_submit(
        self,
        image_key: ExecutionImageKey,
        script_content: ExecutionScriptContent,
        runtime: ExecutionRuntime = ExecutionRuntime.PYTHON,
        input_artifact_ids: ExecutionInputArtifactIds = (),
        parameters: dict[str, JsonValue] = {},
        requested_outputs: ExecutionRequestedOutputs = (),
        resources: RequestedResources = {},
        network_required: bool = False,
    ) -> dict:
        """Submit one governed execution intent through canonical draft fields.

        Only Artifact UUIDs explicitly supplied in ``input_artifact_ids`` are
        mounted. The JSON object named by ``LABBIO_INPUT_MANIFEST_PATH`` maps
        each selected Artifact UUID string directly to its read-only container
        path. Mounted basenames are unique Artifact UUIDs, not original names.
        The sandbox-only JSON object at ``LABBIO_INPUT_IDENTITIES_PATH`` maps
        those same UUIDs to objects containing ``original_filename`` (null for
        legacy records without that provenance). Original names may repeat;
        they are local provenance, not unique identifiers or remote RAW views.
        The offline script writes declared relative outputs beneath the
        directory named by ``LABBIO_OUTPUT_DIR``.

        The receipt status is the process outcome. Failure diagnostics refer to
        that receipt's script_hash; script_error_locations use one-based lines
        and zero-based, end-exclusive columns. missing_key_type describes only
        the failed lookup argument, not the mapping's key types or any values.
        output_issues identify failed requested_outputs by zero-based output_index
        and, where known, the zero-based JSON record_index. They are mechanical
        validation facts, not scientific repair instructions. A new complete
        program may be submitted within the same capability and budget; each
        execution retains its own receipt and source identity.

        Args:
            image_key: Approved key from the default execution capability or a
                verified environment_list/environment_build result.
            script_content: Complete program to execute in the approved runtime.
            runtime: Runtime family from the current execution capability.
            input_artifact_ids: A subset of the current execution capability's
                mountable input Artifact UUIDs to mount read-only; omitted UUIDs
                are not mounted.
            parameters: Optional JSON-compatible execution parameters.
            requested_outputs: Declared relative output files and exposure intent.
            resources: Requested resources within the current trusted envelope.
            network_required: Whether the program requires network access.
        """

        draft = {
            "runtime": runtime,
            "image_key": image_key,
            "script_content": script_content,
            "input_artifact_ids": input_artifact_ids,
            "parameters": parameters,
            "requested_outputs": requested_outputs,
            "resources": resources,
            "network_required": network_required,
        }

        if "execution_submit" not in self.binding.capability_allowlist:
            return await self._call(
                "execution_submit",
                lambda: None,
                execution_submit_request=self._execution_submit_request_audit(draft),
            )
        try:
            validated = ExecutionPlanDraft.model_validate(draft)
        except ValidationError as exc:
            request_audit = self._execution_submit_request_audit(
                draft,
                validation_error=exc,
            )

            def reject_invalid_draft():
                raise _InvalidExecutionDraft

            return await self._call(
                "execution_submit",
                reject_invalid_draft,
                execution_submit_request=request_audit,
            )

        async def submit():
            service = self._required(self.services.execution_submission, "execution")
            return await service.submit(
                validated,
                principal=self.binding.principal,
                workspace=self.binding.workspace,
                run_id=self.binding.run_id,
                stage_id=self.binding.stage_id,
                invocation_id=self.binding.invocation_id,
                mountable_input_artifact_ids=self.binding.mountable_input_artifact_ids,
            )
        return await self._call(
            "execution_submit",
            submit,
            execution_submit_request=self._execution_submit_request_audit(
                draft,
                validation_status=ExecutionSubmitValidationStatus.VALID,
            ),
        )

    @tool
    async def skill_search(
        self,
        offset: int = 0,
        limit: int = 20,
        required_tags: list[str] | None = None,
        artifact_types: list[str] | None = None,
        include_lab: bool = True,
    ) -> dict:
        """Browse visible active Gold candidates without ranking or selecting them.

        Args:
            offset: Zero-based position in the stable visible candidate catalog.
            limit: Maximum candidates to return, from 1 through 50.
            required_tags: Optional exact tags that every result must contain;
                use no filter when exact tags are unknown.
            artifact_types: Optional exact Artifact types that every result must
                contain; use no filter when exact types are unknown.
            include_lab: Whether LAB-scoped candidates may be returned.

        Returned candidates are not ranked by scientific relevance. Compare
        their bounded metadata yourself and fetch another page only if useful.
        """
        tags = required_tags or []
        types = artifact_types or []
        return await self._call(
            "skill_search",
            lambda: self._skill_search(
                offset, limit, tags, types, include_lab
            ),
            skill_search_request=SkillSearchRequestAudit(
                offset=offset,
                limit=limit,
                required_tag_count=len(tags),
                artifact_type_count=len(types),
                include_lab=include_lab,
            ),
        )

    @tool
    async def skill_view(self, authorization_id: str) -> dict:
        """View the exact Skill version bound to an approved run authorization.

        Guidance is reference context, not a mandatory procedure or current-task
        evidence. truncated_fields explicitly names any partially returned fields.
        """
        return await self._call(
            "skill_view",
            lambda: self._skill_view(UUID(authorization_id)),
            request_ids={"authorization_id": authorization_id},
        )

    @tool
    async def skill_propose_use(
        self,
        skill_id: str,
        version: int,
        mode: Literal["REUSE", "ADAPT", "REFERENCE"],
        reason: str,
        proposed_deviations: list[str] | None = None,
    ) -> dict:
        """Create a pending Skill-use proposal; this cannot approve or execute it.

        Args:
            skill_id: Exact UUID returned by skill_search.
            version: Exact immutable version returned by skill_search.
            mode: Model-selected REUSE, ADAPT, or REFERENCE use mode.
            reason: Bounded reason this candidate may help the current task.
            proposed_deviations: Current-task changes proposed by the runtime;
                omit when none are needed.
        """
        return await self._call(
            "skill_propose_use",
            lambda: self._skill_propose_use(
                UUID(skill_id), version, mode, reason, proposed_deviations or []
            ),
            request_ids={"skill_id": skill_id, "skill_version": version},
        )

    @tool
    async def memory_search(
        self,
        offset: int = 0,
        limit: int = 20,
        kind: Literal[
            "PREFERENCE",
            "PROJECT_FACT",
            "BIOLOGICAL_EVIDENCE",
            "HYPOTHESIS",
            "OPERATING_NOTE",
        ] | None = None,
        scope: Literal["PERSONAL", "PROJECT", "LAB"] | None = None,
    ) -> dict:
        """Browse visible latest ACTIVE Memory without ranking relevance.

        Args:
            offset: Zero-based position in the stable visible catalog.
            limit: Maximum candidates to return, from 1 through 50.
            kind: Optional exact semantic kind filter.
            scope: Optional exact visibility scope filter.
        """
        return await self._call(
            "memory_search", lambda: self._memory_search(offset, limit, kind, scope)
        )

    @tool
    async def memory_view(self, memory_id: str, version: int) -> dict:
        """View one authorized immutable Memory version."""
        return await self._call(
            "memory_view",
            lambda: self._memory_view(UUID(memory_id), version),
            request_ids={"memory_id": memory_id, "memory_version": version},
        )

    @tool
    async def memory_propose_update(
        self,
        target_scope: Literal["PERSONAL", "PROJECT", "LAB"],
        reason: str,
        action: Literal["UPSERT", "RETIRE"] = "UPSERT",
        target_memory_id: str | None = None,
        target_version: int | None = None,
        proposed_kind: Literal[
            "PREFERENCE",
            "PROJECT_FACT",
            "BIOLOGICAL_EVIDENCE",
            "HYPOTHESIS",
            "OPERATING_NOTE",
        ] | None = None,
        proposed_content: str | None = None,
        evidence_artifact_ids: list[str] | None = None,
    ) -> dict:
        """Propose governed contextual Memory; approval is always external.

        Trusted owner, project, lab, source-run, and invocation fields are bound
        by the host and cannot be supplied here. RETIRE requires an exact current
        Memory ID/version and does not replace content.
        """
        return await self._call(
            "memory_propose_update",
            lambda: self._memory_proposal(
                target_scope=target_scope,
                reason=reason,
                action=action,
                target_memory_id=target_memory_id,
                target_version=target_version,
                proposed_kind=proposed_kind,
                proposed_content=proposed_content,
                evidence_artifact_ids=evidence_artifact_ids or [],
            ),
        )

    @tool
    async def report_read(
        self, artifact_id: str,
        offset: Annotated[int, Field(strict=True, ge=0)] = 0,
        limit: Annotated[int, Field(strict=True, ge=1, le=8_000)] = 4_000,
        expected_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None,
    ) -> dict:
        """Read exact original model-authored report prose by Artifact UUID.

        This accepts only authorized DERIVED reports, not arbitrary files or RAW
        Artifacts. Prose is MODEL_CONTEXT, not independent scientific evidence.
        Offsets count Unicode characters. Follow next_offset to read another
        page; truncated reports whether later text remains, not whether earlier
        pages were read. expected_sha256 can bind subsequent pages to the first
        page's complete-report identity. Reading does not revise or execute.
        """
        def read():
            page = read_report_page(
                self.services.artifact_store, self.services.artifact_exposure,
                coerce_artifact_id(artifact_id), principal=self.binding.principal,
                workspace=self.binding.workspace, offset=offset, limit=limit,
                expected_sha256=expected_sha256,
            )
            from pantheon.settings import get_settings
            from pantheon.utils.llm import filter_base64_in_tool_result, filter_tool_messages
            from pantheon.utils.token_optimization import get_per_tool_limit

            serialized = json.dumps(page, ensure_ascii=False)
            transport_limit = get_per_tool_limit("report_read", get_settings().max_tool_content_length)
            if len(serialized) > transport_limit - 2_000:
                raise _ReportPageTransportError("REPORT_PAGE_TOO_LARGE")
            filtered = json.dumps(filter_base64_in_tool_result(json.loads(serialized)), ensure_ascii=False)
            filtered = filter_tool_messages([{"role": "tool", "content": filtered}])[0]["content"]
            if filtered != serialized:
                raise _ReportPageTransportError("REPORT_PAGE_TRANSPORT_UNSUPPORTED")
            # Preserve the exact page the Agent selected across finalization
            # and restart, at MODEL_CONTEXT authority. Trace events still carry
            # identifiers only; no unread page is fetched or inferred.
            return page

        return await self._call("report_read", read, request_ids={"artifact_id": artifact_id})

    @tool
    async def report_submit(
        self, title: str, report_text: str, evidence_artifact_ids: list[str] | None = None
    ) -> dict:
        """Register bounded report content without accepting a filename or path."""
        return await self._call(
            "report_submit",
            lambda: self._required(self.services.report_submission, "report").submit(
                title=title,
                report_text=report_text,
                evidence_artifact_ids=tuple(UUID(item) for item in evidence_artifact_ids or []),
                principal=self.binding.principal,
                workspace=self.binding.workspace,
                run_id=self.binding.run_id,
                stage_id=self.binding.stage_id,
                invocation_id=self.binding.invocation_id,
            ),
        )

    def _artifact_list(self, offset: int, limit: int) -> list[ArtifactListItem]:
        if offset < 0 or limit < 1 or limit > 100:
            raise ValueError("Artifact pagination is outside the allowed bounds")
        visible = []
        for ref in self.services.artifact_store.list_refs():
            if (
                ref.project_id != self.binding.workspace.project_id
                or ref.lab_id != self.binding.workspace.lab_id
            ):
                continue
            try:
                authorized = self.services.artifact_exposure.artifact_ref(
                    ref.artifact_id, principal=self.binding.principal
                )
            except AuthorizationDenied:
                continue
            views = []
            for view_type in ArtifactViewType:
                query = ArtifactQuery(
                    view_type=view_type,
                    limit=1 if view_type is ArtifactViewType.TOP_N else None,
                )
                if self.services.artifact_exposure.policy.decide(
                    authorized, query, self.binding.consumer
                ).allowed:
                    views.append(view_type.value)
            visible.append(
                ArtifactListItem(
                    artifact_id=ref.artifact_id,
                    artifact_type=ref.artifact_type,
                    exposure_class=ref.exposure_class.value,
                    release_basis=ref.release_basis.value,
                    schema_=ArtifactSchemaView.from_schema(ref.artifact_schema),
                    available_views=tuple(views),
                    owner_user_id=ref.owner_user_id,
                    project_id=ref.project_id,
                    lab_id=ref.lab_id,
                    run_id=ref.run_id,
                    stage_id=ref.stage_id,
                    producer_invocation_id=ref.producer_invocation_id,
                )
            )
        return visible[offset : offset + limit]

    def _artifact_query(self, artifact_id: str, view_type: str, limit: int | None):
        identifier = coerce_artifact_id(artifact_id)
        ref = self.services.artifact_exposure.artifact_ref(
            identifier, principal=self.binding.principal
        )
        if (
            ref.project_id != self.binding.workspace.project_id
            or ref.lab_id != self.binding.workspace.lab_id
        ):
            raise AuthorizationDenied("Artifact is outside the bound workspace")
        constraints = ArtifactQueryConstraints.from_authorized_artifact(
            ref, exposure_policy=self.services.artifact_exposure.policy
        )
        try:
            typed_view = ArtifactViewType(view_type)
        except ValueError as exc:
            raise _ArtifactQueryFailure("INVALID_ENUM_VALUE", constraints) from exc
        try:
            query = ArtifactQuery(view_type=typed_view, limit=limit)
        except ValidationError as exc:
            raise _ArtifactQueryFailure("INVALID_QUERY_SHAPE", constraints) from exc
        try:
            return self.services.artifact_exposure.artifact_query(
                identifier,
                query,
                self.binding.consumer,
                principal=self.binding.principal,
            )
        except ArtifactExposureDenied as exc:
            raise _ArtifactQueryFailure("ARTIFACT_EXPOSURE_DENIED", constraints) from exc

    def _skill_search(self, offset, limit, tags, artifact_types, include_lab):
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("Skill result offset must be a non-negative integer")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or limit < 1
            or limit > 50
        ):
            raise ValueError("Skill result limit must be between 1 and 50")
        service = self._required(self.services.skill_service, "skill")
        skills = service.search(
            SkillSearchContext(
                user_id=self.binding.principal.user_id,
                project_id=self.binding.workspace.project_id,
                lab_id=self.binding.workspace.lab_id,
                include_lab=include_lab,
            ),
            principal=self.binding.principal,
        )
        latest_by_skill = {}
        for skill in skills:
            current = latest_by_skill.get(skill.skill_id)
            if current is None or skill.version > current.version:
                latest_by_skill[skill.skill_id] = skill
        active = tuple(
            sorted(
                (skill for skill in latest_by_skill.values()
                 if frozenset(tags).issubset(skill.procedure.tags)
                 and frozenset(artifact_types).issubset(skill.procedure.artifact_types)),
                key=lambda skill: (
                    skill.name.casefold(),
                    str(skill.skill_id),
                    skill.version,
                ),
            )
        )
        items = tuple(
            self._skill_candidate(item) for item in active[offset : offset + limit]
        )
        next_offset = offset + len(items)
        truncated = next_offset < len(active)
        return SkillCandidatePage(
            items=items,
            returned_count=len(items),
            available_count=len(active),
            offset=offset,
            effective_limit=limit,
            next_offset=next_offset if truncated else None,
            truncated=truncated,
        )

    def _skill_view(self, authorization_id: UUID):
        skill = self._required(
            self.services.skill_service, "skill"
        ).get_authorized_context(
            authorization_id,
            run_id=self.binding.run_id,
            project_id=self.binding.workspace.project_id,
            principal=self.binding.principal,
        )
        procedure = skill.procedure
        # Keep the established per-field bounds; expose partialness rather than
        # implying that a bounded view contains the entire stored Skill.
        fields = {
            "workflow_outline": (procedure.workflow_outline, 32),
            "collaboration_guidance": (procedure.agent_collaboration_guidance, 16),
            "execution_guidance": (procedure.execution_guidance, 16),
            "parameter_guidance": (procedure.parameter_guidance, 16),
            "known_failure_modes": (procedure.known_failure_modes, 16),
            "debug_lessons": (procedure.debug_lessons, 16),
            "validation_expectations": (procedure.validation_expectations, 16),
            "limitations": (procedure.known_limitations, 16),
            "reusable_principles": (procedure.reusable_principles, 16),
            "adaptation_points": (procedure.adaptation_points, 16),
        }
        candidate = self._skill_candidate(skill).model_dump()
        partial = [name for name, (values, limit) in fields.items() if len(values) > limit]
        for name, values in (
            ("description", skill.description), ("tags", procedure.tags),
            ("artifact_types", procedure.artifact_types),
            ("input_contract_ids", procedure.input_contract_ids),
            ("output_contract_ids", procedure.output_contract_ids),
            ("applicability_preview", procedure.applicability),
            ("limitation_preview", procedure.known_limitations),
        ):
            if len(values) > len(candidate[name]):
                partial.append(name)
        detail = SkillDetailView(
            **candidate,
            source_run_id=skill.source_run_id,
            parent_skill_id=skill.parent_skill_id,
            parent_version=skill.parent_version,
            applicability=procedure.applicability,
            **{name: tuple(values[:limit]) for name, (values, limit) in fields.items()},
            truncated_fields=tuple(partial),
        )
        # Per-field bounds alone do not bound UTF-8 bytes across all fields.
        # Remove whole trailing items from the largest list, without assigning
        # scientific relevance or rewriting any retained Agent statement.
        data = detail.model_dump(mode="json")
        def encoded_size(value):
            return len(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                  separators=(",", ":")).encode("utf-8"))
        while encoded_size(data) > 64_000:
            name = max((name for name, value in data.items()
                        if isinstance(value, list) and value and name != "truncated_fields"),
                       key=lambda name: encoded_size(data[name]))
            data[name].pop()
            if name not in data["truncated_fields"]:
                data["truncated_fields"].append(name)
        return SkillDetailView.model_validate_json(json.dumps(data))

    def _skill_propose_use(self, skill_id, version, mode, reason, deviations):
        if len(deviations) > 32:
            raise ValueError("Too many proposed deviations")
        proposal = SkillUseProposal(
            run_id=self.binding.run_id,
            requesting_user_id=self.binding.principal.user_id,
            project_id=self.binding.workspace.project_id,
            lab_id=self.binding.workspace.lab_id,
            skill_id=skill_id,
            skill_version=version,
            proposed_mode=SkillUseMode(mode),
            reason=reason,
            proposed_deviations=tuple(deviations),
        )
        self._required(self.services.skill_service, "skill").submit_use_proposal(
            proposal, principal=self.binding.principal
        )
        return {
            "proposal_id": str(proposal.proposal_id),
            "approval_gate_id": proposal.approval_gate_id,
            "domain_reference_id": f"skill-use:{proposal.proposal_id}",
            "status": "USER_APPROVAL_REQUIRED",
        }

    def _memory_search(self, offset, limit, kind, scope):
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("Memory result offset must be a non-negative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 50:
            raise ValueError("Memory result limit must be between 1 and 50")
        entries = self._required(self.services.memory_service, "memory").list_candidates(
            self.binding.principal
        )
        filtered = []
        for entry in entries:
            if entry.scope is MemoryScope.PROJECT and entry.project_id != self.binding.workspace.project_id:
                continue
            if entry.scope is MemoryScope.PERSONAL and entry.owner_user_id != self.binding.principal.user_id:
                continue
            if entry.lab_id != self.binding.workspace.lab_id:
                continue
            if kind and entry.kind is not MemoryKind(kind):
                continue
            if scope and entry.scope is not MemoryScope(scope):
                continue
            filtered.append(
                MemoryCandidateView(
                    memory_id=entry.memory_id,
                    version=entry.version,
                    scope=entry.scope,
                    kind=entry.kind,
                    preview=entry.content[:500],
                )
            )
        items = tuple(filtered[offset : offset + limit])
        next_offset = offset + len(items)
        truncated = next_offset < len(filtered)
        return MemoryCandidatePage(
            items=items,
            returned_count=len(items),
            available_count=len(filtered),
            offset=offset,
            effective_limit=limit,
            next_offset=next_offset if truncated else None,
            truncated=truncated,
        )

    def _memory_view(self, memory_id, version):
        entry = self._required(self.services.memory_service, "memory").get(
            self.binding.principal, memory_id, version
        )
        if entry.scope is MemoryScope.PROJECT and entry.project_id != self.binding.workspace.project_id:
            raise AuthorizationDenied("Memory is outside the bound workspace")
        return MemoryDetailView(
            memory_id=entry.memory_id,
            version=entry.version,
            scope=entry.scope,
            kind=entry.kind,
            preview=entry.content[:500],
            content=entry.content[:4000],
            previous_version=entry.previous_version,
            evidence_run_count=len(entry.evidence_run_ids),
            evidence_artifact_count=len(entry.evidence_artifact_ids),
            has_evidence=bool(entry.evidence_run_ids or entry.evidence_artifact_ids),
        )

    def _memory_proposal(
        self,
        *,
        target_scope,
        reason,
        action,
        target_memory_id,
        target_version,
        proposed_kind,
        proposed_content,
        evidence_artifact_ids,
    ):
        scope = MemoryScope(target_scope)
        proposal = MemoryUpdateProposal(
            action=MemoryProposalAction(action),
            target_scope=scope,
            owner_user_id=(
                self.binding.principal.user_id
                if scope in {MemoryScope.PERSONAL, MemoryScope.PROJECT}
                else None
            ),
            project_id=(self.binding.workspace.project_id if scope is MemoryScope.PROJECT else None),
            lab_id=self.binding.workspace.lab_id,
            target_memory_id=UUID(target_memory_id) if target_memory_id is not None else None,
            target_version=target_version,
            proposed_kind=MemoryKind(proposed_kind) if proposed_kind is not None else None,
            proposed_content=proposed_content,
            reason=reason,
            evidence_run_ids=(self.binding.run_id,),
            evidence_artifact_ids=tuple(UUID(item) for item in evidence_artifact_ids),
            proposing_invocation_id=self.binding.invocation_id,
            source_run_id=self.binding.run_id,
        )
        self._required(self.services.memory_service, "memory").submit_proposal(
            self.binding.principal,
            proposal,
            workspace=self.binding.workspace,
        )
        return {
            "proposal_id": str(proposal.proposal_id),
            "approval_gate_id": proposal.approval_gate_id,
            "domain_reference_id": f"memory-proposal:{proposal.proposal_id}",
            "status": "USER_APPROVAL_REQUIRED",
        }

    @staticmethod
    def _skill_candidate(skill) -> SkillCandidateView:
        return SkillCandidateView(
            skill_id=skill.skill_id,
            version=skill.version,
            name=skill.name[:256],
            description=skill.description[:1000],
            scope=skill.scope.value,
            tags=tuple(sorted(skill.procedure.tags))[:32],
            artifact_types=tuple(sorted(skill.procedure.artifact_types))[:32],
            input_contract_ids=tuple(skill.procedure.input_contract_ids[:16]),
            output_contract_ids=tuple(skill.procedure.output_contract_ids[:16]),
            applicability_preview=skill.procedure.applicability[:500],
            limitation_preview=tuple(skill.procedure.known_limitations[:4]),
        )

    @staticmethod
    def _required(value, label: str):
        if value is None:
            raise RuntimeError(f"The {label} capability service is not configured")
        return value

    @staticmethod
    def _safe_error(exc: Exception) -> ToolError:
        if isinstance(exc, EnvironmentRequestError):
            return ToolError(error_code=exc.code, safe_message=exc.safe_message)
        if isinstance(exc, _ArtifactQueryFailure):
            messages = {
                "INVALID_ENUM_VALUE": (
                    "The artifact view type is not supported. See query_constraints "
                    "for the permitted views of this Artifact."
                ),
                "INVALID_QUERY_SHAPE": (
                    "The artifact view and limit combination is invalid. A non-null "
                    "limit is allowed only for query_constraints.limit_allowed_view_type "
                    "and must be an integer at least query_constraints.limit_minimum. "
                    "Other views require an omitted limit or JSON null, not a string."
                ),
                "ARTIFACT_EXPOSURE_DENIED": (
                    "Remote exposure policy denies this Artifact/view combination. "
                    "See query_constraints for its current permitted remote views. "
                    "This is not an execution input eligibility or preflight decision."
                ),
            }
            return ToolError(
                error_code=exc.error_code,
                safe_message=messages[exc.error_code],
                denied_operation=(
                    "REMOTE_ARTIFACT_VIEW"
                    if exc.error_code == "ARTIFACT_EXPOSURE_DENIED" else None
                ),
                query_constraints=exc.query_constraints,
            )
        if isinstance(exc, AuthorizationDenied):
            return ToolError(error_code="AUTHORIZATION_DENIED", safe_message="Access denied by policy.")
        if isinstance(exc, ArtifactExposureDenied):
            return ToolError(
                error_code="ARTIFACT_EXPOSURE_DENIED",
                safe_message=(
                    "Remote exposure policy denies this Artifact/view combination. "
                    "RAW Artifacts have no remote-readable views. This is not an "
                    "execution input eligibility or preflight decision."
                ),
                denied_operation="REMOTE_ARTIFACT_VIEW",
            )
        if isinstance(exc, ArtifactIdentifierError):
            return ToolError(
                error_code="INVALID_IDENTIFIER",
                safe_message="The Artifact identifier must be a UUID.",
            )
        if isinstance(exc, ArtifactNotFoundError):
            return ToolError(
                error_code="ARTIFACT_NOT_FOUND",
                safe_message="The requested Artifact is not registered.",
            )
        if isinstance(exc, SkillApprovalRequiredError):
            return ToolError(
                error_code="SKILL_APPROVAL_REQUIRED",
                safe_message="Exact user approval is required for Skill context access.",
            )
        if isinstance(exc, SkillNotFoundError):
            return ToolError(
                error_code="SKILL_NOT_FOUND",
                safe_message="The requested Skill lifecycle record does not exist.",
            )
        if isinstance(exc, SkillDecisionError):
            return ToolError(
                error_code="INVALID_CONTROL_STATE",
                safe_message="The Skill decision does not match pending control state.",
            )
        if isinstance(exc, SkillStoreError):
            return ToolError(
                error_code="SKILL_OPERATION_FAILED",
                safe_message="The governed Skill operation could not complete.",
            )
        if isinstance(exc, MemoryStaleUpdateError):
            return ToolError(
                error_code="STALE_MEMORY_VERSION",
                safe_message="The Memory proposal no longer targets the latest active version.",
            )
        if isinstance(exc, MemoryEvidenceError):
            return ToolError(
                error_code="INVALID_MEMORY_PROVENANCE",
                safe_message="The proposed Memory evidence lineage is not allowed.",
            )
        if isinstance(exc, MemoryNotFoundError):
            return ToolError(
                error_code="MEMORY_NOT_FOUND",
                safe_message="The requested Memory lifecycle record does not exist.",
            )
        if isinstance(exc, MemoryDecisionError):
            return ToolError(
                error_code="INVALID_CONTROL_STATE",
                safe_message="The Memory decision does not match pending control state.",
            )
        if isinstance(exc, MemoryConflictError):
            return ToolError(
                error_code="MEMORY_CONFLICT",
                safe_message="The governed Memory operation conflicts with current state.",
            )
        if isinstance(exc, MemoryStoreError):
            return ToolError(
                error_code="MEMORY_OPERATION_FAILED",
                safe_message="The governed Memory operation could not complete.",
            )
        if isinstance(exc, _InvalidExecutionDraft):
            return ToolError(
                error_code="INVALID_EXECUTION_DRAFT",
                safe_message="The execution draft does not match the canonical contract.",
            )
        if isinstance(exc, ExecutionInspectionError):
            return ToolError(
                error_code="EXECUTION_SUBMISSION_UNAVAILABLE",
                safe_message=(
                    "The original submission could not be verified in this run and live "
                    "application session. No source was released and this inspection "
                    "did not start an execution."
                ),
            )
        if isinstance(exc, _ExecutionSourceTransportError):
            return ToolError(
                error_code=exc.args[0],
                safe_message=(
                    "No source was released. The serialized page exceeds the transport "
                    "bound; request a smaller source_limit."
                    if exc.args[0] == "EXECUTION_SOURCE_PAGE_TOO_LARGE" else
                    "No source was released. The transport would alter this source page; "
                    "the original remains unchanged and this inspection started no execution."
                ),
            )
        if isinstance(exc, _ReportPageTransportError):
            return ToolError(
                error_code=exc.args[0],
                safe_message=(
                    "No report text was released. Request a smaller limit to fit the transport bound."
                    if exc.args[0] == "REPORT_PAGE_TOO_LARGE" else
                    "No report text was released because transport would alter the original page."
                ),
            )
        if isinstance(exc, ExecutionScriptValidationError):
            return ToolError(
                error_code="INVALID_EXECUTION_SCRIPT",
                safe_message="The submitted Python script is not syntactically valid.",
                script_validation=(ScriptValidationDetails(
                    script_hash=exc.script_hash, diagnostics=exc.diagnostics,
                ) if exc.script_hash is not None and exc.diagnostics else None),
            )
        if isinstance(exc, ExecutionOutputDeclarationError):
            return ToolError(
                error_code="INVALID_OUTPUT_DECLARATION",
                safe_message=(
                    "No execution started. The configured execution requires at least "
                    f"{exc.minimum_queryable_output_count} queryable output(s), but "
                    f"{exc.declared_queryable_output_count} declaration(s) are eligible. "
                    "Eligible declarations request DERIVED exposure with an approved "
                    "output contract that authorizes remote release. Actual output "
                    "files must still pass all collection and release checks."
                ),
            )
        if isinstance(exc, ExecutionInputSelectionError):
            return ToolError(
                error_code="INVALID_EXECUTION_INPUT",
                safe_message=(
                    "No execution started. Selected input Artifact IDs must be a subset "
                    "of the current execution capability's mountable_input_artifact_ids. "
                    "Workspace visibility alone does not authorize execution input use."
                ),
            )
        if isinstance(exc, ValidationError):
            return ToolError(
                error_code="INVALID_REQUEST",
                # Validation locations can contain untrusted dictionary keys.
                # Capability-specific request audits own safe field diagnostics.
                safe_message="The capability request does not match its schema.",
            )
        if isinstance(exc, ValueError):
            return ToolError(error_code="INVALID_REQUEST", safe_message="The capability request is invalid.")
        return ToolError(error_code="CAPABILITY_FAILED", safe_message="The capability could not complete.")

    @staticmethod
    def _artifact_query_request_audit(
        artifact_id: Any,
        view_type: Any,
        limit: Any,
        *,
        canonical_limit: Any,
        normalization_applied: bool,
    ) -> ArtifactQueryRequestAudit:
        """Project only bounded artifact_query fields; never retain arbitrary input."""

        try:
            safe_artifact_id: UUID | Literal["INVALID_IDENTIFIER"] = (
                coerce_artifact_id(artifact_id)
            )
        except ArtifactIdentifierError:
            safe_artifact_id = "INVALID_IDENTIFIER"
        safe_view_type = (
            view_type
            if isinstance(view_type, str)
            and _SAFE_ARTIFACT_QUERY_TOKEN.fullmatch(view_type) is not None
            else "INVALID_VALUE"
        )
        safe_limit: int | Literal["INVALID_VALUE"] | None
        if normalization_applied:
            safe_limit = canonical_limit
            limit_type = ArtifactQueryLimitType.STRING
        elif limit is None:
            safe_limit = limit
            limit_type = ArtifactQueryLimitType.NULL
        elif isinstance(limit, bool):
            safe_limit = "INVALID_VALUE"
            limit_type = ArtifactQueryLimitType.BOOLEAN
        elif isinstance(limit, int):
            safe_limit = limit
            limit_type = ArtifactQueryLimitType.INTEGER
        elif isinstance(limit, str):
            safe_limit = "INVALID_VALUE"
            limit_type = ArtifactQueryLimitType.STRING
        elif isinstance(limit, float):
            safe_limit = "INVALID_VALUE"
            limit_type = ArtifactQueryLimitType.FLOAT
        elif isinstance(limit, (list, tuple)):
            safe_limit = "INVALID_VALUE"
            limit_type = ArtifactQueryLimitType.ARRAY
        elif isinstance(limit, dict):
            safe_limit = "INVALID_VALUE"
            limit_type = ArtifactQueryLimitType.OBJECT
        else:
            safe_limit = "INVALID_VALUE"
            limit_type = ArtifactQueryLimitType.OTHER
        return ArtifactQueryRequestAudit(
            artifact_id=safe_artifact_id,
            view_type=safe_view_type,
            limit=safe_limit,
            limit_type=limit_type,
            normalization_applied=normalization_applied,
        )

    @staticmethod
    def _execution_submit_request_audit(
        draft: Any,
        *,
        validation_status: ExecutionSubmitValidationStatus = (
            ExecutionSubmitValidationStatus.NOT_VALIDATED
        ),
        validation_error: ValidationError | None = None,
    ) -> ExecutionSubmitRequestAudit:
        """Project only execution request shape and stable validation diagnostics."""

        wire_type = LabBioRuntimeToolSet._execution_wire_type(draft)
        if isinstance(draft, ExecutionPlanDraft):
            present_names = draft.model_fields_set

            def field_value(name: str) -> Any:
                return getattr(draft, name)

        elif isinstance(draft, dict):
            present_names = draft.keys()

            def field_value(name: str) -> Any:
                return draft.get(name)

        else:
            present_names = ()

            def field_value(_name: str) -> Any:
                return None

        presence = tuple(
            field
            for field in ExecutionDraftField
            if field.value in present_names
        )
        input_ids = field_value("input_artifact_ids")
        outputs = field_value("requested_outputs")
        network = field_value("network_required")
        paths: list[str] = []
        error_types: list[str] = []
        if validation_error is not None:
            validation_status = ExecutionSubmitValidationStatus.INVALID
            for issue in validation_error.errors(
                include_url=False,
                include_input=False,
                include_context=False,
            )[:16]:
                safe_parts = []
                for item in issue.get("loc", ())[:8]:
                    if isinstance(item, int):
                        safe_parts.append("item")
                    elif item in _EXECUTION_VALIDATION_PATH_PARTS:
                        safe_parts.append(str(item))
                    else:
                        safe_parts.append("unknown_field")
                paths.append(".".join(safe_parts) or "draft")
                error_type = str(issue.get("type", "invalid"))
                error_types.append(
                    error_type
                    if _SAFE_ARTIFACT_QUERY_TOKEN.fullmatch(error_type) is not None
                    else "invalid"
                )
        return ExecutionSubmitRequestAudit(
            wire_type=wire_type,
            known_top_level_field_presence=presence,
            input_artifact_count=(
                len(input_ids) if isinstance(input_ids, (tuple, list)) else None
            ),
            requested_output_count=(
                len(outputs) if isinstance(outputs, (tuple, list)) else None
            ),
            resources_present="resources" in present_names,
            network_required_type=(
                LabBioRuntimeToolSet._execution_wire_type(network)
                if "network_required" in present_names
                else None
            ),
            network_required_value=network if isinstance(network, bool) else None,
            validation_status=validation_status,
            validation_error_field_paths=tuple(paths),
            validation_error_types=tuple(error_types),
        )

    @staticmethod
    def _execution_wire_type(value: Any) -> ExecutionAuditWireType:
        if value is None:
            return ExecutionAuditWireType.NULL
        if isinstance(value, bool):
            return ExecutionAuditWireType.BOOLEAN
        if isinstance(value, (dict, BaseModel)):
            return ExecutionAuditWireType.OBJECT
        if isinstance(value, (tuple, list)):
            return ExecutionAuditWireType.ARRAY
        if isinstance(value, str):
            return ExecutionAuditWireType.STRING
        if isinstance(value, int):
            return ExecutionAuditWireType.INTEGER
        if isinstance(value, float):
            return ExecutionAuditWireType.NUMBER
        return ExecutionAuditWireType.OTHER

    @staticmethod
    def _bounded_identifiers(values: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """Trace canonical IDs/versions only, never arbitrary model strings."""

        bounded: dict[str, JsonValue] = {}
        for key, value in values.items():
            if key.endswith("_id") and isinstance(value, str):
                try:
                    bounded[key] = str(UUID(value))
                except ValueError:
                    continue
            elif key.endswith("_version") and isinstance(value, int):
                bounded[key] = value
        return bounded

    @staticmethod
    def _reference_ids(value: JsonValue) -> tuple[str, ...]:
        """Collect opaque IDs structurally; no relevance or ranking is inferred."""

        found: list[str] = []

        def visit(item: JsonValue) -> None:
            if isinstance(item, dict):
                for key, child in item.items():
                    if key.endswith("_id") and isinstance(child, str):
                        try:
                            UUID(child)
                        except ValueError:
                            pass
                        else:
                            found.append(child)
                    elif key.endswith("_ids") and isinstance(child, list):
                        for identifier in child:
                            if not isinstance(identifier, str):
                                continue
                            try:
                                UUID(identifier)
                            except ValueError:
                                continue
                            found.append(identifier)
                    else:
                        visit(child)
            elif isinstance(item, list):
                for child in item:
                    visit(child)

        visit(value)
        return tuple(dict.fromkeys(found))[:128]

    def _emit(self, event_type, capability, status, payload=None):
        if self.services.trace_recorder is not None:
            return self.services.trace_recorder.emit(
                self.binding.run_id,
                event_type,
                stage_id=self.binding.stage_id,
                invocation_id=self.binding.invocation_id,
                agent_name=self.binding.actor_agent_name,
                status=status,
                payload={
                    "capability": capability,
                    "actor_profile_key": self.binding.actor_profile_key,
                    "actor_agent_name": self.binding.actor_agent_name,
                    **(payload or {}),
                },
            )
        return None
