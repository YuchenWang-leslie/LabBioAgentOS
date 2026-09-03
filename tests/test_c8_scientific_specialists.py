"""Deterministic C8 tests for governed scientific specialist participation."""

from __future__ import annotations

import inspect
from contextlib import contextmanager
from types import MethodType
from uuid import uuid4

import pytest
from pantheon.agent import AgentResponse, AgentRunContext, _RUN_CONTEXT

from labbioagentos import (
    AccessService,
    ArtifactConsumer,
    ArtifactExposureClass,
    ArtifactExposureService,
    ArtifactRepresentation,
    ArtifactReleaseBasis,
    AuthorizationPolicy,
    CapabilityProfile,
    DelegationPolicyPlugin,
    ExecuteStageBody,
    InMemoryDelegationPolicy,
    InMemoryProjectStore,
    InMemoryTraceSink,
    LabBioRuntimeToolSet,
    LocalArtifactStore,
    ModelProfile,
    NextAction,
    NextActionProposal,
    PantheonCapabilityStageInvoker,
    PantheonRuntimeFactory,
    PerInvocationPantheonStageInvoker,
    Principal,
    Project,
    PromptProfile,
    ProviderConfigRef,
    ResponseSchemaRef,
    RunTraceRecorder,
    RuntimeAgentCapabilitySpec,
    RuntimeCapabilityContext,
    RuntimeCapabilityServices,
    RuntimeInvocationMode,
    RuntimeProfileCatalog,
    RuntimeProfileConfigurationError,
    RuntimeStageAssemblySpec,
    RuntimeStageInput,
    RuntimeStageResult,
    RuntimeWorkspaceIdentifiers,
    TraceEventType,
    WorkflowStage,
    WorkspaceContext,
    default_agent_profiles,
    scientific_specialist_profiles,
)
from labbioagentos.artifacts import ExposurePolicy


ROOT_KEY = "execution"
SPECIALIST_KEY = "single-cell-analysis-specialist"
REVIEWER_KEY = "scientific-methods-reviewer"


def _catalog() -> RuntimeProfileCatalog:
    profiles = (*default_agent_profiles(), *scientific_specialist_profiles())
    allowed = {
        "coordinator-capabilities": ("artifact_query",),
        "execution-capabilities": ("artifact_query", "execution_submit"),
        "reviewer-capabilities": ("artifact_query",),
        "single-cell-analysis-specialist-capabilities": ("artifact_query",),
        "scientific-methods-reviewer-capabilities": ("artifact_query",),
    }
    return RuntimeProfileCatalog(
        agents=profiles,
        prompts=(
            PromptProfile(
                template_id="runtime-generic",
                version="c8-1",
                template_text="Use governed evidence and task-dependent reasoning.",
            ),
        ),
        models=(
            ModelProfile(
                profile_key="runtime-default",
                version="c8-1",
                model_identifier="mock/provider-model",
                provider_config=ProviderConfigRef(
                    config_id="external-mock", provider="mock"
                ),
            ),
        ),
        schemas=(ResponseSchemaRef(),),
        capabilities=tuple(
            CapabilityProfile(
                profile_key=profile.capability_profile_key,
                version="c8-1",
                capability_allowlist=allowed[profile.capability_profile_key],
            )
            for profile in profiles
        ),
    )


@pytest.fixture
def governed_boundary(tmp_path):
    projects = InMemoryProjectStore()
    projects.register(
        Project(project_id="project-c8", lab_id="lab-c8", owner_user_id="user-c8")
    )
    access = AccessService(projects, AuthorizationPolicy())
    principal = Principal(user_id="user-c8", lab_id="lab-c8")
    workspace = WorkspaceContext(
        user_id="user-c8", project_id="project-c8", lab_id="lab-c8"
    )
    store = LocalArtifactStore(tmp_path / "artifacts")
    derived = store.register(
        artifact_type="synthetic-c8-derived",
        exposure_class=ArtifactExposureClass.DERIVED,
        release_basis=ArtifactReleaseBasis.TRUSTED_EXECUTION_DECLASSIFICATION,
        representation=ArtifactRepresentation(summary={"bounded_metric": 3}),
        owner_user_id=principal.user_id,
        project_id=workspace.project_id,
        lab_id=workspace.lab_id,
    )
    raw = store.register(
        artifact_type="synthetic-c8-raw",
        exposure_class=ArtifactExposureClass.RAW,
        representation=ArtifactRepresentation(summary={"raw_value": "hidden"}),
        owner_user_id=principal.user_id,
        project_id=workspace.project_id,
        lab_id=workspace.lab_id,
    )
    exposure = ArtifactExposureService(
        store, ExposurePolicy(), access_service=access
    )
    return principal, workspace, store, exposure, derived, raw


def _stage_input() -> RuntimeStageInput:
    return RuntimeStageInput(
        run_id=uuid4(),
        stage_id=WorkflowStage.EXECUTE,
        invocation_id=uuid4(),
        instruction="Assess a synthetic scientific artifact and proceed directly or delegate where useful.",
        workspace=RuntimeWorkspaceIdentifiers(
            user_id="user-c8", project_id="project-c8", lab_id="lab-c8"
        ),
        allowed_capabilities=("artifact_query", "execution_submit"),
    )


def _result() -> RuntimeStageResult:
    return RuntimeStageResult(
        stage_id=WorkflowStage.EXECUTE,
        summary="Synthetic C8 capability exercise completed.",
        body=ExecuteStageBody(execution_status="NOT_REQUESTED"),
        next_action=NextActionProposal(
            action=NextAction.TRANSITION,
            target_stage=WorkflowStage.VALIDATE,
        ),
    )


@contextmanager
def _agent_run_context(agent, kwargs):
    token = _RUN_CONTEXT.set(
        AgentRunContext(
            agent=agent,
            memory=kwargs.get("memory"),
            execution_context_id=kwargs.get("execution_context_id"),
            process_step_message=kwargs.get("process_step_message"),
            process_chunk=kwargs.get("process_chunk"),
        )
    )
    try:
        yield
    finally:
        _RUN_CONTEXT.reset(token)


class _RecordingExecutionService:
    def __init__(self):
        self.calls = 0

    async def submit(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("unconfigured specialist reached execution service")


class _C8Factory(PantheonRuntimeFactory):
    def __init__(self, catalog, *, artifact_id, delegate):
        super().__init__(catalog)
        self.artifact_id = artifact_id
        self.delegate = delegate
        self.toolsets = {}
        self.listed_agents = ""
        self.selected_target = None
        self.root_memory = None
        self.child_memory = None
        self.child_prose = "specialist model assessment is not evidence"

    async def create_team(self, profile_keys, **kwargs):
        team, prompts = await super().create_team(profile_keys, **kwargs)
        if kwargs.get("invocation_mode") is RuntimeInvocationMode.CAPABILITY:
            self.toolsets = dict(kwargs["toolsets"])
            agents = {agent.name: agent for agent in team.team_agents}
            root = agents["ExecutionAgent"]
            specialist = agents["SingleCellAnalysisSpecialist"]

            async def specialist_run(agent_self, _message, **run_kwargs):
                self.child_memory = run_kwargs["memory"]
                toolset = self.toolsets[SPECIALIST_KEY]
                assert any(
                    provider.toolset is toolset
                    for provider in agent_self.providers.values()
                )
                result = await toolset.artifact_query(
                    artifact_id=str(self.artifact_id),
                    view_type="SUMMARY",
                )
                assert result["success"]
                await run_kwargs["process_step_message"](
                    {"role": "assistant", "content": "bounded specialist step"}
                )
                return AgentResponse(
                    agent_name=agent_self.name,
                    content=self.child_prose,
                    details=None,
                )

            async def root_run(agent_self, _message, **run_kwargs):
                self.root_memory = run_kwargs["memory"]
                own = await self.toolsets[ROOT_KEY].artifact_query(
                    artifact_id=str(self.artifact_id),
                    view_type="SUMMARY",
                )
                assert own["success"]
                if self.delegate:
                    context = {
                        "tool_call_id": "c8-runtime-selected-target",
                        "_metadata": {"chain_path": ["executionagent"]},
                    }
                    with _agent_run_context(agent_self, run_kwargs):
                        self.listed_agents = await agent_self.functions["list_agents"](
                            context_variables=context
                        )
                        assert "singlecellanalysisspecialist" in self.listed_agents
                        assert "scientificmethodsreviewer" in self.listed_agents
                        self.selected_target = "SingleCellAnalysisSpecialist"
                        await agent_self.functions["call_agent"](
                            agent_name=self.selected_target,
                            instruction="Provide an independent assessment using governed evidence where useful.",
                            context_variables=context,
                        )
                return AgentResponse(
                    agent_name=agent_self.name,
                    content="root capability phase completed",
                    details=None,
                )

            specialist.run = MethodType(specialist_run, specialist)
            root.run = MethodType(root_run, root)
        else:
            async def finalize(_team_self, _message, **_kwargs):
                return AgentResponse(
                    agent_name="ExecutionAgent",
                    content=_result().model_dump(mode="json"),
                    details=None,
                )

            team.run = MethodType(finalize, team)
        return team, prompts


def _assembly() -> RuntimeStageAssemblySpec:
    return RuntimeStageAssemblySpec(
        stage_id=WorkflowStage.EXECUTE,
        root_profile_key=ROOT_KEY,
        prompt_template_key="runtime-generic",
        capability_allowlist=("artifact_query", "execution_submit"),
        capability_peer_specs=(
            RuntimeAgentCapabilitySpec(
                profile_key=SPECIALIST_KEY,
                capability_allowlist=("artifact_query",),
            ),
            RuntimeAgentCapabilitySpec(
                profile_key=REVIEWER_KEY,
                capability_allowlist=(),
            ),
        ),
    )


def test_specialist_profiles_are_small_and_do_not_encode_a_fixed_pipeline():
    profiles = scientific_specialist_profiles()
    assert [profile.agent_name for profile in profiles] == [
        "SingleCellAnalysisSpecialist",
        "ScientificMethodsReviewer",
    ]
    descriptions = " ".join(profile.role_description for profile in profiles).lower()
    assert "filter → normalize" not in descriptions
    assert len(profiles) == 2


def test_peer_configuration_cannot_exceed_profile_or_consumer_authority(
    governed_boundary,
):
    principal, workspace, store, exposure, _, _ = governed_boundary
    bad = RuntimeStageAssemblySpec(
        stage_id=WorkflowStage.EXECUTE,
        root_profile_key=ROOT_KEY,
        prompt_template_key="runtime-generic",
        capability_allowlist=("artifact_query",),
        capability_peer_specs=(
            RuntimeAgentCapabilitySpec(
                profile_key=SPECIALIST_KEY,
                capability_allowlist=("execution_submit",),
            ),
        ),
    )
    with pytest.raises(RuntimeProfileConfigurationError, match="peer capability profile"):
        PerInvocationPantheonStageInvoker(
            assembly=bad,
            factory=PantheonRuntimeFactory(_catalog()),
            principal=principal,
            workspace=workspace,
            services=RuntimeCapabilityServices(
                artifact_store=store, artifact_exposure=exposure
            ),
        )
    with pytest.raises(ValueError, match="REMOTE_LLM"):
        RuntimeCapabilityContext(
            principal=principal,
            workspace=workspace,
            run_id=uuid4(),
            stage_id=WorkflowStage.EXECUTE,
            invocation_id=uuid4(),
            actor_profile_key=SPECIALIST_KEY,
            actor_agent_name="SingleCellAnalysisSpecialist",
            capability_allowlist=("artifact_query",),
            consumer=ArtifactConsumer.SYSTEM,
        )


@pytest.mark.asyncio
async def test_delegated_specialist_owns_tools_and_evidence_is_attributed_and_aggregated(
    governed_boundary,
):
    principal, workspace, store, exposure, derived, raw = governed_boundary
    stage_input = _stage_input()
    sink = InMemoryTraceSink()
    recorder = RunTraceRecorder(sink)
    execution_service = _RecordingExecutionService()
    factory = _C8Factory(_catalog(), artifact_id=derived.artifact_id, delegate=True)
    observed = {}
    invoker = PerInvocationPantheonStageInvoker(
        assembly=_assembly(),
        factory=factory,
        principal=principal,
        workspace=workspace,
        services=RuntimeCapabilityServices(
            artifact_store=store,
            artifact_exposure=exposure,
            execution_submission=execution_service,
            trace_recorder=recorder,
        ),
        trace_recorder=recorder,
        plugin_factory=lambda: [
            DelegationPolicyPlugin(
                InMemoryDelegationPolicy(
                    {
                        "executionagent": {
                            "singlecellanalysisspecialist",
                            "scientificmethodsreviewer",
                        }
                    }
                )
            )
        ],
        boundary_observer=lambda kind, value: observed.setdefault(kind, value),
    )

    result = await invoker.invoke(stage_input)

    assert result.stage_id is WorkflowStage.EXECUTE
    assert set(factory.toolsets) == {ROOT_KEY, SPECIALIST_KEY, REVIEWER_KEY}
    root = factory.toolsets[ROOT_KEY]
    specialist = factory.toolsets[SPECIALIST_KEY]
    reviewer = factory.toolsets[REVIEWER_KEY]
    assert set(root.tool_functions) == {"artifact_query", "execution_submit"}
    assert set(specialist.tool_functions) == {"artifact_query"}
    assert set(reviewer.tool_functions) == set()
    bindings = [item.binding for item in factory.toolsets.values()]
    assert all(binding.principal is principal for binding in bindings)
    assert all(binding.workspace is workspace for binding in bindings)
    assert all(binding.run_id == stage_input.run_id for binding in bindings)
    assert all(binding.stage_id is WorkflowStage.EXECUTE for binding in bindings)
    assert all(binding.invocation_id == stage_input.invocation_id for binding in bindings)
    assert all(binding.consumer is ArtifactConsumer.REMOTE_LLM for binding in bindings)

    bundle = observed["capability_evidence"]
    assert [(item.actor_profile_key, item.capability_name) for item in bundle.items] == [
        (ROOT_KEY, "artifact_query"),
        (SPECIALIST_KEY, "artifact_query"),
    ]
    assert [item.actor_agent_name for item in bundle.items] == [
        "ExecutionAgent",
        "SingleCellAnalysisSpecialist",
    ]
    assert factory.root_memory is not factory.child_memory
    assert factory.selected_target == "SingleCellAnalysisSpecialist"
    assert factory.child_prose not in bundle.model_dump_json()

    capability_events = [
        event
        for event in sink.read(stage_input.run_id)
        if event.event_type
        in {TraceEventType.CAPABILITY_INVOKED, TraceEventType.CAPABILITY_COMPLETED}
    ]
    assert {event.agent_name for event in capability_events} == {
        "ExecutionAgent",
        "SingleCellAnalysisSpecialist",
    }
    assert {event.payload["actor_profile_key"] for event in capability_events} == {
        ROOT_KEY,
        SPECIALIST_KEY,
    }
    delegated = [
        event
        for event in sink.read(stage_input.run_id)
        if event.event_type is TraceEventType.DELEGATION_COMPLETED
    ]
    assert len(delegated) == 1
    assert delegated[0].target == "singlecellanalysisspecialist"
    assert delegated[0].parent_invocation_id == stage_input.invocation_id
    assert delegated[0].execution_context_id
    assert delegated[0].parent_tool_call_id == "c8-runtime-selected-target"
    assert delegated[0].chain_path

    assert "actor_profile_key" not in inspect.signature(
        specialist.artifact_query
    ).parameters
    denied_execution = await specialist.execution_submit({})
    assert denied_execution["error"]["error_code"] == "AUTHORIZATION_DENIED"
    assert execution_service.calls == 0
    denied_inheritance = await reviewer.artifact_query(
        str(derived.artifact_id), "SUMMARY"
    )
    assert denied_inheritance["error"]["error_code"] == "AUTHORIZATION_DENIED"
    raw_error_codes = []
    for toolset in (root, specialist):
        denied_raw = await toolset.artifact_query(str(raw.artifact_id), "SUMMARY")
        assert not denied_raw["success"]
        raw_error_codes.append(denied_raw["error"]["error_code"])
    assert raw_error_codes == ["CAPABILITY_FAILED", "CAPABILITY_FAILED"]
    encoded = "".join(item.model_dump_json() for item in bundle.items)
    for forbidden in (
        "storage_locator",
        "host_path",
        "provider_raw_body",
        "reasoning_content",
        "raw_value",
        "credentials",
    ):
        assert forbidden not in encoded


@pytest.mark.asyncio
async def test_configured_specialists_are_optional_for_unknown_task(
    governed_boundary,
):
    principal, workspace, store, exposure, derived, _ = governed_boundary
    stage_input = _stage_input()
    sink = InMemoryTraceSink()
    recorder = RunTraceRecorder(sink)
    factory = _C8Factory(_catalog(), artifact_id=derived.artifact_id, delegate=False)
    observed = {}
    invoker = PerInvocationPantheonStageInvoker(
        assembly=_assembly(),
        factory=factory,
        principal=principal,
        workspace=workspace,
        services=RuntimeCapabilityServices(
            artifact_store=store,
            artifact_exposure=exposure,
            trace_recorder=recorder,
        ),
        trace_recorder=recorder,
        plugin_factory=lambda: [
            DelegationPolicyPlugin(
                InMemoryDelegationPolicy(
                    {
                        "executionagent": {
                            "singlecellanalysisspecialist",
                            "scientificmethodsreviewer",
                        }
                    }
                )
            )
        ],
        boundary_observer=lambda kind, value: observed.setdefault(kind, value),
    )

    assert (await invoker.invoke(stage_input)).stage_id is WorkflowStage.EXECUTE
    bundle = observed["capability_evidence"]
    assert [item.actor_profile_key for item in bundle.items] == [ROOT_KEY]
    assert not any(
        event.event_type
        in {
            TraceEventType.DELEGATION_STARTED,
            TraceEventType.DELEGATION_COMPLETED,
            TraceEventType.DELEGATION_DENIED,
            TraceEventType.DELEGATION_FAILED,
        }
        for event in sink.read(stage_input.run_id)
    )


@pytest.mark.asyncio
async def test_multi_toolset_evidence_overflow_fails_instead_of_truncating(
    governed_boundary,
):
    principal, workspace, store, exposure, derived, _ = governed_boundary
    stage_input = _stage_input()
    toolset = LabBioRuntimeToolSet(
        RuntimeCapabilityContext(
            principal=principal,
            workspace=workspace,
            run_id=stage_input.run_id,
            stage_id=stage_input.stage_id,
            invocation_id=stage_input.invocation_id,
            actor_profile_key=ROOT_KEY,
            actor_agent_name="ExecutionAgent",
            capability_allowlist=("artifact_query",),
        ),
        RuntimeCapabilityServices(
            artifact_store=store,
            artifact_exposure=exposure,
        ),
    )
    factory = PantheonRuntimeFactory(_catalog())
    team, prompts = await factory.create_team(
        (ROOT_KEY,),
        toolsets={ROOT_KEY: toolset},
        invocation_mode=RuntimeInvocationMode.CAPABILITY,
    )

    async def run(_team_self, _message, **_kwargs):
        for _ in range(65):
            result = await toolset.artifact_query(
                str(derived.artifact_id), "SUMMARY"
            )
            assert result["success"]
        return AgentResponse(
            agent_name="ExecutionAgent", content="bounded overflow", details=None
        )

    team.run = MethodType(run, team)
    with pytest.raises(
        RuntimeProfileConfigurationError,
        match="Aggregated capability evidence exceeds 64 items",
    ):
        await PantheonCapabilityStageInvoker(
            team,
            profile=_catalog().agents[ROOT_KEY],
            prompt=prompts[ROOT_KEY],
            evidence_sources=(toolset,),
        ).invoke(stage_input)
    assert len(toolset.evidence_items()) == 65
