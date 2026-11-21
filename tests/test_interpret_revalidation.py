"""Model-selected revalidation uses the runtime graph and normal checkpoints."""

import pytest
from pydantic import ValidationError

from labbioagentos import (
    ApplicationRunRequest, LabBioApplication, NativeKernelStageInvoker, NextAction,
    NextActionProposal, Principal, RunStatus, SQLiteRunStateStore, WorkflowStage,
    WorkspaceContext, default_workflow_definition,
)
from labbioagentos.runtime.contracts import runtime_stage_result_format
from test_application_runtime_c5 import MAIN_PATH, _configuration, _next_result


@pytest.mark.asyncio
async def test_interpret_may_send_late_conflict_through_validation(tmp_path, monkeypatch):
    stages = WorkflowStage
    observed = []
    discrepancy = "A later observation conflicts with the earlier validation."

    async def invoke(_self, stage_input):
        stage = stage_input.stage_id
        observed.append(stage)
        result = _next_result(stage)
        if stage == stages.INTERPRET and observed.count(stage) == 1:
            control = stage_input.workflow_control
            assert set(control.transition_targets) == {stages.VALIDATE, stages.REPORT}
            assert "execution_submit" not in stage_input.allowed_capabilities
            schema = runtime_stage_result_format(stage, control)
            payload = dict(stage_id=stage, summary=discrepancy, body=result.body)
            for target in control.transition_targets:
                action = NextActionProposal(action=NextAction.TRANSITION, target_stage=target)
                schema.model_validate(dict(payload, next_action=action.model_dump()))
            with pytest.raises(ValidationError):
                schema.model_validate(dict(payload, next_action={"action": "transition",
                                                                 "target_stage": "EXECUTE"}))
            result = result.model_copy(update={"summary": discrepancy, "next_action": NextActionProposal(
                action=NextAction.TRANSITION, target_stage=stages.VALIDATE)})
        elif stage == stages.VALIDATE and observed[-2] == stages.INTERPRET:
            prior = stage_input.prior_results[-1]
            assert prior.stage_id == stages.INTERPRET
            assert prior.model_summary == discrepancy
            result = result.model_copy(update={"next_action": NextActionProposal(
                action=NextAction.TRANSITION, target_stage=stages.EXECUTE)})
        return result

    assert not default_workflow_definition().allows(stages.INTERPRET, stages.VALIDATE)
    monkeypatch.setattr(NativeKernelStageInvoker, "invoke", invoke)
    path = tmp_path / "state.sqlite"
    state = SQLiteRunStateStore(path)
    app = LabBioApplication(_configuration(tmp_path, run_state_store=state))
    handle = app.create_run(ApplicationRunRequest(
        task_text="Check conflicting observations before delivery.",
        principal=Principal(user_id="user-c5", lab_id="lab-c5"),
        workspace=WorkspaceContext(user_id="user-c5", project_id="project-c5", lab_id="lab-c5"),
    ))
    assert (await app.run(handle)).status is RunStatus.COMPLETED
    state.close()

    reopened = SQLiteRunStateStore(path)
    try:
        checkpoint = reopened.get(handle.run_id)
        expected = (*MAIN_PATH[:7], stages.VALIDATE, stages.EXECUTE, *MAIN_PATH[5:])
        assert tuple(result.stage_id for result in checkpoint.runtime_results) == expected
        assert tuple(result.stage for result in checkpoint.workflow_run.stage_results) == expected
        assert tuple(observed) == expected
        assert checkpoint.workflow_run.retry_counts == {}
    finally:
        reopened.close()
