"""Deterministic Phase 2 WorkflowEngine tests with no live model or data."""

from __future__ import annotations

from types import MethodType

import pytest
from pantheon.agent import Agent, AgentResponse
from pantheon.team import PantheonTeam

from labbioagentos import (
    AgentStageResult,
    NextAction,
    NextActionProposal,
    PantheonStageAdapter,
    RunStatus,
    StageContext,
    UserDecision,
    WorkflowDefinition,
    WorkflowEngine,
    WorkflowEventType,
    WorkflowStage,
    WorkflowTransition,
    default_workflow_definition,
)
from labbioagentos.workflow import (
    InvalidProposalError,
    InvalidRunStateError,
    InvalidTransitionError,
    RetryLimitExceededError,
    UserDecisionRequiredError,
)


DEFAULT_PATH = (
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


def _result(stage: WorkflowStage) -> AgentStageResult:
    return AgentStageResult(
        stage=stage,
        summary=f"Deterministic mock result for {stage.value}.",
        payload={"fixture": "phase-2"},
    )


def _user_gate_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id="user-gate-test",
        nodes=frozenset(
            {
                WorkflowStage.PLAN,
                WorkflowStage.USER_GATE,
                WorkflowStage.PREFLIGHT,
            }
        ),
        allowed_transitions=frozenset(
            {
                WorkflowTransition(
                    source=WorkflowStage.PLAN,
                    target=WorkflowStage.USER_GATE,
                ),
                WorkflowTransition(
                    source=WorkflowStage.USER_GATE,
                    target=WorkflowStage.PREFLIGHT,
                ),
            }
        ),
        initial_stage=WorkflowStage.PLAN,
        terminal_stages=frozenset({WorkflowStage.PREFLIGHT}),
    )


def _contains_identity(value, target: object) -> bool:
    if value is target:
        return True
    if isinstance(value, dict):
        return any(
            _contains_identity(key, target) or _contains_identity(item, target)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_identity(item, target) for item in value)
    return False


def test_default_workflow_reaches_completion_from_manual_results_and_proposals():
    engine = WorkflowEngine(default_workflow_definition())
    run = engine.create_run(retry_limit=1)

    engine.start(run)
    assert run.status is RunStatus.RUNNING
    assert run.current_stage is WorkflowStage.INTAKE

    for index, stage in enumerate(DEFAULT_PATH):
        assert run.current_stage is stage
        engine.record_stage_result(run, _result(stage))
        if index < len(DEFAULT_PATH) - 1:
            engine.apply_proposal(
                run,
                NextActionProposal(
                    action=NextAction.TRANSITION,
                    target_stage=DEFAULT_PATH[index + 1],
                ),
            )
        else:
            engine.apply_proposal(
                run,
                NextActionProposal(action=NextAction.FINISH),
            )

    assert run.status is RunStatus.COMPLETED
    assert tuple(result.stage for result in run.stage_results) == DEFAULT_PATH


def test_invalid_stage_transition_is_rejected_without_mutation():
    engine = WorkflowEngine(default_workflow_definition())
    run = engine.create_run()
    engine.start(run)
    history_before = run.history

    with pytest.raises(InvalidTransitionError):
        engine.transition(run, WorkflowStage.EXECUTE)

    assert run.current_stage is WorkflowStage.INTAKE
    assert run.status is RunStatus.RUNNING
    assert run.history == history_before


@pytest.mark.asyncio
async def test_pantheon_team_cannot_change_engine_owned_workflow_run():
    engine = WorkflowEngine(default_workflow_definition())
    run = engine.create_run()
    engine.start(run)
    state_before = run.model_copy(deep=True)
    observed: dict = {}

    agent = Agent(
        name="phase2_mock_reasoner",
        instructions="Return the supplied protocol object.",
        model="openai/mock-model",
    )

    async def mock_run(self, message, **kwargs):
        observed["message"] = message
        observed["kwargs"] = kwargs
        return AgentResponse(
            agent_name=self.name,
            content=_result(WorkflowStage.INTAKE).model_dump(mode="json"),
            details=None,
        )

    agent.run = MethodType(mock_run, agent)
    team = PantheonTeam(agents=[agent])
    adapter = PantheonStageAdapter(team)
    context = StageContext(
        run_id=run.run_id,
        stage=WorkflowStage.INTAKE,
        instruction="Return the deterministic Phase 2 fixture.",
    )

    result = await adapter.run_stage(context)

    assert run == state_before
    assert not _contains_identity(vars(adapter), run)
    assert not _contains_identity(vars(team), run)
    assert not _contains_identity(observed, run)

    engine.record_stage_result(run, result)
    assert run.stage_results == (result,)


def test_user_gate_enters_waiting_state():
    engine = WorkflowEngine(_user_gate_definition())
    run = engine.create_run()
    engine.start(run)

    engine.apply_proposal(
        run,
        NextActionProposal(
            action=NextAction.REQUEST_USER_INPUT,
            user_prompt="Confirm the structural continuation.",
        ),
    )

    assert run.current_stage is WorkflowStage.USER_GATE
    assert run.status is RunStatus.WAITING_FOR_USER
    assert run.pending_user_gate is not None
    assert run.pending_user_gate.prompt == "Confirm the structural continuation."


def test_user_gate_cannot_be_left_without_explicit_matching_decision():
    engine = WorkflowEngine(_user_gate_definition())
    run = engine.create_run()
    engine.start(run)
    engine.pause_for_user(run, "Confirm continuation.")

    with pytest.raises(InvalidRunStateError):
        engine.transition(run, WorkflowStage.PREFLIGHT)
    with pytest.raises(UserDecisionRequiredError):
        engine.resume(run, None)  # type: ignore[arg-type]
    with pytest.raises(UserDecisionRequiredError):
        engine.resume(
            run,
            UserDecision(
                gate_id="wrong-gate",
                target_stage=WorkflowStage.PREFLIGHT,
            ),
        )

    assert run.current_stage is WorkflowStage.USER_GATE
    assert run.status is RunStatus.WAITING_FOR_USER


def test_resume_after_valid_user_decision():
    engine = WorkflowEngine(_user_gate_definition())
    run = engine.create_run()
    engine.start(run)
    engine.pause_for_user(run, "Confirm continuation.")
    gate = run.pending_user_gate
    assert gate is not None

    engine.resume(
        run,
        UserDecision(
            gate_id=gate.gate_id,
            target_stage=WorkflowStage.PREFLIGHT,
        ),
    )

    assert run.status is RunStatus.RUNNING
    assert run.current_stage is WorkflowStage.PREFLIGHT
    assert run.pending_user_gate is None
    assert any(
        entry.event is WorkflowEventType.USER_DECISION_RECORDED
        for entry in run.history
    )


def test_failure_produces_failed_state_and_history():
    engine = WorkflowEngine(default_workflow_definition())
    run = engine.create_run()
    engine.start(run)

    engine.fail(run, "deterministic mock failure")

    assert run.status is RunStatus.FAILED
    assert run.failure_reason == "deterministic mock failure"
    assert run.history[-1].event is WorkflowEventType.FAILED
    with pytest.raises(InvalidRunStateError):
        engine.transition(run, WorkflowStage.UNDERSTAND)


def test_retry_count_and_limit_are_enforced_without_repair_logic():
    engine = WorkflowEngine(default_workflow_definition())
    run = engine.create_run(retry_limit=2)
    engine.start(run)
    retry = NextActionProposal(action=NextAction.RETRY)

    engine.apply_proposal(run, retry)
    engine.apply_proposal(run, retry)

    assert run.retry_counts == {WorkflowStage.INTAKE: 2}
    assert [
        entry.retry_count
        for entry in run.history
        if entry.event is WorkflowEventType.RETRIED
    ] == [1, 2]
    with pytest.raises(RetryLimitExceededError):
        engine.apply_proposal(run, retry)
    assert run.status is RunStatus.RUNNING
    assert run.current_stage is WorkflowStage.INTAKE


def test_stage_history_reconstructs_workflow_path_and_statuses():
    engine = WorkflowEngine(default_workflow_definition())
    run = engine.create_run()
    engine.start(run)
    engine.transition(run, WorkflowStage.UNDERSTAND)
    engine.transition(run, WorkflowStage.PLAN)

    path = tuple(
        entry.stage
        for entry in run.history
        if entry.event is WorkflowEventType.STAGE_ENTERED
    )

    assert path == (
        WorkflowStage.INTAKE,
        WorkflowStage.UNDERSTAND,
        WorkflowStage.PLAN,
    )
    assert [entry.sequence for entry in run.history] == list(range(len(run.history)))
    assert run.history[0].status is RunStatus.CREATED
    assert run.history[-1].status is RunStatus.RUNNING


def test_manual_next_action_proposal_is_validated_then_applied():
    engine = WorkflowEngine(default_workflow_definition())
    run = engine.create_run()
    engine.start(run)
    proposal = NextActionProposal(
        action=NextAction.TRANSITION,
        target_stage=WorkflowStage.UNDERSTAND,
        reason="Manual structural fixture.",
    )

    assert engine.validate_proposal(run, proposal) is proposal
    engine.apply_proposal(run, proposal)

    assert run.current_stage is WorkflowStage.UNDERSTAND


@pytest.mark.parametrize(
    "proposal",
    [
        {"action": "unsupported"},
        {"action": "transition"},
        {"action": "finish", "target_stage": "LEARN"},
        {"action": "retry", "unexpected": True},
    ],
)
def test_malformed_or_unsupported_proposal_is_rejected_safely(proposal):
    engine = WorkflowEngine(default_workflow_definition())
    run = engine.create_run(retry_limit=1)
    engine.start(run)
    state_before = run.model_copy(deep=True)

    with pytest.raises(InvalidProposalError):
        engine.apply_proposal(run, proposal)

    assert run == state_before

