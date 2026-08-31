"""Hermetic acceptance tests for C4 production integration gaps."""

from __future__ import annotations

import json
from types import MethodType, SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from labbioagentos import (
    AccessService,
    AgentProfile,
    ApprovedImage,
    ApprovedImageRegistry,
    ArtifactExposureClass,
    ArtifactExposureService,
    ArtifactRegistrationPolicy,
    ArtifactRepresentation,
    AuthorizationPolicy,
    CapabilityProfile,
    ExecutionPolicy,
    ExecutionPreflightError,
    ExecutionPreflightRequest,
    ExecutionPreflightService,
    ExecutionRuntime,
    InMemoryProjectStore,
    InMemoryTraceSink,
    IntakeStageBody,
    LabBioRuntimeToolSet,
    LocalArtifactStore,
    ModelProfile,
    NextAction,
    NextActionProposal,
    PerInvocationPantheonStageInvoker,
    PreflightInputRequirement,
    Principal,
    Project,
    PromptProfile,
    ProviderConfigRef,
    RequestedResources,
    ResponseSchemaRef,
    RunTraceRecorder,
    RuntimeCapabilityServices,
    RuntimeInvocationMode,
    RuntimePriorResultView,
    RuntimeProfileCatalog,
    RuntimeProfileConfigurationError,
    RuntimeStageAssemblySpec,
    RuntimeStageInput,
    RuntimeStageResult,
    RuntimeWorkspaceIdentifiers,
    StageRuntimeRegistry,
    StageRuntimeSpec,
    StructuredOutputContract,
    TraceEventType,
    WorkflowStage,
    WorkspaceContext,
)
from labbioagentos.artifacts import ExposurePolicy


def _profile() -> AgentProfile:
    return AgentProfile(
        profile_key="coordinator",
        version="1",
        agent_name="CoordinatorAgent",
        role_description="Coordinate a synthetic stage.",
        prompt_profile_key="runtime-generic",
        response_schema_key="runtime-stage-result",
        model_profile_key="runtime-default",
        capability_profile_key="coordinator-capabilities",
    )


def _catalog() -> RuntimeProfileCatalog:
    return RuntimeProfileCatalog(
        agents=(_profile(),),
        prompts=(
            PromptProfile(
                template_id="runtime-generic",
                version="c4-1",
                template_text="Protocol: {protocol}",
            ),
        ),
        models=(
            ModelProfile(
                profile_key="runtime-default",
                version="1",
                model_identifier="mock/provider-model",
                provider_config=ProviderConfigRef(
                    config_id="external-mock", provider="mock"
                ),
            ),
        ),
        schemas=(ResponseSchemaRef(),),
        capabilities=(
            CapabilityProfile(
                profile_key="coordinator-capabilities",
                version="1",
                capability_allowlist=("artifact_list",),
            ),
        ),
    )


@pytest.fixture
def boundary(tmp_path):
    projects = InMemoryProjectStore()
    projects.register(
        Project(project_id="project-c4", lab_id="lab-c4", owner_user_id="user-c4")
    )
    access = AccessService(projects, AuthorizationPolicy())
    principal = Principal(user_id="user-c4", lab_id="lab-c4")
    workspace = WorkspaceContext(
        user_id="user-c4", project_id="project-c4", lab_id="lab-c4"
    )
    store = LocalArtifactStore(tmp_path / "artifacts")
    exposure = ArtifactExposureService(
        store, ExposurePolicy(), access_service=access
    )
    return access, principal, workspace, store, exposure


def _input(invocation_id=None, *, workspace=None):
    return RuntimeStageInput(
        run_id=uuid4(),
        stage_id=WorkflowStage.INTAKE,
        invocation_id=invocation_id or uuid4(),
        instruction="Synthetic instruction mentioning REPORT and EXECUTE.",
        workspace=workspace
        or RuntimeWorkspaceIdentifiers(
            user_id="user-c4", project_id="project-c4", lab_id="lab-c4"
        ),
        allowed_capabilities=("artifact_list",),
    )


def _result():
    return RuntimeStageResult(
        stage_id=WorkflowStage.INTAKE,
        summary="Validated synthetic intake.",
        body=IntakeStageBody(interpreted_goal="Synthetic goal."),
        next_action=NextActionProposal(
            action=NextAction.TRANSITION,
            target_stage=WorkflowStage.UNDERSTAND,
        ),
    )


@pytest.mark.asyncio
async def test_per_invocation_assembly_uses_fresh_bound_toolsets_and_separates_modes(
    boundary,
):
    _, principal, workspace, store, exposure = boundary
    made_toolsets = []
    made_teams = []
    workflow_state = {"stage": "INTAKE"}

    def toolset_factory(binding, services):
        value = LabBioRuntimeToolSet(binding, services)
        made_toolsets.append(value)
        return value

    from labbioagentos import PantheonRuntimeFactory

    class RecordingFactory(PantheonRuntimeFactory):
        async def create_team(self, profile_keys, **kwargs):
            team, prompts = await super().create_team(profile_keys, **kwargs)
            mode = kwargs.get("invocation_mode")
            made_teams.append((mode, team))
            if mode is RuntimeInvocationMode.CAPABILITY:
                async def run(_self, _message, **_kwargs):
                    assert workflow_state["stage"] == "INTAKE"
                    return SimpleNamespace(content="Explicit capability outcome.")
            else:
                async def run(_self, _message, **_kwargs):
                    return SimpleNamespace(content=_result().model_dump(mode="json"))
            team.run = MethodType(run, team)
            return team, prompts

    factory = RecordingFactory(_catalog())
    assembly = RuntimeStageAssemblySpec(
        stage_id=WorkflowStage.INTAKE,
        root_profile_key="coordinator",
        prompt_template_key="runtime-generic",
        capability_allowlist=("artifact_list",),
        capability_prompt_values={"protocol": "CAPABILITY tools, no schema"},
        finalization_prompt_values={"protocol": "FINALIZE strict schema, no tools"},
        preserve_capability_completion=True,
    )
    invoker = PerInvocationPantheonStageInvoker(
        assembly=assembly,
        factory=factory,
        principal=principal,
        workspace=workspace,
        services=RuntimeCapabilityServices(
            artifact_store=store,
            artifact_exposure=exposure,
        ),
        toolset_factory=toolset_factory,
    )
    StageRuntimeRegistry(
        (
            StageRuntimeSpec(
                stage_id=WorkflowStage.INTAKE,
                profile_key="coordinator",
                prompt_template_key="runtime-generic",
                capability_allowlist=("artifact_list",),
                invoker=invoker,
            ),
        )
    )
    first, second = _input(), _input()
    assert (await invoker.invoke(first)).stage_id is WorkflowStage.INTAKE
    assert (await invoker.invoke(second)).stage_id is WorkflowStage.INTAKE
    assert made_toolsets[0] is not made_toolsets[1]
    assert [item.binding.invocation_id for item in made_toolsets] == [
        first.invocation_id,
        second.invocation_id,
    ]
    capability_teams = [team for mode, team in made_teams if mode is RuntimeInvocationMode.CAPABILITY]
    final_teams = [team for mode, team in made_teams if mode is RuntimeInvocationMode.FINALIZE]
    assert all(team.team_agents[0].response_format is None for team in capability_teams)
    assert all(team.team_agents[0].response_format is RuntimeStageResult for team in final_teams)
    assert all(
        not any(
            isinstance(provider, LabBioRuntimeToolSet)
            for provider in team.team_agents[0].providers.values()
        )
        for team in final_teams
    )
    assert workflow_state == {"stage": "INTAKE"}


@pytest.mark.asyncio
async def test_trusted_binding_and_registry_config_cannot_be_overridden(boundary):
    _, principal, workspace, store, exposure = boundary
    from labbioagentos import PantheonRuntimeFactory

    invoker = PerInvocationPantheonStageInvoker(
        assembly=RuntimeStageAssemblySpec(
            stage_id=WorkflowStage.INTAKE,
            root_profile_key="coordinator",
            prompt_template_key="runtime-generic",
            capability_allowlist=("artifact_list",),
            capability_prompt_values={"protocol": "capability"},
            finalization_prompt_values={"protocol": "finalize"},
        ),
        factory=PantheonRuntimeFactory(_catalog()),
        principal=principal,
        workspace=workspace,
        services=RuntimeCapabilityServices(
            artifact_store=store, artifact_exposure=exposure
        ),
    )
    forged = _input(
        workspace=RuntimeWorkspaceIdentifiers(
            user_id="user-c4", project_id="project-other", lab_id="lab-c4"
        )
    )
    with pytest.raises(RuntimeProfileConfigurationError):
        await invoker.invoke(forged)
    with pytest.raises(RuntimeProfileConfigurationError):
        StageRuntimeRegistry(
            (
                StageRuntimeSpec(
                    stage_id=WorkflowStage.INTAKE,
                    profile_key="coordinator",
                    prompt_template_key="runtime-generic",
                    capability_allowlist=("artifact_query",),
                    invoker=invoker,
                ),
            )
        )


def test_prior_result_view_is_validated_bounded_and_not_a_provider_conversation():
    result = _result()
    view = RuntimePriorResultView.from_result(result)
    assert view.result_id == result.result_id
    assert view.structured_body == result.body.model_dump(mode="json")
    dumped = json.dumps(view.model_dump(mode="json"))
    assert "provider" not in dumped.lower()
    with pytest.raises(ValidationError):
        RuntimePriorResultView(
            result_id=uuid4(),
            stage_id=WorkflowStage.INTAKE,
            summary="bad",
            body_kind="INTAKE",
            structured_body={"provider_raw_body": "forbidden"},
        )
    with pytest.raises(ValidationError):
        values = _input().model_dump()
        values["prior_results"] = tuple(view for _ in range(10))
        RuntimeStageInput(**values)


def _preflight(boundary, recorder=None):
    access, _, _, store, _ = boundary
    contract = StructuredOutputContract(
        contract_id="c4-records-v1",
        schema_id="c4.records.v1",
        allowed_fields=frozenset({"record_type", "value"}),
        required_fields=frozenset({"record_type"}),
    )
    return ExecutionPreflightService(
        artifact_store=store,
        access_service=access,
        image_registry=ApprovedImageRegistry(
            (
                ApprovedImage(
                    key="python-c4",
                    reference="python:3.11-slim",
                    runtime=ExecutionRuntime.PYTHON,
                ),
            )
        ),
        execution_policy=ExecutionPolicy(
            allow_network=False,
            max_cpus=1,
            max_memory_mb=256,
            max_pids=64,
            max_timeout_seconds=30,
        ),
        registration_policy=ArtifactRegistrationPolicy((contract,)),
        trace_recorder=recorder,
    )


def test_deterministic_preflight_checks_public_policy_and_returns_safe_receipt(boundary):
    _, principal, workspace, store, _ = boundary
    raw = store.register(
        artifact_type="csv",
        exposure_class=ArtifactExposureClass.RAW,
        representation=ArtifactRepresentation(),
        owner_user_id=principal.user_id,
        project_id=workspace.project_id,
        lab_id=workspace.lab_id,
    )
    sink = InMemoryTraceSink()
    recorder = RunTraceRecorder(sink)
    run_id = uuid4()
    request = ExecutionPreflightRequest(
        image_key="python-c4",
        input_requirements=(
            PreflightInputRequirement(
                artifact_id=raw.artifact_id,
                exposure_class=ArtifactExposureClass.RAW,
            ),
        ),
        resources=RequestedResources(
            cpus=1, memory_mb=256, pids_limit=64, timeout_seconds=30
        ),
        network_required=False,
        output_contract_ids=("c4-records-v1",),
    )
    receipt = _preflight(boundary, recorder).require_ready(
        request,
        principal=principal,
        workspace=workspace,
        run_id=run_id,
    )
    assert receipt.structurally_valid
    assert receipt.approved_schema_ids == ("c4.records.v1",)
    encoded = receipt.model_dump_json()
    for forbidden in ("storage_locator", "python:3.11-slim", "argv", "script"):
        assert forbidden not in encoded
    assert sink.read(run_id)[-1].event_type is TraceEventType.PREFLIGHT_COMPLETED


@pytest.mark.parametrize(
    "change",
    (
        {"image_key": "unknown"},
        {"network_required": True},
        {"resources": RequestedResources(cpus=2, memory_mb=256, pids_limit=64, timeout_seconds=30)},
        {"output_contract_ids": ("unknown-contract",)},
    ),
)
def test_deterministic_preflight_rejects_unapproved_execution_envelope(boundary, change):
    _, principal, workspace, store, _ = boundary
    raw = store.register(
        artifact_type="csv",
        exposure_class=ArtifactExposureClass.RAW,
        representation=ArtifactRepresentation(),
        owner_user_id=principal.user_id,
        project_id=workspace.project_id,
        lab_id=workspace.lab_id,
    )
    values = {
        "image_key": "python-c4",
        "input_requirements": (
            PreflightInputRequirement(
                artifact_id=raw.artifact_id,
                exposure_class=ArtifactExposureClass.RAW,
            ),
        ),
        "resources": RequestedResources(
            cpus=1, memory_mb=256, pids_limit=64, timeout_seconds=30
        ),
        "network_required": False,
        "output_contract_ids": ("c4-records-v1",),
    }
    values.update(change)
    with pytest.raises(ExecutionPreflightError):
        _preflight(boundary).require_ready(
            ExecutionPreflightRequest(**values),
            principal=principal,
            workspace=workspace,
            run_id=uuid4(),
        )
