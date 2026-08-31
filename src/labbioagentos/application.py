"""Reusable application composition and run-lifecycle boundary."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictStr,
    model_validator,
)

from .artifacts import (
    ArtifactExposureClass,
    ArtifactExposureService,
    ArtifactRepresentation,
    ArtifactSchema,
    ExposurePolicy,
    LocalArtifactStore,
)
from .bioformats import (
    BioFormatInspectionError,
    BioFormatInspectionRegistry,
    BioFormatInspector,
    H5ADInspectionError,
    H5ADInspectionPolicy,
    H5ADInspector,
)
from .contracts import (
    GateUserDecision,
    RunStatus,
    WorkflowDefinition,
    WorkflowRun,
    WorkflowStage,
)
from .execution import (
    ApprovedImage,
    ApprovedImageRegistry,
    ArtifactRegistrationPolicy,
    DockerCommandBuilder,
    DockerExecutor,
    DockerProcessRunner,
    ExecutionPolicy,
    ExecutionPreflightRequest,
    ExecutionPreflightService,
    ExecutionRuntime,
    ExecutionSubmissionService,
    ExecutionWorkspaceManager,
    MountResolver,
    OutputCollector,
    PreflightInputRequirement,
    RequestedResources,
    StructuredOutputContract,
)
from .governance import (
    AccessAction,
    AccessService,
    AuthorizationDenied,
    AuthorizationPolicy,
    InMemoryProjectStore,
    Principal,
    Project,
    WorkspaceContext,
)
from .memory import MemoryGovernanceService
from .runtime import (
    PantheonRuntimeFactory,
    PerInvocationPantheonStageInvoker,
    ReportSubmissionService,
    RuntimeCapabilityServices,
    RuntimeInputBody,
    RuntimeProfileCatalog,
    RuntimeReference,
    RuntimeReferenceKind,
    RuntimeStageAssemblySpec,
    StageRuntimeRegistry,
    StageRuntimeSpec,
)
from .runtime.assembly import BoundaryObserver, PluginFactory
from .runtime.coordinator import RuntimeCoordinatorService
from .skills import GoldSkillService
from .trace import InMemoryTraceSink, RunTraceRecorder, TraceEvent, TraceSink
from .workflow import WorkflowEngine, runtime_workflow_definition


class ApplicationConfigurationError(ValueError):
    """Trusted application configuration is internally inconsistent."""


class ApplicationInputError(ValueError):
    """A trusted host input cannot be admitted to the Artifact boundary."""


class ApplicationRunNotFoundError(LookupError):
    """The application does not own the requested run."""


class ApplicationRunStateError(RuntimeError):
    """The requested application lifecycle operation is invalid for the run."""


class ApplicationArtifactReference(BaseModel):
    """Safe result of trusted Artifact ingestion; it contains no locator/content."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    artifact_id: UUID
    artifact_type: StrictStr = Field(min_length=1, max_length=256)
    exposure_class: ArtifactExposureClass


class ApplicationH5ADInspectionArtifacts(BaseModel):
    """Safe references created by trusted local h5ad inspection."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_artifact_id: UUID
    structural_artifact: ApplicationArtifactReference
    aggregate_artifact: ApplicationArtifactReference


class ApplicationBioFormatInspectionArtifacts(BaseModel):
    """Format-neutral safe references created by trusted local inspection."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_artifact_id: UUID
    format_key: StrictStr = Field(min_length=1, max_length=128)
    artifacts: tuple[ApplicationArtifactReference, ...] = Field(
        min_length=1, max_length=8
    )


class ApplicationExecutionProfile(BaseModel):
    """Trusted script-free preflight configuration selected by the host."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    runtime: ExecutionRuntime = ExecutionRuntime.PYTHON
    image_key: StrictStr = Field(min_length=1, max_length=128)
    resources: RequestedResources = Field(default_factory=RequestedResources)
    network_required: bool = False
    output_contract_ids: tuple[StrictStr, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def reject_duplicate_contracts(self) -> "ApplicationExecutionProfile":
        if len(set(self.output_contract_ids)) != len(self.output_contract_ids):
            raise ValueError("Execution profile output contract IDs must be unique")
        return self


class ApplicationRunRequest(BaseModel):
    """Bounded application request; authority remains in trusted typed objects."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    task_text: StrictStr = Field(min_length=1, max_length=32_000)
    principal: Principal
    workspace: WorkspaceContext
    input_artifact_ids: tuple[UUID, ...] = Field(default=(), max_length=128)
    context_artifact_ids: tuple[UUID, ...] = Field(default=(), max_length=128)
    safe_domain_references: tuple[RuntimeReference, ...] = Field(
        default=(), max_length=64
    )

    @model_validator(mode="after")
    def validate_references(self) -> "ApplicationRunRequest":
        artifact_ids = (*self.input_artifact_ids, *self.context_artifact_ids)
        if len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError("Run request Artifact IDs must be unique")
        allowed_kinds = {
            RuntimeReferenceKind.DOMAIN_PROPOSAL,
            RuntimeReferenceKind.INSTRUCTION,
            RuntimeReferenceKind.OTHER,
        }
        if any(item.kind not in allowed_kinds for item in self.safe_domain_references):
            raise ValueError(
                "Safe domain references cannot inject Artifact, result, execution, "
                "report, Memory, or Gold Skill authority"
            )
        return self


class ApplicationRunHandle(BaseModel):
    """Opaque handle returned instead of a mutable WorkflowRun."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_id: UUID


class ApplicationPendingUserGate(BaseModel):
    """Bounded user-gate snapshot suitable for a future API/CLI."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    gate_id: StrictStr = Field(min_length=1, max_length=256)
    prompt: StrictStr = Field(min_length=1, max_length=4_000)
    source_stage: WorkflowStage
    domain_reference_id: StrictStr | None = Field(
        default=None, min_length=1, max_length=256
    )


class ApplicationRunResult(BaseModel):
    """Model-safe application outcome with IDs and bounded status only."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_id: UUID
    status: RunStatus
    final_stage: WorkflowStage | None = None
    report_artifact_ids: tuple[UUID, ...] = Field(default=(), max_length=128)
    derived_artifact_ids: tuple[UUID, ...] = Field(default=(), max_length=256)
    issue_codes: tuple[StrictStr, ...] = Field(default=(), max_length=32)
    pending_user_gate: ApplicationPendingUserGate | None = None
    trace_run_id: UUID


@dataclass(frozen=True)
class ApplicationStagePlugin:
    """Trusted exact-stage plugin factory; it performs no task-content routing."""

    stage_id: WorkflowStage
    factory: PluginFactory

    def __post_init__(self) -> None:
        if not callable(self.factory):
            raise TypeError("Application stage plugin factory must be callable")


@dataclass(frozen=True)
class ApplicationRuntimeConfiguration:
    """Trusted process configuration used to assemble one application instance."""

    artifact_root: str | Path
    execution_workspace_root: str | Path
    profile_catalog: RuntimeProfileCatalog
    stage_assemblies: tuple[RuntimeStageAssemblySpec, ...]
    projects: tuple[Project, ...] = ()
    allowed_input_roots: tuple[str | Path, ...] = ()
    approved_images: tuple[ApprovedImage, ...] = ()
    output_contracts: tuple[StructuredOutputContract, ...] = ()
    execution_policy: ExecutionPolicy = field(default_factory=ExecutionPolicy)
    execution_profile: ApplicationExecutionProfile | None = None
    workflow_definition: WorkflowDefinition = field(
        default_factory=runtime_workflow_definition
    )
    authorization_policy: AuthorizationPolicy = field(
        default_factory=AuthorizationPolicy
    )
    exposure_policy: ExposurePolicy = field(default_factory=ExposurePolicy)
    stage_plugins: tuple[ApplicationStagePlugin, ...] = ()
    trace_sink: TraceSink | None = None
    process_runner: DockerProcessRunner | None = None
    skill_service: GoldSkillService | None = None
    memory_service: MemoryGovernanceService | None = None
    h5ad_inspection_policy: H5ADInspectionPolicy = field(
        default_factory=H5ADInspectionPolicy
    )
    bioformat_inspectors: tuple[BioFormatInspector, ...] = ()
    boundary_observer: BoundaryObserver | None = None
    retry_limit: int = 1
    max_stage_invocations: int = 64

    def __post_init__(self) -> None:
        if self.retry_limit < 0:
            raise ApplicationConfigurationError("retry_limit cannot be negative")
        if not 9 <= self.max_stage_invocations <= 512:
            raise ApplicationConfigurationError(
                "max_stage_invocations must be between 9 and 512"
            )
        stage_ids = tuple(item.stage_id for item in self.stage_assemblies)
        if len(set(stage_ids)) != len(stage_ids):
            raise ApplicationConfigurationError(
                "Application stage assemblies must have unique stage IDs"
            )
        configured_main = frozenset(stage_ids)
        required_main = self.workflow_definition.nodes.difference(
            {
                WorkflowStage.USER_GATE,
                WorkflowStage.SEARCH,
                WorkflowStage.DEBUG,
            }
        )
        if configured_main != required_main:
            raise ApplicationConfigurationError(
                "Application stage assemblies must exactly cover configured main stages"
            )
        plugin_stages = tuple(item.stage_id for item in self.stage_plugins)
        if len(set(plugin_stages)) != len(plugin_stages):
            raise ApplicationConfigurationError(
                "Application stage plugin bindings must be unique"
            )
        if not set(plugin_stages).issubset(configured_main):
            raise ApplicationConfigurationError(
                "Application stage plugin bindings require a configured stage"
            )


@dataclass(frozen=True)
class _ApplicationRunSession:
    request: ApplicationRunRequest
    run: WorkflowRun
    artifact_references: tuple[RuntimeReference, ...]
    coordinator: RuntimeCoordinatorService


class LabBioApplication:
    """Compose infrastructure once and drive scoped runs through existing owners."""

    def __init__(self, configuration: ApplicationRuntimeConfiguration):
        self.configuration = configuration
        self.trace_sink = configuration.trace_sink or InMemoryTraceSink()
        self.trace_recorder = RunTraceRecorder(self.trace_sink)

        self.project_store = InMemoryProjectStore()
        for project in configuration.projects:
            self.project_store.register(project)
        self.access_service = AccessService(
            self.project_store,
            configuration.authorization_policy,
            trace_recorder=self.trace_recorder,
        )
        self.artifact_store = LocalArtifactStore(
            configuration.artifact_root,
            trace_recorder=self.trace_recorder,
        )
        self.artifact_exposure = ArtifactExposureService(
            self.artifact_store,
            configuration.exposure_policy,
            access_service=self.access_service,
            trace_recorder=self.trace_recorder,
        )
        self.registration_policy = ArtifactRegistrationPolicy(
            configuration.output_contracts
        )
        self.image_registry = ApprovedImageRegistry(configuration.approved_images)
        self.execution_policy = configuration.execution_policy
        self.docker_executor = DockerExecutor(
            store=self.artifact_store,
            image_registry=self.image_registry,
            execution_policy=self.execution_policy,
            mount_resolver=MountResolver(
                self.artifact_store,
                approved_input_roots=(self.artifact_store.root,),
            ),
            workspace_manager=ExecutionWorkspaceManager(
                configuration.execution_workspace_root
            ),
            output_collector=OutputCollector(
                self.artifact_store,
                self.registration_policy,
                trace_recorder=self.trace_recorder,
            ),
            process_runner=configuration.process_runner,
            command_builder=DockerCommandBuilder(),
            trace_recorder=self.trace_recorder,
        )
        self.execution_submission = ExecutionSubmissionService(
            artifact_store=self.artifact_store,
            access_service=self.access_service,
            executor=self.docker_executor,
            trace_recorder=self.trace_recorder,
        )
        self.execution_preflight = ExecutionPreflightService(
            artifact_store=self.artifact_store,
            access_service=self.access_service,
            image_registry=self.image_registry,
            execution_policy=self.execution_policy,
            registration_policy=self.registration_policy,
            trace_recorder=self.trace_recorder,
        )
        self.report_submission = ReportSubmissionService(
            self.artifact_store,
            self.access_service,
            trace_recorder=self.trace_recorder,
        )
        self.capability_services = RuntimeCapabilityServices(
            artifact_store=self.artifact_store,
            artifact_exposure=self.artifact_exposure,
            execution_submission=self.execution_submission,
            skill_service=configuration.skill_service,
            memory_service=configuration.memory_service,
            report_submission=self.report_submission,
            trace_recorder=self.trace_recorder,
        )

        self.runtime_factory = PantheonRuntimeFactory(configuration.profile_catalog)
        self._plugin_factories = {
            binding.stage_id: binding.factory
            for binding in configuration.stage_plugins
        }
        self.workflow_engine = WorkflowEngine(
            configuration.workflow_definition,
            trace_recorder=self.trace_recorder,
        )
        self._sessions: dict[UUID, _ApplicationRunSession] = {}
        self._allowed_input_roots = self._resolve_input_roots(
            configuration.allowed_input_roots
        )
        self.h5ad_inspector = H5ADInspector(configuration.h5ad_inspection_policy)
        try:
            self.bioformat_inspections = BioFormatInspectionRegistry(
                (self.h5ad_inspector, *configuration.bioformat_inspectors)
            )
        except BioFormatInspectionError as exc:
            raise ApplicationConfigurationError(
                "Bioformat inspection configuration is invalid"
            ) from exc

    def register_input_file(
        self,
        source: str | Path,
        *,
        principal: Principal,
        workspace: WorkspaceContext,
        artifact_type: str,
        schema: ArtifactSchema | None = None,
        metadata: dict[str, JsonValue] | None = None,
    ) -> ApplicationArtifactReference:
        """Admit one trusted local file as RAW after root and scope validation."""

        self._require_trusted_scope(principal, workspace)
        source_path = self._validate_input_path(source)
        ref = self.artifact_store.register_file(
            source_path,
            artifact_type=artifact_type,
            exposure_class=ArtifactExposureClass.RAW,
            representation=ArtifactRepresentation(),
            owner_user_id=principal.user_id,
            project_id=workspace.project_id,
            lab_id=workspace.lab_id,
            schema=schema,
            metadata=metadata,
        )
        return self._safe_artifact_reference(ref)

    def register_structural_artifact(
        self,
        *,
        principal: Principal,
        workspace: WorkspaceContext,
        artifact_type: str,
        schema: ArtifactSchema,
        metadata: dict[str, JsonValue] | None = None,
    ) -> ApplicationArtifactReference:
        """Register caller-produced structural metadata without inspecting RAW data."""

        self._require_trusted_scope(principal, workspace)
        ref = self.artifact_store.register(
            artifact_type=artifact_type,
            exposure_class=ArtifactExposureClass.STRUCTURAL,
            representation=ArtifactRepresentation(),
            owner_user_id=principal.user_id,
            project_id=workspace.project_id,
            lab_id=workspace.lab_id,
            schema=schema,
            metadata=metadata,
        )
        return self._safe_artifact_reference(ref)

    def inspect_h5ad(
        self,
        source_artifact_id: UUID,
        *,
        principal: Principal,
        workspace: WorkspaceContext,
    ) -> ApplicationH5ADInspectionArtifacts:
        """H5AD application adapter over the generic trusted inspection boundary."""

        try:
            inspected = self.inspect_bioformat(
                source_artifact_id,
                format_key="h5ad",
                principal=principal,
                workspace=workspace,
            )
        except H5ADInspectionError:
            raise
        except BioFormatInspectionError as exc:
            raise H5ADInspectionError(str(exc)) from exc
        structural = tuple(
            item
            for item in inspected.artifacts
            if item.exposure_class is ArtifactExposureClass.STRUCTURAL
        )
        aggregate = tuple(
            item
            for item in inspected.artifacts
            if item.exposure_class is ArtifactExposureClass.AGGREGATE
        )
        if len(structural) != 1 or len(aggregate) != 1:
            raise H5ADInspectionError(
                "H5AD inspector did not return its required safe Artifact classes"
            )
        return ApplicationH5ADInspectionArtifacts(
            source_artifact_id=inspected.source_artifact_id,
            structural_artifact=structural[0],
            aggregate_artifact=aggregate[0],
        )

    def inspect_bioformat(
        self,
        source_artifact_id: UUID,
        *,
        format_key: str,
        principal: Principal,
        workspace: WorkspaceContext,
    ) -> ApplicationBioFormatInspectionArtifacts:
        """Inspect one authorized RAW Artifact through a selected trusted inspector."""

        self._require_trusted_scope(principal, workspace)
        source_ref = self.artifact_store.get_ref(source_artifact_id)
        if (
            source_ref.project_id != workspace.project_id
            or source_ref.lab_id != workspace.lab_id
        ):
            raise AuthorizationDenied(
                "Inspection Artifact is outside the bound workspace"
            )
        self.access_service.require_artifact(
            principal, source_ref, AccessAction.READ_ARTIFACT
        )
        if source_ref.exposure_class is not ArtifactExposureClass.RAW:
            raise BioFormatInspectionError("Bioformat inspection source must be RAW")
        source_path = self._trusted_artifact_path(source_ref.storage_locator)
        inspection = self.bioformat_inspections.inspect(format_key, source_path)
        lineage = {
            "format": inspection.format_key,
            "inspection_schema_version": inspection.inspection_schema_version,
            "source_artifact_id": str(source_ref.artifact_id),
        }
        refs = tuple(
            self.artifact_store.register(
                artifact_type=spec.artifact_type,
                exposure_class=spec.exposure_class,
                representation=spec.representation,
                owner_user_id=source_ref.owner_user_id,
                project_id=source_ref.project_id,
                lab_id=source_ref.lab_id,
                schema=spec.artifact_schema,
                metadata=lineage,
            )
            for spec in inspection.artifacts
        )
        return ApplicationBioFormatInspectionArtifacts(
            source_artifact_id=source_ref.artifact_id,
            format_key=inspection.format_key,
            artifacts=tuple(self._safe_artifact_reference(ref) for ref in refs),
        )

    def create_run(self, request: ApplicationRunRequest) -> ApplicationRunHandle:
        """Validate scope/references and create, but do not expose, a WorkflowRun."""

        self._require_trusted_scope(request.principal, request.workspace)
        references = tuple(
            self._authorized_runtime_reference(
                artifact_id,
                principal=request.principal,
                workspace=request.workspace,
            )
            for artifact_id in (
                *request.input_artifact_ids,
                *request.context_artifact_ids,
            )
        )
        coordinator = self._build_coordinator(
            principal=request.principal,
            workspace=request.workspace,
        )
        run = coordinator.create_run(
            principal=request.principal,
            workspace=request.workspace,
            access_service=self.access_service,
            retry_limit=self.configuration.retry_limit,
        )
        self._sessions[run.run_id] = _ApplicationRunSession(
            request=request,
            run=run,
            artifact_references=references,
            coordinator=coordinator,
        )
        return ApplicationRunHandle(run_id=run.run_id)

    async def run(
        self, handle: ApplicationRunHandle | UUID
    ) -> ApplicationRunResult:
        """Drive existing coordinator/engine semantics until a stable run state."""

        session = self._session(handle)
        run = session.run
        if run.status is RunStatus.CREATED:
            self.workflow_engine.start(run)
        invocations = 0
        while run.status is RunStatus.RUNNING:
            if invocations >= self.configuration.max_stage_invocations:
                self.workflow_engine.fail(run, "APPLICATION_STAGE_INVOCATION_LIMIT")
                break
            stage = run.current_stage
            if stage is None:
                raise ApplicationRunStateError(
                    "A running application run has no current stage"
                )
            body = self._stage_body(session, stage)
            await session.coordinator.run_current_stage(
                run,
                instruction=(
                    session.request.task_text
                    + f"\nOperate only within the current {stage.value} stage "
                    "and its typed contract."
                ),
                artifact_references=session.artifact_references,
                body=body,
            )
            invocations += 1
        return self.result(handle)

    async def resume_run(
        self,
        handle: ApplicationRunHandle | UUID,
        decision: GateUserDecision,
    ) -> ApplicationRunResult:
        """Apply one trusted gate decision, then continue through the same loop."""

        session = self._session(handle)
        if session.run.status is not RunStatus.WAITING_FOR_USER:
            raise ApplicationRunStateError("Run is not waiting for a user decision")
        session.coordinator.resume_gate(session.run, decision)
        return await self.run(handle)

    def result(
        self, handle: ApplicationRunHandle | UUID
    ) -> ApplicationRunResult:
        """Return only safe IDs/status from current persisted application state."""

        session = self._session(handle)
        run = session.run
        refs = tuple(
            ref for ref in self.artifact_store.list_refs() if ref.run_id == run.run_id
        )
        report_ids = tuple(
            ref.artifact_id for ref in refs if ref.artifact_type == "report"
        )
        derived_ids = tuple(
            ref.artifact_id
            for ref in refs
            if ref.exposure_class is ArtifactExposureClass.DERIVED
            and ref.artifact_type != "report"
        )
        issue_codes = {
            RunStatus.FAILED: ("RUN_FAILED",),
            RunStatus.WAITING_FOR_USER: ("USER_INPUT_REQUIRED",),
            RunStatus.CANCELLED: ("RUN_CANCELLED",),
        }.get(run.status, ())
        pending = run.pending_user_gate
        pending_view = (
            ApplicationPendingUserGate(
                gate_id=pending.gate_id,
                prompt=pending.prompt,
                source_stage=pending.source_stage,
                domain_reference_id=pending.domain_reference_id,
            )
            if pending is not None
            else None
        )
        return ApplicationRunResult(
            run_id=run.run_id,
            status=run.status,
            final_stage=run.current_stage,
            report_artifact_ids=report_ids,
            derived_artifact_ids=derived_ids,
            issue_codes=issue_codes,
            pending_user_gate=pending_view,
            trace_run_id=run.run_id,
        )

    def trace_events(
        self, handle: ApplicationRunHandle | UUID
    ) -> tuple[TraceEvent, ...]:
        """Read the append-only trace for an application-owned run."""

        run_id = self._session(handle).run.run_id
        return self.trace_recorder.events(run_id)

    def _stage_body(
        self,
        session: _ApplicationRunSession,
        stage: WorkflowStage,
    ) -> RuntimeInputBody | None:
        context = session.request.safe_domain_references
        if stage is not WorkflowStage.PREFLIGHT:
            return RuntimeInputBody(context_references=context) if context else None
        profile = self.configuration.execution_profile
        if profile is None:
            return RuntimeInputBody(context_references=context) if context else None
        requirements = tuple(
            PreflightInputRequirement(
                artifact_id=artifact_id,
                exposure_class=self.artifact_store.get_ref(
                    artifact_id
                ).exposure_class,
            )
            for artifact_id in session.request.input_artifact_ids
        )
        receipt = self.execution_preflight.require_ready(
            ExecutionPreflightRequest(
                runtime=profile.runtime,
                image_key=profile.image_key,
                input_requirements=requirements,
                resources=profile.resources,
                network_required=profile.network_required,
                output_contract_ids=profile.output_contract_ids,
            ),
            principal=session.request.principal,
            workspace=session.request.workspace,
            run_id=session.run.run_id,
        )
        return RuntimeInputBody(
            context_references=context,
            notes=("Deterministic preflight receipt: " + receipt.model_dump_json(),),
        )

    def _build_coordinator(
        self,
        *,
        principal: Principal,
        workspace: WorkspaceContext,
    ) -> RuntimeCoordinatorService:
        specs = []
        for assembly in self.configuration.stage_assemblies:
            invoker = PerInvocationPantheonStageInvoker(
                assembly=assembly,
                factory=self.runtime_factory,
                principal=principal,
                workspace=workspace,
                services=self.capability_services,
                trace_recorder=self.trace_recorder,
                plugin_factory=self._plugin_factories.get(assembly.stage_id),
                boundary_observer=self.configuration.boundary_observer,
            )
            specs.append(
                StageRuntimeSpec(
                    stage_id=assembly.stage_id,
                    profile_key=assembly.root_profile_key,
                    prompt_template_key=assembly.prompt_template_key,
                    capability_allowlist=assembly.capability_allowlist,
                    invoker=invoker,
                )
            )
        return RuntimeCoordinatorService(
            self.workflow_engine,
            StageRuntimeRegistry(specs),
        )

    def _authorized_runtime_reference(
        self,
        artifact_id: UUID,
        *,
        principal: Principal,
        workspace: WorkspaceContext,
    ) -> RuntimeReference:
        ref = self.artifact_store.get_ref(artifact_id)
        if ref.project_id != workspace.project_id or ref.lab_id != workspace.lab_id:
            raise AuthorizationDenied("Run Artifact is outside the bound workspace")
        self.access_service.require_artifact(principal, ref, AccessAction.READ_ARTIFACT)
        return RuntimeReference(
            reference_id=str(ref.artifact_id),
            kind=RuntimeReferenceKind.ARTIFACT,
            label=(
                f"{ref.exposure_class.value} Artifact available only through "
                "governed capabilities"
            ),
        )

    def _require_trusted_scope(
        self, principal: Principal, workspace: WorkspaceContext
    ) -> None:
        if (
            principal.user_id != workspace.user_id
            or principal.lab_id != workspace.lab_id
        ):
            raise AuthorizationDenied("Trusted Principal and WorkspaceContext do not match")
        self.access_service.require_project(
            principal,
            workspace.project_id,
            AccessAction.WRITE_PROJECT,
        )

    def _validate_input_path(self, source: str | Path) -> Path:
        source_path = Path(source)
        if source_path.is_symlink():
            raise ApplicationInputError("Application input cannot be a symlink")
        try:
            resolved = source_path.expanduser().resolve(strict=True)
        except OSError as exc:
            raise ApplicationInputError("Application input does not exist") from exc
        if not resolved.is_file():
            raise ApplicationInputError("Application input must be a regular file")
        if not self._allowed_input_roots:
            raise ApplicationInputError("No trusted application input roots are configured")
        if not any(
            resolved == root or root in resolved.parents
            for root in self._allowed_input_roots
        ):
            raise ApplicationInputError("Application input is outside trusted roots")
        return resolved

    def _trusted_artifact_path(self, locator: str) -> Path:
        path = Path(locator)
        if path.is_symlink():
            raise BioFormatInspectionError(
                "Stored bioformat source cannot be a symlink"
            )
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise BioFormatInspectionError(
                "Stored bioformat source does not exist"
            ) from exc
        if not resolved.is_file() or self.artifact_store.root not in resolved.parents:
            raise BioFormatInspectionError(
                "Stored bioformat source is outside the Artifact store"
            )
        return resolved

    @staticmethod
    def _resolve_input_roots(values: tuple[str | Path, ...]) -> tuple[Path, ...]:
        roots = []
        for value in values:
            try:
                root = Path(value).expanduser().resolve(strict=True)
            except OSError as exc:
                raise ApplicationConfigurationError(
                    "Configured application input root does not exist"
                ) from exc
            if not root.is_dir():
                raise ApplicationConfigurationError(
                    "Configured application input root is not a directory"
                )
            roots.append(root)
        if len(set(roots)) != len(roots):
            raise ApplicationConfigurationError(
                "Configured application input roots must be unique"
            )
        return tuple(roots)

    def _session(
        self, handle: ApplicationRunHandle | UUID
    ) -> _ApplicationRunSession:
        run_id = handle.run_id if isinstance(handle, ApplicationRunHandle) else handle
        if not isinstance(run_id, UUID):
            raise TypeError("Application run handle must contain a UUID")
        try:
            return self._sessions[run_id]
        except KeyError as exc:
            raise ApplicationRunNotFoundError(
                f"Application run not found: {run_id}"
            ) from exc

    @staticmethod
    def _safe_artifact_reference(ref) -> ApplicationArtifactReference:
        return ApplicationArtifactReference(
            artifact_id=ref.artifact_id,
            artifact_type=ref.artifact_type,
            exposure_class=ref.exposure_class,
        )


__all__ = [
    "ApplicationArtifactReference",
    "ApplicationBioFormatInspectionArtifacts",
    "ApplicationConfigurationError",
    "ApplicationExecutionProfile",
    "ApplicationH5ADInspectionArtifacts",
    "ApplicationInputError",
    "ApplicationPendingUserGate",
    "ApplicationRunHandle",
    "ApplicationRunNotFoundError",
    "ApplicationRunRequest",
    "ApplicationRunResult",
    "ApplicationRunStateError",
    "ApplicationRuntimeConfiguration",
    "ApplicationStagePlugin",
    "LabBioApplication",
]
