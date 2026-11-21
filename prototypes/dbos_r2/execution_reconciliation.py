"""Read-only R2 acceptance reconciliation, not execution or scientific recovery.

Native owns the request/receipt; the launcher owns acceptance/liveness. Neither
a missing local receipt nor a vanished unit proves that nothing executed.
"""

from pathlib import Path

from labbioagentos.execution.apptainer import SifStore
from labbioagentos.execution.job_ledger import JobLedger, OPEN_STATES
from labbioagentos.execution.launchers.base import JobLauncherError, JobState

from round_trip import RUN_ID, native_record
from runtime_binding import runtime_identity


def execution_binding(settings, plan):
    if settings.execution.backend != "apptainer" or (settings.launcher and settings.launcher.kind != "local"):
        raise ValueError("R2 execution probe requires explicit Local + Apptainer")
    config = settings.execution.apptainer
    if not config.sif_store.is_dir():
        raise ValueError("The bound SIF store is missing")
    if (plan.run_id != RUN_ID or (plan.owner_user_id, plan.project_id, plan.lab_id) !=
            (settings.identity.user_id, settings.identity.project_id, settings.identity.lab_id)):
        raise ValueError("Execution scope differs")
    return {"version": "r2-execution-probe-2", "request": plan.model_dump(mode="json"),
            "runtime": runtime_identity(settings,
                sif_path=SifStore(config.sif_store).path_for(settings.execution.image_reference),
                executable_path=config.binary)}


async def reconcile_execution(root, launcher, plan, settings, *, binding_name="run-binding"):
    if native_record(str(root), binding_name) != execution_binding(settings, plan):
        raise ValueError("Execution request binding differs")
    path = Path(root) / "native-jobs.sqlite"
    if not path.is_file():
        raise ValueError("Native job ledger is missing")
    ledger = JobLedger(path)
    try:
        entry = ledger.get(plan.execution_id)
        if (entry is None or entry.run_id != plan.run_id or entry.launcher != launcher.kind
                or entry.workspace_root != Path(root) / "executions" / str(plan.execution_id)):
            raise ValueError("Native job identity differs")
        receipt = ledger.execution_submission(plan.run_id, plan.execution_id)
        result = {"execution_id": str(plan.execution_id), "replay_allowed": False,
                  "scientific_result_available": False, "job_id": entry.job_id}
        if receipt is not None:
            return {**result, "status": "BLOCKED", "reason": "NATIVE_RECEIPT_REQUIRES_VALIDATION"}
        try:
            handle = await launcher.find(plan.execution_id)
            if handle is None:
                return {**result, "status": "BLOCKED", "reason": "ACCEPTANCE_UNKNOWN"}
            if (handle.execution_id != plan.execution_id or handle.launcher != entry.launcher
                    or (entry.job_id is not None and entry.job_id != handle.job_id)):
                raise ValueError("Launcher job identity differs")
            state = await launcher.status(handle)
        except JobLauncherError:
            return {**result, "status": "BLOCKED", "reason": "LAUNCHER_UNCONFIRMED"}
        result["job_id"] = handle.job_id
        if state in {JobState.PENDING, JobState.RUNNING}:
            if entry.state not in OPEN_STATES:
                return {**result, "status": "BLOCKED", "reason": "NATIVE_LAUNCHER_STATE_CONFLICT"}
            return {**result, "status": "WAITING", "reason": "EXECUTION_ACCEPTED"}
        return {**result, "status": "BLOCKED", "reason": (
            "ACCEPTANCE_UNKNOWN" if state is JobState.UNKNOWN else "TERMINAL_WITHOUT_NATIVE_RECEIPT")}
    finally:
        ledger.close()
