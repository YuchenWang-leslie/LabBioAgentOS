"""C9 deterministic acceptance for safe, durable, governed Gold Skills."""

from __future__ import annotations

import json
from types import MethodType, SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pantheon.agent import Agent
from pantheon.providers import LocalProvider
from pydantic import ValidationError

from labbioagentos import (
    AccessService,
    AgentProfile,
    ApplicationRunRequest,
    ApplicationRunStateError,
    ApplicationRuntimeConfiguration,
    ArtifactExposureClass,
    ArtifactExposureService,
    ArtifactRepresentation,
    AuthorizationDenied,
    AuthorizationPolicy,
    CAPABILITY_INFORMATION_AUTHORITY,
    CapabilityEvidenceBundle,
    CapabilityProfile,
    GateUserDecision,
    GoldSkillService,
    InMemoryProjectStore,
    InMemorySkillStore,
    InMemoryTraceSink,
    InformationAuthority,
    InstructionKind,
    LabBioApplication,
    LabBioRuntimeToolSet,
    LocalArtifactStore,
    ModelProfile,
    NextAction,
    NextActionProposal,
    PantheonSkillCurator,
    PerInvocationPantheonStageInvoker,
    Principal,
    Project,
    PromptProfile,
    ProviderConfigRef,
    ResponseSchemaRef,
    RunStatus,
    RunTraceRecorder,
    RuntimeCapabilityContext,
    RuntimeCapabilityServices,
    RuntimeProfileCatalog,
    RuntimeStageAssemblySpec,
    RuntimeStageResult,
    SKILL_CURATOR_INSTRUCTIONS,
    SQLiteSkillStore,
    SkillApprovalRequiredError,
    SkillCapabilityUsageRef,
    SkillCuratorDraft,
    SkillExecutionRef,
    SkillInstructionRef,
    SkillProcedureDraft,
    SkillProposalContext,
    SkillScope,
    SkillSearchContext,
    SkillSourceBundle,
    SkillSourceProjector,
    SkillStoreError,
    SkillTraceRef,
    SkillUsageOutcome,
    SkillUseMode,
    SkillUseProposal,
    SkillUserDecision,
    SkillVersionConflictError,
    SkillDomainDecisionHandler,
    TraceEventType,
    WorkflowStage,
    WorkspaceContext,
)
from labbioagentos.artifacts import ExposurePolicy
from labbioagentos.runtime.contracts import (
    ExecuteStageBody,
    IntakeStageBody,
    InterpretStageBody,
    LearnStageBody,
    PlanStageBody,
    PreflightStageBody,
    ReportStageBody,
    UnderstandStageBody,
    ValidateStageBody,
)


MAIN_PATH = (
    WorkflowStage.INTAKE,
    WorkflowStage.UNDERSTAND,
    WorkflowStage.PLAN,
    WorkflowStage.PREFLIGHT,
    WorkflowStage.EXECUTE,
    WorkflowStage.VALIDATE,
    WorkflowStage.INTERPRET,
    WorkflowStage.REPORT,
    WorkflowStage.LEARN,
)


def _governed(tmp_path, *, store=None, trace=True):
    projects = InMemoryProjectStore()
    projects.register(
        Project(project_id="project-a", lab_id="lab-a", owner_user_id="user-a")
    )
    sink = InMemoryTraceSink()
    recorder = RunTraceRecorder(sink) if trace else None
    access = AccessService(
        projects, AuthorizationPolicy(), trace_recorder=recorder
    )
    principal = Principal(user_id="user-a", lab_id="lab-a")
    workspace = WorkspaceContext(
        user_id="user-a", project_id="project-a", lab_id="lab-a"
    )
    artifacts = LocalArtifactStore(
        tmp_path / f"artifacts-{uuid4()}", trace_recorder=recorder
    )
    exposure = ArtifactExposureService(
        artifacts,
        ExposurePolicy(),
        access_service=access,
        trace_recorder=recorder,
    )
    skill_store = store or InMemorySkillStore()
    service = GoldSkillService(
        skill_store,
        SkillSourceProjector(artifacts),
        access_service=access,
        trace_recorder=recorder,
    )
    return (
        principal,
        workspace,
        artifacts,
        exposure,
        skill_store,
        service,
        sink,
        recorder,
    )


def _source_bundle(artifacts, *, run_id=None):
    run_id = run_id or uuid4()
    invocation_id = uuid4()
    ref = artifacts.register(
        artifact_type="generic-analysis-result",
        exposure_class=ArtifactExposureClass.RAW,
        representation=ArtifactRepresentation(
            stored_content={"private_value": "RAW_PRIVATE_SENTINEL_4319"}
        ),
        owner_user_id="user-a",
        project_id="project-a",
        lab_id="lab-a",
        run_id=run_id,
        stage_id=WorkflowStage.EXECUTE,
        producer_invocation_id=invocation_id,
        metadata={"private_observation_id": "OBS_PRIVATE_SENTINEL_9821"},
    )
    instruction_event_id = uuid4()
    execution_id = uuid4()
    return SkillSourceBundle(
        source_run_id=run_id,
        task_reference="generic successful analysis task",
        final_status=RunStatus.COMPLETED,
        workflow_stage_path=(
            WorkflowStage.PLAN,
            WorkflowStage.EXECUTE,
            WorkflowStage.VALIDATE,
            WorkflowStage.REPORT,
        ),
        instruction_refs=(
            SkillInstructionRef(
                instruction_id=uuid4(),
                trace_event_id=instruction_event_id,
                stage_id=WorkflowStage.PLAN,
                invocation_id=invocation_id,
                kind=InstructionKind.PLANNING,
                sanitized_instruction="Use current evidence and validate current outputs.",
            ),
        ),
        execution_refs=(
            SkillExecutionRef(
                execution_id=execution_id,
                image_key="python-analysis",
                resolved_image="local/python-analysis:3.11",
                script_hash="a" * 64,
                script_artifact_id=ref.artifact_id,
                input_artifact_ids=(ref.artifact_id,),
                output_artifact_ids=(ref.artifact_id,),
                status="SUCCEEDED",
                exit_code=0,
            ),
        ),
        artifact_ids=(ref.artifact_id,),
        artifact_refs=(ref,),
        retry_refs=(
            SkillTraceRef(
                event_id=uuid4(),
                sequence=2,
                event_type="RETRY_STARTED",
                stage_id=WorkflowStage.EXECUTE,
                status="RUNNING",
            ),
        ),
        validation_refs=(
            SkillTraceRef(
                event_id=uuid4(),
                sequence=3,
                event_type="STAGE_COMPLETED",
                stage_id=WorkflowStage.VALIDATE,
                status="SUCCEEDED",
            ),
        ),
        capability_usage_refs=(
            SkillCapabilityUsageRef(
                capability_invocation_id=uuid4(),
                actor_profile_key="execution",
                actor_agent_name="ExecutionAgent",
                capability_name="execution_submit",
                status="COMPLETED",
                reference_ids=(execution_id, ref.artifact_id),
            ),
        ),
        trace_event_ids=(instruction_event_id, uuid4(), uuid4()),
    )


def _draft(name="Generic validated analysis procedure"):
    return SkillCuratorDraft(
        proposed_name=name,
        description="Reusable procedural context from a validated run.",
        procedure=SkillProcedureDraft(
            applicability="Use when the current task has a compatible governed data contract.",
            workflow_outline=(
                "Inspect current governed inputs and choose a current-task plan.",
                "Execute fresh work and validate current outputs.",
            ),
            agent_collaboration_guidance=(
                "Choose available specialists only when useful for the current task.",
            ),
            execution_guidance=("Generate fresh task-specific code.",),
            validation_expectations=("Validate the current execution evidence.",),
            known_failure_modes=("A prior execution required a bounded retry.",),
            known_limitations=("Prior scientific facts do not prove current facts.",),
            tags=frozenset({"validated-analysis"}),
            artifact_types=frozenset({"generic-analysis-result"}),
        ),
    )


def _context(*, parent=None, source_usage_record_id=None):
    return SkillProposalContext(
        scope=SkillScope.PERSONAL,
        owner_user_id="user-a",
        lab_id="lab-a",
        parent_skill_id=(parent.skill_id if parent is not None else None),
        parent_version=(parent.version if parent is not None else None),
        source_usage_record_id=source_usage_record_id,
    )


def _create_gold(service, store, principal, artifacts, *, bundle=None, draft=None, context=None):
    bundle = bundle or _source_bundle(artifacts)
    store.save_source_bundle(bundle)
    proposal = service.create_proposal(
        bundle.bundle_id,
        draft or _draft(),
        context or _context(),
    )
    gold = service.decide_proposal(
        proposal.proposal_id,
        SkillUserDecision(
            subject_id=proposal.proposal_id,
            gate_id=proposal.approval_gate_id,
            approved=True,
            decided_by=principal.user_id,
        ),
        principal=principal,
    )
    assert gold is not None
    return bundle, proposal, gold


def _submit_use(service, principal, workspace, gold, run_id, *, mode=SkillUseMode.REFERENCE):
    proposal = SkillUseProposal(
        run_id=run_id,
        requesting_user_id=principal.user_id,
        project_id=workspace.project_id,
        lab_id=workspace.lab_id,
        skill_id=gold.skill_id,
        skill_version=gold.version,
        proposed_mode=mode,
        reason="Runtime judged this candidate useful for the current task.",
        proposed_deviations=(
            ("Use current task parameters.",) if mode is SkillUseMode.ADAPT else ()
        ),
    )
    service.submit_use_proposal(proposal, principal=principal)
    return proposal


def _decide_use(service, principal, proposal, *, approved=True):
    return service.decide_use(
        proposal.proposal_id,
        SkillUserDecision(
            subject_id=proposal.proposal_id,
            gate_id=proposal.approval_gate_id,
            approved=approved,
            decided_by=principal.user_id,
        ),
        principal=principal,
    )


def _toolset(principal, workspace, artifacts, exposure, service, run_id, capabilities, recorder=None):
    return LabBioRuntimeToolSet(
        RuntimeCapabilityContext(
            principal=principal,
            workspace=workspace,
            run_id=run_id,
            stage_id=WorkflowStage.PLAN,
            invocation_id=uuid4(),
            actor_profile_key="coordinator",
            actor_agent_name="CoordinatorAgent",
            capability_allowlist=capabilities,
        ),
        RuntimeCapabilityServices(
            artifact_store=artifacts,
            artifact_exposure=exposure,
            skill_service=service,
            trace_recorder=recorder,
        ),
    )


@pytest.mark.asyncio
async def test_s1_s4_safe_curator_projection_and_trusted_proposal_assembly(tmp_path):
    principal, _, artifacts, _, store, service, _, _ = _governed(tmp_path)
    bundle = _source_bundle(artifacts)
    store.save_source_bundle(bundle)
    assert "storage_locator" in bundle.model_dump_json()
    assert "OBS_PRIVATE_SENTINEL_9821" in bundle.model_dump_json()

    view = service.create_curation_view(bundle.bundle_id)
    encoded = view.model_dump_json()
    for forbidden in (
        "storage_locator",
        "OBS_PRIVATE_SENTINEL_9821",
        "RAW_PRIVATE_SENTINEL_4319",
        str(artifacts.root),
        "stored_content",
        "script_content",
        "stdout",
        "stderr",
        "provider_raw_body",
        "reasoning_content",
    ):
        assert forbidden not in encoded
    assert view.artifact_descriptors[0].artifact_id == bundle.artifact_ids[0]
    assert view.capability_usage_refs[0].actor_agent_name == "ExecutionAgent"

    with pytest.raises(ValidationError):
        SkillCuratorDraft.model_validate(
            {**_draft().model_dump(), "scope": "LAB", "source_run_id": str(uuid4())}
        )
    with pytest.raises(ValidationError, match="prohibited unsafe text"):
        SkillCuratorDraft.model_validate(
            {
                **_draft().model_dump(),
                "description": "Read /media/private/source before reuse.",
            }
        )

    captured = {}
    agent = Agent(
        name="SkillCuratorAgent",
        instructions=SKILL_CURATOR_INSTRUCTIONS,
        model="openai/mock",
        response_format=SkillCuratorDraft,
        use_memory=False,
    )

    async def run(_self, message, **_kwargs):
        captured.update(json.loads(message))
        return SimpleNamespace(content=_draft())

    agent.run = MethodType(run, agent)
    curator = PantheonSkillCurator(agent)
    curator_draft = await curator.propose(view)
    assert curator_draft == _draft()
    assert "storage_locator" not in json.dumps(captured)

    proposal = service.create_proposal(bundle.bundle_id, curator_draft, _context())
    assert proposal.source_bundle_id == bundle.bundle_id
    assert proposal.source_run_id == bundle.source_run_id
    assert proposal.scope is SkillScope.PERSONAL
    assert proposal.owner_user_id == principal.user_id
    assert proposal.procedure.important_instruction_ids == (
        bundle.instruction_refs[0].instruction_id,
    )
    assert "scope" not in SkillCuratorDraft.model_json_schema()["properties"]


@pytest.mark.asyncio
async def test_s5_s8_capability_information_authority_is_item_level(tmp_path):
    principal, workspace, artifacts, exposure, store, service, _, recorder = _governed(tmp_path)
    _, _, gold = _create_gold(service, store, principal, artifacts)
    derived = artifacts.register(
        artifact_type="derived-measurements",
        exposure_class=ArtifactExposureClass.DERIVED,
        representation=ArtifactRepresentation(summary={"count": 3}),
        owner_user_id=principal.user_id,
        project_id=workspace.project_id,
        lab_id=workspace.lab_id,
    )
    run_id = uuid4()
    toolset = _toolset(
        principal,
        workspace,
        artifacts,
        exposure,
        service,
        run_id,
        ("artifact_query", "skill_search", "skill_view", "skill_propose_use"),
        recorder,
    )

    artifact = await toolset.artifact_query(str(derived.artifact_id), "SUMMARY")
    search = await toolset.skill_search(query_text="Generic")
    use = await toolset.skill_propose_use(
        str(gold.skill_id), gold.version, "REFERENCE", "Useful context."
    )
    use_proposal = store.get_use_proposal(UUID(use["data"]["proposal_id"]))
    authorization = _decide_use(service, principal, use_proposal)
    view = await toolset.skill_view(str(authorization.authorization_id))
    private_query = "C9_PRIVATE_SEARCH_QUERY_7142"
    private_tag = "C9_PRIVATE_SEARCH_TAG_8124"
    private_type = "C9_PRIVATE_SEARCH_TYPE_9317"
    hidden = await toolset.skill_search(
        query_text=private_query,
        required_tags=[private_tag],
        artifact_types=[private_type],
        include_lab=False,
        limit=7,
    )
    assert hidden["success"] and hidden["data"] == []

    assert artifact["information_authority"] == "AUTHORITATIVE_EVIDENCE"
    assert search["information_authority"] == "MODEL_CONTEXT"
    assert view["information_authority"] == "MODEL_CONTEXT"
    assert view["data"]["authority"] == "MODEL_CONTEXT"
    assert use["information_authority"] == "CONTROL_STATE"
    by_name = {item.capability_name: item for item in toolset.evidence_items()}
    assert by_name["artifact_query"].information_authority is InformationAuthority.AUTHORITATIVE_EVIDENCE
    assert by_name["skill_search"].information_authority is InformationAuthority.MODEL_CONTEXT
    assert by_name["skill_view"].information_authority is InformationAuthority.MODEL_CONTEXT
    assert by_name["skill_propose_use"].information_authority is InformationAuthority.CONTROL_STATE
    search_audit = tuple(
        item.skill_search_request
        for item in toolset.evidence_items()
        if item.capability_name == "skill_search"
    )[-1]
    assert search_audit is not None
    assert search_audit.model_dump() == {
        "query_text_supplied": True,
        "required_tag_count": 1,
        "artifact_type_count": 1,
        "include_lab": False,
        "limit": 7,
    }
    persisted = json.dumps(
        {
            "trace": [event.model_dump(mode="json") for event in recorder.sink.read(run_id)],
            "evidence": [
                item.model_dump(mode="json") for item in toolset.evidence_items()
            ],
        }
    )
    for private_value in (private_query, private_tag, private_type):
        assert private_value not in persisted
    bundle = CapabilityEvidenceBundle(
        run_id=run_id,
        stage_id=WorkflowStage.PLAN,
        invocation_id=toolset.binding.invocation_id,
        items=toolset.evidence_items(),
    )
    assert bundle.authority_mode == "ITEM_LEVEL"
    assert set(CAPABILITY_INFORMATION_AUTHORITY) == {
        "artifact_list",
        "artifact_query",
        "execution_submit",
        "report_submit",
        "skill_search",
        "skill_view",
        "memory_search",
        "memory_view",
        "skill_propose_use",
        "memory_propose_update",
    }

    provider = LocalProvider(toolset)
    await provider.initialize()
    schemas = {item.name: item.inputSchema for item in await provider.list_tools()}
    search_parameters = schemas["skill_search"]["parameters"]
    assert "literal substring" in search_parameters["properties"]["query_text"][
        "description"
    ]
    assert "exact tags" in search_parameters["properties"]["required_tags"][
        "description"
    ]
    assert "exact Artifact types" in search_parameters["properties"][
        "artifact_types"
    ]["description"]
    mode_schema = schemas["skill_propose_use"]["parameters"]["properties"]["mode"]
    assert mode_schema["type"] == "string"
    assert mode_schema["enum"] == ["REUSE", "ADAPT", "REFERENCE"]
    assert "Model-selected" in mode_schema["description"]
    assert schemas["skill_view"]["parameters"]["required"] == [
        "authorization_id"
    ]


@pytest.mark.asyncio
async def test_s9_s11_full_context_requires_exact_run_version_user_and_scope(tmp_path):
    principal, workspace, artifacts, exposure, store, service, _, recorder = _governed(tmp_path)
    _, _, gold = _create_gold(service, store, principal, artifacts)
    run_id = uuid4()
    toolset = _toolset(
        principal,
        workspace,
        artifacts,
        exposure,
        service,
        run_id,
        ("skill_search", "skill_view"),
        recorder,
    )
    search = await toolset.skill_search(query_text="Generic")
    assert search["success"]
    assert "workflow_outline" not in json.dumps(search)

    rejected_proposal = _submit_use(service, principal, workspace, gold, run_id)
    rejected = _decide_use(service, principal, rejected_proposal, approved=False)
    denied = await toolset.skill_view(str(rejected.authorization_id))
    assert denied["error"]["error_code"] == "SKILL_APPROVAL_REQUIRED"

    proposal = _submit_use(service, principal, workspace, gold, run_id)
    authorization = _decide_use(service, principal, proposal)
    approved = await toolset.skill_view(str(authorization.authorization_id))
    assert approved["success"]
    assert approved["data"]["workflow_outline"]
    assert approved["data"]["skill_id"] == str(gold.skill_id)
    assert approved["data"]["version"] == gold.version

    with pytest.raises(SkillApprovalRequiredError):
        service.get_authorized_context(
            authorization.authorization_id,
            run_id=uuid4(),
            project_id=workspace.project_id,
            principal=principal,
        )
    with pytest.raises(SkillApprovalRequiredError):
        service.get_authorized_context(
            authorization.authorization_id,
            run_id=run_id,
            project_id="project-b",
            principal=principal,
        )
    with pytest.raises(SkillApprovalRequiredError):
        service.get_authorized_context(
            authorization.authorization_id,
            run_id=run_id,
            project_id=workspace.project_id,
            principal=Principal(user_id="user-b", lab_id="lab-a"),
        )


def test_s12_s15_sqlite_restart_rejection_and_immutability(tmp_path):
    database = tmp_path / "skills.sqlite3"
    store = SQLiteSkillStore(database)
    principal, workspace, artifacts, _, _, service, _, _ = _governed(
        tmp_path, store=store
    )
    bundle, proposal, gold = _create_gold(service, store, principal, artifacts)
    approved_run_id = uuid4()
    approved_proposal = _submit_use(
        service, principal, workspace, gold, approved_run_id
    )
    approved = _decide_use(service, principal, approved_proposal)
    service.get_authorized_context(
        approved.authorization_id,
        run_id=approved_run_id,
        project_id=workspace.project_id,
        principal=principal,
    )
    usage = service.finalize_run_usage(
        approved_run_id, RunStatus.COMPLETED
    )[0]
    rejected_proposal = _submit_use(service, principal, workspace, gold, uuid4())
    rejected = _decide_use(service, principal, rejected_proposal, approved=False)
    store.close()

    reopened = SQLiteSkillStore(database)
    assert reopened.get_source_bundle(bundle.bundle_id) == bundle
    assert reopened.get_proposal(proposal.proposal_id) == proposal
    assert reopened.get_gold(gold.skill_id, gold.version) == gold
    assert reopened.get_authorization(approved.authorization_id) == approved
    assert reopened.get_authorization(rejected.authorization_id) == rejected
    assert reopened.get_context_access(approved.authorization_id).run_id == approved_run_id
    assert reopened.get_usage(usage.usage_id) == usage
    assert reopened.search(
        SkillSearchContext(user_id="user-a", project_id="project-a", lab_id="lab-a")
    ) == (gold,)
    assert "similarity_score" not in reopened.search(
        SkillSearchContext(user_id="user-a", lab_id="lab-a")
    )[0].model_dump_json()
    with pytest.raises(SkillApprovalRequiredError):
        reopened.record_context_access(
            rejected.authorization_id,
            run_id=rejected.run_id,
            skill_id=rejected.skill_id,
            skill_version=rejected.skill_version,
            accessed_by="user-a",
        )
    with pytest.raises(SkillVersionConflictError):
        reopened.save_source_bundle(bundle)
    reopened.close()


def _catalog():
    profile = AgentProfile(
        profile_key="coordinator",
        version="c9-test",
        agent_name="CoordinatorAgent",
        role_description="Exercise the generic C9 gate boundary.",
        prompt_profile_key="runtime-generic",
        response_schema_key="runtime-stage-result",
        model_profile_key="runtime-default",
        capability_profile_key="coordinator-capabilities",
    )
    return RuntimeProfileCatalog(
        agents=(profile,),
        prompts=(
            PromptProfile(
                template_id="runtime-generic",
                version="c9-test",
                template_text="Finalize the current generic stage.",
            ),
        ),
        models=(
            ModelProfile(
                profile_key="runtime-default",
                version="c9-test",
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
                version="c9-test",
                capability_allowlist=(),
            ),
        ),
    )


def _body(stage):
    return {
        WorkflowStage.INTAKE: IntakeStageBody(interpreted_goal="Safe goal."),
        WorkflowStage.UNDERSTAND: UnderstandStageBody(requirements=("Requirement.",)),
        WorkflowStage.PLAN: PlanStageBody(procedure_steps=("Current-task step.",)),
        WorkflowStage.PREFLIGHT: PreflightStageBody(structurally_valid=True),
        WorkflowStage.EXECUTE: ExecuteStageBody(execution_status="SUCCEEDED"),
        WorkflowStage.VALIDATE: ValidateStageBody(
            technical_status="PASSED", runtime_assessment="Current output is valid."
        ),
        WorkflowStage.INTERPRET: InterpretStageBody(findings=("Current finding.",)),
        WorkflowStage.REPORT: ReportStageBody(report_summary="Current report."),
        WorkflowStage.LEARN: LearnStageBody(learning_summary="No automatic promotion."),
    }[stage]


def _application_configuration(tmp_path, service, handlers=()):
    input_root = tmp_path / f"inputs-{uuid4()}"
    input_root.mkdir()
    return ApplicationRuntimeConfiguration(
        artifact_root=tmp_path / f"application-artifacts-{uuid4()}",
        execution_workspace_root=tmp_path / f"executions-{uuid4()}",
        allowed_input_roots=(input_root,),
        projects=(
            Project(project_id="project-a", lab_id="lab-a", owner_user_id="user-a"),
        ),
        profile_catalog=_catalog(),
        stage_assemblies=tuple(
            RuntimeStageAssemblySpec(
                stage_id=stage,
                root_profile_key="coordinator",
                prompt_template_key="runtime-generic",
                capability_allowlist=(),
                capability_phase_enabled=False,
            )
            for stage in MAIN_PATH
        ),
        skill_service=service,
        domain_decision_handlers=handlers,
    )


def test_application_owns_nested_skill_authority_and_trace_sequence(tmp_path):
    principal, workspace, artifacts, _, store, service, _, _ = _governed(tmp_path)
    _, _, gold = _create_gold(service, store, principal, artifacts)
    application = LabBioApplication(_application_configuration(tmp_path, service))
    assert service.access_service is application.access_service
    assert service.trace_recorder is application.trace_recorder

    run_id = uuid4()
    application.trace_recorder.emit(
        run_id,
        TraceEventType.CAPABILITY_INVOKED,
        stage_id=WorkflowStage.PLAN,
        status="STARTED",
        payload={"capability": "skill_propose_use"},
    )
    proposal = _submit_use(service, principal, workspace, gold, run_id)
    application.trace_recorder.emit(
        run_id,
        TraceEventType.CAPABILITY_COMPLETED,
        stage_id=WorkflowStage.PLAN,
        status="COMPLETED",
        payload={"capability": "skill_propose_use"},
    )
    events = application.trace_sink.read(run_id)
    assert tuple(event.sequence for event in events) == tuple(range(len(events)))
    assert TraceEventType.SKILL_USE_PROPOSED in {
        event.event_type for event in events
    }
    assert store.get_use_proposal(proposal.proposal_id) == proposal


@pytest.mark.asyncio
async def test_s16_s17_domain_decision_must_succeed_before_source_resume(tmp_path, monkeypatch):
    principal, workspace, artifacts, _, store, service, _, _ = _governed(tmp_path)
    _, _, gold = _create_gold(service, store, principal, artifacts)
    state = {"paused": False}
    domain_reference = {"value": None}

    async def invoke(_self, stage_input):
        stage = stage_input.stage_id
        if stage is WorkflowStage.PLAN and not state["paused"]:
            state["paused"] = True
            return RuntimeStageResult(
                stage_id=stage,
                summary="Runtime proposed optional Skill context.",
                body=_body(stage),
                next_action=NextActionProposal(
                    action=NextAction.REQUEST_USER_INPUT,
                    user_prompt="Approve this exact Skill use?",
                    domain_reference_id=domain_reference["value"],
                ),
            )
        index = MAIN_PATH.index(stage)
        action = (
            NextActionProposal(action=NextAction.FINISH)
            if stage is WorkflowStage.LEARN
            else NextActionProposal(
                action=NextAction.TRANSITION,
                target_stage=MAIN_PATH[index + 1],
            )
        )
        return RuntimeStageResult(
            stage_id=stage,
            summary=f"Completed {stage.value}.",
            body=_body(stage),
            next_action=action,
        )

    monkeypatch.setattr(PerInvocationPantheonStageInvoker, "invoke", invoke)
    handler = SkillDomainDecisionHandler(service)
    application = LabBioApplication(
        _application_configuration(tmp_path, service, (handler,))
    )
    handle = application.create_run(
        ApplicationRunRequest(
            task_text="A familiar but current generic task.",
            principal=principal,
            workspace=workspace,
        )
    )
    use = _submit_use(service, principal, workspace, gold, handle.run_id)
    domain_reference["value"] = f"skill-use:{use.proposal_id}"
    waiting = await application.run(handle)
    assert waiting.status is RunStatus.WAITING_FOR_USER
    gate = waiting.pending_user_gate
    assert gate is not None

    with pytest.raises(AuthorizationDenied):
        await application.resume_run(
            handle,
            GateUserDecision(
                gate_id=gate.gate_id,
                approved=True,
                decided_by="intruder",
                domain_reference_id=domain_reference["value"],
            ),
        )
    assert application.result(handle).status is RunStatus.WAITING_FOR_USER
    assert store.get_authorization_for_proposal(use.proposal_id) is None

    completed = await application.resume_run(
        handle,
        GateUserDecision(
            gate_id=gate.gate_id,
            approved=True,
            decided_by=principal.user_id,
            domain_reference_id=domain_reference["value"],
        ),
    )
    assert completed.status is RunStatus.COMPLETED
    decision_record = application._sessions[handle.run_id].run.gate_decisions[-1]
    assert decision_record.source_stage is WorkflowStage.PLAN
    assert decision_record.decision_reference_id is not None
    authorization = store.get_authorization(
        UUID(decision_record.decision_reference_id)
    )
    assert authorization.run_id == handle.run_id
    assert store.get_authorization_for_proposal(use.proposal_id) == authorization


@pytest.mark.asyncio
async def test_s18_s20_usage_requires_access_and_terminal_finalization_is_idempotent(tmp_path):
    principal, workspace, artifacts, exposure, store, service, sink, recorder = _governed(tmp_path)
    _, _, gold = _create_gold(service, store, principal, artifacts)
    run_id = uuid4()
    toolset = _toolset(
        principal,
        workspace,
        artifacts,
        exposure,
        service,
        run_id,
        ("skill_search", "skill_view"),
        recorder,
    )
    assert (await toolset.skill_search(query_text="Generic"))["success"]
    assert service.finalize_run_usage(run_id, RunStatus.COMPLETED) == ()

    proposal = _submit_use(service, principal, workspace, gold, run_id)
    authorization = _decide_use(service, principal, proposal)
    assert service.finalize_run_usage(run_id, RunStatus.COMPLETED) == ()
    assert (
        await toolset.skill_view(str(authorization.authorization_id))
    )["success"]
    first = service.finalize_run_usage(run_id, RunStatus.COMPLETED)
    second = service.finalize_run_usage(run_id, RunStatus.COMPLETED)
    assert len(first) == 1
    assert second == first
    assert first[0].outcome is SkillUsageOutcome.SUCCEEDED
    event_types = [event.event_type for event in sink.read(run_id)]
    assert TraceEventType.SKILL_CONTEXT_ACCESSED in event_types
    assert event_types.count(TraceEventType.SKILL_USAGE_RECORDED) == 1
    trace_json = json.dumps(
        [event.model_dump(mode="json") for event in sink.read(run_id)]
    )
    assert gold.procedure.applicability not in trace_json


def test_s21_s22_successful_adapt_creates_v2_and_v1_is_immutable(tmp_path):
    principal, workspace, artifacts, _, store, service, _, _ = _governed(tmp_path)
    _, _, v1 = _create_gold(service, store, principal, artifacts)
    v1_json = v1.model_dump_json()
    adapted_run_id = uuid4()
    use = _submit_use(
        service,
        principal,
        workspace,
        v1,
        adapted_run_id,
        mode=SkillUseMode.ADAPT,
    )
    authorization = _decide_use(service, principal, use)
    service.get_authorized_context(
        authorization.authorization_id,
        run_id=adapted_run_id,
        project_id=workspace.project_id,
        principal=principal,
    )
    usage = service.finalize_run_usage(adapted_run_id, RunStatus.COMPLETED)[0]
    adapted_bundle = _source_bundle(artifacts, run_id=adapted_run_id)
    store.save_source_bundle(adapted_bundle)
    v2_proposal = service.create_proposal(
        adapted_bundle.bundle_id,
        _draft("Generic validated analysis procedure v2"),
        _context(parent=v1, source_usage_record_id=usage.usage_id),
    )
    v2 = service.decide_proposal(
        v2_proposal.proposal_id,
        SkillUserDecision(
            subject_id=v2_proposal.proposal_id,
            gate_id=v2_proposal.approval_gate_id,
            approved=True,
            decided_by=principal.user_id,
        ),
        principal=principal,
    )
    assert v2 is not None
    assert (v2.skill_id, v2.version, v2.parent_skill_id, v2.parent_version) == (
        v1.skill_id,
        2,
        v1.skill_id,
        1,
    )
    assert store.get_gold(v1.skill_id, 1).model_dump_json() == v1_json

    conflicting = service.create_proposal(
        adapted_bundle.bundle_id,
        _draft("Conflicting v2"),
        _context(parent=v1, source_usage_record_id=usage.usage_id),
    )
    with pytest.raises(SkillVersionConflictError):
        service.decide_proposal(
            conflicting.proposal_id,
            SkillUserDecision(
                subject_id=conflicting.proposal_id,
                gate_id=conflicting.approval_gate_id,
                approved=True,
                decided_by=principal.user_id,
            ),
            principal=principal,
        )


@pytest.mark.asyncio
async def test_s23_s24_no_match_continues_and_skill_does_not_widen_capabilities(
    tmp_path, monkeypatch
):
    principal, workspace, artifacts, exposure, store, service, _, _ = _governed(tmp_path)
    _, _, gold = _create_gold(service, store, principal, artifacts)
    run_id = uuid4()
    root = _toolset(
        principal,
        workspace,
        artifacts,
        exposure,
        service,
        run_id,
        ("skill_search", "skill_view", "skill_propose_use"),
    )
    peer = _toolset(
        principal,
        workspace,
        artifacts,
        exposure,
        service,
        run_id,
        ("artifact_query",),
    )
    no_match = await root.skill_search(query_text="unrelated-proteomics-procedure")
    assert no_match["success"] and no_match["data"] == []
    assert set(root.tool_functions) == {"skill_search", "skill_view", "skill_propose_use"}
    assert set(peer.tool_functions) == {"artifact_query"}

    async def invoke(_self, stage_input):
        stage = stage_input.stage_id
        index = MAIN_PATH.index(stage)
        action = (
            NextActionProposal(action=NextAction.FINISH)
            if stage is WorkflowStage.LEARN
            else NextActionProposal(
                action=NextAction.TRANSITION,
                target_stage=MAIN_PATH[index + 1],
            )
        )
        return RuntimeStageResult(
            stage_id=stage,
            summary=f"Solved novel task at {stage.value} without a forced Skill.",
            body=_body(stage),
            next_action=action,
        )

    monkeypatch.setattr(PerInvocationPantheonStageInvoker, "invoke", invoke)
    application = LabBioApplication(_application_configuration(tmp_path, service))
    outcome = await application.run(
        application.create_run(
            ApplicationRunRequest(
                task_text="A novel task with no applicable stored procedure.",
                principal=principal,
                workspace=workspace,
            )
        )
    )
    assert outcome.status is RunStatus.COMPLETED
    assert store.search(
        SkillSearchContext(
            user_id=principal.user_id,
            project_id=workspace.project_id,
            lab_id=workspace.lab_id,
            query_text="unrelated-proteomics-procedure",
        )
    ) == ()
    assert not any(
        hasattr(gold, method) for method in ("execute", "run", "apply", "execute_workflow")
    )
