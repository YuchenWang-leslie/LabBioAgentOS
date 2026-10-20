"""LabBio authority and evidence binding for the Native Kernel tool layer."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictStr, create_model, model_validator

from labbioagentos.artifacts.arithmetic import ArtifactAggregateMatchValue
from labbioagentos.artifacts.models import ArtifactQueryLimit
from labbioagentos.contracts import WorkflowStage
from labbioagentos.execution.models import (
    ExecutionImageKey,
    ExecutionInputArtifactIds,
    ExecutionRequestedOutputs,
    ExecutionRuntime,
    ExecutionScriptContent,
    ExecutionSourceEdit,
    RequestedResources,
)
from labbioagentos.kernel import (
    DispatchOutcome,
    FinishReason,
    KernelIdentity,
    ModelResultProjector,
    ToolBatchRejectedError,
    ToolCall,
    ToolDispatcher,
    ToolRegistry,
    ToolResult as KernelToolResult,
)
from labbioagentos.literature import LiteratureLimit, LiteratureQuery
from labbioagentos.model_safety import validate_model_visible_json

from .capability_catalog import (
    ALL_CAPABILITIES,
    CAPABILITY_CEILINGS,
    CAPABILITY_INFORMATION_AUTHORITY,
)
from .contracts import (
    CapabilityErrorDetails,
    CapabilityEvidenceBundle,
    CapabilityEvidenceItem,
    CapabilityEvidenceStatus,
    ExecutionReview,
    ReportEvidenceReference,
    RuntimeExecutionCapabilityView,
)


class NativeToolArguments(BaseModel):
    """Strict top-level wire contract shared by native LabBio tools."""

    model_config = ConfigDict(extra="forbid", strict=True)


class ArtifactListArguments(NativeToolArguments):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=100)


class ArtifactQueryArguments(NativeToolArguments):
    artifact_id: UUID = Field(description="Artifact UUID from a trusted runtime reference.")
    view_type: Literal["METADATA", "SCHEMA", "SUMMARY", "TOP_N"] = Field(
        description="Governed view requested from the artifact exposure service."
    )
    limit: ArtifactQueryLimit | StrictStr | None = Field(
        default=None,
        description=(
            "Optional positive JSON integer page size for TOP_N; a canonical decimal "
            "integer string is also audited and normalized by artifact_query. "
            "Null uses the policy default when following next_offset."
        ),
    )
    offset: int = Field(
        default=0,
        ge=0,
        description="Zero-based record offset used only for TOP_N pagination.",
    )


class ArtifactAggregateArguments(NativeToolArguments):
    artifact_id: UUID
    operation: Literal["SUM", "COUNT"]
    field: StrictStr | None = Field(default=None, min_length=1, max_length=128)
    match_field: StrictStr | None = Field(default=None, min_length=1, max_length=128)
    match_value: ArtifactAggregateMatchValue = None


class EnvironmentListArguments(NativeToolArguments):
    requirements: list[Annotated[StrictStr, Field(min_length=1, max_length=256)]] = Field(
        default_factory=list,
        max_length=64,
    )
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=10, ge=1, le=10)


class EnvironmentBuildArguments(NativeToolArguments):
    base_image_key: ExecutionImageKey
    requirements: list[Annotated[StrictStr, Field(min_length=1, max_length=256)]] = Field(
        max_length=64,
    )
    import_modules: list[
        Annotated[
            StrictStr,
            Field(max_length=128, pattern=r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$"),
        ]
    ] = Field(default_factory=list, max_length=64)


class ExecutionInspectArguments(NativeToolArguments):
    execution_id: UUID
    source_offset: int = Field(default=0, ge=0)
    source_limit: int = Field(default=12_000, ge=1, le=32_000)


class ExecutionReviseArguments(NativeToolArguments):
    execution_id: UUID
    expected_script_hash: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")
    edits: tuple[ExecutionSourceEdit, ...] = Field(default=(), max_length=32)
    image_key: ExecutionImageKey | None = None
    requested_outputs: ExecutionRequestedOutputs | None = Field(
        default=None,
        description=(
            "Replacement output declarations, when needed, using the approved "
            "output contracts from execution_capability. Null preserves the "
            "original execution declarations."
        ),
    )
    resources: RequestedResources | None = None
    input_artifact_ids: ExecutionInputArtifactIds | None = Field(
        default=None,
        description=(
            "Replacement Artifact UUID selections from execution_capability."
            "mountable_input_artifact_ids. Null preserves the original selection."
        ),
    )
    parameters: dict[str, JsonValue] | None = None
    review: ExecutionReview | None = Field(
        default=None,
        description="Optional JSON object with a required rationale string; omit when no review is needed. Not a prose string.",
    )


class ExecutionSubmitArguments(NativeToolArguments):
    image_key: ExecutionImageKey
    script_content: ExecutionScriptContent
    runtime: ExecutionRuntime = ExecutionRuntime.PYTHON
    input_artifact_ids: ExecutionInputArtifactIds = Field(
        default=(),
        description=(
            "Explicit Artifact UUIDs selected from execution_capability."
            "mountable_input_artifact_ids; pass an empty list only when the program "
            "does not need task inputs."
        )
    )
    parameters: dict[str, JsonValue] = Field(default_factory=dict)
    requested_outputs: ExecutionRequestedOutputs = Field(
        default=(),
        description=(
            "Explicit relative output declarations. When the stage requires a "
            "queryable output, declare at least one DERIVED file with an approved "
            "output contract from execution_capability; pass an empty list only "
            "when the authoritative minimum is zero."
        )
    )
    resources: RequestedResources = Field(default_factory=RequestedResources)
    network_required: bool = False
    review: ExecutionReview | None = Field(
        default=None,
        description="Optional JSON object with a required rationale string; omit when no review is needed. Not a prose string.",
    )


class FileReadArguments(NativeToolArguments):
    artifact_id: UUID
    offset: int = Field(
        default=0,
        ge=0,
        description=(
            "Unicode-character offset. After a page for the same Artifact returns "
            "a non-null next_offset, copy the returned next_offset here for the "
            "following page; restarting an already delivered page is not progress."
        ),
    )
    limit: int = Field(
        default=4_000,
        ge=1,
        le=8_000,
        description="Page size in Unicode characters; use up to 8000 to finish bounded text efficiently.",
    )
    expected_sha256: StrictStr | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )


class ReportReadArguments(FileReadArguments):
    pass


class LiteratureSearchArguments(NativeToolArguments):
    query: LiteratureQuery
    limit: LiteratureLimit = 3


MemoryKindWire = Literal[
    "PREFERENCE",
    "PROJECT_FACT",
    "BIOLOGICAL_EVIDENCE",
    "HYPOTHESIS",
    "OPERATING_NOTE",
]
MemoryScopeWire = Literal["PERSONAL", "PROJECT", "LAB"]


class MemorySearchArguments(NativeToolArguments):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=20, ge=1, le=50)
    kind: MemoryKindWire | None = None
    scope: MemoryScopeWire | None = None


class MemoryViewArguments(NativeToolArguments):
    memory_id: UUID
    version: int = Field(ge=1)


class MemoryProposeUpdateArguments(NativeToolArguments):
    target_scope: MemoryScopeWire
    reason: StrictStr = Field(min_length=1, max_length=16_000)
    action: Literal["UPSERT", "RETIRE"] = "UPSERT"
    target_memory_id: UUID | None = None
    target_version: int | None = Field(
        default=None,
        ge=1,
        description="Existing positive version for an update or retirement; null for a new entry.",
    )
    proposed_kind: MemoryKindWire | None = None
    proposed_content: StrictStr | None = Field(
        default=None,
        min_length=1,
        max_length=16_000,
    )
    evidence_artifact_ids: list[UUID] | None = Field(default=None, max_length=128)


class ReportCheckArguments(NativeToolArguments):
    evidence_references: list[ReportEvidenceReference] = Field(
        min_length=1, max_length=64,
        description="Required observed-scope citations: source Artifact UUID and actual "
        "capability_invocation_id observation IDs fully delivered to this Agent. "
        "The host derives read scope and source hashes; a source UUID alone is not a citation.",
    )

    @model_validator(mode="after")
    def bounded_observation_count(self):
        if sum(
            len(item.observation_ids) for item in self.evidence_references
        ) > 64:
            raise ValueError("At most 64 report observations may be referenced")
        return self


class ReportSubmitArguments(ReportCheckArguments):
    title: StrictStr = Field(min_length=1, max_length=256)
    report_text: StrictStr = Field(min_length=1, max_length=64_000)


class SkillSearchArguments(NativeToolArguments):
    offset: int = Field(default=0, ge=0, description="Zero-based result offset.")
    limit: int = Field(default=20, ge=1, le=50, description="Page size from 1 through 50.")
    required_tags: list[StrictStr] | None = Field(
        default=None, description="Optional exact tags all returned skills must contain."
    )
    artifact_types: list[StrictStr] | None = Field(
        default=None, description="Optional exact Artifact types the skill must support."
    )
    include_lab: bool = True


SkillSectionWire = Literal[
    "workflow_outline",
    "collaboration_guidance",
    "execution_guidance",
    "parameter_guidance",
    "known_failure_modes",
    "debug_lessons",
    "validation_expectations",
    "limitations",
    "reusable_principles",
    "adaptation_points",
]


class SkillViewArguments(NativeToolArguments):
    authorization_id: UUID
    section: SkillSectionWire | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=4, ge=1, le=8)


class SkillProposeUseArguments(NativeToolArguments):
    skill_id: UUID
    version: int = Field(ge=1)
    mode: Literal["REUSE", "ADAPT", "REFERENCE"] = Field(
        description="Model-selected proposed use mode; user approval remains authoritative."
    )
    reason: StrictStr = Field(min_length=1, max_length=4_000)
    proposed_deviations: list[
        Annotated[StrictStr, Field(min_length=1, max_length=4_000)]
    ] | None = Field(default=None, max_length=100)


class EvidenceReadArguments(NativeToolArguments):
    observation_id: UUID = Field(description=(
        "Copy capability_invocation_id from an actual tool response; this is an "
        "observation identity, not an Artifact ID."
    ))
    offset: int = Field(default=0, ge=0, description=(
        "Zero-based Unicode character offset; continue with the returned next_offset."
    ))
    limit: int = Field(default=8_000, ge=1, le=8_000)


_HANDOFF_SELECTION_DESCRIPTION = (
    "Select distinct capability_invocation_id values from inner governed tool responses "
    "(content), not Artifact IDs, execution_id or outer tool_invocation_id values. "
    "Each selected observation must have been fully delivered to the current model "
    "invocation, directly or through all authorized evidence_read pages."
)


class PrepareHandoffArguments(NativeToolArguments):
    observation_ids: list[UUID] = Field(
        min_length=1, max_length=64, description=_HANDOFF_SELECTION_DESCRIPTION)
    summary: StrictStr = Field(min_length=1, max_length=8_000)
    limitations: list[Annotated[StrictStr, Field(max_length=2_000)]] = Field(
        default_factory=list, max_length=16)


CAPABILITY_ARGUMENT_MODELS: dict[str, type[NativeToolArguments]] = {
    "artifact_aggregate": ArtifactAggregateArguments,
    "artifact_list": ArtifactListArguments,
    "artifact_query": ArtifactQueryArguments,
    "environment_build": EnvironmentBuildArguments,
    "environment_list": EnvironmentListArguments,
    "execution_inspect": ExecutionInspectArguments,
    "execution_revise": ExecutionReviseArguments,
    "execution_submit": ExecutionSubmitArguments,
    "evidence_read": EvidenceReadArguments,
    "file_read": FileReadArguments,
    "literature_search": LiteratureSearchArguments,
    "memory_propose_update": MemoryProposeUpdateArguments,
    "memory_search": MemorySearchArguments,
    "memory_view": MemoryViewArguments,
    "prepare_handoff": PrepareHandoffArguments,
    "report_check": ReportCheckArguments,
    "report_read": ReportReadArguments,
    "report_submit": ReportSubmitArguments,
    "skill_propose_use": SkillProposeUseArguments,
    "skill_search": SkillSearchArguments,
    "skill_view": SkillViewArguments,
}

if frozenset(CAPABILITY_ARGUMENT_MODELS) != ALL_CAPABILITIES:
    raise RuntimeError("Native capability argument model catalog is incomplete")


class LabBioCapabilityHost(Protocol):
    """Host-owned business operation seam; authority never comes from arguments."""

    async def invoke(
        self,
        capability_name: str,
        arguments: NativeToolArguments,
    ) -> JsonValue | BaseModel: ...


class NativeRuntimeCapabilityHost:
    """Expose LabBio-owned operations behind the Native Kernel tool boundary."""

    def __init__(self, binding, services):
        from .tooling import LabBioRuntimeToolSet
        self._host = LabBioRuntimeToolSet(binding, services)

    def begin_model_invocation(self, identity: KernelIdentity) -> None:
        self._host.begin_model_invocation(identity.invocation_id)

    async def invoke(
        self,
        capability_name: str,
        arguments: NativeToolArguments,
    ) -> JsonValue | BaseModel:
        if capability_name not in CAPABILITY_ARGUMENT_MODELS:
            raise ValueError("Unknown native capability")
        method = getattr(self._host, capability_name)
        payload = arguments.model_dump(mode="json")
        return await method(**payload)

    def mark_feedback_delivered(self, results: tuple[KernelToolResult, ...]) -> None:
        # Kernel calls this only after the provider consumed the exact final
        # projection. A projection failure carries no released original body.
        self._host.mark_feedback_delivered(tuple(
            result.content for result in results
            if result.status == "SUCCEEDED" and isinstance(result.content, dict)
        ))

    def on_result_rejected(self, capability_name: str, value: object) -> None:
        """A receipt whose final kernel projection failed cannot be replayed."""
        if (capability_name in {"execution_submit", "execution_revise"}
                and isinstance(value, dict) and value.get("success") is True):
            self._host.mark_execution_handoff_unconfirmed(UUID(value["data"]["execution_id"]))

    @property
    def stop_reason(self) -> str | None:
        return self._host.stop_reason

    def evidence_items(self) -> tuple[CapabilityEvidenceItem, ...]:
        return self._host.evidence_items()

    @property
    def prepared_handoff(self):
        return self._host.prepared_handoff


@dataclass(frozen=True)
class NativeCapabilityBinding:
    """Trusted stage and actor binding absent from provider-visible schemas."""

    run_id: UUID
    stage_id: WorkflowStage
    invocation_id: UUID
    actor_profile_key: str
    actor_agent_name: str
    capability_allowlist: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.actor_profile_key or not self.actor_agent_name:
            raise ValueError("Native capability actor identity is required")
        if len(self.capability_allowlist) != len(set(self.capability_allowlist)):
            raise ValueError("Native capability allowlist entries must be unique")
        ceiling = frozenset(CAPABILITY_CEILINGS.get(self.stage_id, ()))
        if not frozenset(self.capability_allowlist).issubset(ceiling):
            raise ValueError("Stage capability allowlist exceeds its approved ceiling")


_CAPABILITY_DESCRIPTIONS = {
    name: f"Invoke the governed {name} capability with strict arguments."
    for name in ALL_CAPABILITIES
}
_CAPABILITY_DESCRIPTIONS["artifact_query"] = (
    "Request a governed Artifact view. For TOP_N pagination, follow next_offset; "
    "the returned suggested_next_limit is a policy-safe page size. An omitted "
    "limit continues to use the policy-bounded default page size."
)
_CAPABILITY_DESCRIPTIONS["evidence_read"] = (
    "Read an exact archived public tool response using its capability_invocation_id "
    "as observation_id, not an Artifact ID. Follow next_offset to finish its Unicode "
    "text pages. Partial pages do not grant full observation read credit."
)
_CAPABILITY_DESCRIPTIONS["prepare_handoff"] = (
    _HANDOFF_SELECTION_DESCRIPTION
    + " The Agent writes summary and limitations; the host copies the selected responses "
    "unchanged. An invoked replacement clears the previous selection even if rejected "
    "or oversized; no substitute evidence is selected by the host."
)
for _report_capability in ("report_check", "report_submit"):
    _CAPABILITY_DESCRIPTIONS[_report_capability] = (
        "Use evidence_references with artifact_id and observation_ids copied from "
        "successful artifact_query, artifact_aggregate, file_read or report_read "
        "capability_invocation_id values actually delivered in this invocation. "
        "The host preserves exact page, aggregate or summary scopes, not whole-table "
        "read claims or scientific validation. At least one actual observation is "
        "required; source identities or archived files alone are not read evidence."
    )
_CAPABILITY_DESCRIPTIONS["file_read"] = (
    "Read one authorized generated file page by Artifact UUID. For the same Artifact, "
    "copy the returned next_offset into the next call until it is null; requesting an "
    "already delivered page repeats that page rather than advancing the cursor. "
    "Reading a generated file does not prove a new execution capability was attempted."
)
_CAPABILITY_DESCRIPTIONS["execution_submit"] = (
    "Submit one complete program for governed execution. Exposure of this tool "
    "means it is currently authorized by the trusted host; no separate user authorization "
    "is required for in-scope technical use. When execution_capability requires "
    "queryable output, this tool produces the current EXECUTE receipt needed before "
    "a downstream transition; historical evidence cannot replace it and VALIDATE cannot create "
    "it. Explicitly select task "
    "inputs by UUID. LABBIO_INPUT_MANIFEST_PATH names a JSON object whose keys "
    "are the selected UUID strings and whose values are their absolute read-only "
    "container path strings; load it and access a path directly as "
    "manifest[str(artifact_uuid)] without probing for entries or alternate shapes. "
    "write every declared relative output beneath LABBIO_OUTPUT_DIR, and use the "
    "approved output contract and limits supplied in execution_capability. "
    "Technical execution or registration alone does not establish task completion."
)
_CAPABILITY_DESCRIPTIONS["execution_revise"] = (
    "Revise an exact prior execution program using its execution_id and script "
    "hash. Preserve or explicitly replace its selected input UUIDs and output "
    "declarations. LABBIO_INPUT_MANIFEST_PATH always names a direct JSON mapping "
    "from each selected UUID string to its absolute read-only container path "
    "string; it has no entries wrapper and does not require a diagnostic probe."
)


def build_labbio_tool_registry(
    host: LabBioCapabilityHost,
    execution_capability: RuntimeExecutionCapabilityView | None = None,
    *,
    on_result_rejected: Callable[[str, object], None] | None = None,
) -> ToolRegistry:
    """Register the complete capability catalog without a legacy runtime import."""

    registry = ToolRegistry()
    for capability_name in sorted(ALL_CAPABILITIES):
        async def invoke(
            arguments: NativeToolArguments,
            *,
            _capability_name: str = capability_name,
        ):
            return await host.invoke(_capability_name, arguments)

        arguments_model = CAPABILITY_ARGUMENT_MODELS[capability_name]
        if (capability_name == "execution_submit"
                and execution_capability is not None
                and execution_capability.minimum_queryable_output_count > 0):
            required_fields = {
                "requested_outputs": (
                    ExecutionRequestedOutputs,
                    Field(
                        description=(
                            "Declare enough outputs to include at least "
                            f"{execution_capability.minimum_queryable_output_count} "
                            "DERIVED output with an approved output contract. "
                            "Additional local-only outputs may use RAW and omit "
                            "output_contract_id; the host validates the required "
                            "queryable count before execution."
                        ),
                        min_length=execution_capability.minimum_queryable_output_count,
                    ),
                ),
            }
            if execution_capability.mountable_input_artifact_ids:
                required_fields["input_artifact_ids"] = (
                    tuple[UUID, ...],
                    Field(
                        min_length=1,
                        max_length=128,
                        description=(
                            "Required nonempty task input selection because this "
                            "data-bearing run requires queryable execution output. "
                            "Copy UUIDs from execution_capability."
                            "mountable_input_artifact_ids."
                        ),
                    ),
                )
            arguments_model = create_model(
                "OutputRequiredExecutionSubmitArguments",
                __base__=ExecutionSubmitArguments,
                **required_fields,
            )
        registry.register(
            name=capability_name,
            description=_CAPABILITY_DESCRIPTIONS[capability_name],
            arguments_model=arguments_model,
            handler=invoke,
            on_result_rejected=(
                (lambda value, name=capability_name: on_result_rejected(name, value))
                if on_result_rejected is not None
                and capability_name in {"execution_submit", "execution_revise"}
                else None
            ),
        )
    return registry


class NativeCapabilityDispatcher:
    """Bind native dispatch outcomes to LabBio item-level evidence authority."""

    def __init__(
        self,
        *,
        binding: NativeCapabilityBinding,
        registry: ToolRegistry,
        max_result_bytes: int = 60_000,
    ):
        missing = set(binding.capability_allowlist).difference(registry.names)
        if missing:
            raise ValueError("Native capability registry is missing allowed tools")
        self.binding = binding
        self.registry = registry
        self._dispatcher = ToolDispatcher(
            registry,
            ModelResultProjector(
                max_bytes=max_result_bytes,
                validate_value=lambda value: validate_model_visible_json(
                    value,
                    max_serialized_bytes=max_result_bytes,
                ),
            ),
        )
        self._evidence_items: list[CapabilityEvidenceItem] = []

    @property
    def specs(self):
        allowed = set(self.binding.capability_allowlist)
        return tuple(spec for spec in self.registry.specs if spec.name in allowed)

    async def dispatch_batch(
        self,
        calls: Iterable[ToolCall],
        *,
        finish_reason: FinishReason,
    ) -> tuple[DispatchOutcome, ...]:
        batch = tuple(calls)
        allowed = frozenset(self.binding.capability_allowlist)
        denied = next((call for call in batch if call.name not in allowed), None)
        if denied is not None:
            raise ToolBatchRejectedError(
                "CAPABILITY_NOT_ALLOWED",
                "The requested capability is not available in this stage",
                call_id=denied.call_id,
                tool_name=denied.name,
            )
        outcomes = await self._dispatcher.dispatch_batch(
            batch,
            finish_reason=finish_reason,
        )
        for outcome in outcomes:
            result = outcome.result
            authority = CAPABILITY_INFORMATION_AUTHORITY[result.name]
            if result.status == "SUCCEEDED":
                evidence = CapabilityEvidenceItem(
                    capability_invocation_id=result.tool_invocation_id,
                    actor_profile_key=self.binding.actor_profile_key,
                    actor_agent_name=self.binding.actor_agent_name,
                    capability_name=result.name,
                    information_authority=authority,
                    status=CapabilityEvidenceStatus.COMPLETED,
                    reference_ids=_reference_ids(result.content),
                    safe_result=result.content,
                )
            else:
                error = result.error
                if error is None:
                    raise RuntimeError("Failed native tool result lacks an error")
                evidence = CapabilityEvidenceItem(
                    capability_invocation_id=result.tool_invocation_id,
                    actor_profile_key=self.binding.actor_profile_key,
                    actor_agent_name=self.binding.actor_agent_name,
                    capability_name=result.name,
                    information_authority=authority,
                    status=CapabilityEvidenceStatus.FAILED,
                    error_code=error.code,
                    error_details=CapabilityErrorDetails(
                        safe_message=error.message,
                        retryable=error.retryable,
                    ),
                )
            self._evidence_items.append(evidence)
        return outcomes

    def evidence_items(self) -> tuple[CapabilityEvidenceItem, ...]:
        return tuple(self._evidence_items)

    def evidence_bundle(self) -> CapabilityEvidenceBundle:
        """Freeze collected item-level evidence under the trusted run binding."""

        return CapabilityEvidenceBundle(
            run_id=self.binding.run_id,
            stage_id=self.binding.stage_id,
            invocation_id=self.binding.invocation_id,
            items=self.evidence_items(),
        )


def _reference_ids(value: JsonValue | None) -> tuple[str, ...]:
    """Collect valid opaque UUIDs structurally without ranking or inference."""

    found: list[str] = []

    def visit(item: JsonValue) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if key.endswith("_id") and isinstance(child, str):
                    try:
                        UUID(child)
                    except ValueError:
                        continue
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

    if value is not None:
        visit(value)
    return tuple(dict.fromkeys(found))[:128]
