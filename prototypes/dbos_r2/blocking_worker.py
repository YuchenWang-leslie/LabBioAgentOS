"""Synthetic cancellation witness: a sync DBOS writer and a detached writer.

Both wait for a test gate before writing. No scientific program, model request,
container or production run is involved.
"""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from dbos import DBOS, SetWorkflowID

from labbioagentos.execution.job_ledger import JobLedger
from controlled_round import command
from round_trip import RUN_ID, identity


def wait_for(path):
    deadline = time.monotonic() + 75
    while not path.exists():
        if time.monotonic() >= deadline:
            raise TimeoutError("Synthetic gate was not opened")
        time.sleep(0.02)


def late_write(root, name):
    wait_for(root / "release")
    ledger = JobLedger(root / "late.sqlite")
    try:
        ledger.record_execution_submission(run_id=RUN_ID, execution_id=identity(name),
                                           payload=json.dumps({"writer": name}))
    finally:
        ledger.close()


@DBOS.step(name="r2-contained-sync-writer", retries_allowed=False)
def blocking_step(root):
    root = Path(root)
    child = subprocess.Popen(command(Path(__file__), root, "--descendant"),
                             start_new_session=True)
    wait_for(root / "descendant-ready")
    (root / "entered.json").write_text(json.dumps({"worker_pid": os.getpid(),
                                                  "descendant_pid": child.pid}))
    late_write(root, "sync-old")


@DBOS.step(name="r2-contained-forbidden-next", retries_allowed=False)
def next_step(root):
    Path(root, "unexpected-next-step").touch()


@DBOS.workflow(name="r2-contained-cancellation")
def workflow(root):
    blocking_step(root)
    next_step(root)


def main(root):
    DBOS(config={"name": "r2-contained", "application_version": "r2-contained-1",
                 "system_database_url": f"sqlite:///{root / 'cancel.sqlite'}",
                 "executor_id": "r2-contained", "enable_otlp": False, "log_level": "ERROR"})
    DBOS.launch()
    try:
        with SetWorkflowID("r2-contained-cancel"):
            handle = DBOS.start_workflow(workflow, str(root))
        wait_for(root / "cancel-requested")
        DBOS.cancel_workflow(handle.get_workflow_id())
        state = DBOS.get_workflow_status(handle.get_workflow_id())
        (root / "cancelled.json").write_text(json.dumps({"status": state.status}))
        wait_for(root / "release")
    finally:
        DBOS.destroy(workflow_completion_timeout_sec=5)


if __name__ == "__main__":
    root = Path(sys.argv[1])
    if sys.argv[2:] == ["--descendant"]:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        (root / "descendant-ready").touch()
        late_write(root, "detached-old")
    else:
        main(root)
