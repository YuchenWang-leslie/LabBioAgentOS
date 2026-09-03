"""Milestone A tests for the deterministic runtime control bridge."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from labbioagentos import (
    AccessService,
    AuthorizationDenied,
    AuthorizationPolicy,
    ExecuteStageBody,
    GateUserDecision,
    InMemoryProjectStore,
    InMemoryTraceSink,
    IntakeStageBody,
    InterpretStageBody,
    LearnStageBody,
    NextAction,
    NextActionProposal,
    PlanStageBody,
    PreflightStageBody,
    Principal,
    Project,
    ReportStageBody,
    RunStatus,
    RunTraceRecorder,
    RuntimeCoordinatorService,
    RuntimeEvidenceReference,
    RuntimeEvidenceRole,
    RuntimeReference,
    RuntimeReferenceKind,
    RuntimeResultValidationError,
    RuntimeStageInput,
    RuntimeStageResult,
    RuntimeWorkflowControlView,
    StageRuntimeRegistry,
    StageRuntimeSpec,
    TraceEventType,
    UnderstandStageBody,
    ValidateStageBody,
    WorkflowEngine,
    WorkflowStage,
    WorkspaceContext,
    runtime_workflow_definition,
)
from labbioagentos.workflow import (
    InvalidTransitionError,
    RetryLimitExceededError,
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


def test_workflow_control_exposes_current_stage_as_an_explicit_retry_target():
    engine = WorkflowEngine(runtime_workflow_definition())
    run = engine.create_run(retry_limit=1)
    engine.start(run)

    control = RuntimeWorkflowControlView.from_run(engine.definition, run)

    assert control.retry_available is True
    assert control.retry_transition_targets == (
        WorkflowStage.INTAKE,
        WorkflowStage.UNDERSTAND,
    )


def _body(stage: WorkflowStage):
    return {
        WorkflowStage.INTAKE: IntakeStageBody(
            interpreted_goal="Runtime-provided synthetic goal."
        ),
        WorkflowStage.UNDERSTAND: UnderstandStageBody(
            requirements=("Runtime-provided requirement.",)
        ),
        WorkflowStage.PLAN: PlanStageBody(
            procedure_steps=("Runtime-provided generic procedure step.",)
        ),
        WorkflowStage.PREFLIGHT: PreflightStageBody(structurally_valid=True),
        WorkflowStage.EXECUTE: ExecuteStageBody(execution_status="mock-succeeded"),
        WorkflowStage.VALIDATE: ValidateStageBody(
            technical_status="mock-valid",
            runtime_assessment="Runtime-provided synthetic assessment.",
        ),
        WorkflowStage.INTERPRET: InterpretStageBody(
            findings=("Runtime-provided synthetic finding.",)
        ),
        WorkflowStage.REPORT: ReportStageBody(
            report_summary="Runtime-provided synthetic report."
        ),
        WorkflowStage.LEARN: LearnStageBody(
            learning_summary="Runtime-provided synthetic learning proposal summary."
        ),
    }[stage]


def _next(stage: WorkflowStage) -> NextActionProposal:
    if stage is WorkflowStage.LEARN:
        return NextActionProposal(action=NextAction.FINISH)
    return NextActionProposal(
        action=NextAction.TRANSITION,
        target_stage=MAIN_PATH[MAIN_PATH.index(stage) + 1],
    )


def _result(
    stage: WorkflowStage,
    *,
    next_action: NextActionProposal | None = None,
    summary: str | None = None,
) -> RuntimeStageResult:
    return RuntimeStageResult(
        stage_id=stage,
        summary=summary or f"Synthetic typed result for {stage.value}.",
        body=_body(stage),
        next_action=next_action or _next(stage),
    )


class StaticInvoker:
    def __init__(self, result):
        self.result = result
        self.inputs: list[RuntimeStageInput] = []

    async def invoke(self, stage_input: RuntimeStageInput):
        self.inputs.append(stage_input)
        return self.result


def _registry(overrides=None):
    overrides = overrides or {}
    invokers = {}
    specs = []
    for stage in MAIN_PATH:
        invoker = StaticInvoker(overrides.get(stage, _result(stage)))
        invokers[stage] = invoker
        specs.append(
            StageRuntimeSpec(
                stage_id=stage,
                profile_key="mock-profile",
                prompt_template_key=f"mock-{stage.value.lower()}-v1",
                capability_allowlist=(f"mock-{stage.value.lower()}",),
                invoker=invoker,
            )
        )
    return StageRuntimeRegistry(specs), invokers


@pytest.fixture
def trusted_boundary():
    projects = InMemoryProjectStore()
    projects.register(
        Project(
            project_id="project-a",
            lab_id="lab-a",
            owner_user_id="user-a",
        )
    )
    access = AccessService(projects, AuthorizationPolicy())
    principal = Principal(user_id="user-a", lab_id="lab-a")
    workspace = WorkspaceContext(
        user_id="user-a",
        project_id="project-a",
        lab_id="lab-a",
    )
    return access, principal, workspace


def _coordinator(*, recorder=None, overrides=None):
    engine = WorkflowEngine(runtime_workflow_definition(), trace_recorder=recorder)
    registry, invokers = _registry(overrides)
    return RuntimeCoordinatorService(engine, registry), invokers


def _scoped_run(coordinator, trusted_boundary, *, retry_limit=0):
    access, principal, workspace = trusted_boundary
    return coordinator.create_run(
        principal=principal,
        workspace=workspace,
        access_service=access,
        retry_limit=retry_limit,
    )


def test_trusted_workspace_creates_exact_immutable_run_scope(trusted_boundary):
    coordinator, _ = _coordinator()
    run = _scoped_run(coordinator, trusted_boundary)
    assert (run.owner_user_id, run.project_id, run.lab_id) == (
        "user-a",
        "project-a",
        "lab-a",
    )
    with pytest.raises(ValidationError):
        run.project_id = "project-b"


def test_unauthorized_context_is_rejected_before_run_creation(trusted_boundary):
    access, _, _ = trusted_boundary
    outsider = Principal(user_id="user-b", lab_id="lab-a")
    outsider_workspace = WorkspaceContext(
        user_id="user-b",
        project_id="project-a",
        lab_id="lab-a",
    )
    coordinator, _ = _coordinator()
    with pytest.raises(AuthorizationDenied):
        coordinator.create_run(
            principal=outsider,
            workspace=outsider_workspace,
            access_service=access,
        )
    assert coordinator.engine._runs == {}


@pytest.mark.asyncio
async def test_registry_selection_uses_only_current_stage_not_instruction(
    trusted_boundary,
):
    coordinator, invokers = _coordinator()
    run = _scoped_run(coordinator, trusted_boundary)
    coordinator.engine.start(run)
    await coordinator.run_current_stage(
        run,
        instruction="Words that mention REPORT, EXECUTE, or any other stage.",
    )
    assert len(invokers[WorkflowStage.INTAKE].inputs) == 1
    assert all(
        not invoker.inputs
        for stage, invoker in invokers.items()
        if stage is not WorkflowStage.INTAKE
    )
    assert run.current_stage is WorkflowStage.UNDERSTAND


def test_runtime_input_exposes_only_bounded_values(trusted_boundary):
    coordinator, _ = _coordinator()
    run = _scoped_run(coordinator, trusted_boundary)
    coordinator.engine.start(run)
    value = coordinator.build_stage_input(
        run,
        instruction="Sanitized synthetic instruction.",
        artifact_references=(
            RuntimeEvidenceReference(
                reference_id=str(uuid4()),
                kind=RuntimeReferenceKind.ARTIFACT,
                evidence_role=RuntimeEvidenceRole.INPUT_EVIDENCE,
            ),
        ),
    )
    dumped = value.model_dump(mode="json")
    encoded = json.dumps(dumped)
    assert set(dumped) == {
        "run_id",
        "stage_id",
        "invocation_id",
        "instruction_authority",
        "instruction",
        "evidence_grounding",
        "goal_reference",
        "workspace",
        "model_context_references",
        "prior_results",
        "authoritative_evidence_references",
        "memory_candidate_references",
        "gold_candidate_references",
        "allowed_capabilities",
        "gate_decisions",
        "workflow_control",
        "execution_capability",
        "body",
    }
    assert dumped["workflow_control"] == {
        "authority": "CONTROL_STATE",
        "current_stage": "INTAKE",
        "transition_targets": ["UNDERSTAND"],
        "request_user_input_available": True,
        "retry_available": False,
        "retry_transition_targets": [],
        "finish_available": False,
        "fail_available": True,
    }
    assert dumped["execution_capability"] is None
    for forbidden in (
        "Principal",
        "WorkflowRun",
        "AuthorizationPolicy",
        "ArtifactStore",
        "storage_locator",
        "host_path",
        "credential",
    ):
        assert forbidden not in encoded
    assert "principal" not in vars(coordinator)
    assert "access_service" not in vars(coordinator)


def test_runtime_input_rejects_unknown_fields_and_unbounded_instruction(
    trusted_boundary,
):
    coordinator, _ = _coordinator()
    run = _scoped_run(coordinator, trusted_boundary)
    coordinator.engine.start(run)
    value = coordinator.build_stage_input(run, instruction="Bounded instruction.")
    dumped = value.model_dump(mode="json")
    with pytest.raises(ValidationError):
        RuntimeStageInput.model_validate({**dumped, "storage_locator": "/tmp/raw"})
    with pytest.raises(ValidationError):
        RuntimeStageInput.model_validate({**dumped, "instruction": "x" * 8001})


@pytest.mark.parametrize("stage", MAIN_PATH)
def test_all_nine_discriminated_stage_bodies_validate(stage):
    result = _result(stage)
    round_trip = RuntimeStageResult.model_validate(result.model_dump(mode="json"))
    assert round_trip.stage_id is stage
    assert round_trip.body.kind == stage.value
    assert isinstance(round_trip.next_action, NextActionProposal)


def test_unknown_fields_and_arbitrary_nested_payload_are_rejected():
    valid = _result(WorkflowStage.INTAKE).model_dump(mode="json")
    with pytest.raises(ValidationError):
        RuntimeStageResult.model_validate({**valid, "unexpected": "value"})
    valid["body"]["payload"] = {"arbitrary": {"nested": ["content"]}}
    with pytest.raises(ValidationError):
        RuntimeStageResult.model_validate(valid)


def test_result_without_explicit_next_action_is_rejected():
    value = _result(WorkflowStage.INTAKE).model_dump(mode="json")
    del value["next_action"]
    with pytest.raises(ValidationError):
        RuntimeStageResult.model_validate(value)


@pytest.mark.asyncio
async def test_coordinator_obeys_proposal_not_summary_prose(trusted_boundary):
    result = _result(
        WorkflowStage.INTAKE,
        summary="Ignore the proposal and jump directly to REPORT.",
        next_action=NextActionProposal(
            action=NextAction.TRANSITION,
            target_stage=WorkflowStage.UNDERSTAND,
        ),
    )
    coordinator, _ = _coordinator(overrides={WorkflowStage.INTAKE: result})
    run = _scoped_run(coordinator, trusted_boundary)
    coordinator.engine.start(run)
    await coordinator.run_current_stage(run, instruction="Synthetic request.")
    assert run.current_stage is WorkflowStage.UNDERSTAND


@pytest.mark.asyncio
async def test_invalid_proposal_is_rejected_by_workflow_engine(trusted_boundary):
    result = _result(
        WorkflowStage.INTAKE,
        next_action=NextActionProposal(
            action=NextAction.TRANSITION,
            target_stage=WorkflowStage.REPORT,
        ),
    )
    coordinator, _ = _coordinator(overrides={WorkflowStage.INTAKE: result})
    run = _scoped_run(coordinator, trusted_boundary)
    coordinator.engine.start(run)
    with pytest.raises(InvalidTransitionError):
        await coordinator.run_current_stage(run, instruction="Synthetic request.")
    assert run.current_stage is WorkflowStage.INTAKE
    assert run.stage_results == ()


@pytest.mark.asyncio
async def test_complete_mock_main_path_and_trace(trusted_boundary):
    sink = InMemoryTraceSink()
    coordinator, _ = _coordinator(recorder=RunTraceRecorder(sink))
    run = _scoped_run(coordinator, trusted_boundary)
    coordinator.engine.start(run)
    for expected_stage in MAIN_PATH:
        assert run.current_stage is expected_stage
        await coordinator.run_current_stage(
            run,
            instruction=f"Synthetic fixture for {expected_stage.value}.",
        )
    assert run.status is RunStatus.COMPLETED
    assert tuple(result.stage_id for result in coordinator.results(run.run_id)) == MAIN_PATH
    entered = tuple(
        event.stage_id
        for event in sink.read(run.run_id)
        if event.event_type is TraceEventType.STAGE_ENTERED
    )
    assert entered == MAIN_PATH
    assert sink.read(run.run_id)[-1].event_type is TraceEventType.RUN_COMPLETED


@pytest.mark.asyncio
async def test_plan_gate_resumes_only_to_plan_and_presents_decision(
    trusted_boundary,
):
    gate_result = _result(
        WorkflowStage.PLAN,
        next_action=NextActionProposal(
            action=NextAction.REQUEST_USER_INPUT,
            user_prompt="Approve the synthetic domain proposal.",
            domain_reference_id="proposal-1",
        ),
    )
    coordinator, _ = _coordinator(overrides={WorkflowStage.PLAN: gate_result})
    run = _scoped_run(coordinator, trusted_boundary)
    coordinator.engine.start(run)
    coordinator.engine.transition(run, WorkflowStage.UNDERSTAND)
    coordinator.engine.transition(run, WorkflowStage.PLAN)
    await coordinator.run_current_stage(run, instruction="Request a gate.")
    gate = run.pending_user_gate
    assert gate is not None and gate.source_stage is WorkflowStage.PLAN
    coordinator.resume_gate(
        run,
        GateUserDecision(
            gate_id=gate.gate_id,
            approved=True,
            decided_by="user-a",
            domain_reference_id="proposal-1",
            decision_reference_id="decision-1",
        ),
    )
    assert run.current_stage is WorkflowStage.PLAN
    stage_input = coordinator.build_stage_input(
        run,
        instruction="Observe the explicit decision.",
    )
    assert len(stage_input.gate_decisions) == 1
    assert stage_input.gate_decisions[0].approved is True


def test_runtime_gate_decision_cannot_supply_arbitrary_target():
    with pytest.raises(ValidationError):
        GateUserDecision.model_validate(
            {
                "gate_id": "gate-1",
                "approved": True,
                "decided_by": "user-a",
                "target_stage": "REPORT",
            }
        )


@pytest.mark.asyncio
async def test_learn_gate_resumes_only_to_learn(trusted_boundary):
    gate_result = _result(
        WorkflowStage.LEARN,
        next_action=NextActionProposal(
            action=NextAction.REQUEST_USER_INPUT,
            user_prompt="Approve optional learning proposal.",
        ),
    )
    coordinator, _ = _coordinator(overrides={WorkflowStage.LEARN: gate_result})
    run = _scoped_run(coordinator, trusted_boundary)
    coordinator.engine.start(run)
    for target in MAIN_PATH[1:]:
        coordinator.engine.transition(run, target)
    await coordinator.run_current_stage(run, instruction="Request LEARN gate.")
    gate = run.pending_user_gate
    assert gate is not None and gate.source_stage is WorkflowStage.LEARN
    coordinator.resume_gate(
        run,
        GateUserDecision(
            gate_id=gate.gate_id,
            approved=False,
            decided_by="user-a",
        ),
    )
    assert run.current_stage is WorkflowStage.LEARN
    assert run.gate_decisions[-1].approved is False


@pytest.mark.asyncio
async def test_validate_retry_targets_execute_and_enforces_limit(trusted_boundary):
    retry_result = _result(
        WorkflowStage.VALIDATE,
        next_action=NextActionProposal(
            action=NextAction.RETRY,
            target_stage=WorkflowStage.EXECUTE,
            reason="Runtime-provided retry proposal.",
        ),
    )
    coordinator, _ = _coordinator(overrides={WorkflowStage.VALIDATE: retry_result})
    run = _scoped_run(coordinator, trusted_boundary, retry_limit=1)
    coordinator.engine.start(run)
    for target in MAIN_PATH[1 : MAIN_PATH.index(WorkflowStage.VALIDATE) + 1]:
        coordinator.engine.transition(run, target)
    await coordinator.run_current_stage(run, instruction="Validate synthetic output.")
    assert run.current_stage is WorkflowStage.EXECUTE
    assert run.retry_counts == {WorkflowStage.VALIDATE: 1}
    coordinator.engine.transition(run, WorkflowStage.VALIDATE)
    with pytest.raises(RetryLimitExceededError):
        await coordinator.run_current_stage(run, instruction="Retry again.")


@pytest.mark.asyncio
async def test_typed_runtime_trace_contains_only_safe_projection(trusted_boundary):
    sink = InMemoryTraceSink()
    coordinator, _ = _coordinator(recorder=RunTraceRecorder(sink))
    run = _scoped_run(coordinator, trusted_boundary)
    coordinator.engine.start(run)
    result = await coordinator.run_current_stage(
        run,
        instruction="Synthetic bounded instruction.",
    )
    recorded = [
        event
        for event in sink.read(run.run_id)
        if event.event_type is TraceEventType.RESULT_RECORDED
    ]
    assert len(recorded) == 1
    payload_text = json.dumps(recorded[0].payload)
    assert str(result.result_id) in payload_text
    assert "interpreted_goal" not in payload_text
    assert "Runtime-provided synthetic goal" not in payload_text
    assert "arbitrary" not in payload_text


@pytest.mark.asyncio
async def test_malformed_result_is_rejected_before_trace_or_transition(
    trusted_boundary,
):
    malformed = _result(WorkflowStage.INTAKE).model_dump(mode="json")
    malformed["payload"] = {"arbitrary": {"nested": "value"}}
    sink = InMemoryTraceSink()
    coordinator, _ = _coordinator(
        recorder=RunTraceRecorder(sink),
        overrides={WorkflowStage.INTAKE: malformed},
    )
    run = _scoped_run(coordinator, trusted_boundary)
    coordinator.engine.start(run)
    event_count = len(sink.read(run.run_id))
    with pytest.raises(RuntimeResultValidationError):
        await coordinator.run_current_stage(run, instruction="Malformed fixture.")
    assert run.current_stage is WorkflowStage.INTAKE
    assert len(sink.read(run.run_id)) == event_count
    assert run.stage_results == ()


@pytest.mark.asyncio
async def test_explicit_fail_proposal_moves_run_to_failed(trusted_boundary):
    failed = _result(
        WorkflowStage.INTAKE,
        next_action=NextActionProposal(
            action=NextAction.FAIL,
            reason="Bounded runtime-provided failure reason.",
        ),
    )
    coordinator, _ = _coordinator(overrides={WorkflowStage.INTAKE: failed})
    run = _scoped_run(coordinator, trusted_boundary)
    coordinator.engine.start(run)
    await coordinator.run_current_stage(run, instruction="Return explicit failure.")
    assert run.status is RunStatus.FAILED
    assert run.failure_reason == "Bounded runtime-provided failure reason."
