"""Opt-in tests of the existing local resource owner around a DBOS worker.

LABBIO_RUN_LOCAL_SYSTEMD=1 python -m pytest prototypes/dbos_r2/test_controlled_round.py
"""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import controlled_round
from controlled_round import command, worker, worker_id
from labbioagentos.execution.job_ledger import JobLedger
from labbioagentos.execution.launchers.base import JobLauncherError
from labbioagentos.local_continuation import RunWriterActiveError, run_writer
from round_trip import RUN_ID, identity, native_record
from test_round_trip import acceptances


HERE = Path(__file__).parent
LOCAL = pytest.mark.skipif(os.environ.get("LABBIO_RUN_LOCAL_SYSTEMD") != "1",
                          reason="Explicit local systemd process-boundary acceptance")


def test_worker_identity_is_the_same_for_lexical_directory_aliases(tmp_path):
    root = tmp_path / "run"
    root.mkdir()
    (tmp_path / "alias").mkdir()
    assert worker_id(root) == worker_id(tmp_path / "alias" / ".." / "run")


def run_controlled(root, *, fault=None, extra=()):
    return subprocess.run(
        (sys.executable, str(HERE / "controlled_round.py"), str(root),
         *(("--fault", fault) if fault else ()), *extra),
        env={**os.environ, "PYTHONPATH": str(Path.cwd() / "src")},
        capture_output=True, text=True, timeout=60,
    )


async def ready(path):
    for _ in range(300):
        if path.exists():
            return
        await asyncio.sleep(0.05)
    pytest.fail(f"Worker did not reach {path.name}")


def assert_gone(pids):
    for pid in pids:
        path = Path(f"/proc/{pid}/stat")
        assert not path.exists() or path.read_text().split(") ", 1)[1].startswith("Z")


def assert_no_old_receipt(root):
    ledger = JobLedger(root / "late.sqlite")
    try:
        for name in ("sync-old", "detached-old"):
            assert ledger.execution_submission(RUN_ID, identity(name)) is None
    finally:
        ledger.close()
    assert not (root / "unexpected-next-step").exists()


@LOCAL
def test_managed_success_and_cached_replay(tmp_path):
    first, replay = run_controlled(tmp_path), run_controlled(tmp_path)
    assert first.returncode == replay.returncode == 0, first.stderr + replay.stderr
    assert first.stdout == replay.stdout
    assert acceptances(tmp_path, "provider") == acceptances(tmp_path, "tool") == 1


@LOCAL
@pytest.mark.parametrize("extra", [("--request", "changed"), ("--version", "changed")])
def test_managed_changed_binding_refused_before_dispatch(tmp_path, extra):
    assert run_controlled(tmp_path).returncode == 0
    replay = run_controlled(tmp_path, extra=extra)
    assert replay.returncode != 0 and "different input or version" in replay.stderr
    assert acceptances(tmp_path, "provider") == acceptances(tmp_path, "tool") == 1


@LOCAL
@pytest.mark.parametrize("damage", ["missing", "tampered"])
def test_managed_scheduler_success_does_not_replace_native_evidence(tmp_path, damage):
    assert run_controlled(tmp_path).returncode == 0
    path = tmp_path / "observations" / f"{identity('observation')}.json"
    if damage == "missing":
        path.rename(path.with_suffix(".preserved"))
    else:
        envelope = json.loads(path.read_text())
        envelope["observation"]["public_response"]["success"] = False
        path.chmod(0o600)
        path.write_text(json.dumps(envelope))
    replay = run_controlled(tmp_path)
    expected = "ObservationNotAvailableError" if damage == "missing" else "Observation integrity differs"
    assert replay.returncode != 0 and expected in replay.stderr, replay.stderr
    assert acceptances(tmp_path, "provider") == acceptances(tmp_path, "tool") == 1


@LOCAL
@pytest.mark.parametrize("fault,expected", [
    ("provider-native-before-dbos", None), ("tool-native-before-dbos", None),
    ("provider-return-before-native", "PROVIDER_OUTCOME_UNKNOWN"),
    ("tool-intent-before-submit", "TOOL_OUTCOME_UNKNOWN"),
    ("tool-accepted-before-receipt", "TOOL_OUTCOME_UNKNOWN"),
])
def test_managed_faults_keep_native_reconciliation(tmp_path, fault, expected):
    first = run_controlled(tmp_path, fault=fault)
    assert first.returncode != 0, first.stderr
    replay = run_controlled(tmp_path)
    if expected:
        assert replay.returncode != 0 and expected in replay.stderr, replay.stderr
    else:
        assert replay.returncode == 0, replay.stderr
    assert acceptances(tmp_path, "provider") == 1
    assert acceptances(tmp_path, "tool") == (0 if fault in {
        "provider-return-before-native", "tool-intent-before-submit"} else 1)
    if fault == "provider-return-before-native":
        assert native_record(str(tmp_path), "provider-intent")["usage"] is None


@LOCAL
@pytest.mark.asyncio
async def test_cancelled_sync_and_detached_writers_cannot_write_after_handover(tmp_path):
    async with worker(tmp_path, command(HERE / "blocking_worker.py", tmp_path)) as (launcher, handle):
        await ready(tmp_path / "entered.json")
        pids = json.loads((tmp_path / "entered.json").read_text())
        with pytest.raises(RunWriterActiveError):
            with run_writer(tmp_path):
                pytest.fail("A live controller lost the run lease")
        (tmp_path / "cancel-requested").touch()
        await ready(tmp_path / "cancelled.json")
        assert json.loads((tmp_path / "cancelled.json").read_text())["status"] == "CANCELLED"
        # DBOS cancellation alone has not retired either writer.
        assert all(Path(f"/proc/{pid}").exists() for pid in pids.values())
        await launcher.cancel(handle)
        assert_gone(pids.values())
    with run_writer(tmp_path):
        assert_gone(pids.values())
        ledger = JobLedger(tmp_path / "late.sqlite")
        try:
            ledger.record_execution_submission(run_id=RUN_ID, execution_id=identity("replacement"),
                                               payload='{"writer":"new"}')
            assert ledger.execution_submission(RUN_ID, identity("replacement")) == '{"writer":"new"}'
        finally:
            ledger.close()
        (tmp_path / "release").touch()
    assert_no_old_receipt(tmp_path)
    (tmp_path / "handover.json").write_text(json.dumps({
        "dbos_status": "CANCELLED", "old_pids": pids, "old_writers_gone": True,
        "replacement_receipt_verified": True, "late_old_receipts": 0,
        "scope": "synthetic worker cancellation, not recovery of a cancelled user run",
    }, indent=2))


@LOCAL
@pytest.mark.asyncio
async def test_controller_death_retires_orphan_before_replacement(tmp_path):
    code = (
        "import asyncio,os,sys;from pathlib import Path;"
        f"sys.path.insert(0,{str(HERE)!r});from controlled_round import command,worker\n"
        "async def main():\n"
        f"    root=Path({str(tmp_path)!r})\n"
        f"    async with worker(root,command(Path({str(HERE / 'blocking_worker.py')!r}),root)):\n"
        "        while not (root/'entered.json').exists(): await asyncio.sleep(0.02)\n"
        "        os._exit(75)\n"
        "asyncio.run(main())"
    )
    with (tmp_path / "parent.log").open("x") as log:
        parent = subprocess.Popen((sys.executable, "-c", code), stdout=log, stderr=log)
        try:
            await ready(tmp_path / "entered.json")
            pids = json.loads((tmp_path / "entered.json").read_text())
            assert await asyncio.to_thread(parent.wait, 15) == 75
            async with worker(tmp_path, command(HERE / "controlled_round.py", tmp_path, "--child")) as (launcher, handle):
                assert_gone(pids.values())
                (tmp_path / "release").touch()
                await launcher.wait(handle)
                outcome = await launcher.collect(handle)
                assert outcome.exit_code == 0, outcome.stderr.decode()
            assert_no_old_receipt(tmp_path)
            assert acceptances(tmp_path, "provider") == acceptances(tmp_path, "tool") == 1
            (tmp_path / "handover.json").write_text(json.dumps({
                "old_controller_exit": 75, "old_pids": pids, "old_writers_gone": True,
                "replacement_exit": outcome.exit_code, "late_old_receipts": 0,
                "scope": "synthetic process ownership, not Native Workflow continuation",
            }, indent=2))
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait(timeout=10)


@pytest.mark.asyncio
async def test_unconfirmed_stop_blocks_replacement_without_dispatch(tmp_path, monkeypatch):
    state = {"handle": None, "submissions": 0}

    class Unconfirmed:
        async def find(self, _):
            return state["handle"]

        async def submit(self, spec):
            state["submissions"] += 1
            state["handle"] = spec.execution_id
            return state["handle"]

        async def cancel(self, _):
            raise JobLauncherError("stop unconfirmed")

    monkeypatch.setattr(controlled_round, "owner", Unconfirmed)
    with pytest.raises(JobLauncherError, match="unconfirmed"):
        async with worker(tmp_path, ("unused",)):
            pass
    with pytest.raises(JobLauncherError, match="unconfirmed"):
        async with worker(tmp_path, ("must-not-start",)):
            pytest.fail("Replacement must not dispatch")
    assert state["submissions"] == 1
    assert state["handle"] == worker_id(tmp_path)


@pytest.mark.asyncio
async def test_repeated_controller_cancellation_keeps_lease_until_stop_finishes(tmp_path, monkeypatch):
    running, stopping, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class SlowStop:
        async def find(self, _):
            return None

        async def submit(self, spec):
            return spec.execution_id

        async def cancel(self, _):
            stopping.set()
            await release.wait()

    monkeypatch.setattr(controlled_round, "owner", SlowStop)

    async def control():
        async with worker(tmp_path, ("unused",)):
            running.set()
            await asyncio.Future()

    task = asyncio.create_task(control())
    try:
        await running.wait()
        task.cancel()
        await stopping.wait()
        task.cancel()
        await asyncio.sleep(0)
        with pytest.raises(RunWriterActiveError):
            with run_writer(tmp_path):
                pytest.fail("Second cancellation released a still-active writer")
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    with run_writer(tmp_path):
        pass
