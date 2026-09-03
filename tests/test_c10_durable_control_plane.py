"""C10 deterministic acceptance for durable, restart-safe control state."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from labbioagentos import (
    AgentProfile,
    ApplicationRecoveryError,
    ApplicationRecoveryIssueCode,
    ApplicationDomainDecisionHandler,
    ApplicationRunRecord,
    ApplicationRunRequest,
    ApplicationRuntimeConfiguration,
    ArtifactExposureClass,
    ArtifactRepresentation,
    AuthorizationDenied,
    CapabilityProfile,
    ExecuteStageBody,
    GateUserDecision,
    GoldSkillService,
    InMemoryRunStateStore,
    IntakeStageBody,
    InterpretStageBody,
    JsonlTraceSink,
    LabBioApplication,
    LearnStageBody,
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
    RunStateVersionConflictError,
    SQLiteRunStateStore,
    SQLiteSkillStore,
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
    SkillUseMode,
    SkillUseProposal,
    SkillUserDecision,
    TraceEventType,
    UnderstandStageBody,
    ValidateStageBody,
    WorkflowEngine,
    WorkflowRun,
    WorkflowStage,
    WorkspaceContext,
    runtime_workflow_definition,
)
from labbioagentos.workflow import InvalidRunStateError, UnknownWorkflowRunError


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


def _catalog() -> RuntimeProfileCatalog:
    return RuntimeProfileCatalog(
        agents=(
            AgentProfile(
                profile_key="coordinator",
                version="c10-test",
                agent_name="CoordinatorAgent",
                role_description="Exercise restart-safe generic control state.",
                prompt_profile_key="runtime-generic",
                response_schema_key="runtime-stage-result",
                model_profile_key="runtime-default",
                capability_profile_key="coordinator-capabilities",
            ),
        ),
        prompts=(
            PromptProfile(
                template_id="runtime-generic",
                version="c10-test",
                template_text="Finalize the current generic stage.",
            ),
        ),
        models=(
            ModelProfile(
                profile_key="runtime-default",
                version="c10-test",
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
                version="c10-test",
                capability_allowlist=(),
            ),
        ),
    )


def _body(stage: WorkflowStage):
    return {
        WorkflowStage.INTAKE: IntakeStageBody(interpreted_goal="Safe goal."),
        WorkflowStage.UNDERSTAND: UnderstandStageBody(
            requirements=("Current requirement.",)
        ),
        WorkflowStage.PLAN: PlanStageBody(procedure_steps=("Current step.",)),
        WorkflowStage.PREFLIGHT: PreflightStageBody(structurally_valid=True),
        WorkflowStage.EXECUTE: ExecuteStageBody(execution_status="SUCCEEDED"),
        WorkflowStage.VALIDATE: ValidateStageBody(
            technical_status="PASSED", runtime_assessment="Current output is valid."
        ),
        WorkflowStage.INTERPRET: InterpretStageBody(findings=("Current finding.",)),
        WorkflowStage.REPORT: ReportStageBody(report_summary="Current report."),
        WorkflowStage.LEARN: LearnStageBody(learning_summary="No promotion."),
    }[stage]


def _next_result(stage: WorkflowStage) -> RuntimeStageResult:
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


def _principal() -> Principal:
    return Principal(user_id="user-c10", lab_id="lab-c10")


def _workspace() -> WorkspaceContext:
    return WorkspaceContext(
        user_id="user-c10", project_id="project-c10", lab_id="lab-c10"
    )


def _configuration(
    tmp_path,
    store,
    *,
    revision="runtime-c10-test-v1",
    trace_sink=None,
    projects=None,
    artifact_root=None,
    domain_decision_handlers=(),
    skill_service=None,
) -> ApplicationRuntimeConfiguration:
    input_root = tmp_path / "inputs"
    input_root.mkdir(exist_ok=True)
    return ApplicationRuntimeConfiguration(
        artifact_root=artifact_root or tmp_path / "artifacts",
        execution_workspace_root=tmp_path / "executions",
        allowed_input_roots=(input_root,),
        projects=(
            projects
            if projects is not None
            else (
                Project(
                    project_id="project-c10",
                    lab_id="lab-c10",
                    owner_user_id="user-c10",
                ),
            )
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
        skill_service=skill_service,
        domain_decision_handlers=domain_decision_handlers,
        run_state_store=store,
        runtime_revision=revision,
    )


def _request(*, input_artifact_ids=()) -> ApplicationRunRequest:
    return ApplicationRunRequest(
        task_text="Exercise restart-safe generic control state.",
        principal=_principal(),
        workspace=_workspace(),
        input_artifact_ids=input_artifact_ids,
    )


class _SimulatedProcessLoss(BaseException):
    pass


class _StopAfterCheckpointStore:
    """Persist a selected checkpoint, then simulate immediate process loss."""

    def __init__(self, delegate, predicate):
        self.delegate = delegate
        self.predicate = predicate
        self.stopped = False

    def create(self, record):
        return self.delegate.create(record)

    def get(self, run_id):
        return self.delegate.get(run_id)

    def update(self, record, *, expected_version):
        saved = self.delegate.update(record, expected_version=expected_version)
        if not self.stopped and self.predicate(saved):
            self.stopped = True
            raise _SimulatedProcessLoss
        return saved

    def list(self):
        return self.delegate.list()


def _record() -> ApplicationRunRecord:
    definition = runtime_workflow_definition()
    run = WorkflowRun(
        workflow_id=definition.workflow_id,
        owner_user_id="user-c10",
        project_id="project-c10",
        lab_id="lab-c10",
    )
    return ApplicationRunRecord(
        run_id=run.run_id,
        task_text="Exercise durable generic control state.",
        owner_user_id=run.owner_user_id,
        project_id=run.project_id,
        lab_id=run.lab_id,
        workflow_run=run,
        runtime_revision="runtime-c10-test-v1",
        recovery_state=RunRecoveryState.STABLE,
    )


def test_d1_sqlite_run_state_roundtrip_uses_validated_json(tmp_path):
    database = tmp_path / "run-state.sqlite3"
    expected = _record()
    first = SQLiteRunStateStore(database)
    created = first.create(expected)
    first.close()

    reopened = SQLiteRunStateStore(database)
    loaded = reopened.get(expected.run_id)
    assert loaded == created == expected
    assert reopened.list() == (expected,)
    assert '"runtime_revision":"runtime-c10-test-v1"' in loaded.model_dump_json()
    assert "pickle" not in database.read_bytes().lower().decode(
        "utf-8", errors="ignore"
    )
    reopened.close()


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_d2_optimistic_record_version_conflict_is_explicit(tmp_path, store_kind):
    store = (
        InMemoryRunStateStore()
        if store_kind == "memory"
        else SQLiteRunStateStore(tmp_path / "state.sqlite3")
    )
    original = store.create(_record())
    replacement = original.model_copy(
        update={"task_text": "Updated only through the versioned store."}
    )
    current = store.update(replacement, expected_version=1)
    assert current.record_version == 2
    assert current.created_at == original.created_at
    assert current.updated_at >= original.updated_at

    with pytest.raises(RunStateVersionConflictError):
        store.update(original, expected_version=1)
    assert store.get(original.run_id) == current
    close = getattr(store, "close", None)
    if close is not None:
        close()


def test_d1_in_memory_store_does_not_share_mutable_workflow_objects():
    store = InMemoryRunStateStore()
    original = store.create(_record())
    loaded = store.get(original.run_id)
    loaded.workflow_run.status = RunStatus.RUNNING
    assert store.get(original.run_id).workflow_run.status.value == "CREATED"


def test_durable_record_is_data_only_and_contains_no_runtime_object_fields():
    expected_fields = {
        "run_id",
        "task_text",
        "owner_user_id",
        "project_id",
        "lab_id",
        "input_artifact_ids",
        "context_artifact_ids",
        "safe_domain_references",
        "workflow_run",
        "runtime_results",
        "runtime_revision",
        "recovery_state",
        "inflight_stage",
        "inflight_invocation_id",
        "inflight_operation",
        "created_at",
        "updated_at",
        "record_version",
    }
    assert set(ApplicationRunRecord.model_fields) == expected_fields
    encoded = _record().model_dump_json().lower()
    for forbidden in (
        "storage_locator",
        "provider_client",
        "runtimecoordinatorservice",
        "toolset",
        "dockerexecutor",
        "accessservice",
        "callback",
        "plugin",
        "open_file",
        "api_key",
        "credential",
        "pickle",
    ):
        assert forbidden not in encoded


def test_recovered_workflow_run_is_validated_and_owned_by_new_engine():
    definition = runtime_workflow_definition()
    first = WorkflowEngine(definition)
    original = first.create_run()
    recovered_snapshot = WorkflowRun.model_validate_json(original.model_dump_json())
    second = WorkflowEngine(definition)

    with pytest.raises(UnknownWorkflowRunError):
        second.start(recovered_snapshot)
    owned = second.attach_recovered_run(recovered_snapshot)
    assert owned is not recovered_snapshot
    assert second.start(owned).current_stage is WorkflowStage.INTAKE
    with pytest.raises(InvalidRunStateError, match="already attached"):
        second.attach_recovered_run(owned)


@pytest.mark.parametrize(
    "updates",
    [
        {"workflow_id": "wrong-workflow"},
        {"status": RunStatus.RUNNING, "current_stage": None},
        {"status": RunStatus.CREATED, "current_stage": WorkflowStage.INTAKE},
        {
            "status": RunStatus.WAITING_FOR_USER,
            "current_stage": WorkflowStage.USER_GATE,
            "pending_user_gate": None,
        },
    ],
)
def test_recovered_workflow_run_rejects_invalid_authoritative_state(updates):
    definition = runtime_workflow_definition()
    run = WorkflowRun(workflow_id=definition.workflow_id).model_copy(update=updates)
    with pytest.raises(InvalidRunStateError):
        WorkflowEngine(definition).attach_recovered_run(run)


def test_d3_d22_d24_created_run_recovers_without_model_call(tmp_path, monkeypatch):
    calls = []

    async def invoke(_self, stage_input):
        calls.append(stage_input)
        return _next_result(stage_input.stage_id)

    monkeypatch.setattr(PerInvocationPantheonStageInvoker, "invoke", invoke)
    database = tmp_path / "run-state.sqlite3"
    first_store = SQLiteRunStateStore(database)
    first = LabBioApplication(_configuration(tmp_path, first_store))
    handle = first.create_run(_request())
    first_coordinator = first._sessions[handle.run_id].coordinator
    first_store.close()

    second_store = SQLiteRunStateStore(database)
    second = LabBioApplication(_configuration(tmp_path, second_store))
    status = second.recovery_status(
        handle.run_id, principal=_principal(), workspace=_workspace()
    )
    recovered = second.recover_run(
        handle.run_id, principal=_principal(), workspace=_workspace()
    )

    assert recovered == handle
    assert status.recoverable is True
    assert status.automatic_continuation_allowed is True
    assert status.issue_code is None
    assert second.result(recovered).status is RunStatus.CREATED
    assert second._sessions[handle.run_id].coordinator is not first_coordinator
    assert calls == []
    second_store.close()


@pytest.mark.asyncio
async def test_d4_d8_stable_running_boundary_restores_retry_and_prior_results(
    tmp_path, monkeypatch
):
    calls = []

    async def invoke(_self, stage_input):
        calls.append(stage_input)
        if stage_input.stage_id is WorkflowStage.INTAKE and len(calls) == 1:
            return RuntimeStageResult(
                stage_id=WorkflowStage.INTAKE,
                summary="The runtime explicitly requested one workflow retry.",
                body=_body(WorkflowStage.INTAKE),
                next_action=NextActionProposal(action=NextAction.RETRY),
            )
        return _next_result(stage_input.stage_id)

    monkeypatch.setattr(PerInvocationPantheonStageInvoker, "invoke", invoke)
    durable = InMemoryRunStateStore()
    stopping = _StopAfterCheckpointStore(
        durable,
        lambda record: (
            record.recovery_state is RunRecoveryState.STABLE
            and record.workflow_run.status is RunStatus.RUNNING
            and record.workflow_run.retry_counts.get(WorkflowStage.INTAKE) == 1
        ),
    )
    first = LabBioApplication(_configuration(tmp_path, stopping))
    handle = first.create_run(_request())
    with pytest.raises(_SimulatedProcessLoss):
        await first.run(handle)
    boundary = durable.get(handle.run_id)
    assert boundary.workflow_run.current_stage is WorkflowStage.INTAKE
    assert boundary.workflow_run.retry_counts == {WorkflowStage.INTAKE: 1}
    assert len(boundary.workflow_run.stage_results) == 1
    assert len(boundary.runtime_results) == 1
    first_result_id = boundary.runtime_results[0].result_id

    second = LabBioApplication(_configuration(tmp_path, durable))
    second.recover_run(
        handle.run_id, principal=_principal(), workspace=_workspace()
    )
    assert len(calls) == 1
    completed = await second.run(handle)
    assert completed.status is RunStatus.COMPLETED
    assert [item.stage_id for item in calls].count(WorkflowStage.INTAKE) == 2
    assert calls[1].prior_results[0].result_id == first_result_id
    final = durable.get(handle.run_id)
    assert final.workflow_run.retry_counts == {WorkflowStage.INTAKE: 1}
    assert len(final.runtime_results) == len(final.workflow_run.stage_results) == 10
    assert len({item.result_id for item in final.runtime_results}) == 10


@pytest.mark.asyncio
async def test_d5_d6_waiting_gate_survives_restart_and_resumes_normally(
    tmp_path, monkeypatch
):
    async def invoke(_self, stage_input):
        if stage_input.stage_id is WorkflowStage.PLAN and not stage_input.gate_decisions:
            return RuntimeStageResult(
                stage_id=WorkflowStage.PLAN,
                summary="A current typed decision is required.",
                body=_body(WorkflowStage.PLAN),
                next_action=NextActionProposal(
                    action=NextAction.REQUEST_USER_INPUT,
                    user_prompt="Approve continuation?",
                    domain_reference_id=None,
                ),
            )
        return _next_result(stage_input.stage_id)

    monkeypatch.setattr(PerInvocationPantheonStageInvoker, "invoke", invoke)
    database = tmp_path / "run-state.sqlite3"
    first_store = SQLiteRunStateStore(database)
    first = LabBioApplication(_configuration(tmp_path, first_store))
    handle = first.create_run(_request())
    waiting = await first.run(handle)
    assert waiting.status is RunStatus.WAITING_FOR_USER
    original_gate = waiting.pending_user_gate
    assert original_gate is not None
    original = first_store.get(handle.run_id)
    first_store.close()

    second_store = SQLiteRunStateStore(database)
    second = LabBioApplication(_configuration(tmp_path, second_store))
    second.recover_run(
        handle.run_id, principal=_principal(), workspace=_workspace()
    )
    recovered = second.result(handle)
    assert recovered.pending_user_gate == original_gate
    assert recovered.pending_user_gate.source_stage is WorkflowStage.PLAN
    assert recovered.pending_user_gate.domain_reference_id is None
    assert second_store.get(handle.run_id).workflow_run.stage_results == (
        original.workflow_run.stage_results
    )

    completed = await second.resume_run(
        handle,
        GateUserDecision(
            gate_id=original_gate.gate_id,
            approved=True,
            decided_by=_principal().user_id,
        ),
    )
    assert completed.status is RunStatus.COMPLETED
    second_store.close()


def test_d9_run_uuid_does_not_bypass_current_identity(tmp_path):
    store = InMemoryRunStateStore()
    first = LabBioApplication(_configuration(tmp_path, store))
    handle = first.create_run(_request())
    second = LabBioApplication(_configuration(tmp_path, store))

    with pytest.raises(AuthorizationDenied):
        second.recover_run(
            handle.run_id,
            principal=Principal(user_id="intruder", lab_id="lab-c10"),
            workspace=WorkspaceContext(
                user_id="intruder",
                project_id="project-c10",
                lab_id="lab-c10",
            ),
        )


def test_d10_current_project_authorization_is_rechecked(tmp_path):
    store = InMemoryRunStateStore()
    first = LabBioApplication(_configuration(tmp_path, store))
    handle = first.create_run(_request())
    changed_projects = (
        Project(
            project_id="project-c10",
            lab_id="lab-c10",
            owner_user_id="different-owner",
        ),
    )
    second = LabBioApplication(
        _configuration(tmp_path, store, projects=changed_projects)
    )

    with pytest.raises(AuthorizationDenied):
        second.recover_run(
            handle.run_id, principal=_principal(), workspace=_workspace()
        )


def test_d11_missing_required_artifact_fails_recovery_explicitly(tmp_path):
    store = InMemoryRunStateStore()
    first = LabBioApplication(_configuration(tmp_path, store))
    artifact = first.artifact_store.register(
        artifact_type="generic-input",
        exposure_class=ArtifactExposureClass.DERIVED,
        representation=ArtifactRepresentation(summary={"records": 1}),
        owner_user_id=_principal().user_id,
        project_id=_workspace().project_id,
        lab_id=_workspace().lab_id,
    )
    handle = first.create_run(_request(input_artifact_ids=(artifact.artifact_id,)))
    second = LabBioApplication(
        _configuration(tmp_path, store, artifact_root=tmp_path / "empty-artifacts")
    )

    with pytest.raises(ApplicationRecoveryError) as caught:
        second.recover_run(
            handle.run_id, principal=_principal(), workspace=_workspace()
        )
    assert caught.value.issue_code is ApplicationRecoveryIssueCode.REQUIRED_ARTIFACT_MISSING


def test_d12_runtime_revision_mismatch_blocks_recovery(tmp_path):
    store = InMemoryRunStateStore()
    first = LabBioApplication(_configuration(tmp_path, store, revision="revision-a"))
    handle = first.create_run(_request())
    second = LabBioApplication(_configuration(tmp_path, store, revision="revision-b"))

    status = second.recovery_status(
        handle.run_id, principal=_principal(), workspace=_workspace()
    )
    assert status.recoverable is False
    assert status.issue_code is ApplicationRecoveryIssueCode.RUNTIME_REVISION_MISMATCH
    with pytest.raises(ApplicationRecoveryError) as caught:
        second.recover_run(
            handle.run_id, principal=_principal(), workspace=_workspace()
        )
    assert caught.value.issue_code is ApplicationRecoveryIssueCode.RUNTIME_REVISION_MISMATCH


@pytest.mark.asyncio
async def test_d13_stage_inflight_is_not_replayed_after_restart(tmp_path, monkeypatch):
    calls = []

    async def invoke(_self, stage_input):
        calls.append(stage_input)
        raise RuntimeError("simulated provider disconnect")

    monkeypatch.setattr(PerInvocationPantheonStageInvoker, "invoke", invoke)
    store = InMemoryRunStateStore()
    first = LabBioApplication(_configuration(tmp_path, store))
    handle = first.create_run(_request())
    with pytest.raises(RuntimeError, match="simulated provider disconnect"):
        await first.run(handle)
    durable = store.get(handle.run_id)
    assert durable.recovery_state is RunRecoveryState.STAGE_IN_FLIGHT
    assert durable.inflight_stage is WorkflowStage.INTAKE
    assert durable.inflight_invocation_id == calls[0].invocation_id
    assert durable.workflow_run.retry_counts == {}

    second = LabBioApplication(_configuration(tmp_path, store))
    status = second.recovery_status(
        handle.run_id, principal=_principal(), workspace=_workspace()
    )
    assert status.inflight_stage is WorkflowStage.INTAKE
    assert status.inflight_invocation_id == calls[0].invocation_id
    assert status.automatic_continuation_allowed is False
    with pytest.raises(ApplicationRecoveryError) as caught:
        second.recover_run(
            handle.run_id, principal=_principal(), workspace=_workspace()
        )
    assert caught.value.issue_code is ApplicationRecoveryIssueCode.STAGE_IN_FLIGHT
    assert len(calls) == 1
    assert store.get(handle.run_id).workflow_run.retry_counts == {}


@pytest.mark.asyncio
async def test_d14_uncertain_execute_side_effect_is_never_duplicated(
    tmp_path, monkeypatch
):
    effects = []

    async def invoke(_self, stage_input):
        if stage_input.stage_id is WorkflowStage.EXECUTE:
            effects.append(stage_input.invocation_id)
            raise _SimulatedProcessLoss
        return _next_result(stage_input.stage_id)

    monkeypatch.setattr(PerInvocationPantheonStageInvoker, "invoke", invoke)
    durable = InMemoryRunStateStore()
    stop_at_execute = _StopAfterCheckpointStore(
        durable,
        lambda record: (
            record.recovery_state is RunRecoveryState.STABLE
            and record.workflow_run.status is RunStatus.RUNNING
            and record.workflow_run.current_stage is WorkflowStage.EXECUTE
        ),
    )
    first = LabBioApplication(_configuration(tmp_path, stop_at_execute))
    handle = first.create_run(_request())
    with pytest.raises(_SimulatedProcessLoss):
        await first.run(handle)
    assert effects == []

    second = LabBioApplication(_configuration(tmp_path, durable))
    second.recover_run(
        handle.run_id, principal=_principal(), workspace=_workspace()
    )
    with pytest.raises(_SimulatedProcessLoss):
        await second.run(handle)
    marker = durable.get(handle.run_id)
    assert marker.recovery_state is RunRecoveryState.STAGE_IN_FLIGHT
    assert marker.inflight_stage is WorkflowStage.EXECUTE
    assert len(effects) == 1

    third = LabBioApplication(_configuration(tmp_path, durable))
    with pytest.raises(ApplicationRecoveryError) as caught:
        third.recover_run(
            handle.run_id, principal=_principal(), workspace=_workspace()
        )
    assert caught.value.issue_code is ApplicationRecoveryIssueCode.STAGE_IN_FLIGHT
    assert len(effects) == 1


class _EffectThenStopHandler(ApplicationDomainDecisionHandler):
    def __init__(self, effects):
        self.effects = effects

    def supports(self, domain_reference_id: str) -> bool:
        return domain_reference_id == "test-decision:1"

    async def apply(self, **_kwargs) -> str:
        self.effects.append("applied")
        raise _SimulatedProcessLoss


@pytest.mark.asyncio
async def test_d15_gate_decision_inflight_is_not_reapplied(tmp_path, monkeypatch):
    async def invoke(_self, stage_input):
        if stage_input.stage_id is WorkflowStage.PLAN:
            return RuntimeStageResult(
                stage_id=WorkflowStage.PLAN,
                summary="A domain decision is required.",
                body=_body(WorkflowStage.PLAN),
                next_action=NextActionProposal(
                    action=NextAction.REQUEST_USER_INPUT,
                    user_prompt="Apply the current domain decision?",
                    domain_reference_id="test-decision:1",
                ),
            )
        return _next_result(stage_input.stage_id)

    monkeypatch.setattr(PerInvocationPantheonStageInvoker, "invoke", invoke)
    effects = []
    store = InMemoryRunStateStore()
    first_handler = _EffectThenStopHandler(effects)
    first = LabBioApplication(
        _configuration(
            tmp_path, store, domain_decision_handlers=(first_handler,)
        )
    )
    handle = first.create_run(_request())
    waiting = await first.run(handle)
    gate = waiting.pending_user_gate
    assert gate is not None
    with pytest.raises(_SimulatedProcessLoss):
        await first.resume_run(
            handle,
            GateUserDecision(
                gate_id=gate.gate_id,
                approved=True,
                decided_by=_principal().user_id,
                domain_reference_id="test-decision:1",
            ),
        )
    assert effects == ["applied"]
    assert store.get(handle.run_id).recovery_state is (
        RunRecoveryState.GATE_DECISION_IN_FLIGHT
    )

    second_handler = _EffectThenStopHandler(effects)
    second = LabBioApplication(
        _configuration(
            tmp_path, store, domain_decision_handlers=(second_handler,)
        )
    )
    with pytest.raises(ApplicationRecoveryError) as caught:
        second.recover_run(
            handle.run_id, principal=_principal(), workspace=_workspace()
        )
    assert caught.value.issue_code is (
        ApplicationRecoveryIssueCode.GATE_DECISION_IN_FLIGHT
    )
    assert effects == ["applied"]


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED])
async def test_d16_d18_terminal_run_recovers_without_replay(
    tmp_path, monkeypatch, terminal
):
    calls = 0

    async def invoke(_self, stage_input):
        nonlocal calls
        calls += 1
        if terminal is RunStatus.FAILED:
            return RuntimeStageResult(
                stage_id=stage_input.stage_id,
                summary="The runtime failed explicitly.",
                body=_body(stage_input.stage_id),
                next_action=NextActionProposal(
                    action=NextAction.FAIL, reason="TEST_RUNTIME_FAILURE"
                ),
            )
        return _next_result(stage_input.stage_id)

    monkeypatch.setattr(PerInvocationPantheonStageInvoker, "invoke", invoke)
    store = InMemoryRunStateStore()
    first = LabBioApplication(_configuration(tmp_path, store))
    handle = first.create_run(_request())
    if terminal is RunStatus.CANCELLED:
        outcome = first.cancel_run(handle, reason="operator cancellation")
    else:
        outcome = await first.run(handle)
    assert outcome.status is terminal
    calls = 0

    second = LabBioApplication(_configuration(tmp_path, store))
    second.recover_run(
        handle.run_id, principal=_principal(), workspace=_workspace()
    )
    repeated = await second.run(handle)
    assert repeated.status is terminal
    assert calls == 0


def test_d19_jsonl_trace_sequence_continues_across_application_restart(tmp_path):
    store = InMemoryRunStateStore()
    trace_path = tmp_path / "trace.jsonl"
    first = LabBioApplication(
        _configuration(tmp_path, store, trace_sink=JsonlTraceSink(trace_path))
    )
    handle = first.create_run(_request())
    before = first.trace_events(handle)

    second = LabBioApplication(
        _configuration(tmp_path, store, trace_sink=JsonlTraceSink(trace_path))
    )
    second.recover_run(
        handle.run_id, principal=_principal(), workspace=_workspace()
    )
    second.cancel_run(handle, reason="trace continuity check")
    after = second.trace_events(handle)
    assert tuple(event.sequence for event in after) == tuple(range(len(after)))
    assert after[len(before)].sequence == before[-1].sequence + 1


def test_d20_observational_trace_cannot_mutate_durable_run_state(tmp_path):
    store = InMemoryRunStateStore()
    first = LabBioApplication(_configuration(tmp_path, store))
    handle = first.create_run(_request())
    first.trace_recorder.emit(
        handle.run_id,
        TraceEventType.RUN_FAILED,
        stage_id=WorkflowStage.EXECUTE,
        status=RunStatus.FAILED.value,
        payload={"reason": "observational-only"},
    )

    second = LabBioApplication(_configuration(tmp_path, store))
    second.recover_run(
        handle.run_id, principal=_principal(), workspace=_workspace()
    )
    record = store.get(handle.run_id)
    assert record.workflow_run.status is RunStatus.CREATED
    assert record.workflow_run.current_stage is None
    assert record.workflow_run.retry_counts == {}
    assert record.workflow_run.pending_user_gate is None


@pytest.mark.asyncio
async def test_d23_recovered_artifact_reference_has_no_storage_locator(
    tmp_path, monkeypatch
):
    captured = []

    async def invoke(_self, stage_input):
        captured.append(stage_input)
        return RuntimeStageResult(
            stage_id=stage_input.stage_id,
            summary="Stop at a safe generic gate.",
            body=_body(stage_input.stage_id),
            next_action=NextActionProposal(
                action=NextAction.REQUEST_USER_INPUT,
                user_prompt="Stop after inspecting current references?",
            ),
        )

    monkeypatch.setattr(PerInvocationPantheonStageInvoker, "invoke", invoke)
    store = InMemoryRunStateStore()
    first = LabBioApplication(_configuration(tmp_path, store))
    artifact = first.artifact_store.register(
        artifact_type="generic-input",
        exposure_class=ArtifactExposureClass.DERIVED,
        representation=ArtifactRepresentation(summary={"records": 1}),
        owner_user_id=_principal().user_id,
        project_id=_workspace().project_id,
        lab_id=_workspace().lab_id,
    )
    handle = first.create_run(_request(input_artifact_ids=(artifact.artifact_id,)))

    second = LabBioApplication(_configuration(tmp_path, store))
    second.recover_run(
        handle.run_id, principal=_principal(), workspace=_workspace()
    )
    encoded_references = second._sessions[
        handle.run_id
    ].artifact_references[0].model_dump_json()
    assert "storage_locator" not in encoded_references
    assert str(artifact.artifact_id) in encoded_references
    await second.run(handle)
    assert "storage_locator" not in captured[0].model_dump_json()


def _create_sqlite_gold(service, store):
    bundle = SkillSourceBundle(
        source_run_id=uuid4(),
        task_reference="Prior generic successful task.",
        final_status=RunStatus.COMPLETED,
        workflow_stage_path=(WorkflowStage.PLAN, WorkflowStage.EXECUTE),
        trace_event_ids=(),
    )
    store.save_source_bundle(bundle)
    proposal = service.create_proposal(
        bundle.bundle_id,
        SkillCuratorDraft(
            proposed_name="Generic restart-safe test procedure",
            description="Reusable procedure for a compatible current task.",
            procedure=SkillProcedureDraft(
                applicability="Use only with a compatible governed task.",
                workflow_outline=("Perform fresh current-task work.",),
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
async def test_d7_d21_c9_sqlite_skill_gate_approves_after_application_restart(
    tmp_path, monkeypatch
):
    domain_reference = {"value": None}

    async def invoke(_self, stage_input):
        if stage_input.stage_id is WorkflowStage.PLAN and not stage_input.gate_decisions:
            return RuntimeStageResult(
                stage_id=WorkflowStage.PLAN,
                summary="The runtime proposed governed Skill context.",
                body=_body(WorkflowStage.PLAN),
                next_action=NextActionProposal(
                    action=NextAction.REQUEST_USER_INPUT,
                    user_prompt="Approve this exact Skill use?",
                    domain_reference_id=domain_reference["value"],
                ),
            )
        return _next_result(stage_input.stage_id)

    monkeypatch.setattr(PerInvocationPantheonStageInvoker, "invoke", invoke)
    skill_database = tmp_path / "skills.sqlite3"
    first_skill_store = SQLiteSkillStore(skill_database)
    first_service = GoldSkillService(
        first_skill_store, SkillSourceProjector()
    )
    gold = _create_sqlite_gold(first_service, first_skill_store)
    run_store = InMemoryRunStateStore()
    first = LabBioApplication(
        _configuration(
            tmp_path,
            run_store,
            skill_service=first_service,
            domain_decision_handlers=(SkillDomainDecisionHandler(first_service),),
        )
    )
    handle = first.create_run(_request())
    use = SkillUseProposal(
        run_id=handle.run_id,
        requesting_user_id=_principal().user_id,
        project_id=_workspace().project_id,
        lab_id=_workspace().lab_id,
        skill_id=gold.skill_id,
        skill_version=gold.version,
        proposed_mode=SkillUseMode.REFERENCE,
        reason="The runtime selected this candidate for the current task.",
    )
    first_service.submit_use_proposal(use, principal=_principal())
    domain_reference["value"] = f"skill-use:{use.proposal_id}"
    waiting = await first.run(handle)
    gate = waiting.pending_user_gate
    assert gate is not None
    assert gate.domain_reference_id == domain_reference["value"]
    first_skill_store.close()

    second_skill_store = SQLiteSkillStore(skill_database)
    second_service = GoldSkillService(
        second_skill_store, SkillSourceProjector()
    )
    second = LabBioApplication(
        _configuration(
            tmp_path,
            run_store,
            skill_service=second_service,
            domain_decision_handlers=(SkillDomainDecisionHandler(second_service),),
        )
    )
    second.recover_run(
        handle.run_id, principal=_principal(), workspace=_workspace()
    )
    completed = await second.resume_run(
        handle,
        GateUserDecision(
            gate_id=gate.gate_id,
            approved=True,
            decided_by=_principal().user_id,
            domain_reference_id=domain_reference["value"],
        ),
    )
    assert completed.status is RunStatus.COMPLETED
    authorization = second_skill_store.get_authorization_for_proposal(
        use.proposal_id
    )
    assert authorization is not None
    assert authorization.approved is True
    assert authorization.run_id == handle.run_id
    decision = run_store.get(handle.run_id).workflow_run.gate_decisions[-1]
    assert UUID(decision.decision_reference_id or "") == authorization.authorization_id
    second_skill_store.close()
