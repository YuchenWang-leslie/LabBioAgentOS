"""C12 deterministic composition of gates, retry, recovery, and finalization."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from labbioagentos import (
    AgentProfile,
    ApplicationRunRequest,
    ApplicationRuntimeConfiguration,
    CapabilityProfile,
    ExecuteStageBody,
    GateUserDecision,
    GoldSkillService,
    IntakeStageBody,
    InterpretStageBody,
    JsonlTraceSink,
    LabBioApplication,
    LabBioRuntimeToolSet,
    LearnStageBody,
    MemoryDomainDecisionHandler,
    MemoryGovernanceService,
    ModelProfile,
    NextAction,
    NextActionProposal,
    PerInvocationPantheonStageInvoker,
    PlanStageBody,
    PreflightStageBody,
    Principal,
    Project,
    PromptProfile,
    ProviderConfigRef,
    ReportStageBody,
    ResponseSchemaRef,
    RunRecoveryState,
    RunStatus,
    RuntimeCapabilityContext,
    RuntimeProfileCatalog,
    RuntimeStageAssemblySpec,
    RuntimeStageResult,
    SkillCuratorDraft,
    SkillDomainDecisionHandler,
    SkillProcedureDraft,
    SkillProposalContext,
    SkillScope,
    SkillSourceBundle,
    SkillSourceProjector,
    SkillUsageOutcome,
    SkillUseMode,
    SkillUserDecision,
    SQLiteMemoryStore,
    SQLiteRunStateStore,
    SQLiteSkillStore,
    UnderstandStageBody,
    ValidateStageBody,
    WorkflowStage,
    WorkspaceContext,
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


def _principal():
    return Principal(user_id="user-c12", lab_id="lab-c12")


def _workspace():
    return WorkspaceContext(
        user_id="user-c12", project_id="project-c12", lab_id="lab-c12"
    )


def _catalog():
    return RuntimeProfileCatalog(
        agents=(
            AgentProfile(
                profile_key="coordinator",
                version="c12-test",
                agent_name="CoordinatorAgent",
                role_description="Exercise governed control composition.",
                prompt_profile_key="runtime-generic",
                response_schema_key="runtime-stage-result",
                model_profile_key="runtime-default",
                capability_profile_key="coordinator-capabilities",
            ),
        ),
        prompts=(
            PromptProfile(
                template_id="runtime-generic",
                version="c12-test",
                template_text="Finalize the current generic stage.",
            ),
        ),
        models=(
            ModelProfile(
                profile_key="runtime-default",
                version="c12-test",
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
                version="c12-test",
                capability_allowlist=(),
            ),
        ),
    )


def _configuration(tmp_path, run_store, skill_service, memory_service):
    input_root = tmp_path / "inputs"
    input_root.mkdir(exist_ok=True)
    return ApplicationRuntimeConfiguration(
        artifact_root=tmp_path / "artifacts",
        execution_workspace_root=tmp_path / "executions",
        allowed_input_roots=(input_root,),
        projects=(
            Project(
                project_id=_workspace().project_id,
                lab_id=_workspace().lab_id,
                owner_user_id=_principal().user_id,
            ),
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
        trace_sink=JsonlTraceSink(tmp_path / "trace.jsonl"),
        run_state_store=run_store,
        skill_service=skill_service,
        memory_service=memory_service,
        domain_decision_handlers=(
            SkillDomainDecisionHandler(skill_service),
            MemoryDomainDecisionHandler(memory_service),
        ),
        runtime_revision="runtime-c12-composition-v1",
    )


def _body(stage):
    return {
        WorkflowStage.INTAKE: IntakeStageBody(interpreted_goal="Safe goal."),
        WorkflowStage.UNDERSTAND: UnderstandStageBody(
            requirements=("Current requirement.",)
        ),
        WorkflowStage.PLAN: PlanStageBody(procedure_steps=("Current step.",)),
        WorkflowStage.PREFLIGHT: PreflightStageBody(structurally_valid=True),
        WorkflowStage.EXECUTE: ExecuteStageBody(execution_status="SUCCEEDED"),
        WorkflowStage.VALIDATE: ValidateStageBody(
            technical_status="PASSED",
            runtime_assessment="Current output is valid.",
        ),
        WorkflowStage.INTERPRET: InterpretStageBody(
            findings=("Current bounded finding.",)
        ),
        WorkflowStage.REPORT: ReportStageBody(report_summary="Current report."),
        WorkflowStage.LEARN: LearnStageBody(
            learning_summary="Optional contextual proposal."
        ),
    }[stage]


def _next(stage):
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


def _create_gold(service, store):
    bundle = SkillSourceBundle(
        source_run_id=uuid4(),
        final_status=RunStatus.COMPLETED,
        workflow_stage_path=(WorkflowStage.PLAN, WorkflowStage.REPORT),
        trace_event_ids=(),
    )
    store.save_source_bundle(bundle)
    proposal = service.create_proposal(
        bundle.bundle_id,
        SkillCuratorDraft(
            proposed_name="Optional C12 procedure",
            description="Optional procedural context for composition testing.",
            procedure=SkillProcedureDraft(
                applicability="Only if useful for the current task.",
                workflow_outline=("Inspect current governed evidence.",),
            ),
        ),
        SkillProposalContext(
            scope=SkillScope.PERSONAL,
            owner_user_id=_principal().user_id,
            lab_id=_principal().lab_id,
        ),
    )
    gold = service.decide_proposal(
        proposal.proposal_id,
        SkillUserDecision(
            subject_id=proposal.proposal_id,
            gate_id=proposal.approval_gate_id,
            approved=True,
            decided_by=_principal().user_id,
        ),
        principal=_principal(),
    )
    assert gold is not None
    return gold


@pytest.mark.asyncio
async def test_retry_gold_gate_memory_gate_restart_and_finalization_compose_once(
    tmp_path, monkeypatch
):
    run_path = tmp_path / "runs.sqlite3"
    skill_path = tmp_path / "skills.sqlite3"
    memory_path = tmp_path / "memory.sqlite3"
    active = {"app": None, "gold": None}
    state = {
        "intake_calls": 0,
        "skill_proposed": False,
        "skill_context_accessed": False,
        "memory_proposed": False,
        "skill_proposal_id": None,
        "memory_proposal_id": None,
    }

    async def invoke(_self, stage_input):
        app = active["app"]
        assert app is not None
        stage = stage_input.stage_id
        if stage is WorkflowStage.INTAKE:
            state["intake_calls"] += 1
            if state["intake_calls"] == 1:
                return RuntimeStageResult(
                    stage_id=stage,
                    summary="Request one bounded workflow retry.",
                    body=_body(stage),
                    next_action=NextActionProposal(action=NextAction.RETRY),
                )
        if stage is WorkflowStage.PLAN and not state["skill_proposed"]:
            toolset = LabBioRuntimeToolSet(
                RuntimeCapabilityContext(
                    principal=_principal(),
                    workspace=_workspace(),
                    run_id=stage_input.run_id,
                    stage_id=stage,
                    invocation_id=stage_input.invocation_id,
                    actor_profile_key="coordinator",
                    actor_agent_name="CoordinatorAgent",
                    capability_allowlist=("skill_propose_use",),
                ),
                app.capability_services,
            )
            receipt = await toolset.skill_propose_use(
                skill_id=str(active["gold"].skill_id),
                version=active["gold"].version,
                mode="REFERENCE",
                reason="The runtime selected optional context.",
            )
            assert receipt["success"] is True
            state["skill_proposed"] = True
            state["skill_proposal_id"] = UUID(receipt["data"]["proposal_id"])
            return RuntimeStageResult(
                stage_id=stage,
                summary="Optional Gold use awaits user approval.",
                body=_body(stage),
                next_action=NextActionProposal(
                    action=NextAction.REQUEST_USER_INPUT,
                    user_prompt="Approve this exact Gold use?",
                    domain_reference_id=receipt["data"]["domain_reference_id"],
                ),
            )
        if stage is WorkflowStage.PLAN and not state["skill_context_accessed"]:
            decision = next(
                item
                for item in stage_input.gate_decisions
                if item.source_stage is WorkflowStage.PLAN
            )
            toolset = LabBioRuntimeToolSet(
                RuntimeCapabilityContext(
                    principal=_principal(),
                    workspace=_workspace(),
                    run_id=stage_input.run_id,
                    stage_id=stage,
                    invocation_id=stage_input.invocation_id,
                    actor_profile_key="coordinator",
                    actor_agent_name="CoordinatorAgent",
                    capability_allowlist=("skill_view",),
                ),
                app.capability_services,
            )
            viewed = await toolset.skill_view(decision.decision_reference_id)
            assert viewed["success"] is True
            state["skill_context_accessed"] = True
        if stage is WorkflowStage.LEARN and not state["memory_proposed"]:
            toolset = LabBioRuntimeToolSet(
                RuntimeCapabilityContext(
                    principal=_principal(),
                    workspace=_workspace(),
                    run_id=stage_input.run_id,
                    stage_id=stage,
                    invocation_id=stage_input.invocation_id,
                    actor_profile_key="coordinator",
                    actor_agent_name="CoordinatorAgent",
                    capability_allowlist=("memory_propose_update",),
                ),
                app.capability_services,
            )
            receipt = await toolset.memory_propose_update(
                target_scope="PERSONAL",
                proposed_kind="PREFERENCE",
                proposed_content="Prefer concise bounded reports.",
                reason="The user requested a durable reporting preference.",
            )
            assert receipt["success"] is True
            state["memory_proposed"] = True
            state["memory_proposal_id"] = UUID(receipt["data"]["proposal_id"])
            return RuntimeStageResult(
                stage_id=stage,
                summary="Optional Memory awaits user approval.",
                body=_body(stage),
                next_action=NextActionProposal(
                    action=NextAction.REQUEST_USER_INPUT,
                    user_prompt="Approve this exact Memory proposal?",
                    domain_reference_id=receipt["data"]["domain_reference_id"],
                ),
            )
        return _next(stage)

    monkeypatch.setattr(PerInvocationPantheonStageInvoker, "invoke", invoke)

    first_runs = SQLiteRunStateStore(run_path)
    first_skills = SQLiteSkillStore(skill_path)
    first_skill_service = GoldSkillService(
        first_skills, SkillSourceProjector()
    )
    active["gold"] = _create_gold(first_skill_service, first_skills)
    first_memory = SQLiteMemoryStore(memory_path)
    first_memory_service = MemoryGovernanceService(first_memory)
    first = LabBioApplication(
        _configuration(
            tmp_path,
            first_runs,
            first_skill_service,
            first_memory_service,
        )
    )
    active["app"] = first
    handle = first.create_run(
        ApplicationRunRequest(
            task_text="Exercise composed trusted control boundaries.",
            principal=_principal(),
            workspace=_workspace(),
        )
    )

    skill_wait = await first.run(handle)
    assert skill_wait.status is RunStatus.WAITING_FOR_USER
    skill_gate = skill_wait.pending_user_gate
    assert skill_gate is not None
    memory_wait = await first.resume_run(
        handle,
        GateUserDecision(
            gate_id=skill_gate.gate_id,
            approved=True,
            decided_by=_principal().user_id,
            domain_reference_id=skill_gate.domain_reference_id,
        ),
    )
    assert memory_wait.status is RunStatus.WAITING_FOR_USER
    memory_gate = memory_wait.pending_user_gate
    assert memory_gate is not None
    assert first_runs.get(handle.run_id).recovery_state is RunRecoveryState.STABLE
    first_runs.close()
    first_skills.close()
    first_memory.close()

    second_runs = SQLiteRunStateStore(run_path)
    second_skills = SQLiteSkillStore(skill_path)
    second_skill_service = GoldSkillService(
        second_skills, SkillSourceProjector()
    )
    second_memory = SQLiteMemoryStore(memory_path)
    second_memory_service = MemoryGovernanceService(second_memory)
    second = LabBioApplication(
        _configuration(
            tmp_path,
            second_runs,
            second_skill_service,
            second_memory_service,
        )
    )
    active["app"] = second
    second.recover_run(
        handle.run_id, principal=_principal(), workspace=_workspace()
    )
    completed = await second.resume_run(
        handle,
        GateUserDecision(
            gate_id=memory_gate.gate_id,
            approved=True,
            decided_by=_principal().user_id,
            domain_reference_id=memory_gate.domain_reference_id,
        ),
    )

    assert completed.status is RunStatus.COMPLETED
    record = second_runs.get(handle.run_id)
    assert record.recovery_state is RunRecoveryState.STABLE
    assert record.workflow_run.retry_counts == {WorkflowStage.INTAKE: 1}
    assert [item.stage for item in record.workflow_run.stage_results].count(
        WorkflowStage.EXECUTE
    ) == 1
    assert [item.stage for item in record.workflow_run.stage_results].count(
        WorkflowStage.VALIDATE
    ) == 1
    assert len(record.workflow_run.gate_decisions) == 2
    assert len(second_memory.entries()) == 1
    assert second_memory.get_decision(state["memory_proposal_id"]).approved is True

    authorization = second_skills.get_authorization_for_proposal(
        state["skill_proposal_id"]
    )
    usage = second_skill_service.finalize_run_usage(
        handle.run_id, RunStatus.COMPLETED
    )
    assert authorization is not None and authorization.approved is True
    assert len(usage) == 1
    assert usage[0].outcome is SkillUsageOutcome.SUCCEEDED
    assert second_skill_service.finalize_run_usage(
        handle.run_id, RunStatus.COMPLETED
    ) == usage
    sequences = [event.sequence for event in second.trace_sink.read(handle.run_id)]
    assert sequences == list(range(len(sequences)))

    second_runs.close()
    second_skills.close()
    second_memory.close()
