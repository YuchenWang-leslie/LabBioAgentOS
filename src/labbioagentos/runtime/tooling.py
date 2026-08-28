"""Pantheon ToolSet exposing only host-bound, governed LabBio capabilities."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable
from uuid import UUID, uuid4

from pantheon.toolset import ToolSet, tool
from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictStr, ValidationError

from labbioagentos.artifacts import (
    ArtifactConsumer,
    ArtifactExposureService,
    ArtifactQuery,
    ArtifactSchema,
    ArtifactStore,
    ArtifactViewType,
)
from labbioagentos.contracts import WorkflowStage
from labbioagentos.execution import (
    ExecutionPlanDraft,
    ExecutionSubmissionService,
)
from labbioagentos.governance import AuthorizationDenied, Principal, WorkspaceContext
from labbioagentos.memory import (
    MemoryGovernanceService,
    MemoryKind,
    MemoryScope,
    MemoryUpdateProposal,
)
from labbioagentos.skills import (
    GoldSkillService,
    SkillSearchContext,
    SkillUseMode,
    SkillUseProposal,
)
from labbioagentos.trace import RunTraceRecorder, TraceEventType

from .contracts import (
    CapabilityEvidenceItem,
    CapabilityEvidenceStatus,
)
from .reporting import ReportSubmissionService


CAPABILITY_CEILINGS: dict[WorkflowStage, tuple[str, ...]] = {
    WorkflowStage.INTAKE: ("artifact_list", "artifact_query"),
    WorkflowStage.UNDERSTAND: (
        "artifact_list", "artifact_query", "skill_search", "skill_view",
        "memory_search", "memory_view",
    ),
    WorkflowStage.PLAN: (
        "artifact_query", "skill_search", "skill_view", "skill_propose_use",
        "memory_search", "memory_view",
    ),
    WorkflowStage.PREFLIGHT: ("artifact_query",),
    WorkflowStage.EXECUTE: ("artifact_query", "execution_submit"),
    WorkflowStage.VALIDATE: ("artifact_query",),
    WorkflowStage.INTERPRET: ("artifact_query",),
    WorkflowStage.REPORT: ("artifact_query", "report_submit"),
    WorkflowStage.LEARN: (
        "skill_search", "skill_view", "memory_search", "memory_view",
        "memory_propose_update",
    ),
}
ALL_CAPABILITIES = frozenset(
    capability for values in CAPABILITY_CEILINGS.values() for capability in values
)


class ToolError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    error_code: StrictStr = Field(min_length=1, max_length=128)
    safe_message: StrictStr = Field(min_length=1, max_length=1000)
    correlation_id: UUID = Field(default_factory=uuid4)
    retryable: bool = False


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    success: bool
    data: JsonValue | None = None
    error: ToolError | None = None


class ArtifactListItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    artifact_id: UUID
    artifact_type: StrictStr
    exposure_class: StrictStr
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
    skill_id: UUID
    version: int
    name: StrictStr
    description: StrictStr
    scope: StrictStr
    tags: tuple[StrictStr, ...] = ()
    artifact_types: tuple[StrictStr, ...] = ()


class SkillDetailView(SkillCandidateView):
    source_run_id: UUID
    parent_skill_id: UUID | None = None
    parent_version: int | None = None
    applicability: StrictStr
    workflow_outline: tuple[StrictStr, ...]
    collaboration_guidance: tuple[StrictStr, ...] = ()
    execution_guidance: tuple[StrictStr, ...] = ()
    validation_expectations: tuple[StrictStr, ...] = ()
    limitations: tuple[StrictStr, ...] = ()


class MemoryCandidateView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    memory_id: UUID
    version: int
    scope: StrictStr
    kind: StrictStr
    preview: StrictStr


class MemoryDetailView(MemoryCandidateView):
    content: StrictStr
    previous_version: int | None = None
    evidence_run_ids: tuple[UUID, ...] = ()
    evidence_artifact_ids: tuple[UUID, ...] = ()


@dataclass(frozen=True)
class RuntimeCapabilityContext:
    """Trusted host binding; this object never appears in a tool signature."""

    principal: Principal
    workspace: WorkspaceContext
    run_id: UUID
    stage_id: WorkflowStage
    invocation_id: UUID
    capability_allowlist: tuple[str, ...]
    consumer: ArtifactConsumer = ArtifactConsumer.REMOTE_LLM

    @classmethod
    def from_stage_spec(
        cls,
        spec,
        *,
        principal: Principal,
        workspace: WorkspaceContext,
        run_id: UUID,
        invocation_id: UUID,
    ) -> "RuntimeCapabilityContext":
        """Bind the exact StageRuntimeSpec allowlist outside model input."""

        return cls(
            principal=principal,
            workspace=workspace,
            run_id=run_id,
            stage_id=spec.stage_id,
            invocation_id=invocation_id,
            capability_allowlist=tuple(spec.capability_allowlist),
        )

    def __post_init__(self) -> None:
        ceiling = frozenset(CAPABILITY_CEILINGS.get(self.stage_id, ()))
        requested = frozenset(self.capability_allowlist)
        if not requested.issubset(ceiling):
            raise ValueError("Stage capability allowlist exceeds its approved ceiling")
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


class LabBioRuntimeToolSet(ToolSet):
    """One filtered ToolSet; allowed tools are available but never auto-invoked."""

    def __init__(self, binding: RuntimeCapabilityContext, services: RuntimeCapabilityServices):
        self.binding = binding
        self.services = services
        self._evidence_items: list[CapabilityEvidenceItem] = []
        super().__init__(name=f"labbio-{binding.stage_id.value.lower()}")
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
    ) -> dict[str, Any]:
        capability_invocation_id = uuid4()
        trace_event_ids: list[UUID] = []
        try:
            self._guard(capability)
            bounded_ids = self._bounded_identifiers(request_ids or {})
            event = self._emit(
                TraceEventType.CAPABILITY_INVOKED,
                capability,
                "STARTED",
                {
                    "capability_invocation_id": str(capability_invocation_id),
                    **bounded_ids,
                },
            )
            if event is not None:
                trace_event_ids.append(event.event_id)
            value = operation()
            if hasattr(value, "__await__"):
                value = await value
            if isinstance(value, BaseModel):
                value = value.model_dump(mode="json", by_alias=True)
            elif isinstance(value, (tuple, list)) and all(isinstance(item, BaseModel) for item in value):
                value = [item.model_dump(mode="json", by_alias=True) for item in value]
            result = ToolResult(success=True, data=value)
        except Exception as exc:
            error = self._safe_error(exc)
            event = self._emit(
                TraceEventType.CAPABILITY_FAILED,
                capability,
                "FAILED",
                {
                    "capability_invocation_id": str(capability_invocation_id),
                    "error_code": error.error_code,
                    "correlation_id": str(error.correlation_id),
                },
            )
            if event is not None:
                trace_event_ids.append(event.event_id)
            self._evidence_items.append(
                CapabilityEvidenceItem(
                    capability_invocation_id=capability_invocation_id,
                    capability_name=capability,
                    status=CapabilityEvidenceStatus.FAILED,
                    trace_event_ids=tuple(trace_event_ids),
                    error_code=error.error_code,
                    correlation_id=error.correlation_id,
                )
            )
            return ToolResult(success=False, error=error).model_dump(mode="json")
        event = self._emit(
            TraceEventType.CAPABILITY_COMPLETED,
            capability,
            "COMPLETED",
            {
                "capability_invocation_id": str(capability_invocation_id),
                **self._bounded_identifiers(request_ids or {}),
            },
        )
        if event is not None:
            trace_event_ids.append(event.event_id)
        self._evidence_items.append(
            CapabilityEvidenceItem(
                capability_invocation_id=capability_invocation_id,
                capability_name=capability,
                status=CapabilityEvidenceStatus.COMPLETED,
                trace_event_ids=tuple(trace_event_ids),
                reference_ids=self._reference_ids(value),
                safe_result=value,
            )
        )
        return result.model_dump(mode="json", by_alias=True)

    def evidence_items(self) -> tuple[CapabilityEvidenceItem, ...]:
        """Return immutable bounded outcomes accumulated by model-selected calls."""

        return tuple(self._evidence_items)

    @tool
    async def artifact_list(self, offset: int = 0, limit: int = 20) -> dict:
        """List bounded metadata for artifacts in the bound workspace."""
        return await self._call("artifact_list", lambda: self._artifact_list(offset, limit))

    @tool
    async def artifact_query(
        self, artifact_id: str, view_type: str, limit: int | None = None
    ) -> dict:
        """Request one policy-controlled artifact view by UUID."""
        return await self._call(
            "artifact_query",
            lambda: self._artifact_query(artifact_id, view_type, limit),
            request_ids={"artifact_id": artifact_id},
        )

    @tool
    async def execution_submit(self, draft: dict) -> dict:
        """Submit an untrusted execution draft through the governed executor bridge."""
        async def submit():
            service = self._required(self.services.execution_submission, "execution")
            return await service.submit(
                ExecutionPlanDraft.model_validate(draft),
                principal=self.binding.principal,
                workspace=self.binding.workspace,
                run_id=self.binding.run_id,
                stage_id=self.binding.stage_id,
                invocation_id=self.binding.invocation_id,
            )
        return await self._call("execution_submit", submit)

    @tool
    async def skill_search(
        self,
        query_text: str | None = None,
        required_tags: list[str] | None = None,
        artifact_types: list[str] | None = None,
        include_lab: bool = True,
        limit: int = 20,
    ) -> dict:
        """Return governed Gold Skill candidates without a score or use decision."""
        return await self._call(
            "skill_search",
            lambda: self._skill_search(
                query_text, required_tags or [], artifact_types or [], include_lab, limit
            ),
        )

    @tool
    async def skill_view(self, skill_id: str, version: int) -> dict:
        """View one authorized immutable procedural-memory version."""
        return await self._call(
            "skill_view",
            lambda: self._skill_view(UUID(skill_id), version),
            request_ids={"skill_id": skill_id, "skill_version": version},
        )

    @tool
    async def skill_propose_use(
        self,
        skill_id: str,
        version: int,
        mode: str,
        reason: str,
        proposed_deviations: list[str] | None = None,
    ) -> dict:
        """Create a pending Skill-use proposal; this cannot approve or execute it."""
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
        query_text: str | None = None,
        kind: str | None = None,
        scope: str | None = None,
        limit: int = 20,
    ) -> dict:
        """Return bounded authorized Memory candidates."""
        return await self._call(
            "memory_search", lambda: self._memory_search(query_text, kind, scope, limit)
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
    async def memory_propose_update(self, draft: dict) -> dict:
        """Create a pending governed Memory proposal; it cannot approve it."""
        return await self._call("memory_propose_update", lambda: self._memory_proposal(draft))

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
        identifier = UUID(artifact_id)
        ref = self.services.artifact_exposure.artifact_ref(
            identifier, principal=self.binding.principal
        )
        if (
            ref.project_id != self.binding.workspace.project_id
            or ref.lab_id != self.binding.workspace.lab_id
        ):
            raise AuthorizationDenied("Artifact is outside the bound workspace")
        return self.services.artifact_exposure.artifact_query(
            identifier,
            ArtifactQuery(view_type=ArtifactViewType(view_type), limit=limit),
            self.binding.consumer,
            principal=self.binding.principal,
        )

    def _skill_search(self, query_text, tags, artifact_types, include_lab, limit):
        if limit < 1 or limit > 50:
            raise ValueError("Skill result limit must be between 1 and 50")
        service = self._required(self.services.skill_service, "skill")
        skills = service.search(
            SkillSearchContext(
                user_id=self.binding.principal.user_id,
                project_id=self.binding.workspace.project_id,
                lab_id=self.binding.workspace.lab_id,
                include_lab=include_lab,
                query_text=query_text,
                required_tags=frozenset(tags),
                artifact_types=frozenset(artifact_types),
            ),
            principal=self.binding.principal,
        )
        return [self._skill_candidate(item) for item in skills[:limit]]

    def _skill_view(self, skill_id: UUID, version: int):
        skill = self._required(self.services.skill_service, "skill").get_gold(
            skill_id, version, principal=self.binding.principal
        )
        return SkillDetailView(
            **self._skill_candidate(skill).model_dump(),
            source_run_id=skill.source_run_id,
            parent_skill_id=skill.parent_skill_id,
            parent_version=skill.parent_version,
            applicability=skill.procedure.applicability[:4000],
            workflow_outline=tuple(skill.procedure.workflow_outline[:32]),
            collaboration_guidance=tuple(skill.procedure.agent_collaboration_guidance[:16]),
            execution_guidance=tuple(skill.procedure.execution_guidance[:16]),
            validation_expectations=tuple(skill.procedure.validation_expectations[:16]),
            limitations=tuple(skill.procedure.known_limitations[:16]),
        )

    def _skill_propose_use(self, skill_id, version, mode, reason, deviations):
        if len(deviations) > 32:
            raise ValueError("Too many proposed deviations")
        proposal = SkillUseProposal(
            run_id=self.binding.run_id,
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
            "status": "USER_APPROVAL_REQUIRED",
        }

    def _memory_search(self, query_text, kind, scope, limit):
        if limit < 1 or limit > 50:
            raise ValueError("Memory result limit must be between 1 and 50")
        entries = self._required(self.services.memory_service, "memory").list_visible(
            self.binding.principal
        )
        filtered = []
        for entry in entries:
            if entry.scope is MemoryScope.PROJECT and entry.project_id != self.binding.workspace.project_id:
                continue
            if kind and entry.kind is not MemoryKind(kind):
                continue
            if scope and entry.scope is not MemoryScope(scope):
                continue
            if query_text and query_text.casefold() not in entry.content.casefold():
                continue
            filtered.append(
                MemoryCandidateView(
                    memory_id=entry.memory_id,
                    version=entry.version,
                    scope=entry.scope.value,
                    kind=entry.kind.value,
                    preview=entry.content[:500],
                )
            )
        return filtered[:limit]

    def _memory_view(self, memory_id, version):
        entry = self._required(self.services.memory_service, "memory").get(
            self.binding.principal, memory_id, version
        )
        if entry.scope is MemoryScope.PROJECT and entry.project_id != self.binding.workspace.project_id:
            raise AuthorizationDenied("Memory is outside the bound workspace")
        return MemoryDetailView(
            memory_id=entry.memory_id,
            version=entry.version,
            scope=entry.scope.value,
            kind=entry.kind.value,
            preview=entry.content[:500],
            content=entry.content[:4000],
            previous_version=entry.previous_version,
            evidence_run_ids=entry.evidence_run_ids[:64],
            evidence_artifact_ids=entry.evidence_artifact_ids[:64],
        )

    def _memory_proposal(self, draft: dict):
        allowed = {
            "target_scope", "target_memory_id", "target_version", "proposed_kind",
            "proposed_content", "reason", "evidence_run_ids", "evidence_artifact_ids",
        }
        if set(draft) - allowed:
            raise ValueError("Memory proposal contains trusted or unsupported fields")
        scope = MemoryScope(draft["target_scope"])
        proposal = MemoryUpdateProposal(
            target_scope=scope,
            owner_user_id=(self.binding.principal.user_id if scope is MemoryScope.PERSONAL else None),
            project_id=(self.binding.workspace.project_id if scope is MemoryScope.PROJECT else None),
            lab_id=self.binding.workspace.lab_id,
            target_memory_id=draft.get("target_memory_id"),
            target_version=draft.get("target_version"),
            proposed_kind=MemoryKind(draft["proposed_kind"]),
            proposed_content=draft["proposed_content"],
            reason=draft["reason"],
            evidence_run_ids=tuple(draft.get("evidence_run_ids", ())),
            evidence_artifact_ids=tuple(draft.get("evidence_artifact_ids", ())),
            proposing_invocation_id=self.binding.invocation_id,
            source_run_id=self.binding.run_id,
        )
        self._required(self.services.memory_service, "memory").submit_proposal(
            self.binding.principal, proposal
        )
        return {
            "proposal_id": str(proposal.proposal_id),
            "approval_gate_id": proposal.approval_gate_id,
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
        )

    @staticmethod
    def _required(value, label: str):
        if value is None:
            raise RuntimeError(f"The {label} capability service is not configured")
        return value

    @staticmethod
    def _safe_error(exc: Exception) -> ToolError:
        if isinstance(exc, AuthorizationDenied):
            return ToolError(error_code="AUTHORIZATION_DENIED", safe_message="Access denied by policy.")
        if isinstance(exc, (ValueError, ValidationError)):
            return ToolError(error_code="INVALID_REQUEST", safe_message="The capability request is invalid.")
        return ToolError(error_code="CAPABILITY_FAILED", safe_message="The capability could not complete.")

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
                status=status,
                payload={"capability": capability, **(payload or {})},
            )
        return None
