"""Legal follow-up identities and terminal invalid-result persistence."""

from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator

from labbioagentos import LabBioApplication, RunStatus, SQLiteRunStateStore, WorkflowStage
from labbioagentos.run_state import RunRecoveryState
from labbioagentos.runtime.contracts import RuntimeWorkflowControlView, runtime_stage_result_format
from labbioagentos.runtime.pantheon import PantheonRuntimeIntegrationError, PantheonTypedStageInvoker
from test_clarification_continuation import _started, _answer, _question
from test_c10_durable_control_plane import _next_result, _principal, _request, _workspace
from test_interruption_reconciliation import _capability_configuration, governed_invokers  # noqa: F401
from test_next_action_semantics import _provider_schema
from test_runtime_milestone_b import MockExecutor


def accepts_question(engine, run, question):
    control = RuntimeWorkflowControlView.from_run(engine.definition, run)
    schema = _provider_schema(runtime_stage_result_format(run.current_stage, control))
    result = _next_result(run.current_stage).model_dump(mode="json")
    result["next_action"] = question.model_dump(mode="json")
    return not list(Draft202012Validator(schema).iter_errors({"result": result}))


def test_first_question_provider_schema_rejects_explanation_in_followup_id():
    engine, run = _started()
    assert accepts_question(engine, run, _question())
    assert not accepts_question(engine, run, _question(followup="No prior clarification round occurred; this is the first question."))
    assert not accepts_question(engine, run, _question(followup=str(uuid4())))


def test_followup_schema_only_exposes_an_answered_not_exhausted_identity():
    engine, run = _started()
    engine.apply_proposal(run, _question())
    first_id = run.pending_clarification.question_id
    _answer(engine, run)
    assert accepts_question(engine, run, _question(followup=first_id))
    assert not accepts_question(engine, run, _question(followup=str(uuid4())))
    engine.apply_proposal(run, _question(followup=first_id))
    second_id = run.pending_clarification.question_id
    _answer(engine, run)
    assert not accepts_question(engine, run, _question(followup=second_id))
    assert accepts_question(engine, run, _question(issue="different-user-choice"))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["semantic_proposal", "schema_result"])
async def test_invalid_final_result_fails_stably_without_replaying_completed_tools(
    tmp_path, governed_invokers, monkeypatch, failure,
):
    async def finalize(self, stage_input, *, capability_evidence=None):
        result = _next_result(stage_input.stage_id)
        if stage_input.stage_id is WorkflowStage.EXECUTE:
            if failure == "schema_result":
                raise PantheonRuntimeIntegrationError("MALFORMED_RUNTIME_RESULT", "PRIVATE_ERROR_MESSAGE")
            return result.model_copy(update={"next_action": _question(followup="This is a first question, not an ID.")})
        return result

    monkeypatch.setattr(PantheonTypedStageInvoker, "invoke", finalize)
    path = tmp_path / "state.sqlite"
    store = SQLiteRunStateStore(path)
    app = LabBioApplication(_capability_configuration(tmp_path, store))
    executor = MockExecutor(app.artifact_store)
    app.execution_submission.executor = executor
    handle = app.create_run(_request())
    result = await app.run(handle)
    assert result.status is RunStatus.FAILED
    saved = store.get(handle.run_id)
    assert saved.recovery_state is RunRecoveryState.STABLE
    assert saved.inflight_invocation_id is None and saved.inflight_result is None
    assert saved.workflow_run.pending_clarification is None
    assert not any(r.stage_id is WorkflowStage.EXECUTE for r in saved.runtime_results)
    rejected = saved.rejected_stage_checkpoint
    assert rejected.stage_input.stage_id is WorkflowStage.EXECUTE
    assert len(rejected.evidence.items) == 2
    assert (rejected.result is not None) == (failure == "semantic_proposal")
    assert "PRIVATE_ERROR_MESSAGE" not in saved.model_dump_json()
    assert "PRIVATE_PROGRAM_MARKER" not in saved.model_dump_json()
    assert len(executor.plans) == 1
    store.close()
    store = SQLiteRunStateStore(path)
    app = LabBioApplication(_capability_configuration(tmp_path, store))
    app.execution_submission.executor = executor
    status = app.reconcile_run(handle.run_id, principal=_principal(), workspace=_workspace())
    assert status.continuation_action == "STABLE" and not status.uncertain_side_effects
    assert len(status.confirmed_calls) == 2
    assert (await app.continue_run(handle.run_id, principal=_principal(), workspace=_workspace())).status is RunStatus.FAILED
    assert len(executor.plans) == 1
    assert store.get(handle.run_id).rejected_stage_checkpoint == rejected
    store.close()


@pytest.mark.asyncio
async def test_recovered_finalization_rejection_preserves_evidence_without_replay(tmp_path, governed_invokers, monkeypatch):
    from test_interruption_reconciliation import _interrupt_after_tools
    app, store, handle, executor = await _interrupt_after_tools(tmp_path, governed_invokers)

    async def invalid(self, stage_input, *, capability_evidence=None):
        return _next_result(stage_input.stage_id).model_copy(update={
            "next_action": _question(followup="First question explanation is not an ID.")})

    monkeypatch.setattr(PantheonTypedStageInvoker, "invoke", invalid)
    result = await app.continue_run(handle.run_id, principal=_principal(), workspace=_workspace())
    assert result.status is RunStatus.FAILED
    assert store.get(handle.run_id).recovery_state is RunRecoveryState.STABLE
    assert len(store.get(handle.run_id).rejected_stage_checkpoint.evidence.items) == 2
    assert len(executor.plans) == 1
    store.close()
