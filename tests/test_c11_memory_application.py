"""C11 application USER_GATE, restart, and replay-barrier acceptance."""

from __future__ import annotations

from uuid import UUID

import pytest

from labbioagentos import (
    AgentProfile,
    ApplicationDomainDecisionHandler,
    ApplicationRecoveryError,
    ApplicationRecoveryIssueCode,
    ApplicationRunRequest,
    ApplicationRuntimeConfiguration,
    CapabilityProfile,
    ExecuteStageBody,
    GateUserDecision,
    InMemoryMemoryStore,
    InMemoryProjectStore,
    InMemoryTraceSink,
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
    PantheonRuntimeFactory,
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
    RunTraceRecorder,
    RuntimeCapabilityContext,
    RuntimeProfileCatalog,
    RuntimeStageAssemblySpec,
    RuntimeStageResult,
    SQLiteMemoryStore,
    SQLiteRunStateStore,
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
    return Principal(user_id="user-c11", lab_id="lab-c11")


def _workspace():
    return WorkspaceContext(
        user_id="user-c11", project_id="project-c11", lab_id="lab-c11"
    )


def _catalog():
    return RuntimeProfileCatalog(
        agents=(
            AgentProfile(
                profile_key="coordinator",
                version="c11-test",
                agent_name="CoordinatorAgent",
                role_description="Exercise governed persistent Memory.",
                prompt_profile_key="runtime-generic",
                response_schema_key="runtime-stage-result",
                model_profile_key="runtime-default",
                capability_profile_key="coordinator-capabilities",
            ),
        ),
        prompts=(
            PromptProfile(
                template_id="runtime-generic",
                version="c11-test",
                template_text="Finalize the current generic stage.",
            ),
        ),
        models=(
            ModelProfile(
                profile_key="runtime-default",
                version="c11-test",
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
                version="c11-test",
                capability_allowlist=(),
            ),
        ),
    )


def _configuration(
    tmp_path,
    run_store,
    memory_service,
    *,
    trace_sink,
    handler=None,
):
    input_root = tmp_path / "inputs"
    input_root.mkdir(exist_ok=True)
    return ApplicationRuntimeConfiguration(
        artifact_root=tmp_path / "artifacts",
        execution_workspace_root=tmp_path / "executions",
        allowed_input_roots=(input_root,),
        projects=(
            Project(
                project_id="project-c11",
                lab_id="lab-c11",
                owner_user_id="user-c11",
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
        trace_sink=trace_sink,
        run_state_store=run_store,
        memory_service=memory_service,
        domain_decision_handlers=((handler,) if handler is not None else ()),
        runtime_revision="runtime-c11-test-v1",
    )


def _request():
    return ApplicationRunRequest(
        task_text="Remember my durable reporting preference only if proposed and approved.",
        principal=_principal(),
        workspace=_workspace(),
    )


def _body(stage):
    return {
        WorkflowStage.INTAKE: IntakeStageBody(interpreted_goal="Safe goal."),
        WorkflowStage.UNDERSTAND: UnderstandStageBody(requirements=("Current requirement.",)),
        WorkflowStage.PLAN: PlanStageBody(procedure_steps=("Current step.",)),
        WorkflowStage.PREFLIGHT: PreflightStageBody(structurally_valid=True),
        WorkflowStage.EXECUTE: ExecuteStageBody(execution_status="SUCCEEDED"),
        WorkflowStage.VALIDATE: ValidateStageBody(
            technical_status="PASSED", runtime_assessment="Current output is valid."
        ),
        WorkflowStage.INTERPRET: InterpretStageBody(findings=("Current finding.",)),
        WorkflowStage.REPORT: ReportStageBody(report_summary="Current report."),
        WorkflowStage.LEARN: LearnStageBody(learning_summary="Optional Memory proposal."),
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


def _install_memory_gate_runtime(monkeypatch, active_application, state):
    async def invoke(_self, stage_input):
        if stage_input.stage_id is WorkflowStage.LEARN and not stage_input.gate_decisions:
            app = active_application["value"]
            toolset = LabBioRuntimeToolSet(
                RuntimeCapabilityContext(
                    principal=_principal(),
                    workspace=_workspace(),
                    run_id=stage_input.run_id,
                    stage_id=WorkflowStage.LEARN,
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
                proposed_content="Prefer concise reports with explicit limitations.",
                reason="The user explicitly asked to retain this preference.",
            )
            assert receipt["success"] is True
            state.update(receipt["data"])
            return RuntimeStageResult(
                stage_id=WorkflowStage.LEARN,
                summary="A governed Memory proposal awaits the user.",
                body=_body(WorkflowStage.LEARN),
                next_action=NextActionProposal(
                    action=NextAction.REQUEST_USER_INPUT,
                    user_prompt="Approve this persistent Memory proposal?",
                    domain_reference_id=receipt["data"]["domain_reference_id"],
                ),
            )
        return _next(stage_input.stage_id)

    monkeypatch.setattr(PerInvocationPantheonStageInvoker, "invoke", invoke)


@pytest.mark.asyncio
async def test_m25_m27_m29_sqlite_memory_gate_survives_application_restart(
    tmp_path, monkeypatch
):
    run_path = tmp_path / "runs.sqlite3"
    memory_path = tmp_path / "memory.sqlite3"
    trace_path = tmp_path / "trace.jsonl"
    active_application = {"value": None}
    proposal_state = {}
    _install_memory_gate_runtime(monkeypatch, active_application, proposal_state)

    first_run_store = SQLiteRunStateStore(run_path)
    first_memory_store = SQLiteMemoryStore(memory_path)
    first_service = MemoryGovernanceService(first_memory_store)
    first_handler = MemoryDomainDecisionHandler(first_service)
    first = LabBioApplication(
        _configuration(
            tmp_path,
            first_run_store,
            first_service,
            trace_sink=JsonlTraceSink(trace_path),
            handler=first_handler,
        )
    )
    active_application["value"] = first
    assert first_service.access is first.access_service
    assert first_service.trace_recorder is first.trace_recorder
    assert first_service.artifact_store is first.artifact_store
    handle = first.create_run(_request())
    waiting = await first.run(handle)
    assert waiting.status is RunStatus.WAITING_FOR_USER
    assert first_memory_store.entries() == ()
    gate = waiting.pending_user_gate
    assert gate is not None
    assert gate.domain_reference_id == proposal_state["domain_reference_id"]
    first_run_store.close()
    first_memory_store.close()

    second_run_store = SQLiteRunStateStore(run_path)
    second_memory_store = SQLiteMemoryStore(memory_path)
    second_service = MemoryGovernanceService(second_memory_store)
    second = LabBioApplication(
        _configuration(
            tmp_path,
            second_run_store,
            second_service,
            trace_sink=JsonlTraceSink(trace_path),
            handler=MemoryDomainDecisionHandler(second_service),
        )
    )
    active_application["value"] = second
    second.recover_run(handle.run_id, principal=_principal(), workspace=_workspace())
    completed = await second.resume_run(
        handle,
        GateUserDecision(
            gate_id=gate.gate_id,
            approved=True,
            decided_by=_principal().user_id,
            domain_reference_id=gate.domain_reference_id,
        ),
    )
    assert completed.status is RunStatus.COMPLETED
    proposal_id = UUID(proposal_state["proposal_id"])
    decision = second_memory_store.get_decision(proposal_id)
    assert decision is not None and decision.approved is True
    entries = second_memory_store.entries()
    assert len(entries) == 1 and entries[0].source_proposal_id == proposal_id
    assert second_run_store.get(handle.run_id).recovery_state is RunRecoveryState.STABLE
    sequences = tuple(event.sequence for event in second.trace_sink.read(handle.run_id))
    assert sequences == tuple(range(len(sequences)))
    second_run_store.close()
    second_memory_store.close()


@pytest.mark.asyncio
async def test_m26_rejected_memory_gate_creates_decision_without_version(
    tmp_path, monkeypatch
):
    active_application = {"value": None}
    proposal_state = {}
    _install_memory_gate_runtime(monkeypatch, active_application, proposal_state)
    run_store = SQLiteRunStateStore(tmp_path / "runs.sqlite3")
    memory_store = SQLiteMemoryStore(tmp_path / "memory.sqlite3")
    service = MemoryGovernanceService(memory_store)
    app = LabBioApplication(
        _configuration(
            tmp_path,
            run_store,
            service,
            trace_sink=JsonlTraceSink(tmp_path / "trace.jsonl"),
            handler=MemoryDomainDecisionHandler(service),
        )
    )
    active_application["value"] = app
    handle = app.create_run(_request())
    waiting = await app.run(handle)
    gate = waiting.pending_user_gate
    assert gate is not None
    completed = await app.resume_run(
        handle,
        GateUserDecision(
            gate_id=gate.gate_id,
            approved=False,
            decided_by=_principal().user_id,
            domain_reference_id=gate.domain_reference_id,
        ),
    )
    assert completed.status is RunStatus.COMPLETED
    assert memory_store.entries() == ()
    assert memory_store.get_decision(UUID(proposal_state["proposal_id"])).approved is False
    run_store.close()
    memory_store.close()


class _ProcessLoss(BaseException):
    pass


class _CommitThenStopMemoryHandler(ApplicationDomainDecisionHandler):
    def __init__(self, delegate):
        self.delegate = delegate

    def supports(self, domain_reference_id: str) -> bool:
        return self.delegate.supports(domain_reference_id)

    async def apply(self, **kwargs) -> str:
        await self.delegate.apply(**kwargs)
        raise _ProcessLoss


@pytest.mark.asyncio
async def test_m28_committed_memory_is_not_replayed_after_gate_inflight_crash(
    tmp_path, monkeypatch
):
    run_path = tmp_path / "runs.sqlite3"
    memory_path = tmp_path / "memory.sqlite3"
    trace_path = tmp_path / "trace.jsonl"
    active_application = {"value": None}
    proposal_state = {}
    _install_memory_gate_runtime(monkeypatch, active_application, proposal_state)
    run_store = SQLiteRunStateStore(run_path)
    memory_store = SQLiteMemoryStore(memory_path)
    service = MemoryGovernanceService(memory_store)
    app = LabBioApplication(
        _configuration(
            tmp_path,
            run_store,
            service,
            trace_sink=JsonlTraceSink(trace_path),
            handler=_CommitThenStopMemoryHandler(
                MemoryDomainDecisionHandler(service)
            ),
        )
    )
    active_application["value"] = app
    handle = app.create_run(_request())
    waiting = await app.run(handle)
    gate = waiting.pending_user_gate
    assert gate is not None
    with pytest.raises(_ProcessLoss):
        await app.resume_run(
            handle,
            GateUserDecision(
                gate_id=gate.gate_id,
                approved=True,
                decided_by=_principal().user_id,
                domain_reference_id=gate.domain_reference_id,
            ),
        )
    assert run_store.get(handle.run_id).recovery_state is RunRecoveryState.GATE_DECISION_IN_FLIGHT
    assert len(memory_store.entries()) == 1
    assert memory_store.get_decision(UUID(proposal_state["proposal_id"])) is not None
    run_store.close()
    memory_store.close()

    reopened_runs = SQLiteRunStateStore(run_path)
    reopened_memory = SQLiteMemoryStore(memory_path)
    reopened_service = MemoryGovernanceService(reopened_memory)
    restarted = LabBioApplication(
        _configuration(
            tmp_path,
            reopened_runs,
            reopened_service,
            trace_sink=JsonlTraceSink(trace_path),
            handler=MemoryDomainDecisionHandler(reopened_service),
        )
    )
    with pytest.raises(ApplicationRecoveryError) as caught:
        restarted.recover_run(
            handle.run_id, principal=_principal(), workspace=_workspace()
        )
    assert caught.value.issue_code is ApplicationRecoveryIssueCode.GATE_DECISION_IN_FLIGHT
    assert len(reopened_memory.entries()) == 1
    assert reopened_memory.get_decision(UUID(proposal_state["proposal_id"])) is not None
    reopened_runs.close()
    reopened_memory.close()


@pytest.mark.asyncio
async def test_m30_no_proposal_means_no_memory_and_no_automatic_injection(
    tmp_path, monkeypatch
):
    observed = []

    async def invoke(_self, stage_input):
        observed.append(stage_input.model_dump_json())
        return _next(stage_input.stage_id)

    monkeypatch.setattr(PerInvocationPantheonStageInvoker, "invoke", invoke)
    store = InMemoryMemoryStore()
    service = MemoryGovernanceService(store)
    app = LabBioApplication(
        _configuration(
            tmp_path,
            SQLiteRunStateStore(tmp_path / "runs.sqlite3"),
            service,
            trace_sink=InMemoryTraceSink(),
        )
    )
    handle = app.create_run(_request())
    completed = await app.run(handle)
    assert completed.status is RunStatus.COMPLETED
    assert store.entries() == ()
    assert all("Prefer concise reports" not in value for value in observed)
