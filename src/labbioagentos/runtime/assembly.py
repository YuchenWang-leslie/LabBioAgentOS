"""Per-invocation Pantheon assembly bound to trusted LabBio stage config."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from labbioagentos.contracts import WorkflowStage
from labbioagentos.governance import Principal, WorkspaceContext
from labbioagentos.trace import RunTraceRecorder

from .contracts import (
    CapabilityEvidenceBundle,
    CapabilityEvidenceStatus,
    RuntimeExecutionCapabilityView,
    RuntimeStageInput,
    RuntimeStageResult,
)
from .pantheon import (
    PantheonCapabilityStageInvoker,
    PantheonRuntimeFactory,
    PantheonTwoModeStageInvoker,
    PantheonTypedStageInvoker,
    RuntimeProfileConfigurationError,
)
from .profiles import RuntimeInvocationMode
from .tooling import (
    LabBioRuntimeToolSet,
    RuntimeCapabilityContext,
    RuntimeCapabilityServices,
)


@dataclass(frozen=True)
class RuntimeAgentCapabilitySpec:
    """Trusted capability assignment for one non-root team profile."""

    profile_key: str
    capability_allowlist: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.profile_key:
            raise ValueError("Peer profile_key is required")
        if len(set(self.capability_allowlist)) != len(self.capability_allowlist):
            raise ValueError("Peer capability allowlist must not contain duplicates")


@dataclass(frozen=True)
class RuntimeStageAssemblySpec:
    """Trusted assembly facts selected by exact workflow stage identity."""

    stage_id: WorkflowStage
    root_profile_key: str
    prompt_template_key: str
    capability_allowlist: tuple[str, ...]
    capability_peer_specs: tuple[RuntimeAgentCapabilitySpec, ...] = ()
    capability_prompt_values: Mapping[str, str] | None = None
    finalization_prompt_values: Mapping[str, str] | None = None
    max_delegate_depth: int = 5
    capability_phase_enabled: bool = True
    preserve_capability_completion: bool = False
    required_capabilities: tuple[str, ...] = ()
    max_capability_turns: int = 24

    def __post_init__(self) -> None:
        if self.stage_id in {
            WorkflowStage.USER_GATE,
            WorkflowStage.SEARCH,
            WorkflowStage.DEBUG,
        }:
            raise ValueError("Runtime assembly is limited to the nine main stages")
        if self.max_delegate_depth < 1:
            raise ValueError("max_delegate_depth must be positive")
        if self.max_capability_turns < 4 or self.max_capability_turns > 128:
            raise ValueError("max_capability_turns must be between 4 and 128")
        if not set(self.required_capabilities).issubset(self.capability_allowlist):
            raise ValueError("required_capabilities must be within the allowlist")
        if not self.capability_phase_enabled and (
            self.capability_allowlist
            or self.capability_peer_specs
            or self.preserve_capability_completion
            or self.required_capabilities
        ):
            raise ValueError(
                "Disabled capability phase cannot expose capabilities or peers"
            )


PluginFactory = Callable[[], list]
ToolSetFactory = Callable[
    [RuntimeCapabilityContext, RuntimeCapabilityServices], LabBioRuntimeToolSet
]
BoundaryObserver = Callable[[str, object], None]


class PerInvocationPantheonStageInvoker:
    """Build fresh Pantheon teams and ToolSets for each stage invocation.

    Authority is taken only from the trusted constructor binding.  Values in
    ``RuntimeStageInput`` are correlation/presentation values and cannot widen
    scope, capabilities, profiles, or stage identity.
    """

    def __init__(
        self,
        *,
        assembly: RuntimeStageAssemblySpec,
        factory: PantheonRuntimeFactory,
        principal: Principal,
        workspace: WorkspaceContext,
        services: RuntimeCapabilityServices,
        trace_recorder: RunTraceRecorder | None = None,
        execution_capability: RuntimeExecutionCapabilityView | None = None,
        plugin_factory: PluginFactory | None = None,
        toolset_factory: ToolSetFactory = LabBioRuntimeToolSet,
        boundary_observer: BoundaryObserver | None = None,
    ):
        self.assembly = assembly
        self.factory = factory
        self.principal = principal
        self.workspace = workspace
        self.services = services
        self.trace_recorder = trace_recorder
        self.execution_capability = execution_capability
        self.plugin_factory = plugin_factory
        self.toolset_factory = toolset_factory
        self.boundary_observer = boundary_observer
        self._validate_catalog_binding()

    def validate_stage_spec(self, spec) -> None:
        """Fail composition if registry and assembly authority disagree."""

        expected = self.assembly
        if (
            spec.stage_id is not expected.stage_id
            or spec.profile_key != expected.root_profile_key
            or spec.prompt_template_key != expected.prompt_template_key
            or tuple(spec.capability_allowlist) != expected.capability_allowlist
        ):
            raise RuntimeProfileConfigurationError(
                "Stage registry and Pantheon assembly bindings do not match"
            )

    async def invoke(self, stage_input: RuntimeStageInput) -> RuntimeStageResult:
        self._validate_input_binding(stage_input)
        if self.boundary_observer is not None:
            self.boundary_observer("stage_input", stage_input)
        root_key = self.assembly.root_profile_key
        prompt_values = self._prompt_values(
            root_key, self.assembly.finalization_prompt_values
        )
        final_team, final_prompts = await self.factory.create_team(
            (root_key,),
            prompt_values=prompt_values,
            invocation_mode=RuntimeInvocationMode.FINALIZE,
            finalization_stage=self.assembly.stage_id,
        )
        finalizer = PantheonTypedStageInvoker(
            final_team,
            profile=self.factory.catalog.agents[root_key],
            prompt=final_prompts[root_key],
            response_schema=self.factory.catalog.schemas[
                self.factory.catalog.agents[root_key].response_schema_key
            ],
            trace_recorder=self.trace_recorder,
        )
        if not self.assembly.capability_phase_enabled:
            result = await finalizer.invoke(stage_input)
            if self.boundary_observer is not None:
                self.boundary_observer("stage_result", result)
            return result

        capability_specs = (
            RuntimeAgentCapabilitySpec(
                profile_key=root_key,
                capability_allowlist=self.assembly.capability_allowlist,
            ),
            *self.assembly.capability_peer_specs,
        )
        capability_keys = tuple(spec.profile_key for spec in capability_specs)
        toolsets: dict[str, LabBioRuntimeToolSet] = {}
        for spec in capability_specs:
            profile = self.factory.catalog.agents[spec.profile_key]
            binding = RuntimeCapabilityContext(
                principal=self.principal,
                workspace=self.workspace,
                run_id=stage_input.run_id,
                stage_id=self.assembly.stage_id,
                invocation_id=stage_input.invocation_id,
                actor_profile_key=profile.profile_key,
                actor_agent_name=profile.agent_name,
                capability_allowlist=spec.capability_allowlist,
            )
            toolsets[spec.profile_key] = self.toolset_factory(binding, self.services)
        capability_team, capability_prompts = await self.factory.create_team(
            capability_keys,
            prompt_values=self._prompt_values_for_profiles(
                capability_keys, self.assembly.capability_prompt_values
            ),
            toolsets=toolsets,
            plugins=(self.plugin_factory() if self.plugin_factory else None),
            max_delegate_depth=self.assembly.max_delegate_depth,
            invocation_mode=RuntimeInvocationMode.CAPABILITY,
        )
        capability = PantheonCapabilityStageInvoker(
            capability_team,
            profile=self.factory.catalog.agents[root_key],
            prompt=capability_prompts[root_key],
            evidence_sources=tuple(toolsets.values()),
            trace_recorder=self.trace_recorder,
            preserve_explicit_completion=(
                self.assembly.preserve_capability_completion
            ),
            max_turns=self.assembly.max_capability_turns,
        )
        result = await PantheonTwoModeStageInvoker(
            capability,
            finalizer,
            boundary_observer=self.boundary_observer,
            evidence_validator=self._validate_required_capabilities,
        ).invoke(stage_input)
        if self.boundary_observer is not None:
            self.boundary_observer("stage_result", result)
        return result

    def _validate_required_capabilities(
        self, evidence: CapabilityEvidenceBundle
    ) -> None:
        completed = {
            item.capability_name
            for item in evidence.items
            if item.status is CapabilityEvidenceStatus.COMPLETED
        }
        missing = tuple(
            capability
            for capability in self.assembly.required_capabilities
            if capability not in completed
        )
        if missing:
            raise RuntimeProfileConfigurationError(
                "Required stage capabilities did not complete: " + ", ".join(missing)
            )

    def _validate_catalog_binding(self) -> None:
        try:
            root = self.factory.catalog.agents[self.assembly.root_profile_key]
        except KeyError as exc:
            raise RuntimeProfileConfigurationError(
                f"Unknown root profile {self.assembly.root_profile_key!r}"
            ) from exc
        if root.prompt_profile_key != self.assembly.prompt_template_key:
            raise RuntimeProfileConfigurationError(
                "Assembly prompt template does not match the root agent profile"
            )
        capability_profile = self.factory.catalog.capabilities[
            root.capability_profile_key
        ]
        if not set(self.assembly.capability_allowlist).issubset(
            capability_profile.capability_allowlist
        ):
            raise RuntimeProfileConfigurationError(
                "Assembly allowlist exceeds the root capability profile"
            )
        for peer in self.assembly.capability_peer_specs:
            if peer.profile_key not in self.factory.catalog.agents:
                raise RuntimeProfileConfigurationError(
                    f"Unknown capability peer profile {peer.profile_key!r}"
                )
            peer_profile = self.factory.catalog.agents[peer.profile_key]
            peer_capability_profile = self.factory.catalog.capabilities[
                peer_profile.capability_profile_key
            ]
            if not set(peer.capability_allowlist).issubset(
                peer_capability_profile.capability_allowlist
            ):
                raise RuntimeProfileConfigurationError(
                    "Assembly allowlist exceeds a peer capability profile"
                )
        peer_keys = tuple(peer.profile_key for peer in self.assembly.capability_peer_specs)
        if len(set((self.assembly.root_profile_key, *peer_keys))) != (
            1 + len(peer_keys)
        ):
            raise RuntimeProfileConfigurationError(
                "Capability team profile keys must be unique"
            )

    def _validate_input_binding(self, stage_input: RuntimeStageInput) -> None:
        if stage_input.stage_id is not self.assembly.stage_id:
            raise RuntimeProfileConfigurationError(
                "Runtime input stage does not match trusted assembly stage"
            )
        if stage_input.allowed_capabilities != self.assembly.capability_allowlist:
            raise RuntimeProfileConfigurationError(
                "Runtime input capabilities do not match trusted assembly allowlist"
            )
        expected_execution_capability = (
            self.execution_capability
            if self.assembly.stage_id
            in {WorkflowStage.PREFLIGHT, WorkflowStage.EXECUTE}
            else None
        )
        if stage_input.execution_capability != expected_execution_capability:
            raise RuntimeProfileConfigurationError(
                "Runtime input execution capability does not match trusted configuration"
            )
        expected_workspace = (
            self.workspace.user_id,
            self.workspace.project_id,
            self.workspace.lab_id,
        )
        presented_workspace = (
            stage_input.workspace.user_id,
            stage_input.workspace.project_id,
            stage_input.workspace.lab_id,
        )
        principal_workspace = (
            self.principal.user_id,
            self.workspace.project_id,
            self.principal.lab_id,
        )
        if presented_workspace != expected_workspace or expected_workspace != principal_workspace:
            raise RuntimeProfileConfigurationError(
                "Runtime input workspace does not match trusted principal binding"
            )

    @staticmethod
    def _prompt_values(
        profile_key: str, values: Mapping[str, str] | None
    ) -> dict[str, dict[str, str]] | None:
        if values is None:
            return None
        return {profile_key: dict(values)}

    @staticmethod
    def _prompt_values_for_profiles(
        profile_keys: tuple[str, ...], values: Mapping[str, str] | None
    ) -> dict[str, dict[str, str]] | None:
        if values is None:
            return None
        return {key: dict(values) for key in profile_keys}
