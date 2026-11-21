"""Queries may establish acceptance, never fabricate an execution outcome."""

import json

import pytest

from labbioagentos import ExecutionPlan, WorkflowStage
from labbioagentos.execution.apptainer import SifStore, SifStoreError
from labbioagentos.execution.job_ledger import JobLedger, JobLedgerState
from labbioagentos.execution.launchers.base import JobHandle, JobLauncherError, JobState
from execution_reconciliation import execution_binding, reconcile_execution
from round_trip import RUN_ID, identity, native_record
from runtime_binding import probe_settings


@pytest.fixture
def bound(tmp_path):
    source = tmp_path / "synthetic-file-not-an-executable-sif"
    source.write_bytes(b"binding-only-unit-fixture")
    reference = SifStore(tmp_path / "sif").register(source)
    prefix = tmp_path / "installation"
    binary = prefix / "bin/apptainer"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"binding-only-not-executed")
    (prefix / "installation-receipt.json").write_text(json.dumps({
        "prefix": str(prefix), "status": "INSTALLED", "setuid": False,
    }))
    raw = probe_settings(tmp_path).model_dump(mode="json", by_alias=True)
    raw["execution"]["image_reference"] = reference
    raw["execution"]["apptainer"].update(binary=str(binary), sif_store=str(tmp_path / "sif"))
    settings = type(probe_settings(tmp_path)).model_validate(raw)
    plan = ExecutionPlan(run_id=RUN_ID, execution_id=identity("reconcile-unit"),
                         stage_id=WorkflowStage.EXECUTE, image_key=settings.execution.image_key,
                         script_content="# infrastructure-only fixture\npass\n",
                         owner_user_id="r2-user", project_id="r2", lab_id="r2-lab")
    native_record(str(tmp_path), "run-binding", execution_binding(settings, plan))
    ledger = JobLedger(tmp_path / "native-jobs.sqlite")
    ledger.record_intent(run_id=RUN_ID, execution_id=plan.execution_id, launcher="fixture",
                         workspace_root=tmp_path / "executions" / str(plan.execution_id))
    try:
        yield tmp_path, settings, plan, ledger
    finally:
        ledger.close()


class Launcher:
    kind = "fixture"

    def __init__(self, state):
        self.state = state
        self.lookups = 0

    async def find(self, execution_id):
        self.lookups += 1
        if isinstance(self.state, Exception):
            raise self.state
        return None if self.state is None else JobHandle(self.kind, "accepted-job", execution_id)

    async def status(self, _):
        return self.state

    async def submit(self, _):
        pytest.fail("Reconciliation must never submit")

    async def collect(self, _):
        pytest.fail("A reconnected launcher has no original result receipt")


@pytest.mark.asyncio
@pytest.mark.parametrize("state,expected,reason", [
    (JobState.RUNNING, "WAITING", "EXECUTION_ACCEPTED"),
    (JobState.PENDING, "WAITING", "EXECUTION_ACCEPTED"),
    (JobState.COMPLETED, "BLOCKED", "TERMINAL_WITHOUT_NATIVE_RECEIPT"),
    (JobState.FAILED, "BLOCKED", "TERMINAL_WITHOUT_NATIVE_RECEIPT"),
    (JobState.UNKNOWN, "BLOCKED", "ACCEPTANCE_UNKNOWN"),
    (None, "BLOCKED", "ACCEPTANCE_UNKNOWN"),
    (JobLauncherError("unavailable"), "BLOCKED", "LAUNCHER_UNCONFIRMED"),
])
async def test_query_preserves_acceptance_and_uncertainty_without_replay(bound, state, expected, reason):
    root, settings, plan, ledger = bound
    result = await reconcile_execution(root, Launcher(state), plan, settings)
    assert result["status"] == expected and result["reason"] == reason
    assert result["replay_allowed"] is result["scientific_result_available"] is False
    assert ledger.get(plan.execution_id).state is JobLedgerState.SUBMITTING
    assert ledger.execution_submission(RUN_ID, plan.execution_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["request", "scope", "version", "job", "receipt"])
async def test_binding_mismatch_blocks_before_query_or_result_promotion(bound, mutation):
    root, settings, plan, ledger = bound
    owner = Launcher(JobState.RUNNING)
    if mutation == "request":
        plan = plan.model_copy(update={"script_content": "different\n"})
    elif mutation == "scope":
        plan = plan.model_copy(update={"run_id": identity("wrong-run")})
    elif mutation == "version":
        settings = settings.model_copy(update={"provider": settings.provider.model_copy(
            update={"model_identifier": "changed"})})
    elif mutation == "job":
        ledger.record_submitted(plan.execution_id, "different-job")
    else:
        ledger.record_execution_submission(run_id=RUN_ID, execution_id=plan.execution_id, payload='{}')
        result = await reconcile_execution(root, owner, plan, settings)
        assert result["status"] == "BLOCKED"
        assert result["reason"] == "NATIVE_RECEIPT_REQUIRES_VALIDATION"
        assert owner.lookups == 0
        return
    with pytest.raises(ValueError):
        await reconcile_execution(root, owner, plan, settings)
    assert owner.lookups == (1 if mutation == "job" else 0)


@pytest.mark.asyncio
async def test_missing_native_ledger_is_not_recreated_by_reconciliation(bound):
    root, settings, plan, ledger = bound
    ledger.path.rename(ledger.path.with_suffix(".preserved"))
    with pytest.raises(ValueError, match="ledger is missing"):
        await reconcile_execution(root, Launcher(JobState.RUNNING), plan, settings)
    assert not ledger.path.exists()


@pytest.mark.asyncio
async def test_terminal_native_state_cannot_be_overridden_by_a_running_unit(bound):
    root, settings, plan, ledger = bound
    ledger.record_not_submitted(plan.execution_id)
    result = await reconcile_execution(root, Launcher(JobState.RUNNING), plan, settings)
    assert result["status"] == "BLOCKED" and result["reason"] == "NATIVE_LAUNCHER_STATE_CONFLICT"


@pytest.mark.asyncio
async def test_missing_sif_store_is_not_created_by_a_query(bound):
    root, settings, plan, ledger = bound
    directory = settings.execution.apptainer.sif_store
    directory.rename(directory.with_suffix(".preserved"))
    with pytest.raises((SifStoreError, ValueError)):
        await reconcile_execution(root, Launcher(JobState.RUNNING), plan, settings)
    assert not directory.exists()
