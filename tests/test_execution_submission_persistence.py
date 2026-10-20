"""Exact completed submission recovery never discovers or re-executes RAW files."""

import json
from pathlib import Path
from uuid import uuid4

import pytest

from labbioagentos import (
    AuthorizationDenied, ExecutionPlanDraft, ExecutionSubmissionService, WorkflowStage,
)
from labbioagentos.execution import ExecutionSubmissionError, JobLedger, JobLedgerState, JobLauncherError
from labbioagentos.execution.errors import ExecutionInputSelectionError
from labbioagentos.execution.models import ExecutionSourceEdit
from test_execution_inspection import inspection, _inspect, _submit  # noqa: F401
from test_runtime_milestone_b import boundary  # noqa: F401
from test_local_cli import local_cli  # noqa: F401


def _persistent(inspection, boundary, ledger):
    _, executor, run_id = inspection
    _, recorder, access, _, _, store, _ = boundary
    service = ExecutionSubmissionService(
        artifact_store=store, access_service=access, executor=executor,
        trace_recorder=recorder,
        job_ledger=ledger,
    )
    return service, executor, run_id


@pytest.mark.asyncio
async def test_restart_restores_exact_receipt_contract_and_source_without_execution(
    inspection, boundary, tmp_path, monkeypatch,
):
    ledger = JobLedger(tmp_path / "submissions.sqlite")
    initial = _persistent(inspection, boundary, ledger)
    source = "# private-original-café\r\nx = 1\r\n"
    receipt = await _submit(initial, boundary, source)
    ledger.close()
    reopened = JobLedger(tmp_path / "submissions.sqlite")
    restored = _persistent(inspection, boundary, reopened)
    monkeypatch.setattr(restored[1], "execute", lambda plan: pytest.fail("restore executed"))
    monkeypatch.setattr(boundary[5], "list_refs", lambda **kw: pytest.fail("RAW discovery"))
    page = _inspect(restored, boundary, receipt.execution_id)
    assert page["receipt"] == receipt.model_dump(mode="json")
    assert page["submitted_program"]["source"] == source
    assert page["submitted_contract"] == ExecutionPlanDraft(
        image_key="approved", script_content=source,
    ).model_dump(mode="json", exclude={"script_content"})
    assert source.encode() not in reopened.path.read_bytes()
    assert len(restored[1].plans) == 1
    reopened.close()


@pytest.mark.asyncio
async def test_restart_revision_is_explicit_new_execution_with_original_contract(
    inspection, boundary, tmp_path,
):
    ledger = JobLedger(tmp_path / "submissions.sqlite")
    initial = _persistent(inspection, boundary, ledger)
    first = await _submit(initial, boundary, "x = 1\n")
    fresh, executor, run_id = _persistent(inspection, boundary, ledger)
    _, _, _, principal, workspace, _, _ = boundary
    second = await fresh.revise(
        first.execution_id, first.script_hash,
        (ExecutionSourceEdit(old="x = 1", new="x = 2"),),
        principal=principal, workspace=workspace, run_id=run_id,
        stage_id=WorkflowStage.EXECUTE, invocation_id=uuid4(),
    )
    assert second.execution_id != first.execution_id
    assert executor.plans[-1].script_content == "x = 2\n"
    assert len(executor.plans) == 2
    assert _inspect(initial, boundary, first.execution_id)["submitted_program"]["source"] == "x = 1\n"
    ledger.close()


@pytest.mark.asyncio
async def test_restart_checkpoint_inputs_require_same_run_registered_output(
    inspection, boundary, tmp_path,
):
    ledger = JobLedger(tmp_path / "submissions.sqlite")
    initial = _persistent(inspection, boundary, ledger)
    first = await _submit(initial, boundary)
    checkpoint = next(iter(first.registered_outputs))
    fresh, executor, run_id = _persistent(inspection, boundary, ledger)
    _, _, _, principal, workspace, _, _ = boundary
    args = dict(principal=principal, workspace=workspace, stage_id=WorkflowStage.EXECUTE,
                invocation_id=uuid4(), mountable_input_artifact_ids=())
    draft = ExecutionPlanDraft(image_key="approved", script_content="pass\n",
                               input_artifact_ids=(checkpoint,))
    await fresh.submit(draft, run_id=run_id, **args)
    with pytest.raises(ExecutionInputSelectionError):
        await fresh.submit(draft, run_id=uuid4(), **args)
    assert len(executor.plans) == 2
    ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["source", "artifact", "run", "ledger"])
async def test_restored_binding_rejects_tampering_and_cross_run(
    inspection, boundary, tmp_path, mutation,
):
    ledger = JobLedger(tmp_path / "submissions.sqlite")
    initial = _persistent(inspection, boundary, ledger)
    receipt = await _submit(initial, boundary)
    fresh = _persistent(inspection, boundary, ledger)
    ref = initial[1].script_refs[receipt.execution_id]
    if mutation == "source":
        Path(ref.storage_locator).write_bytes(b"exit\n")
    elif mutation == "artifact":
        descriptor = boundary[5].root / f"{ref.artifact_id}.json"
        payload = json.loads(descriptor.read_text())
        payload["ref"]["original_filename"] = "changed.py"
        descriptor.write_text(json.dumps(payload))
    elif mutation == "run":
        fresh = (fresh[0], fresh[1], uuid4())
    else:
        ledger._connection.execute(
            "UPDATE execution_submissions SET payload = '{}' WHERE execution_id = ?",
            (str(receipt.execution_id),),
        )
    with pytest.raises((ExecutionSubmissionError, AuthorizationDenied, ValueError)):
        _inspect(fresh, boundary, receipt.execution_id)
    assert len(initial[1].plans) == 1
    ledger.close()


@pytest.mark.asyncio
async def test_failed_submission_persistence_retains_identity_but_never_live_credit(
    inspection, boundary, tmp_path, monkeypatch,
):
    ledger = JobLedger(tmp_path / "submissions.sqlite")
    initial = _persistent(inspection, boundary, ledger)

    def fail_write(**kwargs):
        raise OSError("private disk diagnostic")

    monkeypatch.setattr(ledger, "record_execution_submission", fail_write)
    with pytest.raises(ExecutionSubmissionError) as caught:
        await _submit(initial, boundary)
    execution_id = initial[1].plans[0].execution_id
    assert caught.value.execution_id == execution_id
    assert "private disk diagnostic" not in str(caught.value)
    assert execution_id not in initial[0]._submitted_executions
    for service in (initial, _persistent(inspection, boundary, ledger)):
        with pytest.raises(ExecutionSubmissionError):
            _inspect(service, boundary, execution_id)
    assert len(initial[1].plans) == 1
    ledger.close()


@pytest.mark.asyncio
async def test_collected_execution_without_submission_blocks_restart_not_replayed(
    inspection, boundary, tmp_path, monkeypatch,
):
    ledger = JobLedger(tmp_path / "submissions.sqlite")
    initial = _persistent(inspection, boundary, ledger)
    executor = initial[1]
    original = executor.execute

    def collected(plan):
        ledger.record_intent(run_id=plan.run_id, execution_id=plan.execution_id,
                             launcher="local", workspace_root=tmp_path / str(plan.execution_id))
        ledger.record_submitted(plan.execution_id, "known-process")
        result = original(plan)
        ledger.record_collected(plan.execution_id)
        return result

    monkeypatch.setattr(executor, "execute", collected)
    monkeypatch.setattr(ledger, "record_execution_submission",
                        lambda **kw: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(ExecutionSubmissionError):
        await _submit(initial, boundary)
    execution_id = executor.plans[0].execution_id
    ledger.close()
    restarted = JobLedger(tmp_path / "submissions.sqlite")
    assert restarted.unreceipted_executions(initial[2]) == (execution_id,)
    fresh = _persistent(inspection, boundary, restarted)
    with pytest.raises(ExecutionSubmissionError) as caught:
        await _submit(fresh, boundary)
    assert caught.value.execution_id == execution_id
    assert len(executor.plans) == 1
    restarted.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["ledger_write", "unknown_wait"])
async def test_real_tool_chain_stops_on_submission_write_failure(
    inspection, boundary, tmp_path, monkeypatch, failure,
):
    from dataclasses import replace
    from native_test_provider import LocalProvider
    from labbioagentos import LabBioRuntimeToolSet
    from test_runtime_milestone_b import _toolset

    ledger = JobLedger(tmp_path / "submissions.sqlite")
    service, executor, run_id = _persistent(inspection, boundary, ledger)
    tools = _toolset(boundary, WorkflowStage.EXECUTE, ("execution_submit",),
                     execution_submission=service)
    tools = LabBioRuntimeToolSet(replace(tools.binding, run_id=run_id), tools.services)
    provider = LocalProvider(tools)
    await provider.initialize()
    if failure == "ledger_write":
        monkeypatch.setattr(ledger, "record_execution_submission",
                            lambda **kw: (_ for _ in ()).throw(OSError("private-disk-canary")))
    else:
        original = executor.execute

        def unknown_wait(plan):
            ledger.record_intent(run_id=plan.run_id, execution_id=plan.execution_id,
                                 launcher="local", workspace_root=tmp_path / str(plan.execution_id))
            ledger.record_submitted(plan.execution_id, "unknown-process")
            original(plan)
            raise JobLauncherError("private-disk-canary")

        monkeypatch.setattr(executor, "execute", unknown_wait)
    arguments = {"image_key": "approved", "script_content": "pass\n"}
    first = await provider.call_tool("execution_submit", arguments)
    assert first["success"] is False
    assert first["error"]["error_code"] == "EXECUTION_SUBMISSION_PERSISTENCE_FAILED"
    assert first["error"]["retryable"] is False
    assert first["data"] == {"execution_id": str(executor.plans[0].execution_id),
                             "replay_blocked": True}
    assert tools.stop_reason == "EXECUTION_SUBMISSION_PERSISTENCE_FAILED"
    assert "private-disk-canary" not in json.dumps(first)
    second = await provider.call_tool("execution_submit", arguments)
    assert second["success"] is False
    assert len(executor.plans) == 1
    ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [
    JobLedgerState.SUBMITTING, JobLedgerState.SUBMITTED, JobLedgerState.COLLECTED,
    JobLedgerState.UNCONFIRMED, JobLedgerState.LOST, JobLedgerState.NOT_SUBMITTED,
])
async def test_only_unconfirmed_same_run_jobs_block_new_execution(
    inspection, boundary, tmp_path, state,
):
    ledger = JobLedger(tmp_path / "submissions.sqlite")
    service, executor, run_id = _persistent(inspection, boundary, ledger)
    execution_id = uuid4()
    ledger.record_intent(run_id=run_id, execution_id=execution_id,
                         launcher="local", workspace_root=tmp_path / str(execution_id))
    if state in {JobLedgerState.SUBMITTED, JobLedgerState.COLLECTED}:
        ledger.record_submitted(execution_id, "job")
    if state is JobLedgerState.COLLECTED:
        ledger.record_collected(execution_id)
    elif state is JobLedgerState.NOT_SUBMITTED:
        ledger.record_not_submitted(execution_id)
    elif state in {JobLedgerState.UNCONFIRMED, JobLedgerState.LOST}:
        ledger.resolve(execution_id, state)
    if state is JobLedgerState.NOT_SUBMITTED:
        await _submit((service, executor, run_id), boundary)
    else:
        with pytest.raises(ExecutionSubmissionError) as caught:
            await _submit((service, executor, run_id), boundary)
        assert caught.value.execution_id == execution_id
        assert not executor.plans
    await _submit((service, executor, uuid4()), boundary)
    assert len(executor.plans) == (2 if state is JobLedgerState.NOT_SUBMITTED else 1)
    ledger.close()


def test_docker_local_application_wires_same_durable_ledger(local_cli):
    from labbioagentos.cli import _close
    from labbioagentos.local_config import build_application

    settings, _, directory, _ = local_cli
    application = build_application(settings, directory, load_provider=False)
    try:
        ledger = application.configuration.job_ledger
        assert ledger is not None
        assert ledger.path == directory / "jobs.sqlite"
        assert application.execution_submission.job_ledger is ledger
        assert application.container_executor.job_ledger is ledger
    finally:
        _close(application)


@pytest.mark.asyncio
async def test_persistence_failure_is_scoped_to_its_run(
    inspection, boundary, tmp_path, monkeypatch,
):
    ledger = JobLedger(tmp_path / "submissions.sqlite")
    initial = _persistent(inspection, boundary, ledger)
    original = ledger.record_execution_submission
    monkeypatch.setattr(ledger, "record_execution_submission",
                        lambda **kw: (_ for _ in ()).throw(OSError("disk")))
    with pytest.raises(ExecutionSubmissionError):
        await _submit(initial, boundary)
    monkeypatch.setattr(ledger, "record_execution_submission", original)
    with pytest.raises(ExecutionSubmissionError):
        await _submit(initial, boundary)
    other = (initial[0], initial[1], uuid4())
    receipt = await _submit(other, boundary)
    assert _inspect(other, boundary, receipt.execution_id)["receipt"] is not None
    assert len(initial[1].plans) == 2
    ledger.close()


@pytest.mark.asyncio
async def test_invalid_return_after_effect_blocks_resubmission(
    inspection, boundary, tmp_path, monkeypatch,
):
    ledger = JobLedger(tmp_path / "submissions.sqlite")
    initial = _persistent(inspection, boundary, ledger)
    original = initial[1].execute
    monkeypatch.setattr(initial[1], "execute", lambda plan: original(plan).model_copy(
        update={"run_id": uuid4()}))
    with pytest.raises(ExecutionSubmissionError) as caught:
        await _submit(initial, boundary)
    assert caught.value.execution_id == initial[1].plans[0].execution_id
    with pytest.raises(ExecutionSubmissionError):
        await _submit(initial, boundary)
    assert len(initial[1].plans) == 1
    ledger.close()


@pytest.mark.asyncio
async def test_committed_binding_survives_failed_receipt_handoff_without_replay(
    inspection, boundary, tmp_path, monkeypatch,
):
    ledger = JobLedger(tmp_path / "submissions.sqlite")
    initial = _persistent(inspection, boundary, ledger)
    recorder = boundary[1]
    original = recorder.emit

    def fail_completion(run_id, event_type, **kwargs):
        if event_type.value == "EXECUTION_COMPLETED":
            raise OSError("private-audit-canary")
        return original(run_id, event_type, **kwargs)

    monkeypatch.setattr(recorder, "emit", fail_completion)
    with pytest.raises(ExecutionSubmissionError) as caught:
        await _submit(initial, boundary)
    execution_id = initial[1].plans[0].execution_id
    assert caught.value.execution_id == execution_id
    assert ledger.execution_submission(initial[2], execution_id) is not None
    assert _inspect(initial, boundary, execution_id)["receipt"] is not None
    fresh = _persistent(inspection, boundary, ledger)
    assert _inspect(fresh, boundary, execution_id)["receipt"] is not None
    with pytest.raises(ExecutionSubmissionError):
        await _submit(initial, boundary)
    assert len(initial[1].plans) == 1
    ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["selected", "unselected"])
async def test_checkpoint_scope_does_not_require_unselected_old_outputs(
    inspection, boundary, tmp_path, monkeypatch, missing,
):
    from labbioagentos import ArtifactExposureClass, ArtifactRepresentation
    from labbioagentos.artifacts import ArtifactNotFoundError

    ledger = JobLedger(tmp_path / "submissions.sqlite")
    initial = _persistent(inspection, boundary, ledger)
    executor, store = initial[1], boundary[5]
    original = executor.execute

    def two_outputs(plan):
        result = original(plan)
        other = store.register(
            artifact_type="unused-output", exposure_class=ArtifactExposureClass.RAW,
            representation=ArtifactRepresentation(), owner_user_id=plan.owner_user_id,
            project_id=plan.project_id, lab_id=plan.lab_id, run_id=plan.run_id,
            stage_id=plan.stage_id, producer_invocation_id=plan.invocation_id,
        )
        return result.model_copy(update={"output_artifact_refs": (*result.output_artifact_refs, other)})

    monkeypatch.setattr(executor, "execute", two_outputs)
    receipt = await _submit(initial, boundary)
    selected, unselected = tuple(receipt.registered_outputs)
    removed = selected if missing == "selected" else unselected
    descriptor = store.root / f"{removed}.json"
    descriptor.rename(descriptor.with_suffix(".removed"))
    fresh, _, run_id = _persistent(inspection, boundary, ledger)
    draft = ExecutionPlanDraft(image_key="approved", script_content="pass\n",
                               input_artifact_ids=(selected,))
    kwargs = dict(principal=boundary[3], workspace=boundary[4], run_id=run_id,
                  stage_id=WorkflowStage.EXECUTE, invocation_id=uuid4(),
                  mountable_input_artifact_ids=())
    if missing == "selected":
        with pytest.raises(ArtifactNotFoundError):
            await fresh.submit(draft, **kwargs)
        assert len(executor.plans) == 1
    else:
        await fresh.submit(draft, **kwargs)
        assert len(executor.plans) == 2
    ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["selected", "unselected"])
@pytest.mark.parametrize("restart", [False, True])
async def test_checkpoint_verifies_only_selected_producer_source(
    inspection, boundary, tmp_path, missing, restart,
):
    ledger = JobLedger(tmp_path / "submissions.sqlite")
    initial = _persistent(inspection, boundary, ledger)
    prior = await _submit(initial, boundary, "# prior\npass\n")
    chosen = await _submit(initial, boundary, "# chosen\npass\n")
    removed = chosen if missing == "selected" else prior
    path = Path(initial[1].script_refs[removed.execution_id].storage_locator)
    path.rename(path.with_suffix(".removed"))
    active = _persistent(inspection, boundary, ledger) if restart else initial
    service, executor, run_id = active
    draft = ExecutionPlanDraft(image_key="approved", script_content="pass\n",
                               input_artifact_ids=(next(iter(chosen.registered_outputs)),))
    kwargs = dict(principal=boundary[3], workspace=boundary[4], run_id=run_id,
                  stage_id=WorkflowStage.EXECUTE, invocation_id=uuid4(),
                  mountable_input_artifact_ids=())
    if missing == "selected":
        with pytest.raises(ExecutionSubmissionError):
            await service.submit(draft, **kwargs)
        assert len(executor.plans) == 2
    else:
        await service.submit(draft, **kwargs)
        assert len(executor.plans) == 3
    ledger.close()
