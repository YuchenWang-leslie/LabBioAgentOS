"""Authoritative phase checkpoints never replay uncertain external operations."""

from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from labbioagentos import (
    ApplicationRecoveryError, ApplicationRecoveryIssueCode, LabBioApplication,
    ApplicationRunRecord, AuthorizationDenied,
    NextAction, NextActionProposal, PerInvocationPantheonStageInvoker,
    RunStatus, RuntimeProfileCatalog, SQLiteRunStateStore, WorkflowStage,
)
from labbioagentos.runtime import CapabilityEvidenceBundle
from labbioagentos.runtime.pantheon import (
    PantheonCapabilityStageInvoker, PantheonTypedStageInvoker,
)
from test_c10_durable_control_plane import (
    _configuration, _next_result, _principal, _request, _SimulatedProcessLoss,
    _workspace,
)
from test_runtime_milestone_b import MockExecutor


def _capability_configuration(tmp_path, store, *, observer=None):
    config = _configuration(tmp_path, store)
    catalog = config.profile_catalog
    capabilities = ("artifact_query", "execution_submit")
    return replace(config, boundary_observer=observer, profile_catalog=RuntimeProfileCatalog(
        agents=tuple(catalog.agents.values()), prompts=tuple(catalog.prompts.values()),
        models=tuple(catalog.models.values()), schemas=tuple(catalog.schemas.values()),
        capabilities=tuple(item.model_copy(update={"capability_allowlist": capabilities})
                           for item in catalog.capabilities.values()),
    ), stage_assemblies=tuple(
        replace(item, capability_phase_enabled=True, capability_allowlist=capabilities,
                required_capabilities=("execution_submit",))
        if item.stage_id is WorkflowStage.EXECUTE else item
        for item in config.stage_assemblies
    ))


@pytest.fixture
def governed_invokers(monkeypatch):
    calls = {"capability": [], "finalize": []}

    async def capability(self, stage_input):
        calls["capability"].append(stage_input.invocation_id)
        tools = self.evidence_sources[0]
        await tools.artifact_query(artifact_id="malformed-identifier", view_type="SUMMARY")
        result = await tools.execution_submit(
            image_key="approved", script_content="print('PRIVATE_PROGRAM_MARKER')",
        )
        assert result["success"] is True
        return CapabilityEvidenceBundle(
            run_id=stage_input.run_id, stage_id=stage_input.stage_id,
            invocation_id=stage_input.invocation_id, items=tools.evidence_items(),
        )

    async def finalize(self, stage_input, *, capability_evidence=None):
        calls["finalize"].append((stage_input.stage_id, stage_input.invocation_id, capability_evidence))
        return _next_result(stage_input.stage_id)

    monkeypatch.setattr(PantheonCapabilityStageInvoker, "invoke", capability)
    monkeypatch.setattr(PantheonTypedStageInvoker, "invoke", finalize)
    return calls


async def _interrupt_after_tools(tmp_path, governed_invokers):
    def stop(kind, value):
        if kind == "capability_evidence":
            raise _SimulatedProcessLoss

    path = tmp_path / "runs.sqlite"
    store = SQLiteRunStateStore(path)
    first = LabBioApplication(_capability_configuration(tmp_path, store, observer=stop))
    executor = MockExecutor(first.artifact_store)
    first.execution_submission.executor = executor
    handle = first.create_run(_request())
    with pytest.raises(_SimulatedProcessLoss):
        await first.run(handle)
    record = store.get(handle.run_id)
    assert len(executor.plans) == 1
    assert record.inflight_evidence is not None
    assert "PRIVATE_PROGRAM_MARKER" not in record.model_dump_json()
    store.close()
    reopened = SQLiteRunStateStore(path)
    second = LabBioApplication(_capability_configuration(tmp_path, reopened))
    second.execution_submission.executor = executor
    return second, reopened, handle, executor


@pytest.mark.asyncio
async def test_completed_tools_reopen_finalizes_only_with_failed_evidence_preserved(tmp_path, governed_invokers):
    app, store, handle, executor = await _interrupt_after_tools(tmp_path, governed_invokers)
    saved = store.get(handle.run_id)
    status = app.reconcile_run(handle.run_id, principal=_principal(), workspace=_workspace())
    assert status.continuation_action == "FINALIZE_ONLY"
    assert status.uncertain_side_effects is False
    assert [item.status.value for item in status.confirmed_calls] == ["FAILED", "COMPLETED"]
    assert status.confirmed_calls[0].error_code is not None
    result = await app.continue_run(handle.run_id, principal=_principal(), workspace=_workspace())
    assert result.status is RunStatus.COMPLETED
    assert len(executor.plans) == len(governed_invokers["capability"]) == 1
    finalized = [item for item in governed_invokers["finalize"] if item[0] is WorkflowStage.EXECUTE]
    assert len(finalized) == 1
    assert finalized[0][1] == saved.inflight_invocation_id
    assert finalized[0][2] == saved.inflight_evidence
    assert store.get(handle.run_id).inflight_evidence is None
    store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["missing_artifact", "missing_evidence", "required_capability", "illegal_result"])
async def test_checkpoint_recovery_fails_closed_without_replay(tmp_path, governed_invokers, fault):
    app, store, handle, executor = await _interrupt_after_tools(tmp_path, governed_invokers)
    record = store.get(handle.run_id)
    if fault == "missing_artifact":
        artifact_id = record.inflight_evidence.items[-1].safe_result["output_artifact_ids"][0]
        (app.artifact_store.root / f"{artifact_id}.json").unlink()
    elif fault == "required_capability":
        config = replace(app.configuration, stage_assemblies=tuple(
            replace(item, required_capabilities=("execution_submit", "artifact_query"))
            if item.stage_id is WorkflowStage.EXECUTE else item
            for item in app.configuration.stage_assemblies
        ))
        app = LabBioApplication(config)
    else:
        update = {"inflight_evidence": None} if fault == "missing_evidence" else {
            "inflight_result": _next_result(WorkflowStage.EXECUTE).model_copy(update={
                "next_action": NextActionProposal(action=NextAction.FINISH),
            }),
        }
        store.update(record.model_copy(update=update), expected_version=record.record_version)
    before = store.get(handle.run_id).model_dump_json()
    status = app.reconcile_run(handle.run_id, principal=_principal(), workspace=_workspace())
    assert status.continuation_action == "BLOCKED"
    expected = {
        "missing_artifact": ApplicationRecoveryIssueCode.REQUIRED_ARTIFACT_MISSING,
        "missing_evidence": ApplicationRecoveryIssueCode.STAGE_IN_FLIGHT,
    }.get(fault, ApplicationRecoveryIssueCode.CHECKPOINT_INVALID)
    assert status.recovery.issue_code is expected
    with pytest.raises(ApplicationRecoveryError):
        await app.continue_run(handle.run_id, principal=_principal(), workspace=_workspace())
    assert store.get(handle.run_id).model_dump_json() == before
    assert len(executor.plans) == len(governed_invokers["capability"]) == 1
    store.close()


@pytest.mark.asyncio
async def test_repeated_finalization_interruptions_keep_original_capability_checkpoint(
    tmp_path, governed_invokers, monkeypatch,
):
    app, store, handle, executor = await _interrupt_after_tools(tmp_path, governed_invokers)
    before = store.get(handle.run_id)
    original = PantheonTypedStageInvoker.invoke

    async def disconnect(self, stage_input, *, capability_evidence=None):
        if stage_input.stage_id is WorkflowStage.EXECUTE:
            assert capability_evidence == before.inflight_evidence
            raise _SimulatedProcessLoss
        return await original(self, stage_input, capability_evidence=capability_evidence)

    monkeypatch.setattr(PantheonTypedStageInvoker, "invoke", disconnect)
    with pytest.raises(_SimulatedProcessLoss):
        await app.continue_run(handle.run_id, principal=_principal(), workspace=_workspace())
    assert store.get(handle.run_id).inflight_evidence == before.inflight_evidence
    store.close()
    monkeypatch.setattr(PantheonTypedStageInvoker, "invoke", original)
    store = SQLiteRunStateStore(tmp_path / "runs.sqlite")
    restarted = LabBioApplication(_capability_configuration(tmp_path, store))
    result = await restarted.continue_run(handle.run_id, principal=_principal(), workspace=_workspace())
    assert result.status is RunStatus.COMPLETED
    assert len(executor.plans) == len(governed_invokers["capability"]) == 1
    store.close()


@pytest.mark.asyncio
async def test_checkpoint_scope_and_invocation_binding_survive_reopen(tmp_path, governed_invokers):
    app, store, handle, executor = await _interrupt_after_tools(tmp_path, governed_invokers)
    with pytest.raises(AuthorizationDenied):
        app.reconcile_run(handle.run_id, principal=_principal().model_copy(update={"user_id": "another"}),
                          workspace=_workspace())
    record = store.get(handle.run_id)
    for name, replacement in (
        ("inflight_input", record.inflight_input.model_copy(update={"invocation_id": uuid4()})),
        ("inflight_evidence", record.inflight_evidence.model_copy(update={"run_id": uuid4()})),
    ):
        with pytest.raises(ValidationError):
            store.update(record.model_copy(update={name: replacement}), expected_version=record.record_version)
    assert store.get(handle.run_id) == record
    assert len(executor.plans) == 1
    store.close()


def test_pre_checkpoint_records_load_without_invented_history():
    from test_c10_durable_control_plane import _record
    old = _record().model_dump(mode="json")
    for name in ("inflight_input", "inflight_evidence", "inflight_result"):
        old.pop(name)
    import json
    recovered = ApplicationRunRecord.model_validate_json(json.dumps(old))
    assert recovered.inflight_input is recovered.inflight_evidence is recovered.inflight_result is None


@pytest.mark.asyncio
async def test_returned_stage_result_survives_crash_and_is_not_reinvoked(tmp_path, monkeypatch):
    calls = []

    async def invoke(self, stage_input):
        self.boundary_observer("stage_input", stage_input)
        calls.append(stage_input.stage_id)
        result = _next_result(stage_input.stage_id)
        self.boundary_observer("stage_result", result)
        return result

    def stop(kind, value):
        if kind == "stage_result" and value.stage_id is WorkflowStage.EXECUTE:
            raise _SimulatedProcessLoss

    monkeypatch.setattr(PerInvocationPantheonStageInvoker, "invoke", invoke)
    path = tmp_path / "runs.sqlite"
    store = SQLiteRunStateStore(path)
    first = LabBioApplication(replace(_configuration(tmp_path, store), boundary_observer=stop))
    handle = first.create_run(_request())
    with pytest.raises(_SimulatedProcessLoss):
        await first.run(handle)
    store.close()

    store = SQLiteRunStateStore(path)
    second = LabBioApplication(_configuration(tmp_path, store))
    before = store.get(handle.run_id).model_dump_json()
    status = second.reconcile_run(handle.run_id, principal=_principal(), workspace=_workspace())
    assert status.continuation_action == "APPLY_RESULT"
    assert store.get(handle.run_id).model_dump_json() == before
    result = await second.continue_run(handle.run_id, principal=_principal(), workspace=_workspace())
    assert result.status is RunStatus.COMPLETED
    assert calls.count(WorkflowStage.EXECUTE) == 1
    store.close()


@pytest.mark.asyncio
async def test_mid_capability_has_unknown_effect_and_is_never_replayed(tmp_path, monkeypatch):
    calls = []

    async def invoke(self, stage_input):
        self.boundary_observer("stage_input", stage_input)
        calls.append(stage_input.stage_id)
        raise _SimulatedProcessLoss

    monkeypatch.setattr(PerInvocationPantheonStageInvoker, "invoke", invoke)
    store = SQLiteRunStateStore(tmp_path / "runs.sqlite")
    config = _configuration(tmp_path, store)
    config = replace(config, stage_assemblies=tuple(
        replace(item, capability_phase_enabled=True) if item.stage_id is WorkflowStage.INTAKE else item
        for item in config.stage_assemblies
    ))
    first = LabBioApplication(config)
    handle = first.create_run(_request())
    with pytest.raises(_SimulatedProcessLoss):
        await first.run(handle)
    second = LabBioApplication(config)
    status = second.reconcile_run(handle.run_id, principal=_principal(), workspace=_workspace())
    assert status.continuation_action == "BLOCKED"
    assert status.uncertain_side_effects is True
    with pytest.raises(ApplicationRecoveryError):
        await second.continue_run(handle.run_id, principal=_principal(), workspace=_workspace())
    assert calls == [WorkflowStage.INTAKE]
    store.close()
