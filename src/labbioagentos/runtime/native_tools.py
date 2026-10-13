"""LabBio authority and evidence binding for the Native Kernel tool layer."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictStr

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
    ModelResultProjector,
    ToolBatchRejectedError,
    ToolCall,
    ToolDispatcher,
    ToolRegistry,
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
    limit: ArtifactQueryLimit | None = Field(
        default=None,
        description="Optional positive page size for TOP_N; null uses the policy default.",
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
    match_value: str | int | float | bool | None = None


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
    review: ExecutionReview | None = None


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
    review: ExecutionReview | None = None


class FileReadArguments(NativeToolArguments):
    artifact_id: UUID
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=4_000, ge=1, le=8_000)
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
    evidence_artifact_ids: list[UUID] = Field(max_length=256)


class ReportSubmitArguments(NativeToolArguments):
    title: StrictStr = Field(min_length=1, max_length=256)
    report_text: StrictStr = Field(min_length=1, max_length=64_000)
    evidence_artifact_ids: list[UUID] | None = Field(
        default=None,
        max_length=256,
    )


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


CAPABILITY_ARGUMENT_MODELS: dict[str, type[NativeToolArguments]] = {
    "artifact_aggregate": ArtifactAggregateArguments,
    "artifact_list": ArtifactListArguments,
    "artifact_query": ArtifactQueryArguments,
    "environment_build": EnvironmentBuildArguments,
    "environment_list": EnvironmentListArguments,
    "execution_inspect": ExecutionInspectArguments,
    "execution_revise": ExecutionReviseArguments,
    "execution_submit": ExecutionSubmitArguments,
    "file_read": FileReadArguments,
    "literature_search": LiteratureSearchArguments,
    "memory_propose_update": MemoryProposeUpdateArguments,
    "memory_search": MemorySearchArguments,
    "memory_view": MemoryViewArguments,
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

    def mark_feedback_delivered(self) -> None:
        self._host.mark_feedback_delivered()

    @property
    def stop_reason(self) -> str | None:
        return self._host.stop_reason

    def evidence_items(self) -> tuple[CapabilityEvidenceItem, ...]:
        return self._host.evidence_items()


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
_CAPABILITY_DESCRIPTIONS["execution_submit"] = (
    "Submit one complete program for governed execution. Explicitly select task "
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


def build_labbio_tool_registry(host: LabBioCapabilityHost) -> ToolRegistry:
    """Register the complete capability catalog without a legacy runtime import."""

    registry = ToolRegistry()
    for capability_name in sorted(ALL_CAPABILITIES):
        async def invoke(
            arguments: NativeToolArguments,
            *,
            _capability_name: str = capability_name,
        ):
            return await host.invoke(_capability_name, arguments)

        registry.register(
            name=capability_name,
            description=_CAPABILITY_DESCRIPTIONS[capability_name],
            arguments_model=CAPABILITY_ARGUMENT_MODELS[capability_name],
            handler=invoke,
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
