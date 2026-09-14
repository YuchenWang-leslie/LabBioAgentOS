"""User answers are durable input, not approval or a replay of completed work."""

import pytest
from pydantic import ValidationError

from labbioagentos import LabBioApplication, NextActionProposal, RunStatus, SQLiteRunStateStore, WorkflowStage
from labbioagentos.runtime.pantheon import PantheonTypedStageInvoker
from test_c10_durable_control_plane import _next_result, _principal, _request, _workspace
from test_interruption_reconciliation import _capability_configuration, governed_invokers
from test_runtime_milestone_b import MockExecutor
from labbioagentos import WorkflowEngine, runtime_workflow_definition
from labbioagentos.runtime.contracts import RuntimeWorkflowControlView, runtime_stage_result_format
from labbioagentos.workflow import InvalidProposalError
from labbioagentos.application import ApplicationRunStateError


def _question(issue="audience", followup=None):
    return NextActionProposal(action="request_clarification", question={
        "issue_key": issue, "prompt": "Who will read this synthetic example?",
        "why_needed": "The audience determines the explanation's level of detail.",
        "followup_to": followup,
    })


def test_clarification_has_a_distinct_free_text_contract():
    proposal = _question()
    assert proposal.action.value == "request_clarification"
    assert proposal.question.issue_key == "audience"
    assert proposal.domain_reference_id is None


@pytest.mark.asyncio
async def test_answer_restart_preserves_completed_tools_and_reaches_later_stages(
    tmp_path, governed_invokers, monkeypatch,
):
    observed = []

    async def finalize(self, stage_input, *, capability_evidence=None):
        observed.append(stage_input)
        result = _next_result(stage_input.stage_id)
        if stage_input.stage_id is WorkflowStage.EXECUTE and not stage_input.clarifications:
            return result.model_copy(update={"next_action": _question()})
        return result

    monkeypatch.setattr(PantheonTypedStageInvoker, "invoke", finalize)
    path = tmp_path / "state.sqlite"
    store = SQLiteRunStateStore(path)
    app = LabBioApplication(_capability_configuration(tmp_path, store))
    executor = MockExecutor(app.artifact_store)
    app.execution_submission.executor = executor
    handle = app.create_run(_request())
    waiting = await app.run(handle)
    assert waiting.status is RunStatus.WAITING_FOR_USER
    assert waiting.pending_user_gate is None
    question_id = waiting.pending_clarification.question_id
    before = store.get(handle.run_id)
    assert before.clarification_checkpoint.evidence.items
    assert len(executor.plans) == 1
    store.close()


    store = SQLiteRunStateStore(path)
    app = LabBioApplication(_capability_configuration(tmp_path, store))
    app.execution_submission.executor = executor
    count = len(observed)
    assessment = app.reconcile_run(handle.run_id, principal=_principal(), workspace=_workspace())
    assert assessment.continuation_action == "WAITING_FOR_ANSWER"
    assert len(assessment.confirmed_calls) == 2
    still_waiting = await app.continue_run(handle.run_id, principal=_principal(), workspace=_workspace())
    assert still_waiting.status is RunStatus.WAITING_FOR_USER
    assert len(observed) == count
    assert app.submit_answer(handle.run_id, question_id=question_id,
                             answer_text="Readers are new to the example.",
                             principal=_principal(), workspace=_workspace()) is True
    answered = store.get(handle.run_id)
    assert answered.inflight_evidence == before.clarification_checkpoint.evidence
    assert answered.workflow_run.clarifications[0].status == "ANSWERED"
    assert len(observed) == count
    assert app.submit_answer(handle.run_id, question_id=question_id,
                             answer_text="Readers are new to the example.",
                             principal=_principal(), workspace=_workspace()) is False
    assert store.get(handle.run_id).record_version == answered.record_version
    store.close()

    store = SQLiteRunStateStore(path)
    app = LabBioApplication(_capability_configuration(tmp_path, store))
    app.execution_submission.executor = executor
    assert app.reconcile_run(handle.run_id, principal=_principal(), workspace=_workspace()).continuation_action == "FINALIZE_ONLY"
    result = await app.continue_run(handle.run_id, principal=_principal(), workspace=_workspace())
    assert result.status is RunStatus.COMPLETED
    assert len(executor.plans) == len(governed_invokers["capability"]) == 1
    for stage_input in observed[count:]:
        answer = stage_input.clarifications[0]
        assert answer.answer_text == "Readers are new to the example."
        assert answer.answer_authority.value == "USER_ASSERTION"
    assert store.get(handle.run_id).workflow_run.clarifications[0].status == "RESOLVED"
    assert store.get(handle.run_id).clarification_checkpoint is None
    store.close()


def _started():
    engine = WorkflowEngine(runtime_workflow_definition())
    run = engine.create_run()
    engine.start(run)
    return engine, run


def _answer(engine, run, text="A synthetic user preference."):
    engine.answer_clarification(run, question_id=run.pending_clarification.question_id,
                                answer_text=text, answered_by="test-user")


def test_single_followup_and_resolved_questions_do_not_repeat():
    engine, run = _started()
    engine.apply_proposal(run, _question())
    question_id = run.pending_clarification.question_id
    _answer(engine, run, "I am unsure what you mean.")
    with pytest.raises(InvalidProposalError):
        engine.apply_proposal(run, _question())
    engine.apply_proposal(run, _question(followup=question_id))
    second_id = run.pending_clarification.question_id
    _answer(engine, run, "Readers are new to this subject.")
    with pytest.raises(InvalidProposalError):
        engine.apply_proposal(run, _question(followup=second_id))
    engine.apply_proposal(run, NextActionProposal(action="continue_stage"))
    assert all(item.status == "RESOLVED" for item in run.clarifications)
    assert run.retry_counts == {}
    with pytest.raises(InvalidProposalError):
        engine.apply_proposal(run, NextActionProposal(action="continue_stage"))
    with pytest.raises(InvalidProposalError):
        engine.apply_proposal(run, _question(followup=second_id))


def test_round_limit_survives_restart_and_is_visible_in_provider_schema():
    engine, run = _started()
    for index in range(3):
        engine.apply_proposal(run, _question(issue=f"decision-{index}"))
        _answer(engine, run)
        engine.apply_proposal(run, NextActionProposal(action="continue_stage"))
    engine = WorkflowEngine(runtime_workflow_definition())
    run = engine.attach_recovered_run(run)
    with pytest.raises(InvalidProposalError, match="round limit"):
        engine.apply_proposal(run, _question(issue="another-phrasing"))
    control = RuntimeWorkflowControlView.from_run(engine.definition, run)
    assert control.clarification_rounds_remaining == 0
    assert not control.clarification_available and not control.continue_stage_available
    schema = runtime_stage_result_format(run.current_stage, control).model_json_schema()
    assert "request_clarification" not in str(schema)
    assert "continue_stage" not in str(schema)


@pytest.mark.parametrize("answer", ["", "  \n", "x" * 4001])
def test_invalid_answer_does_not_unpause_or_change_question(answer):
    engine, run = _started()
    engine.apply_proposal(run, _question())
    before = run.model_dump_json()
    with pytest.raises(ValidationError):
        _answer(engine, run, answer)
    assert run.model_dump_json() == before


@pytest.mark.asyncio
async def test_new_tools_require_explicit_answer_driven_reentry(tmp_path, governed_invokers, monkeypatch):
    async def finalize(self, stage_input, *, capability_evidence=None):
        result = _next_result(stage_input.stage_id)
        if stage_input.stage_id is WorkflowStage.EXECUTE:
            if not stage_input.clarifications:
                return result.model_copy(update={"next_action": _question()})
            if stage_input.clarifications[-1].status == "ANSWERED":
                return result.model_copy(update={"next_action": NextActionProposal(action="continue_stage")})
        return result

    monkeypatch.setattr(PantheonTypedStageInvoker, "invoke", finalize)
    store = SQLiteRunStateStore(tmp_path / "state.sqlite")
    app = LabBioApplication(_capability_configuration(tmp_path, store))
    executor = MockExecutor(app.artifact_store)
    app.execution_submission.executor = executor
    handle = app.create_run(_request())
    waiting = await app.run(handle)
    app.submit_answer(handle.run_id, question_id=waiting.pending_clarification.question_id,
        answer_text="Please use the updated synthetic preference.", principal=_principal(), workspace=_workspace())
    assert len(executor.plans) == 1
    result = await app.continue_run(handle.run_id, principal=_principal(), workspace=_workspace())
    assert result.status is RunStatus.COMPLETED
    assert len(executor.plans) == 2
    assert len(set(governed_invokers["capability"])) == 2
    assert store.get(handle.run_id).workflow_run.retry_counts == {}
    store.close()


@pytest.mark.asyncio
async def test_disconnect_immediately_after_answer_commit_is_recoverable(tmp_path, governed_invokers, monkeypatch):
    from test_c10_durable_control_plane import _SimulatedProcessLoss, _StopAfterCheckpointStore

    async def finalize(self, stage_input, *, capability_evidence=None):
        result = _next_result(stage_input.stage_id)
        if stage_input.stage_id is WorkflowStage.EXECUTE and not stage_input.clarifications:
            return result.model_copy(update={"next_action": _question()})
        return result

    monkeypatch.setattr(PantheonTypedStageInvoker, "invoke", finalize)
    path = tmp_path / "state.sqlite"
    backing = SQLiteRunStateStore(path)
    store = _StopAfterCheckpointStore(backing, lambda record:
        any(item.status == "ANSWERED" for item in record.workflow_run.clarifications))
    app = LabBioApplication(_capability_configuration(tmp_path, store))
    executor = MockExecutor(app.artifact_store)
    app.execution_submission.executor = executor
    handle = app.create_run(_request())
    waiting = await app.run(handle)
    question_id = waiting.pending_clarification.question_id
    with pytest.raises(_SimulatedProcessLoss):
        app.submit_answer(handle.run_id, question_id=question_id, answer_text="A beginner audience.",
                          principal=_principal(), workspace=_workspace())
    backing.close()
    reopened = SQLiteRunStateStore(path)
    app = LabBioApplication(_capability_configuration(tmp_path, reopened))
    app.execution_submission.executor = executor
    assert not app.submit_answer(handle.run_id, question_id=question_id, answer_text="A beginner audience.",
                                 principal=_principal(), workspace=_workspace())
    with pytest.raises(ApplicationRunStateError, match="overwritten"):
        app.submit_answer(handle.run_id, question_id=question_id, answer_text="A different answer.",
                          principal=_principal(), workspace=_workspace())
    assert (await app.continue_run(handle.run_id, principal=_principal(), workspace=_workspace())).status is RunStatus.COMPLETED
    assert len(executor.plans) == 1
    assert not any("beginner audience" in str(event.payload) for event in app.trace_events(handle))
    reopened.close()


def test_provider_wire_schema_distinguishes_question_approval_and_new_work():
    from test_next_action_semantics import _provider_schema, _actions

    engine, run = _started()
    def actions():
        control = RuntimeWorkflowControlView.from_run(engine.definition, run)
        return _actions(_provider_schema(runtime_stage_result_format(run.current_stage, control)))

    initial = actions()
    assert "request_clarification" in initial and "request_user_input" in initial
    assert "question" in initial["request_clarification"]["properties"]
    assert "user_prompt" not in initial["request_clarification"]["properties"]
    assert "continue_stage" not in initial
    engine.apply_proposal(run, _question())
    _answer(engine, run)
    assert "continue_stage" in actions()


@pytest.mark.asyncio
async def test_exact_user_answer_reaches_actual_finalizer_message():
    import json
    from types import MethodType, SimpleNamespace
    from labbioagentos import PantheonRuntimeFactory, ResponseSchemaRef, RuntimeInvocationMode, RuntimeStageInput
    from test_runtime_milestone_a import _coordinator
    from test_runtime_milestone_c2 import _catalog, _profile

    coordinator, _ = _coordinator()
    run = coordinator.engine.create_run()
    coordinator.engine.start(run)
    coordinator.engine.apply_proposal(run, _question())
    answer = '面向初学者；保留 "原结果"。\n数值格式由 Agent 自行决定。'
    _answer(coordinator.engine, run, answer)
    stage_input = coordinator.build_stage_input(run, instruction="A synthetic user request.")
    stage_input = RuntimeStageInput.model_validate_json(stage_input.model_dump_json())
    team, prompts = await PantheonRuntimeFactory(_catalog()).create_team(
        ("coordinator",), invocation_mode=RuntimeInvocationMode.FINALIZE)
    captured = []

    async def invoke(_self, message, **_kwargs):
        visible = json.loads(message)
        captured.append(visible["clarifications"][0])
        assert captured[-1]["answer_text"] == answer
        assert captured[-1]["answer_authority"] == "USER_ASSERTION"
        return SimpleNamespace(content=_next_result(WorkflowStage.INTAKE).model_dump(mode="json"))

    team.run = MethodType(invoke, team)
    result = await PantheonTypedStageInvoker(team, profile=_profile(), prompt=prompts["coordinator"],
        response_schema=ResponseSchemaRef()).invoke(stage_input)
    assert result.next_action.target_stage is WorkflowStage.UNDERSTAND
    assert len(captured) == 1
