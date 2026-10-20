"""Workflow checkpoints, not an in-memory marker, prevent post-handoff replay."""

from dataclasses import replace

import pytest

from labbioagentos import (
    ApplicationRecoveryError, LabBioApplication, LabBioRuntimeToolSet,
    RunRecoveryState, SQLiteRunStateStore, WorkflowStage,
)
from labbioagentos.execution import JobLedger
from test_c10_durable_control_plane import _principal, _request, _SimulatedProcessLoss, _workspace
from test_execution_inspection import OriginalScriptExecutor
from test_interruption_reconciliation import _capability_configuration, governed_invokers  # noqa: F401


@pytest.mark.asyncio
async def test_persisted_receipt_without_capability_checkpoint_cannot_resume(
    tmp_path, monkeypatch, governed_invokers,
):
    path = tmp_path / "runs.sqlite"
    ledger_path = tmp_path / "jobs.sqlite"
    state, ledger = SQLiteRunStateStore(path), JobLedger(ledger_path)
    configuration = replace(_capability_configuration(tmp_path, state), job_ledger=ledger)
    first = LabBioApplication(configuration)
    executor = OriginalScriptExecutor(first.artifact_store, tmp_path)
    first.execution_submission.executor = executor
    original_record = LabBioRuntimeToolSet._record_observation

    def unavailable_archive(self, item, response):
        if item.capability_name == "execution_submit":
            raise OSError("synthetic archive failure after execution return")
        return original_record(self, item, response)

    monkeypatch.setattr(LabBioRuntimeToolSet, "_record_observation", unavailable_archive)
    mark = first.execution_submission.mark_unconfirmed_handoff

    def lose_process_after_marker(run_id, execution_id):
        mark(run_id, execution_id)
        raise _SimulatedProcessLoss

    monkeypatch.setattr(first.execution_submission, "mark_unconfirmed_handoff",
                        lose_process_after_marker)
    handle = first.create_run(_request())
    with pytest.raises(_SimulatedProcessLoss):
        await first.run(handle)
    saved = state.get(handle.run_id)
    assert saved.recovery_state is RunRecoveryState.STAGE_IN_FLIGHT
    assert saved.inflight_stage is WorkflowStage.EXECUTE
    assert saved.inflight_input is not None
    assert saved.inflight_evidence is None
    execution_id = executor.plans[0].execution_id
    assert ledger.execution_submission(handle.run_id, execution_id) is not None
    assert first.execution_submission._persistence_failures == {handle.run_id: execution_id}
    state.close()
    ledger.close()

    reopened_state, reopened_ledger = SQLiteRunStateStore(path), JobLedger(ledger_path)
    second = LabBioApplication(replace(configuration, run_state_store=reopened_state,
                                       job_ledger=reopened_ledger))
    second.execution_submission.executor = executor
    try:
        assert second.execution_submission._persistence_failures == {}
        page = second.execution_submission.inspect(
            execution_id, principal=_principal(), workspace=_workspace(), run_id=handle.run_id,
        )
        assert page["receipt"]["execution_id"] == str(execution_id)
        assessment = second.reconcile_run(
            handle.run_id, principal=_principal(), workspace=_workspace(),
        )
        assert assessment.continuation_action == "BLOCKED"
        assert assessment.uncertain_side_effects is True
        with pytest.raises(ApplicationRecoveryError):
            await second.continue_run(
                handle.run_id, principal=_principal(), workspace=_workspace(),
            )
        assert reopened_state.get(handle.run_id) == saved
        assert len(executor.plans) == len(governed_invokers["capability"]) == 1
        assert all(stage is not WorkflowStage.EXECUTE
                   for stage, _, _ in governed_invokers["finalize"])
    finally:
        reopened_state.close()
        reopened_ledger.close()
