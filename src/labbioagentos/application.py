"""Reusable application composition and run-lifecycle boundary."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4

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
    ArtifactReleaseBasis,
    ArtifactExposureService,
    ArtifactNotFoundError,
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
    NextAction,
    NextActionProposal,
    PendingUserGate,
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
    ExecutionPreflightError,
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
from .memory import MemoryDecision, MemoryGovernanceService
from .run_state import (
    ApplicationRunRecord,
    InMemoryRunStateStore,
    RunInflightOperation,
    RunRecoveryState,
    RunStateStore,
    RunStateVersionConflictError,
)
from .runtime import (
    PantheonRuntimeFactory,
    PerInvocationPantheonStageInvoker,
    PreflightStageBody,
    ReportSubmissionService,
    RuntimeCapabilityServices,
    RuntimeEvidenceReference,
    RuntimeEvidenceRole,
    RuntimeExecutionCapabilityView,
    RuntimeInputBody,
    RuntimeProfileCatalog,
    RuntimeReference,
    RuntimeReferenceKind,
    RuntimeStageAssemblySpec,
    RuntimeStageResult,
    StageRuntimeRegistry,
    StageRuntimeSpec,
)
from .runtime.assembly import BoundaryObserver, PluginFactory
from .runtime.coordinator import RuntimeCoordinatorService
from .skills import GoldSkillService, SkillUserDecision
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


_EXECUTION_PREFLIGHT_FAILURE_CODE = "EXECUTION_PREFLIGHT_FAILED"


class ApplicationRecoveryIssueCode(StrEnum):
    """Bounded reason why a durable run cannot be safely reconstructed."""

    STAGE_IN_FLIGHT = "STAGE_IN_FLIGHT"
    GATE_DECISION_IN_FLIGHT = "GATE_DECISION_IN_FLIGHT"
    RUNTIME_REVISION_MISMATCH = "RUNTIME_REVISION_MISMATCH"
    REQUIRED_ARTIFACT_MISSING = "REQUIRED_ARTIFACT_MISSING"


class ApplicationRecoveryError(ApplicationRunStateError):
    """A durable run requires explicit operator reconciliation."""

    def __init__(self, run_id: UUID, issue_code: ApplicationRecoveryIssueCode):
        self.run_id = run_id
        self.issue_code = issue_code
        super().__init__(f"Run recovery blocked: {issue_code.value}")


class ApplicationDomainDecisionHandler(ABC):
    """Apply one domain authorization before WorkflowEngine resumes a gate."""

    @abstractmethod
    def supports(self, domain_reference_id: str) -> bool:
        """Return whether this handler owns the exact structural reference kind."""

    @abstractmethod
    async def apply(
        self,
        *,
        pending_gate: PendingUserGate,
        decision: GateUserDecision,
        principal: Principal,
        workspace: WorkspaceContext,
        run_id: UUID,
    ) -> str:
        """Persist the domain decision and return its opaque reference ID."""


class SkillDomainDecisionHandler(ApplicationDomainDecisionHandler):
    """Bridge Skill creation/use proposals into the generic application gate."""

    _CREATION_PREFIX = "skill-proposal:"
    _USE_PREFIX = "skill-use:"

    def __init__(self, service: GoldSkillService):
        self.service = service

    def supports(self, domain_reference_id: str) -> bool:
        try:
            self._parse(domain_reference_id)
        except ValueError:
            return False
        return True

    async def apply(
        self,
        *,
        pending_gate: PendingUserGate,
        decision: GateUserDecision,
        principal: Principal,
        workspace: WorkspaceContext,
        run_id: UUID,
    ) -> str:
        if (
            decision.gate_id != pending_gate.gate_id
            or decision.domain_reference_id != pending_gate.domain_reference_id
        ):
            raise ApplicationRunStateError(
                "Skill domain decision does not match the pending workflow gate"
            )
        if decision.decided_by != principal.user_id:
            raise AuthorizationDenied(
                "Skill domain decision identity does not match the Principal"
            )
        if decision.decision_reference_id is not None:
            raise ApplicationRunStateError(
                "External Skill decisions cannot supply a decision reference"
            )
        kind, subject_id = self._parse(decision.domain_reference_id or "")
        if kind == "creation":
            proposal = self.service.pending_proposal(subject_id)
            if proposal.source_run_id != run_id or proposal.lab_id != workspace.lab_id:
                raise AuthorizationDenied(
                    "Skill proposal does not match the application run scope"
                )
            if (
                proposal.owner_user_id is not None
                and proposal.owner_user_id != principal.user_id
            ) or (
                proposal.project_id is not None
                and proposal.project_id != workspace.project_id
            ):
                raise AuthorizationDenied(
                    "Skill proposal ownership does not match the application scope"
                )
            skill_decision = SkillUserDecision(
                subject_id=proposal.proposal_id,
                gate_id=proposal.approval_gate_id,
                approved=decision.approved,
                decided_by=decision.decided_by,
            )
            self.service.decide_proposal(
                proposal.proposal_id,
                skill_decision,
                principal=principal,
            )
            return str(skill_decision.decision_id)

        proposal = self.service.pending_use_proposal(subject_id)
        if (
            proposal.run_id != run_id
            or proposal.requesting_user_id != principal.user_id
            or proposal.project_id != workspace.project_id
            or proposal.lab_id != workspace.lab_id
        ):
            raise AuthorizationDenied(
                "Skill use proposal does not match the application run scope"
            )
        authorization = self.service.decide_use(
            proposal.proposal_id,
            SkillUserDecision(
                subject_id=proposal.proposal_id,
                gate_id=proposal.approval_gate_id,
                approved=decision.approved,
                decided_by=decision.decided_by,
            ),
            principal=principal,
        )
        return str(authorization.authorization_id)

    @classmethod
    def _parse(cls, reference: str) -> tuple[str, UUID]:
        for kind, prefix in (
            ("creation", cls._CREATION_PREFIX),
            ("use", cls._USE_PREFIX),
        ):
            if reference.startswith(prefix):
                try:
                    return kind, UUID(reference.removeprefix(prefix))
                except ValueError as exc:
                    raise ValueError("Invalid Skill domain reference") from exc
        raise ValueError("Unsupported Skill domain reference")


class MemoryDomainDecisionHandler(ApplicationDomainDecisionHandler):
    """Bridge one exact Memory proposal into the generic application gate."""

    _PREFIX = "memory-proposal:"

    def __init__(self, service: MemoryGovernanceService):
        self.service = service

    def supports(self, domain_reference_id: str) -> bool:
        try:
            self._parse(domain_reference_id)
        except ValueError:
            return False
        return True

    async def apply(
        self,
        *,
        pending_gate: PendingUserGate,
        decision: GateUserDecision,
        principal: Principal,
        workspace: WorkspaceContext,
        run_id: UUID,
    ) -> str:
        if (
            decision.gate_id != pending_gate.gate_id
            or decision.domain_reference_id != pending_gate.domain_reference_id
        ):
            raise ApplicationRunStateError(
                "Memory domain decision does not match the pending workflow gate"
            )
        if decision.decided_by != principal.user_id:
            raise AuthorizationDenied(
                "Memory domain decision identity does not match the Principal"
            )
        if decision.decision_reference_id is not None:
            raise ApplicationRunStateError(
                "External Memory decisions cannot supply a decision reference"
            )
        proposal_id = self._parse(decision.domain_reference_id or "")
        proposal = self.service.pending_proposal(proposal_id)
        if (
            proposal.source_run_id != run_id
            or proposal.lab_id != workspace.lab_id
        ):
            raise AuthorizationDenied(
                "Memory proposal does not match the application run scope"
            )
        if proposal.project_id is not None and proposal.project_id != workspace.project_id:
            raise AuthorizationDenied(
                "Memory proposal project does not match the application workspace"
            )
        if (
            proposal.owner_user_id is not None
            and proposal.owner_user_id != principal.user_id
        ):
            raise AuthorizationDenied(
                "Memory proposal owner does not match the application Principal"
            )
        memory_decision = MemoryDecision(
            proposal_id=proposal.proposal_id,
            gate_id=proposal.approval_gate_id,
            approved=decision.approved,
            decided_by=decision.decided_by,
        )
        self.service.decide(
            principal,
            proposal.proposal_id,
            memory_decision,
        )
        return str(memory_decision.decision_id)

    @classmethod
    def _parse(cls, reference: str) -> UUID:
        if not reference.startswith(cls._PREFIX):
            raise ValueError("Unsupported Memory domain reference")
        try:
            return UUID(reference.removeprefix(cls._PREFIX))
        except ValueError as exc:
            raise ValueError("Invalid Memory domain reference") from exc


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


class ApplicationRecoveryStatus(BaseModel):
    """Bounded recovery assessment after current identity reauthorization."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_id: UUID
    run_status: RunStatus
    current_stage: WorkflowStage | None = None
    recovery_state: RunRecoveryState
    inflight_stage: WorkflowStage | None = None
    inflight_invocation_id: UUID | None = None
    inflight_operation: RunInflightOperation | None = None
    recoverable: bool
    automatic_continuation_allowed: bool
    issue_code: ApplicationRecoveryIssueCode | None = None
    record_version: int = Field(ge=1)


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
    runtime_revision: str
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
    run_state_store: RunStateStore | None = None
    process_runner: DockerProcessRunner | None = None
    skill_service: GoldSkillService | None = None
    memory_service: MemoryGovernanceService | None = None
    domain_decision_handlers: tuple[ApplicationDomainDecisionHandler, ...] = ()
    h5ad_inspection_policy: H5ADInspectionPolicy = field(
        default_factory=H5ADInspectionPolicy
    )
    bioformat_inspectors: tuple[BioFormatInspector, ...] = ()
    boundary_observer: BoundaryObserver | None = None
    retry_limit: int = 1
    max_stage_invocations: int = 64

    def __post_init__(self) -> None:
        if (
            not isinstance(self.runtime_revision, str)
            or not self.runtime_revision.strip()
            or len(self.runtime_revision) > 256
        ):
            raise ApplicationConfigurationError(
                "runtime_revision must be a non-empty string of at most 256 characters"
            )
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
        if any(
            not isinstance(handler, ApplicationDomainDecisionHandler)
            for handler in self.domain_decision_handlers
        ):
            raise ApplicationConfigurationError(
                "Domain decision handlers must implement the application contract"
            )


@dataclass
class _ApplicationRunSession:
    request: ApplicationRunRequest
    run: WorkflowRun
    artifact_references: tuple[RuntimeEvidenceReference, ...]
    coordinator: RuntimeCoordinatorService
    record_version: int


class LabBioApplication:
    """Compose infrastructure once and drive scoped runs through existing owners."""

    def __init__(self, configuration: ApplicationRuntimeConfiguration):
        self.configuration = configuration
        self.run_state_store = (
            configuration.run_state_store or InMemoryRunStateStore()
        )
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
        if configuration.skill_service is not None:
            configuration.skill_service.bind_runtime_context(
                access_service=self.access_service,
                trace_recorder=self.trace_recorder,
            )
        self.artifact_store = LocalArtifactStore(
            configuration.artifact_root,
            trace_recorder=self.trace_recorder,
        )
        if (
            configuration.exposure_policy.user_approved_enabled
            and not configuration.exposure_policy.approval_store.durable
        ):
            raise ApplicationConfigurationError(
                "Enabled USER_APPROVED exposure requires a durable approval store"
            )
        if configuration.memory_service is not None:
            configuration.memory_service.bind_runtime_context(
                access_service=self.access_service,
                trace_recorder=self.trace_recorder,
                artifact_store=self.artifact_store,
            )
            if any(
                isinstance(handler, MemoryDomainDecisionHandler)
                and handler.service is not configuration.memory_service
                for handler in configuration.domain_decision_handlers
            ):
                raise ApplicationConfigurationError(
                    "Memory decision handlers must use the configured Memory service"
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
        profile = configuration.execution_profile
        self.execution_capability = (
            RuntimeExecutionCapabilityView.from_trusted_configuration(
                runtime=profile.runtime,
                image_key=profile.image_key,
                resources=profile.resources,
                network_required=profile.network_required,
                output_contract_ids=profile.output_contract_ids,
                image_registry=self.image_registry,
                execution_policy=self.execution_policy,
                registration_policy=self.registration_policy,
            )
            if profile is not None
            else None
        )
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
                max_output_file_bytes=self.execution_policy.max_output_file_bytes,
                max_collected_output_bytes=(
                    self.execution_policy.max_collected_output_bytes
                ),
            ),
            process_runner=configuration.process_runner,
            command_builder=DockerCommandBuilder(
                max_output_file_bytes=self.execution_policy.max_output_file_bytes
            ),
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
            release_basis=ArtifactReleaseBasis.RAW_INGESTION,
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
            release_basis=ArtifactReleaseBasis.TRUSTED_STRUCTURAL_INSPECTOR,
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
                release_basis=spec.release_basis,
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
        session = _ApplicationRunSession(
            request=request,
            run=run,
            artifact_references=references,
            coordinator=coordinator,
            record_version=1,
        )
        stored = self.run_state_store.create(self._new_run_record(session))
        session.record_version = stored.record_version
        self._sessions[run.run_id] = session
        return ApplicationRunHandle(run_id=run.run_id)

    def recovery_status(
        self,
        run_id: UUID,
        *,
        principal: Principal,
        workspace: WorkspaceContext,
    ) -> ApplicationRecoveryStatus:
        """Assess a durable run after exact current identity reauthorization."""

        if not isinstance(run_id, UUID):
            raise TypeError("Application recovery run_id must be a UUID")
        record = self.run_state_store.get(run_id)
        self._reauthorize_recovery(record, principal, workspace)
        issue_code = self._recovery_issue(record)
        if issue_code is None:
            issue_code = self._required_artifact_issue(
                record, principal=principal, workspace=workspace
            )
        recoverable = issue_code is None
        return ApplicationRecoveryStatus(
            run_id=record.run_id,
            run_status=record.workflow_run.status,
            current_stage=record.workflow_run.current_stage,
            recovery_state=record.recovery_state,
            inflight_stage=record.inflight_stage,
            inflight_invocation_id=record.inflight_invocation_id,
            inflight_operation=record.inflight_operation,
            recoverable=recoverable,
            automatic_continuation_allowed=(
                recoverable
                and record.workflow_run.status
                in {RunStatus.CREATED, RunStatus.RUNNING}
            ),
            issue_code=issue_code,
            record_version=record.record_version,
        )

    def recover_run(
        self,
        run_id: UUID,
        *,
        principal: Principal,
        workspace: WorkspaceContext,
    ) -> ApplicationRunHandle:
        """Explicitly reconstruct one stable run without invoking a model or tool."""

        if run_id in self._sessions:
            raise ApplicationRunStateError(
                f"Application run is already attached: {run_id}"
            )
        status = self.recovery_status(
            run_id, principal=principal, workspace=workspace
        )
        if not status.recoverable:
            assert status.issue_code is not None
            raise ApplicationRecoveryError(run_id, status.issue_code)
        record = self.run_state_store.get(run_id)
        if record.record_version != status.record_version:
            raise RunStateVersionConflictError(
                f"Run-state version changed during recovery for {run_id}"
            )
        references = tuple(
            self._authorized_runtime_reference(
                artifact_id,
                principal=principal,
                workspace=workspace,
            )
            for artifact_id in (
                *record.input_artifact_ids,
                *record.context_artifact_ids,
            )
        )
        coordinator = self._build_coordinator(
            principal=principal,
            workspace=workspace,
        )
        coordinator.attach_recovered_results(run_id, record.runtime_results)
        run = self.workflow_engine.attach_recovered_run(record.workflow_run)
        request = ApplicationRunRequest(
            task_text=record.task_text,
            principal=principal,
            workspace=workspace,
            input_artifact_ids=record.input_artifact_ids,
            context_artifact_ids=record.context_artifact_ids,
            safe_domain_references=record.safe_domain_references,
        )
        self._sessions[run_id] = _ApplicationRunSession(
            request=request,
            run=run,
            artifact_references=references,
            coordinator=coordinator,
            record_version=record.record_version,
        )
        return ApplicationRunHandle(run_id=run_id)

    async def run(
        self, handle: ApplicationRunHandle | UUID
    ) -> ApplicationRunResult:
        """Drive existing coordinator/engine semantics until a stable run state."""

        session = self._session(handle)
        self._require_stable_session(session)
        run = session.run
        if run.status is RunStatus.CREATED:
            self.workflow_engine.start(run)
            self._checkpoint(session, recovery_state=RunRecoveryState.STABLE)
        invocations = len(session.coordinator.results(run.run_id))
        while run.status is RunStatus.RUNNING:
            if invocations >= self.configuration.max_stage_invocations:
                self.workflow_engine.fail(run, "APPLICATION_STAGE_INVOCATION_LIMIT")
                self._checkpoint(session, recovery_state=RunRecoveryState.STABLE)
                break
            stage = run.current_stage
            if stage is None:
                raise ApplicationRunStateError(
                    "A running application run has no current stage"
                )
            body = self._stage_body(session)
            invocation_id = uuid4()
            self._checkpoint(
                session,
                recovery_state=RunRecoveryState.STAGE_IN_FLIGHT,
                inflight_stage=stage,
                inflight_invocation_id=invocation_id,
                inflight_operation=RunInflightOperation.RUNTIME_STAGE,
            )
            if (
                stage is WorkflowStage.PREFLIGHT
                and self.configuration.execution_profile is not None
            ):
                session.coordinator.accept_trusted_stage_result(
                    run,
                    self._configured_preflight_result(session),
                    invocation_id,
                )
            else:
                await session.coordinator.run_current_stage(
                    run,
                    instruction=(
                        session.request.task_text
                        + f"\nOperate only within the current {stage.value} stage "
                        "and its typed contract."
                    ),
                    artifact_references=self._authoritative_evidence_references(
                        session,
                        stage=stage,
                    ),
                    body=body,
                    invocation_id=invocation_id,
                )
            self._checkpoint(session, recovery_state=RunRecoveryState.STABLE)
            invocations += 1
        if self.configuration.skill_service is not None:
            self.configuration.skill_service.finalize_run_usage(
                run.run_id,
                run.status,
            )
        return self.result(handle)

    async def resume_run(
        self,
        handle: ApplicationRunHandle | UUID,
        decision: GateUserDecision,
    ) -> ApplicationRunResult:
        """Apply one trusted gate decision, then continue through the same loop."""

        session = self._session(handle)
        self._require_stable_session(session)
        if session.run.status is not RunStatus.WAITING_FOR_USER:
            raise ApplicationRunStateError("Run is not waiting for a user decision")
        session.coordinator.validate_gate_resume(session.run, decision)
        pending = session.run.pending_user_gate
        if pending is None:
            raise ApplicationRunStateError("Run has no pending user gate")
        if decision.decided_by != session.request.principal.user_id:
            raise AuthorizationDenied(
                "Gate decision identity does not match the current Principal"
            )
        effective_decision = decision
        handlers: tuple[ApplicationDomainDecisionHandler, ...] = ()
        if pending.domain_reference_id is not None:
            handlers = tuple(
                handler
                for handler in self.configuration.domain_decision_handlers
                if handler.supports(pending.domain_reference_id)
            )
            if len(handlers) != 1:
                raise ApplicationRunStateError(
                    "A domain user gate requires exactly one decision handler"
                )
        decision_invocation_id = uuid4()
        self._checkpoint(
            session,
            recovery_state=RunRecoveryState.GATE_DECISION_IN_FLIGHT,
            inflight_stage=WorkflowStage.USER_GATE,
            inflight_invocation_id=decision_invocation_id,
            inflight_operation=RunInflightOperation.DOMAIN_GATE_DECISION,
        )
        if pending.domain_reference_id is not None:
            decision_reference_id = await handlers[0].apply(
                pending_gate=pending,
                decision=decision,
                principal=session.request.principal,
                workspace=session.request.workspace,
                run_id=session.run.run_id,
            )
            effective_decision = decision.model_copy(
                update={"decision_reference_id": decision_reference_id}
            )
        session.coordinator.resume_gate(session.run, effective_decision)
        self._checkpoint(session, recovery_state=RunRecoveryState.STABLE)
        return await self.run(handle)

    def cancel_run(
        self,
        handle: ApplicationRunHandle | UUID,
        *,
        reason: str | None = None,
    ) -> ApplicationRunResult:
        """Cancel an attached stable run and persist the terminal boundary."""

        session = self._session(handle)
        self._require_stable_session(session)
        self.workflow_engine.cancel(session.run, reason)
        self._checkpoint(session, recovery_state=RunRecoveryState.STABLE)
        if self.configuration.skill_service is not None:
            self.configuration.skill_service.finalize_run_usage(
                session.run.run_id, session.run.status
            )
        return self.result(handle)

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
            RunStatus.FAILED: (
                (_EXECUTION_PREFLIGHT_FAILURE_CODE,)
                if run.failure_reason == _EXECUTION_PREFLIGHT_FAILURE_CODE
                else ("RUN_FAILED",)
            ),
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
    ) -> RuntimeInputBody | None:
        """Build bounded presentation input without performing side effects."""

        context = session.request.safe_domain_references
        return RuntimeInputBody(user_assertion_references=context) if context else None

    def _configured_preflight_result(
        self,
        session: _ApplicationRunSession,
    ) -> RuntimeStageResult:
        """Return one host-owned structural PREFLIGHT result."""

        profile = self.configuration.execution_profile
        if profile is None:  # pragma: no cover - caller binds the trusted branch
            raise ApplicationRunStateError(
                "Configured preflight requires an execution profile"
            )
        requirements = tuple(
            PreflightInputRequirement(
                artifact_id=artifact_id,
                exposure_class=self.artifact_store.get_ref(
                    artifact_id
                ).exposure_class,
            )
            for artifact_id in session.request.input_artifact_ids
        )
        try:
            self.execution_preflight.require_ready(
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
        except ExecutionPreflightError:
            return RuntimeStageResult(
                stage_id=WorkflowStage.PREFLIGHT,
                summary="Deterministic execution preflight failed.",
                body=PreflightStageBody(
                    structurally_valid=False,
                    issues=(_EXECUTION_PREFLIGHT_FAILURE_CODE,),
                ),
                next_action=NextActionProposal(
                    action=NextAction.FAIL,
                    reason=_EXECUTION_PREFLIGHT_FAILURE_CODE,
                ),
            )
        return RuntimeStageResult(
            stage_id=WorkflowStage.PREFLIGHT,
            summary="Deterministic execution preflight completed.",
            body=PreflightStageBody(structurally_valid=True),
            next_action=NextActionProposal(
                action=NextAction.TRANSITION,
                target_stage=WorkflowStage.EXECUTE,
            ),
        )

    def _authoritative_evidence_references(
        self,
        session: _ApplicationRunSession,
        *,
        stage: WorkflowStage,
    ) -> tuple[RuntimeEvidenceReference, ...]:
        """Refresh governed run evidence without consulting model-authored prose."""

        references = list(session.artifact_references)
        seen = {(item.kind, item.reference_id) for item in references}
        current_invocation_id = (
            None
            if stage is WorkflowStage.EXECUTE
            else self._latest_execute_invocation_id(session.run)
        )
        for ref in self.artifact_store.list_refs():
            if (
                ref.run_id != session.run.run_id
                or ref.exposure_class is ArtifactExposureClass.RAW
            ):
                continue
            role = (
                RuntimeEvidenceRole.CURRENT_ATTEMPT_EVIDENCE
                if (
                    ref.stage_id is WorkflowStage.EXECUTE
                    and ref.producer_invocation_id is not None
                    and ref.producer_invocation_id == current_invocation_id
                )
                else RuntimeEvidenceRole.HISTORICAL_EVIDENCE
            )
            artifact_reference = self._authorized_runtime_reference(
                ref.artifact_id,
                principal=session.request.principal,
                workspace=session.request.workspace,
                evidence_role=role,
            )
            key = (artifact_reference.kind, artifact_reference.reference_id)
            if key not in seen:
                references.append(artifact_reference)
                seen.add(key)
            execution_id = ref.metadata.get("execution_id")
            if isinstance(execution_id, str):
                try:
                    execution_reference_id = str(UUID(execution_id))
                except ValueError:
                    continue
                execution_reference = RuntimeEvidenceReference(
                    reference_id=execution_reference_id,
                    kind=RuntimeReferenceKind.EXECUTION,
                    label="Governed execution reference from Artifact provenance",
                    evidence_role=role,
                    producer_invocation_id=ref.producer_invocation_id,
                )
                key = (
                    execution_reference.kind,
                    execution_reference.reference_id,
                )
                if key not in seen:
                    references.append(execution_reference)
                    seen.add(key)
        if len(references) > 128:
            raise ApplicationRunStateError(
                "Authoritative evidence references exceed the runtime bound"
            )
        return tuple(references)

    @staticmethod
    def _latest_execute_invocation_id(run: WorkflowRun) -> UUID | None:
        """Resolve the most recent accepted EXECUTE invocation from workflow state."""

        for result in reversed(run.stage_results):
            if result.stage is not WorkflowStage.EXECUTE:
                continue
            value = result.payload.get("invocation_id")
            if not isinstance(value, str):
                raise ApplicationRunStateError(
                    "Accepted EXECUTE result is missing its trusted invocation ID"
                )
            try:
                return UUID(value)
            except ValueError as exc:
                raise ApplicationRunStateError(
                    "Accepted EXECUTE result has an invalid trusted invocation ID"
                ) from exc
        return None

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
                execution_capability=self.execution_capability,
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
            execution_capability=self.execution_capability,
        )

    def _authorized_runtime_reference(
        self,
        artifact_id: UUID,
        *,
        principal: Principal,
        workspace: WorkspaceContext,
        evidence_role: RuntimeEvidenceRole = RuntimeEvidenceRole.INPUT_EVIDENCE,
    ) -> RuntimeEvidenceReference:
        ref = self.artifact_store.get_ref(artifact_id)
        if ref.project_id != workspace.project_id or ref.lab_id != workspace.lab_id:
            raise AuthorizationDenied("Run Artifact is outside the bound workspace")
        self.access_service.require_artifact(principal, ref, AccessAction.READ_ARTIFACT)
        return RuntimeEvidenceReference(
            reference_id=str(ref.artifact_id),
            kind=RuntimeReferenceKind.ARTIFACT,
            label=(
                f"{ref.exposure_class.value} Artifact available only through "
                "governed capabilities"
            ),
            evidence_role=evidence_role,
            producer_invocation_id=ref.producer_invocation_id,
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

    def _new_run_record(
        self, session: _ApplicationRunSession
    ) -> ApplicationRunRecord:
        request = session.request
        return ApplicationRunRecord(
            run_id=session.run.run_id,
            task_text=request.task_text,
            owner_user_id=request.workspace.user_id,
            project_id=request.workspace.project_id,
            lab_id=request.workspace.lab_id,
            input_artifact_ids=request.input_artifact_ids,
            context_artifact_ids=request.context_artifact_ids,
            safe_domain_references=request.safe_domain_references,
            workflow_run=session.run,
            runtime_results=session.coordinator.results(session.run.run_id),
            runtime_revision=self.configuration.runtime_revision,
            recovery_state=RunRecoveryState.STABLE,
        )

    def _checkpoint(
        self,
        session: _ApplicationRunSession,
        *,
        recovery_state: RunRecoveryState,
        inflight_stage: WorkflowStage | None = None,
        inflight_invocation_id: UUID | None = None,
        inflight_operation: RunInflightOperation | None = None,
    ) -> ApplicationRunRecord:
        current = self.run_state_store.get(session.run.run_id)
        if current.record_version != session.record_version:
            raise RunStateVersionConflictError(
                f"Run-state version conflict for {session.run.run_id}"
            )
        replacement = current.model_copy(
            update={
                "workflow_run": session.run,
                "runtime_results": session.coordinator.results(session.run.run_id),
                "recovery_state": recovery_state,
                "inflight_stage": inflight_stage,
                "inflight_invocation_id": inflight_invocation_id,
                "inflight_operation": inflight_operation,
            }
        )
        stored = self.run_state_store.update(
            replacement, expected_version=session.record_version
        )
        session.record_version = stored.record_version
        return stored

    def _require_stable_session(self, session: _ApplicationRunSession) -> None:
        record = self.run_state_store.get(session.run.run_id)
        if record.record_version != session.record_version:
            raise RunStateVersionConflictError(
                f"Run-state version conflict for {session.run.run_id}"
            )
        issue = self._recovery_issue(record)
        if issue is not None:
            raise ApplicationRecoveryError(record.run_id, issue)
        if (
            record.workflow_run.model_dump_json() != session.run.model_dump_json()
            or record.runtime_results != session.coordinator.results(session.run.run_id)
        ):
            raise ApplicationRunStateError(
                "Attached application session diverged from durable run state"
            )

    def _reauthorize_recovery(
        self,
        record: ApplicationRunRecord,
        principal: Principal,
        workspace: WorkspaceContext,
    ) -> None:
        if (
            principal.user_id != record.owner_user_id
            or principal.lab_id != record.lab_id
            or workspace.user_id != record.owner_user_id
            or workspace.project_id != record.project_id
            or workspace.lab_id != record.lab_id
        ):
            raise AuthorizationDenied(
                "Current recovery identity does not match the durable run scope"
            )
        self._require_trusted_scope(principal, workspace)

    def _recovery_issue(
        self, record: ApplicationRunRecord
    ) -> ApplicationRecoveryIssueCode | None:
        if record.runtime_revision != self.configuration.runtime_revision:
            return ApplicationRecoveryIssueCode.RUNTIME_REVISION_MISMATCH
        if record.recovery_state is RunRecoveryState.STAGE_IN_FLIGHT:
            return ApplicationRecoveryIssueCode.STAGE_IN_FLIGHT
        if record.recovery_state is RunRecoveryState.GATE_DECISION_IN_FLIGHT:
            return ApplicationRecoveryIssueCode.GATE_DECISION_IN_FLIGHT
        return None

    def _required_artifact_issue(
        self,
        record: ApplicationRunRecord,
        *,
        principal: Principal,
        workspace: WorkspaceContext,
    ) -> ApplicationRecoveryIssueCode | None:
        for artifact_id in (
            *record.input_artifact_ids,
            *record.context_artifact_ids,
        ):
            try:
                self._authorized_runtime_reference(
                    artifact_id,
                    principal=principal,
                    workspace=workspace,
                )
            except ArtifactNotFoundError:
                return ApplicationRecoveryIssueCode.REQUIRED_ARTIFACT_MISSING
        return None

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
    "ApplicationDomainDecisionHandler",
    "ApplicationExecutionProfile",
    "ApplicationH5ADInspectionArtifacts",
    "ApplicationInputError",
    "ApplicationPendingUserGate",
    "ApplicationRecoveryError",
    "ApplicationRecoveryIssueCode",
    "ApplicationRecoveryStatus",
    "ApplicationRunHandle",
    "ApplicationRunNotFoundError",
    "ApplicationRunRequest",
    "ApplicationRunResult",
    "ApplicationRunStateError",
    "ApplicationRuntimeConfiguration",
    "ApplicationStagePlugin",
    "LabBioApplication",
    "SkillDomainDecisionHandler",
    "MemoryDomainDecisionHandler",
]
