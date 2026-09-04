"""Milestone C2 tests for separated capability and typed-finalization turns."""

from __future__ import annotations

import json
from types import MethodType, SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError
from pantheon.agent import NoObservableProgressError, ProviderTurnObservation

from labbioagentos import (
    AccessService,
    AgentProfile,
    ArtifactExposureClass,
    ArtifactExposureService,
    ArtifactRepresentation,
    AuthorizationPolicy,
    CapabilityEvidenceBundle,
    CapabilityEvidenceItem,
    CapabilityEvidenceStatus,
    CapabilityProfile,
    InMemoryProjectStore,
    InMemoryTraceSink,
    InformationAuthority,
    IntakeStageBody,
    LabBioRuntimeToolSet,
    LocalArtifactStore,
    ModelProfile,
    NextAction,
    NextActionProposal,
    PantheonCapabilityStageInvoker,
    PantheonRuntimeFactory,
    PantheonRuntimeIntegrationError,
    PantheonTwoModeStageInvoker,
    PantheonTypedStageInvoker,
    Principal,
    Project,
    PromptProfile,
    ProviderConfigRef,
    ProviderThinkingWireFormat,
    ProviderTransport,
    ResponseSchemaRef,
    RunTraceRecorder,
    RuntimeCapabilityContext,
    RuntimeCapabilityServices,
    RuntimeCoordinatorService,
    RuntimeInvocationMode,
    RuntimeProfileCatalog,
    RuntimeProfileConfigurationError,
    RuntimeResultValidationError,
    RuntimeStageInput,
    RuntimeStageResult,
    RuntimeWorkspaceIdentifiers,
    StageRuntimeRegistry,
    StageRuntimeSpec,
    TraceEventType,
    ValidateStageBody,
    WorkflowEngine,
    WorkflowStage,
    WorkspaceContext,
    runtime_workflow_definition,
)
from labbioagentos.artifacts import ExposurePolicy


def _profile() -> AgentProfile:
    return AgentProfile(
        profile_key="coordinator",
        version="1",
        agent_name="CoordinatorAgent",
        role_description="Coordinate the current synthetic stage.",
        prompt_profile_key="runtime-generic",
        response_schema_key="runtime-stage-result",
        model_profile_key="runtime-default",
        capability_profile_key="coordinator-capabilities",
    )


def _catalog(*, model: ModelProfile | None = None) -> RuntimeProfileCatalog:
    return RuntimeProfileCatalog(
        agents=(_profile(),),
        prompts=(
            PromptProfile(
                template_id="runtime-generic",
                version="1",
                template_text="Use only the supplied synthetic context.",
            ),
        ),
        models=(
            model
            or ModelProfile(
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


def _stage_input(stage: WorkflowStage = WorkflowStage.INTAKE) -> RuntimeStageInput:
    return RuntimeStageInput(
        run_id=uuid4(),
        stage_id=stage,
        instruction="Synthetic integration request.",
        workspace=RuntimeWorkspaceIdentifiers(
            user_id="user-c2", project_id="project-c2", lab_id="lab-c2"
        ),
        allowed_capabilities=("artifact_list",),
    )


def _toolset(tmp_path, stage_input, recorder=None):
    projects = InMemoryProjectStore()
    projects.register(
        Project(
            project_id="project-c2", lab_id="lab-c2", owner_user_id="user-c2"
        )
    )
    access = AccessService(projects, AuthorizationPolicy())
    principal = Principal(user_id="user-c2", lab_id="lab-c2")
    workspace = WorkspaceContext(
        user_id="user-c2", project_id="project-c2", lab_id="lab-c2"
    )
    store = LocalArtifactStore(tmp_path / str(stage_input.invocation_id))
    store.register(
        artifact_type="synthetic-c2-result",
        exposure_class=ArtifactExposureClass.DERIVED,
        representation=ArtifactRepresentation(summary={"safe_count": 2}),
        owner_user_id="user-c2",
        project_id="project-c2",
        lab_id="lab-c2",
        run_id=stage_input.run_id,
        stage_id=stage_input.stage_id,
        producer_invocation_id=stage_input.invocation_id,
    )
    exposure = ArtifactExposureService(
        store, ExposurePolicy(), access_service=access
    )
    binding = RuntimeCapabilityContext(
        principal=principal,
        workspace=workspace,
        run_id=stage_input.run_id,
        stage_id=stage_input.stage_id,
        invocation_id=stage_input.invocation_id,
        actor_profile_key="coordinator",
        actor_agent_name="CoordinatorAgent",
        capability_allowlist=("artifact_list",),
    )
    return LabBioRuntimeToolSet(
        binding,
        RuntimeCapabilityServices(
            artifact_store=store,
            artifact_exposure=exposure,
            trace_recorder=recorder,
        ),
    )


def _intake_result() -> RuntimeStageResult:
    return RuntimeStageResult(
        stage_id=WorkflowStage.INTAKE,
        summary="Synthetic typed finalization.",
        body=IntakeStageBody(interpreted_goal="Synthetic goal."),
        next_action=NextActionProposal(
            action=NextAction.TRANSITION,
            target_stage=WorkflowStage.UNDERSTAND,
        ),
    )


@pytest.mark.asyncio
async def test_factory_separates_capability_tools_from_finalization_schema(tmp_path):
    factory = PantheonRuntimeFactory(_catalog())
    stage_input = _stage_input()
    toolset = _toolset(tmp_path, stage_input)
    capability_team, _ = await factory.create_team(
        ("coordinator",),
        toolsets={"coordinator": toolset},
        invocation_mode=RuntimeInvocationMode.CAPABILITY,
    )
    finalization_team, _ = await factory.create_team(
        ("coordinator",), invocation_mode=RuntimeInvocationMode.FINALIZE
    )
    assert capability_team.team_agents[0].response_format is None
    assert "artifact_list" in toolset.tool_functions
    assert finalization_team.team_agents[0].response_format is RuntimeStageResult
    assert all(
        not isinstance(provider, LabBioRuntimeToolSet)
        for provider in finalization_team.team_agents[0].providers.values()
    )
    with pytest.raises(RuntimeProfileConfigurationError):
        await factory.create_team(
            ("coordinator",),
            toolsets={"coordinator": toolset},
            invocation_mode=RuntimeInvocationMode.FINALIZE,
        )


@pytest.mark.asyncio
async def test_capability_mode_uses_structural_tool_loop_and_builds_bounded_evidence(tmp_path):
    sink = InMemoryTraceSink()
    recorder = RunTraceRecorder(sink)
    stage_input = _stage_input()
    toolset = _toolset(tmp_path, stage_input, recorder)
    factory = PantheonRuntimeFactory(_catalog())
    team, rendered = await factory.create_team(
        ("coordinator",),
        toolsets={"coordinator": toolset},
        invocation_mode=RuntimeInvocationMode.CAPABILITY,
    )

    async def run(_self, _message, **_kwargs):
        await toolset.artifact_list()
        return SimpleNamespace(content="structural provider loop completed")

    team.run = MethodType(run, team)
    before = toolset.evidence_items()
    invoker = PantheonCapabilityStageInvoker(
        team,
        profile=_profile(),
        prompt=rendered["coordinator"],
        evidence_sources=(toolset,),
        trace_recorder=recorder,
    )
    bundle = await invoker.invoke(stage_input)
    assert before == ()
    assert len(bundle.items) == 1
    item = bundle.items[0]
    assert item.capability_name == "artifact_list"
    assert item.status is CapabilityEvidenceStatus.COMPLETED
    assert item.trace_event_ids
    assert item.capability_invocation_id
    encoded = bundle.model_dump_json()
    assert "synthetic-c2-result" in encoded
    for forbidden in (
        "storage_locator",
        "host_path",
        "api_key",
        "provider_raw_body",
        "reasoning_content",
    ):
        assert forbidden not in encoded
    event_types = [event.event_type for event in sink.read(stage_input.run_id)]
    assert TraceEventType.CAPABILITY_PHASE_STARTED in event_types
    assert TraceEventType.CAPABILITY_INVOKED in event_types
    assert TraceEventType.CAPABILITY_COMPLETED in event_types
    assert TraceEventType.CAPABILITY_PHASE_COMPLETED in event_types


@pytest.mark.asyncio
async def test_capability_mode_persists_content_free_provider_turn_observation(tmp_path):
    sink = InMemoryTraceSink()
    recorder = RunTraceRecorder(sink)
    stage_input = _stage_input()
    factory = PantheonRuntimeFactory(_catalog())
    team, rendered = await factory.create_team(
        ("coordinator",), invocation_mode=RuntimeInvocationMode.CAPABILITY
    )

    async def run(_self, _message, **kwargs):
        kwargs["process_turn_observation"](
            ProviderTurnObservation(
                agent_name="CoordinatorAgent",
                execution_context_id=None,
                turn_index=1,
                progress_kind="REASONING_ONLY",
                observable_progress=False,
                elapsed_ms=1234,
                total_tokens=321,
                tool_names=(),
            )
        )
        return SimpleNamespace(content=None)

    team.run = MethodType(run, team)
    await PantheonCapabilityStageInvoker(
        team,
        profile=_profile(),
        prompt=rendered["coordinator"],
        trace_recorder=recorder,
        max_no_progress_seconds=300,
    ).invoke(stage_input)

    event = next(
        item
        for item in sink.read(stage_input.run_id)
        if item.event_type is TraceEventType.PROVIDER_TURN_OBSERVED
    )
    assert event.payload == {
        "invocation_mode": "CAPABILITY",
        "template_id": "runtime-generic",
        "template_version": "1",
        "template_hash": rendered["coordinator"].template_hash,
        "profile_key": "coordinator",
        "turn_index": 1,
        "progress_kind": "REASONING_ONLY",
        "observable_progress": False,
        "elapsed_ms": 1234,
        "total_tokens": 321,
        "tool_names": [],
    }
    encoded = event.model_dump_json()
    for forbidden in (
        "reasoning_content",
        "provider_request",
        "provider_response",
        "api_key",
        "script",
        "stdout",
    ):
        assert forbidden not in encoded


@pytest.mark.asyncio
async def test_capability_mode_maps_no_progress_budget_to_safe_error(tmp_path):
    sink = InMemoryTraceSink()
    recorder = RunTraceRecorder(sink)
    stage_input = _stage_input()
    factory = PantheonRuntimeFactory(_catalog())
    team, rendered = await factory.create_team(
        ("coordinator",), invocation_mode=RuntimeInvocationMode.CAPABILITY
    )

    async def run(_self, _message, **_kwargs):
        raise NoObservableProgressError(elapsed_ms=301000, turn_count=2)

    team.run = MethodType(run, team)
    invoker = PantheonCapabilityStageInvoker(
        team,
        profile=_profile(),
        prompt=rendered["coordinator"],
        trace_recorder=recorder,
        max_no_progress_seconds=300,
    )

    with pytest.raises(PantheonRuntimeIntegrationError) as caught:
        await invoker.invoke(stage_input)

    assert caught.value.error_code == "PROVIDER_NO_OBSERVABLE_PROGRESS"
    failed = next(
        item
        for item in sink.read(stage_input.run_id)
        if item.event_type is TraceEventType.CAPABILITY_PHASE_FAILED
    )
    assert failed.payload["error_code"] == "PROVIDER_NO_OBSERVABLE_PROGRESS"
    assert failed.payload["elapsed_ms"] == 301000
    assert failed.payload["turn_count"] == 2


@pytest.mark.asyncio
async def test_capability_mode_preserves_evidence_after_contentless_completion(tmp_path):
    stage_input = _stage_input()
    toolset = _toolset(tmp_path, stage_input)
    factory = PantheonRuntimeFactory(_catalog())
    team, rendered = await factory.create_team(
        ("coordinator",),
        toolsets={"coordinator": toolset},
        invocation_mode=RuntimeInvocationMode.CAPABILITY,
    )

    async def run(_self, _message):
        await toolset.artifact_list()
        return SimpleNamespace(content=None)

    team.run = MethodType(run, team)
    bundle = await PantheonCapabilityStageInvoker(
        team,
        profile=_profile(),
        prompt=rendered["coordinator"],
        evidence_sources=(toolset,),
    ).invoke(stage_input)

    assert [item.capability_name for item in bundle.items] == ["artifact_list"]
    assert bundle.explicit_completion is None
    assert bundle.technical_status == "COMPLETED"


@pytest.mark.asyncio
async def test_capability_mode_does_not_auto_invoke_or_use_prose_termination(tmp_path):
    stage_input = _stage_input()
    toolset = _toolset(tmp_path, stage_input)
    factory = PantheonRuntimeFactory(_catalog())
    team, rendered = await factory.create_team(
        ("coordinator",),
        toolsets={"coordinator": toolset},
        invocation_mode=RuntimeInvocationMode.CAPABILITY,
    )

    async def run(_self, _message):
        return SimpleNamespace(content="analyze mean group sufficient done")

    team.run = MethodType(run, team)
    bundle = await PantheonCapabilityStageInvoker(
        team,
        profile=_profile(),
        prompt=rendered["coordinator"],
        evidence_sources=(toolset,),
    ).invoke(stage_input)
    assert bundle.items == ()
    assert toolset.evidence_items() == ()


@pytest.mark.parametrize(
    "field",
    (
        "storage_locator",
        "host_path",
        "api_key",
        "provider_raw_body",
        "reasoning_content",
        "raw_matrix",
    ),
)
def test_capability_evidence_rejects_leak_fields(field):
    with pytest.raises(ValidationError):
        CapabilityEvidenceItem(
            actor_profile_key="coordinator",
            actor_agent_name="CoordinatorAgent",
            capability_name="artifact_query",
            information_authority=InformationAuthority.AUTHORITATIVE_EVIDENCE,
            status=CapabilityEvidenceStatus.COMPLETED,
            safe_result={"nested": {field: "prohibited"}},
        )


@pytest.mark.asyncio
async def test_finalization_receives_safe_evidence_and_returns_explicit_proposal():
    sink = InMemoryTraceSink()
    recorder = RunTraceRecorder(sink)
    factory = PantheonRuntimeFactory(_catalog())
    team, rendered = await factory.create_team(
        ("coordinator",), invocation_mode=RuntimeInvocationMode.FINALIZE
    )
    stage_input = _stage_input()
    evidence = CapabilityEvidenceBundle(
        run_id=stage_input.run_id,
        stage_id=stage_input.stage_id,
        invocation_id=stage_input.invocation_id,
        items=(
            CapabilityEvidenceItem(
                actor_profile_key="coordinator",
                actor_agent_name="CoordinatorAgent",
                capability_name="artifact_list",
                information_authority=InformationAuthority.AUTHORITATIVE_EVIDENCE,
                status=CapabilityEvidenceStatus.COMPLETED,
                safe_result={"artifact_id": str(uuid4()), "artifact_type": "synthetic"},
            ),
        ),
    )
    captured = {}
    result = _intake_result()

    async def run(_self, message):
        captured.update(json.loads(message))
        return SimpleNamespace(content=result.model_dump(mode="json"))

    team.run = MethodType(run, team)
    invoker = PantheonTypedStageInvoker(
        team,
        profile=_profile(),
        prompt=rendered["coordinator"],
        response_schema=ResponseSchemaRef(),
        trace_recorder=recorder,
    )
    assert await invoker.invoke(stage_input, capability_evidence=evidence) == result
    assert captured["capability_evidence"]["evidence_id"] == str(evidence.evidence_id)
    assert captured["stage_input"]["stage_id"] == "INTAKE"
    assert result.next_action.action is NextAction.TRANSITION
    event_types = [event.event_type for event in sink.read(stage_input.run_id)]
    assert TraceEventType.FINALIZATION_PHASE_STARTED in event_types
    assert TraceEventType.FINALIZATION_PHASE_COMPLETED in event_types


@pytest.mark.asyncio
async def test_capability_turn_cannot_mutate_workflow_and_only_finalize_reaches_coordinator():
    calls = []
    profile = _profile()

    class Capability:
        def __init__(self):
            self.profile = profile

        async def invoke(self, value):
            calls.append(("capability", value.stage_id))
            return CapabilityEvidenceBundle(
                run_id=value.run_id,
                stage_id=value.stage_id,
                invocation_id=value.invocation_id,
            )

    class Finalize:
        def __init__(self):
            self.profile = profile

        async def invoke(self, value, *, capability_evidence):
            calls.append(("finalize", capability_evidence.evidence_id))
            return _intake_result()

    invoker = PantheonTwoModeStageInvoker(Capability(), Finalize())
    registry = StageRuntimeRegistry(
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
    engine = WorkflowEngine(runtime_workflow_definition())
    coordinator = RuntimeCoordinatorService(engine, registry)
    projects = InMemoryProjectStore()
    projects.register(
        Project(project_id="project-c2", lab_id="lab-c2", owner_user_id="user-c2")
    )
    run = coordinator.create_run(
        principal=Principal(user_id="user-c2", lab_id="lab-c2"),
        workspace=WorkspaceContext(
            user_id="user-c2", project_id="project-c2", lab_id="lab-c2"
        ),
        access_service=AccessService(projects, AuthorizationPolicy()),
    )
    engine.start(run)
    before = tuple(run.stage_results)
    await coordinator.run_current_stage(run, instruction="Synthetic request.")
    assert before == ()
    assert [name for name, _ in calls] == ["capability", "finalize"]
    assert len(run.stage_results) == 1
    assert run.current_stage is WorkflowStage.UNDERSTAND


def test_stage_action_policy_is_projected_and_enforced():
    class UnusedInvoker:
        async def invoke(self, _stage_input):  # pragma: no cover
            raise AssertionError("not invoked")

    registry = StageRuntimeRegistry(
        (
            StageRuntimeSpec(
                stage_id=WorkflowStage.VALIDATE,
                profile_key="reviewer",
                prompt_template_key="runtime-generic",
                capability_allowlist=(),
                invoker=UnusedInvoker(),
                retry_enabled=False,
                user_input_enabled=False,
            ),
        )
    )
    engine = WorkflowEngine(runtime_workflow_definition())
    coordinator = RuntimeCoordinatorService(engine, registry)
    run = engine.create_run(retry_limit=1)
    engine.start(run)
    for stage in (
        WorkflowStage.UNDERSTAND,
        WorkflowStage.PLAN,
        WorkflowStage.PREFLIGHT,
        WorkflowStage.EXECUTE,
        WorkflowStage.VALIDATE,
    ):
        engine.transition(run, stage)

    stage_input = coordinator.build_stage_input(
        run,
        instruction="Validate only the technical result.",
    )
    assert stage_input.workflow_control is not None
    assert stage_input.workflow_control.retry_available is False
    assert stage_input.workflow_control.retry_transition_targets == ()
    assert stage_input.workflow_control.request_user_input_available is False

    retry = RuntimeStageResult(
        stage_id=WorkflowStage.VALIDATE,
        summary="Technical validation passed.",
        body=ValidateStageBody(
            technical_status="PASSED",
            runtime_assessment="Current execution is technically complete.",
        ),
        next_action=NextActionProposal(
            action=NextAction.RETRY,
            target_stage=WorkflowStage.EXECUTE,
            reason="Retry despite disabled policy.",
        ),
    )
    with pytest.raises(RuntimeResultValidationError):
        coordinator.accept_trusted_stage_result(run, retry, uuid4())

    request_input = retry.model_copy(
        update={
            "next_action": NextActionProposal(
                action=NextAction.REQUEST_USER_INPUT,
                user_prompt="Confirm whether to continue.",
            )
        }
    )
    with pytest.raises(RuntimeResultValidationError):
        coordinator.accept_trusted_stage_result(run, request_input, uuid4())


@pytest.mark.asyncio
async def test_chat_transport_uses_compatible_alias_for_pro_suffix(monkeypatch):
    monkeypatch.setenv("OPENAI_API_BASE", "https://example.invalid/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-test-key")
    model = ModelProfile(
        profile_key="runtime-default",
        version="1",
        model_identifier="mimo-v2.5-pro",
        provider_config=ProviderConfigRef(config_id="external-mimo", provider="mimo"),
        transport=ProviderTransport.OPENAI_CHAT_COMPLETIONS,
        thinking_enabled=False,
        thinking_wire_format=ProviderThinkingWireFormat.TYPE_OBJECT,
        max_output_tokens=1200,
    )
    agent, _ = await PantheonRuntimeFactory(_catalog(model=model)).create_agent(
        "coordinator", invocation_mode=RuntimeInvocationMode.CAPABILITY
    )
    assert agent.models[0].startswith("labbiochat-")
    assert agent.models[0].endswith("/mimo-v2.5-pro")
    assert agent.model_params == {
        "thinking": {"type": "disabled"},
        "max_tokens": 1200,
    }
