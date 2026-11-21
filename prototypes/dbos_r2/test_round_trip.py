"""Independent DBOS feasibility tests; intentionally outside the production suite."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import threading

from dbos import DBOS, SetWorkflowID
import pytest

from labbioagentos.execution.job_ledger import JobLedger
from labbioagentos.local_continuation import run_writer
from round_trip import RUN_ID, identity, native_record


PROBE = Path(__file__).with_name("round_trip.py")


def run_probe(root, *, fault=None, extra=()):
    env = dict(os.environ, PYTHONPATH=str(Path.cwd() / "src"))
    env.pop("R2_CRASH_POINT", None)
    if fault:
        env["R2_CRASH_POINT"] = fault
    return subprocess.run([sys.executable, str(PROBE), str(root), *extra],
                          capture_output=True, text=True, env=env, timeout=30)


def acceptances(root, name):
    path = root / f"fake-{name}-acceptances"
    return 0 if not path.exists() else len(path.read_text().splitlines())


def test_success_and_cached_replay_preserve_native_receipts(tmp_path):
    first, replay = run_probe(tmp_path), run_probe(tmp_path)
    assert first.returncode == replay.returncode == 0, first.stderr + replay.stderr
    assert first.stdout == replay.stdout
    assert acceptances(tmp_path, "provider") == acceptances(tmp_path, "tool") == 1


@pytest.mark.parametrize("fault", ["provider-native-before-dbos", "tool-native-before-dbos"])
def test_native_completion_before_scheduler_checkpoint_is_not_repeated(tmp_path, fault):
    assert run_probe(tmp_path, fault=fault).returncode == 75
    recovered = run_probe(tmp_path)
    assert recovered.returncode == 0, recovered.stderr
    assert acceptances(tmp_path, "provider") == acceptances(tmp_path, "tool") == 1


def test_provider_result_lost_is_unknown_not_a_free_new_decision(tmp_path):
    assert run_probe(tmp_path, fault="provider-return-before-native").returncode == 75
    recovered = run_probe(tmp_path)
    assert recovered.returncode != 0 and "PROVIDER_OUTCOME_UNKNOWN" in recovered.stderr
    assert native_record(str(tmp_path), "provider-intent")["usage"] is None
    assert acceptances(tmp_path, "provider") == 1
    assert acceptances(tmp_path, "tool") == 0


@pytest.mark.parametrize("fault,accepted", [("tool-intent-before-submit", 0),
                                           ("tool-accepted-before-receipt", 1)])
def test_open_tool_intent_never_means_safe_resubmission(tmp_path, fault, accepted):
    assert run_probe(tmp_path, fault=fault).returncode == 75
    recovered = run_probe(tmp_path)
    assert recovered.returncode != 0 and "TOOL_OUTCOME_UNKNOWN" in recovered.stderr
    assert acceptances(tmp_path, "tool") == accepted


def test_scheduler_success_cannot_mask_a_missing_native_observation(tmp_path):
    assert run_probe(tmp_path).returncode == 0
    path = tmp_path / "observations" / f"{identity('observation')}.json"
    path.rename(path.with_suffix(".preserved"))
    replay = run_probe(tmp_path)
    assert replay.returncode != 0 and "ObservationNotAvailableError" in replay.stderr
    assert acceptances(tmp_path, "tool") == 1


@pytest.mark.parametrize("extra", [("--request", "changed"), ("--version", "r2-different")])
def test_existing_identity_rejects_a_changed_request_or_runtime_version(tmp_path, extra):
    assert run_probe(tmp_path).returncode == 0
    replay = run_probe(tmp_path, extra=extra)
    assert replay.returncode != 0 and "different input or version" in replay.stderr
    assert acceptances(tmp_path, "provider") == acceptances(tmp_path, "tool") == 1


def test_live_native_writer_blocks_new_dbos_process_before_dispatch(tmp_path):
    with run_writer(tmp_path):
        refused = run_probe(tmp_path)
    assert refused.returncode != 0 and "RunWriterActiveError" in refused.stderr
    assert acceptances(tmp_path, "provider") == 0
    assert run_probe(tmp_path).returncode == 0


def test_dbos_cancellation_alone_does_not_revoke_native_sync_writer(tmp_path):
    """A passing counterexample: direct framework cancellation is insufficient.

    No wrapper is added to conceal this result. The safe local prototype requires
    full controller death before lease handover; live takeover stays unaccepted.
    """
    entered, release, committed = threading.Event(), threading.Event(), threading.Event()

    @DBOS.step(name="r2-blocking-native-write", retries_allowed=False)
    def blocked_write():
        entered.set()
        assert release.wait(5)
        ledger = JobLedger(tmp_path / "late-jobs.sqlite")
        try:
            ledger.record_execution_submission(run_id=RUN_ID, execution_id=identity("late"),
                                               payload='{"late":true}')
        finally:
            ledger.close()
        committed.set()

    @DBOS.step(name="r2-check-cancellation", retries_allowed=False)
    def next_step():
        raise AssertionError("Cancelled workflow must not start another step")

    @DBOS.workflow(name="r2-cancellation-counterexample")
    def cancellation_workflow():
        blocked_write()
        next_step()

    DBOS(config={"name": "r2-cancel", "application_version": "r2-cancel-1",
                 "system_database_url": f"sqlite:///{tmp_path / 'cancel.sqlite'}",
                 "enable_otlp": False, "log_level": "ERROR"})
    DBOS.launch()
    try:
        with SetWorkflowID("r2-cancel-proof"):
            handle = DBOS.start_workflow(cancellation_workflow)
        assert entered.wait(5)
        DBOS.cancel_workflow(handle.get_workflow_id())
        release.set()
        assert committed.wait(5), "Counterexample changed: review upstream cancellation behavior"
        ledger = JobLedger(tmp_path / "late-jobs.sqlite")
        try:
            assert ledger.execution_submission(RUN_ID, identity("late")) == '{"late":true}'
        finally:
            ledger.close()
    finally:
        release.set()
        DBOS.destroy(workflow_completion_timeout_sec=5)
