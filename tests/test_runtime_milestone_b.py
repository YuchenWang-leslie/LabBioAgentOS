"""Milestone B acceptance tests for the governed model-capability bridge."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import MethodType, SimpleNamespace
from uuid import uuid4

import pytest
from pantheon.agent import Agent
from pydantic import ValidationError

from labbioagentos import (
    AccessService,
    CAPABILITY_CEILINGS,
    AgentProfile,
    ArtifactConsumer,
    ArtifactExposureClass,
    ArtifactExposureService,
    ArtifactRepresentation,
    AuthorizationPolicy,
    CapabilityProfile,
    ExecutionPlanDraft,
    ExecutionReceipt,
    ExecutionResult,
    ExecutionRuntime,
    ExecutionStatus,
    ExecutionSubmissionService,
    GoldSkillService,
    InMemoryMemoryStore,
    InMemoryProjectStore,
    InMemorySkillStore,
    InMemoryTraceSink,
    IntakeStageBody,
    LabBioRuntimeToolSet,
    LocalArtifactStore,
    MemoryDecision,
    MemoryGovernanceService,
    MemoryKind,
    MemoryScope,
    ModelProfile,
    NextAction,
    NextActionProposal,
    PantheonRuntimeFactory,
    PantheonRuntimeIntegrationError,
    PantheonTypedStageInvoker,
    Principal,
    Project,
    PromptProfile,
    ProviderConfigRef,
    ProviderTransport,
    ReportSubmissionService,
    ResponseSchemaRef,
    RunStatus,
    RunTraceRecorder,
    RuntimeCapabilityContext,
    RuntimeCapabilityServices,
    RuntimeInvocationMode,
    RuntimeProfileCatalog,
    RuntimeStageInput,
    RuntimeStageResult,
    RuntimeWorkspaceIdentifiers,
    SkillProcedure,
    SkillProposal,
    SkillScope,
    SkillSourceBundle,
    SkillSourceProjector,
    SkillUserDecision,
    StageRuntimeSpec,
    TraceEventType,
    WorkflowStage,
    WorkspaceContext,
    default_agent_profiles,
)
from labbioagentos.artifacts import ExposurePolicy


@pytest.fixture
def boundary(tmp_path):
    sink = InMemoryTraceSink()
    recorder = RunTraceRecorder(sink)
    projects = InMemoryProjectStore()
    projects.register(Project(project_id="project-a", lab_id="lab-a", owner_user_id="user-a"))
    projects.register(Project(project_id="project-b", lab_id="lab-a", owner_user_id="user-b"))
    access = AccessService(projects, AuthorizationPolicy(), trace_recorder=recorder)
    principal = Principal(user_id="user-a", lab_id="lab-a")
    workspace = WorkspaceContext(user_id="user-a", project_id="project-a", lab_id="lab-a")
    store = LocalArtifactStore(tmp_path / "artifacts", trace_recorder=recorder)
    exposure = ArtifactExposureService(
        store, ExposurePolicy(), access_service=access, trace_recorder=recorder
    )
    return sink, recorder, access, principal, workspace, store, exposure


def _catalog(model_identifier="mock/provider-model"):
    capabilities = tuple(
        CapabilityProfile(
            profile_key=profile.capability_profile_key,
            version="1",
            capability_allowlist=(),
        )
        for profile in default_agent_profiles()
    )
    return RuntimeProfileCatalog(
        agents=default_agent_profiles(),
        prompts=(
            PromptProfile(
                template_id="runtime-generic",
                version="1",
                template_text="Follow the current typed stage contract. Boundary: {boundary}",
            ),
        ),
        models=(
            ModelProfile(
                profile_key="runtime-default",
                version="1",
                model_identifier=model_identifier,
                provider_config=ProviderConfigRef(
                    config_id="external-mock-config", provider="mock"
                ),
            ),
        ),
        schemas=(ResponseSchemaRef(),),
        capabilities=capabilities,
    )


@pytest.mark.asyncio
async def test_three_profiles_assemble_and_model_is_configuration_driven():
    factory = PantheonRuntimeFactory(_catalog("configured/mock-model"))
    team, prompts = await factory.create_team(
        ("coordinator", "execution", "reviewer"),
        prompt_values={
            key: {"boundary": "synthetic contracts only"}
            for key in ("coordinator", "execution", "reviewer")
        },
    )
    assert [agent.name for agent in team.team_agents] == [
        "CoordinatorAgent", "ExecutionAgent", "ReviewerAgent"
    ]
    assert all(agent.models == ["configured/mock-model"] for agent in team.team_agents)
    assert all(agent.response_format is RuntimeStageResult for agent in team.team_agents)
    assert set(prompts) == {"coordinator", "execution", "reviewer"}
    assert not any("route" in profile.role_description.lower() for profile in default_agent_profiles())


def test_prompt_is_versioned_hashed_bounded_and_rejects_bad_input():
    prompt = PromptProfile(template_id="generic", version="2", template_text="Hello {value}")
    rendered = prompt.render({"value": "bounded"})
    assert rendered.version == "2" and len(rendered.template_hash) == 64
    with pytest.raises(ValueError):
        prompt.render({"value": "x" * 2001})
    with pytest.raises(ValueError):
        prompt.render({"wrong": "value"})


def test_provider_transport_and_thinking_are_trusted_model_configuration():
    profile = ModelProfile(
        profile_key="mimo",
        version="1",
        model_identifier="mimo-v2.5-pro",
        provider_config=ProviderConfigRef(config_id="external", provider="mimo"),
        transport=ProviderTransport.OPENAI_CHAT_COMPLETIONS,
        thinking_enabled=False,
        max_output_tokens=1200,
    )
    dumped = profile.model_dump(mode="json")
    assert dumped["transport"] == "OPENAI_CHAT_COMPLETIONS"
    assert dumped["thinking_enabled"] is False
    assert dumped["max_output_tokens"] == 1200
    assert "api_key" not in dumped and "base_url" not in dumped


def _intake_input():
    return RuntimeStageInput(
        run_id=uuid4(),
        stage_id=WorkflowStage.INTAKE,
        instruction="Synthetic stage input.",
        workspace=RuntimeWorkspaceIdentifiers(
            user_id="user-a", project_id="project-a", lab_id="lab-a"
        ),
    )


@pytest.mark.asyncio
async def test_typed_invoker_records_prompt_metadata_and_validates_result():
    sink = InMemoryTraceSink()
    recorder = RunTraceRecorder(sink)
    factory = PantheonRuntimeFactory(_catalog())
    team, rendered = await factory.create_team(
        ("coordinator",), prompt_values={"coordinator": {"boundary": "bounded"}}
    )
    stage_input = _intake_input()
    result = RuntimeStageResult(
        stage_id=WorkflowStage.INTAKE,
        summary="Synthetic typed result.",
        body=IntakeStageBody(interpreted_goal="Synthetic goal."),
        next_action=NextActionProposal(
            action=NextAction.TRANSITION, target_stage=WorkflowStage.UNDERSTAND
        ),
    )

    async def run(_self, _message):
        return SimpleNamespace(content=result.model_dump(mode="json"))

    team.run = MethodType(run, team)
    invoker = PantheonTypedStageInvoker(
        team,
        profile=_catalog().agents["coordinator"],
        prompt=rendered["coordinator"],
        response_schema=ResponseSchemaRef(),
        trace_recorder=recorder,
    )
    assert await invoker.invoke(stage_input) == result
    instruction = next(event for event in sink.read() if event.event_type is TraceEventType.INSTRUCTION_RECORDED)
    record = instruction.payload["instruction"]
    assert (record["template_id"], record["template_version"]) == ("runtime-generic", "1")
    assert record["template_hash"] == rendered["coordinator"].template_hash


@pytest.mark.asyncio
async def test_malformed_runtime_result_is_bounded_and_raw_body_not_traced():
    sink = InMemoryTraceSink()
    recorder = RunTraceRecorder(sink)
    factory = PantheonRuntimeFactory(_catalog())
    team, rendered = await factory.create_team(
        ("coordinator",), prompt_values={"coordinator": {"boundary": "bounded"}}
    )
    secret = "RAW_PROVIDER_BODY_SHOULD_NOT_APPEAR"

    async def run(_self, _message):
        return SimpleNamespace(content={"unexpected": secret})

    team.run = MethodType(run, team)
    invoker = PantheonTypedStageInvoker(
        team,
        profile=_catalog().agents["coordinator"],
        prompt=rendered["coordinator"],
        response_schema=ResponseSchemaRef(),
        trace_recorder=recorder,
    )
    with pytest.raises(PantheonRuntimeIntegrationError) as caught:
        await invoker.invoke(_intake_input())
    assert caught.value.error_code == "MALFORMED_RUNTIME_RESULT"
    assert secret not in json.dumps([event.model_dump(mode="json") for event in sink.read()])


@pytest.mark.asyncio
async def test_pantheon_native_validation_failure_is_classified_as_malformed():
    factory = PantheonRuntimeFactory(_catalog())
    team, rendered = await factory.create_team(
        ("coordinator",), prompt_values={"coordinator": {"boundary": "bounded"}}
    )

    async def run(_self, _message):
        RuntimeStageResult.model_validate({"invalid": "provider value"})

    team.run = MethodType(run, team)
    invoker = PantheonTypedStageInvoker(
        team,
        profile=_catalog().agents["coordinator"],
        prompt=rendered["coordinator"],
        response_schema=ResponseSchemaRef(),
    )
    with pytest.raises(PantheonRuntimeIntegrationError) as caught:
        await invoker.invoke(_intake_input())
    assert caught.value.error_code == "MALFORMED_RUNTIME_RESULT"


def _toolset(boundary, stage, capabilities, **service_overrides):
    _, recorder, access, principal, workspace, store, exposure = boundary
    services = RuntimeCapabilityServices(
        artifact_store=store,
        artifact_exposure=exposure,
        trace_recorder=recorder,
        **service_overrides,
    )
    binding = RuntimeCapabilityContext(
        principal=principal,
        workspace=workspace,
        run_id=uuid4(),
        stage_id=stage,
        invocation_id=uuid4(),
        actor_profile_key="coordinator",
        actor_agent_name="CoordinatorAgent",
        capability_allowlist=capabilities,
    )
    return LabBioRuntimeToolSet(binding, services)


def test_stage_spec_allowlist_controls_tool_exposure_and_never_auto_calls(boundary):
    calls = []

    class Invoker:
        async def invoke(self, value):
            calls.append(value)

    spec = StageRuntimeSpec(
        stage_id=WorkflowStage.INTAKE,
        profile_key="coordinator",
        prompt_template_key="runtime-generic",
        capability_allowlist=("artifact_list",),
        invoker=Invoker(),
    )
    _, _, _, principal, workspace, _, _ = boundary
    binding = RuntimeCapabilityContext.from_stage_spec(
        spec,
        principal=principal,
        workspace=workspace,
        run_id=uuid4(),
        invocation_id=uuid4(),
        actor_profile_key="coordinator",
        actor_agent_name="CoordinatorAgent",
    )
    toolset = _toolset(boundary, WorkflowStage.INTAKE, binding.capability_allowlist)
    assert set(toolset.tool_functions) == {"artifact_list"}
    assert calls == []


@pytest.mark.parametrize(
    ("stage", "expected"),
    (
        (WorkflowStage.INTAKE, {"artifact_list", "artifact_query"}),
        (WorkflowStage.UNDERSTAND, {"artifact_list", "artifact_query", "skill_search", "skill_view", "memory_search", "memory_view"}),
        (WorkflowStage.PLAN, {"artifact_query", "skill_search", "skill_view", "skill_propose_use", "memory_search", "memory_view"}),
        (WorkflowStage.PREFLIGHT, {"artifact_query"}),
        (WorkflowStage.EXECUTE, {"artifact_query", "execution_submit"}),
        (WorkflowStage.VALIDATE, {"artifact_query"}),
        (WorkflowStage.INTERPRET, {"artifact_query"}),
        (WorkflowStage.REPORT, {"artifact_query", "report_submit"}),
        (WorkflowStage.LEARN, {"skill_search", "skill_view", "memory_search", "memory_view", "memory_propose_update"}),
    ),
)
def test_stage_capability_ceiling_is_exact(stage, expected):
    assert set(CAPABILITY_CEILINGS[stage]) == expected


@pytest.mark.asyncio
async def test_pantheon_tool_schema_contains_only_stage_allowed_functions(boundary):
    toolset = _toolset(
        boundary, WorkflowStage.INTAKE, ("artifact_list", "artifact_query")
    )
    description = await toolset.list_tools()
    assert {item["name"] for item in description["tools"]} == {
        "artifact_list", "artifact_query"
    }


@pytest.mark.asyncio
async def test_disallowed_tool_cannot_be_invoked(boundary):
    toolset = _toolset(boundary, WorkflowStage.INTAKE, ("artifact_list",))
    result = await toolset.execution_submit({})
    assert result["success"] is False
    assert result["error"]["error_code"] == "AUTHORIZATION_DENIED"


@pytest.mark.asyncio
async def test_native_pantheon_delegation_is_separate_from_labbio_tools(boundary):
    factory = PantheonRuntimeFactory(_catalog())
    toolset = _toolset(boundary, WorkflowStage.INTAKE, ("artifact_list",))
    team, _ = await factory.create_team(
        ("coordinator", "execution"),
        prompt_values={
            "coordinator": {"boundary": "bounded"},
            "execution": {"boundary": "bounded"},
        },
        toolsets={"coordinator": toolset},
        invocation_mode=RuntimeInvocationMode.CAPABILITY,
    )
    await team.async_setup()
    assert {"list_agents", "call_agent"}.issubset(team.team_agents[0].functions)
    assert "call_agent" not in toolset.tool_functions


@pytest.mark.asyncio
async def test_artifact_tools_are_authorized_bounded_and_locator_free(boundary):
    _, _, _, _, _, store, _ = boundary
    own = store.register(
        artifact_type="synthetic-result",
        exposure_class=ArtifactExposureClass.DERIVED,
        representation=ArtifactRepresentation(summary={"count": 2}),
        owner_user_id="user-a", project_id="project-a", lab_id="lab-a",
    )
    other = store.register(
        artifact_type="other-result",
        exposure_class=ArtifactExposureClass.DERIVED,
        representation=ArtifactRepresentation(summary={"count": 9}),
        owner_user_id="user-b", project_id="project-b", lab_id="lab-a",
    )
    toolset = _toolset(
        boundary, WorkflowStage.INTAKE, ("artifact_list", "artifact_query")
    )
    listing = await toolset.artifact_list(limit=10)
    encoded = json.dumps(listing)
    assert str(own.artifact_id) in encoded and str(other.artifact_id) not in encoded
    assert "storage_locator" not in encoded
    query = await toolset.artifact_query(str(own.artifact_id), "SUMMARY")
    assert query["success"] and query["data"]["artifact_id"] == str(own.artifact_id)
    denied = await toolset.artifact_query(str(other.artifact_id), "SUMMARY")
    assert denied["error"]["error_code"] == "AUTHORIZATION_DENIED"
    assert "storage_locator" not in json.dumps(query)


def test_execution_draft_forbids_trusted_and_host_fields():
    for field in ("run_id", "project_id", "user_id", "host_path", "docker_argv", "mounts"):
        with pytest.raises(ValidationError):
            ExecutionPlanDraft.model_validate(
                {"image_key": "approved", "script_content": "print(1)", field: "bad"}
            )


class MockExecutor:
    def __init__(self, store):
        self.store = store
        self.plans = []

    def execute(self, plan):
        self.plans.append(plan)
        kwargs = dict(
            owner_user_id=plan.owner_user_id,
            project_id=plan.project_id,
            lab_id=plan.lab_id,
            run_id=plan.run_id,
            stage_id=plan.stage_id,
            producer_invocation_id=plan.invocation_id,
        )
        script = self.store.register(
            artifact_type="script", exposure_class=ArtifactExposureClass.RAW,
            representation=ArtifactRepresentation(), **kwargs,
        )
        output = self.store.register(
            artifact_type="output", exposure_class=ArtifactExposureClass.DERIVED,
            representation=ArtifactRepresentation(summary={"ok": True}), **kwargs,
        )
        now = datetime.now(timezone.utc)
        return ExecutionResult(
            execution_id=plan.execution_id,
            run_id=plan.run_id,
            stage_id=plan.stage_id,
            invocation_id=plan.invocation_id,
            status=ExecutionStatus.SUCCEEDED,
            image_key=plan.image_key,
            resolved_image="internal-approved-image@sha256:123",
            script_hash="a" * 64,
            script_ref=script,
            output_artifact_refs=(output,),
            exit_code=0,
            started_at=now,
            completed_at=now,
            duration_seconds=0,
        )


@pytest.mark.asyncio
async def test_execution_host_injects_scope_authorizes_inputs_and_returns_receipt(boundary):
    _, recorder, access, principal, workspace, store, _ = boundary
    input_ref = store.register(
        artifact_type="input", exposure_class=ArtifactExposureClass.RAW,
        representation=ArtifactRepresentation(), owner_user_id="user-a",
        project_id="project-a", lab_id="lab-a",
    )
    executor = MockExecutor(store)
    service = ExecutionSubmissionService(
        artifact_store=store, access_service=access, executor=executor,
        trace_recorder=recorder,
    )
    run_id, invocation_id = uuid4(), uuid4()
    receipt = await service.submit(
        ExecutionPlanDraft(
            image_key="approved", script_content="print('synthetic')",
            input_artifact_ids=(input_ref.artifact_id,),
        ),
        principal=principal, workspace=workspace, run_id=run_id,
        stage_id=WorkflowStage.EXECUTE, invocation_id=invocation_id,
    )
    plan = executor.plans[0]
    assert (plan.owner_user_id, plan.project_id, plan.lab_id, plan.run_id) == (
        "user-a", "project-a", "lab-a", run_id
    )
    assert isinstance(receipt, ExecutionReceipt)
    assert set(receipt.model_dump()) >= {"execution_id", "output_artifact_ids"}
    assert "storage_locator" not in receipt.model_dump_json()
    output = store.get_ref(receipt.output_artifact_ids[0])
    assert (output.owner_user_id, output.project_id, output.lab_id, output.run_id) == (
        "user-a", "project-a", "lab-a", run_id
    )


@pytest.mark.asyncio
async def test_cross_project_execution_input_rejected_before_executor(boundary):
    _, _, access, principal, workspace, store, _ = boundary
    foreign = store.register(
        artifact_type="input", exposure_class=ArtifactExposureClass.RAW,
        representation=ArtifactRepresentation(), owner_user_id="user-b",
        project_id="project-b", lab_id="lab-a",
    )
    executor = MockExecutor(store)
    service = ExecutionSubmissionService(
        artifact_store=store, access_service=access, executor=executor
    )
    with pytest.raises(PermissionError):
        await service.submit(
            ExecutionPlanDraft(
                image_key="approved", script_content="print(1)",
                input_artifact_ids=(foreign.artifact_id,),
            ),
            principal=principal, workspace=workspace, run_id=uuid4(),
            stage_id=WorkflowStage.EXECUTE, invocation_id=uuid4(),
        )
    assert executor.plans == []


def _skill_service(boundary):
    _, _, access, principal, _, store, _ = boundary
    skill_store = InMemorySkillStore()
    service = GoldSkillService(
        skill_store, SkillSourceProjector(store), access_service=access
    )
    bundle = SkillSourceBundle(
        source_run_id=uuid4(), final_status=RunStatus.COMPLETED,
        workflow_stage_path=(WorkflowStage.PLAN,), trace_event_ids=(uuid4(),),
    )
    skill_store.save_source_bundle(bundle)
    proposal = SkillProposal(
        source_bundle_id=bundle.bundle_id, source_run_id=bundle.source_run_id,
        proposed_name="Synthetic skill", description="Procedural context only.",
        scope=SkillScope.PERSONAL, owner_user_id="user-a", lab_id="lab-a",
        procedure=SkillProcedure(
            applicability="Runtime model decides relevance.",
            workflow_outline=("Review prior evidence.",), tags=frozenset({"synthetic"}),
        ),
    )
    skill_store.save_proposal(proposal)
    gold = skill_store.decide_proposal(
        proposal.proposal_id,
        SkillUserDecision(
            subject_id=proposal.proposal_id, gate_id=proposal.approval_gate_id,
            approved=True, decided_by="user-a",
        ),
    )
    return service, skill_store, gold, principal


@pytest.mark.asyncio
async def test_skill_tools_return_candidates_no_score_and_pending_use(boundary):
    service, store, gold, _ = _skill_service(boundary)
    toolset = _toolset(
        boundary, WorkflowStage.PLAN,
        ("skill_search", "skill_view", "skill_propose_use"), skill_service=service,
    )
    search = await toolset.skill_search()
    encoded = json.dumps(search)
    assert search["success"] and str(gold.skill_id) in encoded
    assert not any(word in encoded for word in ("similarity_score", '"ranking"', '"mode"'))
    assert "workflow_outline" not in encoded
    proposed = await toolset.skill_propose_use(
        str(gold.skill_id), 1, "REFERENCE", "Runtime-provided reason."
    )
    assert proposed["data"]["status"] == "USER_APPROVAL_REQUIRED"
    use_proposal = store.get_use_proposal(
        uuid_from(proposed["data"]["proposal_id"])
    )
    assert use_proposal.skill_id == gold.skill_id
    authorization = service.decide_use(
        use_proposal.proposal_id,
        SkillUserDecision(
            subject_id=use_proposal.proposal_id,
            gate_id=use_proposal.approval_gate_id,
            approved=True,
            decided_by="user-a",
        ),
        principal=toolset.binding.principal,
    )
    view = await toolset.skill_view(str(authorization.authorization_id))
    assert view["data"]["applicability"] == "Runtime model decides relevance."
    assert not any(key in view["data"] for key in ("execute", "run", "apply"))


def uuid_from(value):
    from uuid import UUID
    return UUID(value)


def _memory_service(boundary):
    _, _, access, principal, _, _, _ = boundary
    store = InMemoryMemoryStore()
    service = MemoryGovernanceService(store, access)
    proposal = __import__("labbioagentos").MemoryUpdateProposal(
        target_scope=MemoryScope.PERSONAL, owner_user_id="user-a", lab_id="lab-a",
        proposed_kind=MemoryKind.OPERATING_NOTE,
        proposed_content="Synthetic persistent note.", reason="Synthetic reason.",
    )
    service.submit_proposal(principal, proposal)
    entry = service.decide(
        principal, proposal.proposal_id,
        MemoryDecision(
            proposal_id=proposal.proposal_id, gate_id=proposal.approval_gate_id,
            approved=True, decided_by="user-a",
        ),
    )
    return service, store, entry


@pytest.mark.asyncio
async def test_memory_tools_obey_scope_and_only_create_pending_proposal(boundary):
    service, store, entry = _memory_service(boundary)
    toolset = _toolset(
        boundary, WorkflowStage.LEARN,
        ("memory_search", "memory_view", "memory_propose_update"),
        memory_service=service,
    )
    search = await toolset.memory_search(query_text="persistent")
    assert search["success"] and search["data"][0]["memory_id"] == str(entry.memory_id)
    view = await toolset.memory_view(str(entry.memory_id), 1)
    assert view["data"]["content"] == "Synthetic persistent note."
    before = store.entries()
    receipt = await toolset.memory_propose_update(
        {
            "target_scope": "PERSONAL", "proposed_kind": "PREFERENCE",
            "proposed_content": "Runtime supplied content.",
            "reason": "Runtime supplied reason.",
        }
    )
    assert receipt["data"]["status"] == "USER_APPROVAL_REQUIRED"
    assert store.entries() == before
    assert store.get_proposal(uuid_from(receipt["data"]["proposal_id"]))


@pytest.mark.asyncio
async def test_report_submission_is_scoped_path_free_and_trace_content_safe(boundary):
    sink, recorder, access, _, _, store, _ = boundary
    service = ReportSubmissionService(store, access, trace_recorder=recorder)
    toolset = _toolset(
        boundary, WorkflowStage.REPORT, ("report_submit",), report_submission=service
    )
    report_text = "SENSITIVE_SYNTHETIC_REPORT_BODY"
    result = await toolset.report_submit("Synthetic title", report_text, [])
    assert result["success"] and set(result["data"]) == {"report_artifact_id", "status"}
    ref = store.get_ref(result["data"]["report_artifact_id"])
    assert (ref.owner_user_id, ref.project_id, ref.lab_id) == (
        "user-a", "project-a", "lab-a"
    )
    assert "storage_locator" not in json.dumps(result)
    trace_json = json.dumps([event.model_dump(mode="json") for event in sink.read()])
    assert report_text not in trace_json
    assert str(ref.artifact_id) in trace_json


@pytest.mark.asyncio
async def test_tool_errors_and_trace_are_bounded_sanitized(boundary):
    sink, *_ = boundary
    toolset = _toolset(boundary, WorkflowStage.INTAKE, ("artifact_query",))
    host_secret = "/Users/private/secret-file"
    result = await toolset.artifact_query(host_secret, "SUMMARY")
    encoded = json.dumps(result)
    assert result["error"]["error_code"] == "INVALID_IDENTIFIER"
    assert host_secret not in encoded
    trace = json.dumps([event.model_dump(mode="json") for event in sink.read()])
    assert host_secret not in trace
    assert "correlation_id" in trace
