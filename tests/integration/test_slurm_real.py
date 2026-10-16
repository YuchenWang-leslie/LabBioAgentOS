"""Opt-in C13 phase 3a check: real Slurm + Apptainer, end to end (acceptance 19).

Run on a login node; jobs run on compute nodes. Every path below must be on a
filesystem shared by login and compute nodes.

    LABBIO_RUN_SLURM=1 \
    LABBIO_APPTAINER_SIF=/shared/path/python.sif \
    LABBIO_APPTAINER_BINARY=/absolute/path/to/apptainer \
    LABBIO_SHARED_DIR=/shared/path/labbio-test \
    LABBIO_SLURM_PARTITION=<partition> \
        python -m pytest tests/integration/test_slurm_real.py -v

Optional: LABBIO_SLURM_ACCOUNT, LABBIO_SLURM_QOS,
LABBIO_APPTAINER_RESOURCE_ENFORCEMENT ("scheduler" by default) and
LABBIO_KEEP_TEST_FILES=1 to keep each test's directory for inspection.
Where Slurm accounting (sacct) is available, the timeout test also checks it.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from labbioagentos import (
    ApprovedImage,
    ApprovedImageRegistry,
    ArtifactRegistrationPolicy,
    ContainerExecutor,
    ExecutionPlan,
    ExecutionPolicy,
    ExecutionRuntime,
    ExecutionStatus,
    ExecutionWorkspaceManager,
    InMemoryTraceSink,
    LocalArtifactStore,
    MountResolver,
    OutputCollector,
    RequestedResources,
    RunTraceRecorder,
    TraceEventType,
    WorkflowStage,
)
from labbioagentos.execution import (
    ApptainerCommandBuilder,
    JobLedger,
    JobLedgerState,
    OrphanAction,
    SifStore,
    SubprocessApptainerRunner,
    reconcile_orphan_jobs,
)
from labbioagentos.execution.apptainer_probe import probe_capabilities
from labbioagentos.execution.launchers import (
    BUILTIN_TEMPLATES,
    SchedulerLauncher,
    SchedulerPolicy,
    check_scheduler_client,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("LABBIO_RUN_SLURM") != "1" or not sys.platform.startswith("linux"),
    reason="set LABBIO_RUN_SLURM=1 and the LABBIO_* paths on a Linux login node",
)

SENTINEL = "PRIVATE_C13_SCHEDULER_SENTINEL_9274"
ENFORCEMENT = os.environ.get("LABBIO_APPTAINER_RESOURCE_ENFORCEMENT", "scheduler")
MAX_FILE_BYTES = 16 * 1024 * 1024


def _policy(**overrides) -> SchedulerPolicy:
    return SchedulerPolicy(
        max_output_file_bytes=MAX_FILE_BYTES,
        partition=os.environ["LABBIO_SLURM_PARTITION"],
        account=os.environ.get("LABBIO_SLURM_ACCOUNT") or None,
        qos=os.environ.get("LABBIO_SLURM_QOS") or None,
        walltime_margin_seconds=120, poll_interval_seconds=5, completion_grace_seconds=60,
        **overrides,
    )


@pytest.fixture
def cluster(request):
    base = Path(os.environ["LABBIO_SHARED_DIR"]) / f"slurm-{uuid4().hex[:12]}"
    base.mkdir(parents=True)
    binary = os.environ["LABBIO_APPTAINER_BINARY"]
    sif_store = SifStore(base / "sif")
    reference = sif_store.register(Path(os.environ["LABBIO_APPTAINER_SIF"]))
    runner = SubprocessApptainerRunner(max_output_file_bytes=MAX_FILE_BYTES)
    builder = ApptainerCommandBuilder(sif_store, scratch_root=base / "scratch",
                                      resource_enforcement=ENFORCEMENT, apptainer_binary=binary)

    def backend_check():
        capabilities = probe_capabilities(
            binary=binary, sif_path=sif_store.path_for(reference), executable="python",
            runner=runner, scratch_root=base / "scratch",
        )
        capabilities.require(resource_enforcement=ENFORCEMENT)
        return capabilities.trace_record(resource_enforcement=ENFORCEMENT)

    sink = InMemoryTraceSink()
    recorder = RunTraceRecorder(sink)
    store = LocalArtifactStore(base / "artifacts", trace_recorder=recorder)
    ledger = JobLedger(base / "jobs.sqlite")
    launcher = SchedulerLauncher(template=BUILTIN_TEMPLATES["slurm"],
                                 policy=_policy(**getattr(request, "param", {})),
                                 workspace_root=base / "executions")
    executor = ContainerExecutor(
        store=store,
        image_registry=ApprovedImageRegistry((ApprovedImage(
            key="python-real", reference=reference, runtime=ExecutionRuntime.PYTHON,
        ),)),
        execution_policy=ExecutionPolicy(include_error_context=True),
        mount_resolver=MountResolver(store, approved_input_roots=(store.root,)),
        workspace_manager=ExecutionWorkspaceManager(base / "executions"),
        output_collector=OutputCollector(store, ArtifactRegistrationPolicy()),
        process_runner=runner, command_builder=builder, trace_recorder=recorder,
        launcher=launcher, job_ledger=ledger, backend_check=backend_check,
    )
    yield executor, ledger, sink, base
    ledger.close()
    if os.environ.get("LABBIO_KEEP_TEST_FILES") != "1":
        shutil.rmtree(base, ignore_errors=True)


def _plan(script: str, timeout: float = 300.0) -> ExecutionPlan:
    return ExecutionPlan(
        run_id=uuid4(), stage_id=WorkflowStage.EXECUTE, image_key="python-real",
        script_content=script,
        resources=RequestedResources(cpus=1.0, memory_mb=1024, timeout_seconds=timeout),
    )


def _squeue_state(job_id: str) -> str:
    """The job's Slurm state, or an empty string once it is no longer listed."""
    result = subprocess.run(["squeue", "--noheader", "--jobs", job_id, "--format", "%T"],
                            capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def _accounting(job_id: str) -> tuple[str, ...] | None:
    """State and exit code from Slurm accounting, or None where accounting is unavailable."""
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                ["sacct", "--noheader", "--parsable2", "--allocations", "--jobs", job_id,
                 "--format", "State,ExitCode"],
                capture_output=True, text=True, check=False, timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        line = result.stdout.strip().split("\n")[0]
        if line and not line.startswith(("PENDING", "RUNNING", "COMPLETING")):
            return tuple(line.split("|"))
        time.sleep(2)
    return None


@pytest.mark.asyncio
async def test_mock_task_runs_end_to_end_through_slurm(cluster):
    executor, ledger, sink, base = cluster
    plan = _plan("print('c13 scheduled mock task')\n")

    result = await executor.execute_async(plan)

    assert result.status is ExecutionStatus.SUCCEEDED and result.exit_code == 0
    assert Path(result.stdout_ref.storage_locator).read_text() == "c13 scheduled mock task\n"
    assert ledger.get(plan.execution_id).state is JobLedgerState.COLLECTED
    assert not (base / "scratch" / plan.execution_id.hex).exists()
    planned = next(event for event in sink.read() if event.event_type is TraceEventType.EXECUTION_PLANNED)
    assert planned.payload["isolation"]["backend"] == "apptainer"
    script = (base / "executions" / str(plan.execution_id) / "job" / "job.sh").read_text()
    assert script.startswith("#!/bin/bash\n") and "#SBATCH --export=NONE" in script


@pytest.mark.asyncio
async def test_sandbox_holds_inside_a_scheduled_job(cluster, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", SENTINEL)  # Present in the control-plane process.
    executor, _, _, base = cluster
    script = (
        "import json, os, socket\n"
        f"report = {{'secret_visible': any({SENTINEL!r} in v for v in os.environ.values())}}\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
        "    report['network'] = True\n"
        "except OSError:\n"
        "    report['network'] = False\n"
        "try:\n"
        "    open('/labbio/script.py', 'a').write('#')\n"
        "    report['script_writable'] = True\n"
        "except OSError:\n"
        "    report['script_writable'] = False\n"
        f"report['host_root_visible'] = os.path.exists({str(base)!r})\n"
        "open(os.path.join(os.environ['LABBIO_OUTPUT_DIR'], 'written.txt'), 'w').write('ok')\n"
        "print(json.dumps(report))\n"
    )
    plan = _plan(script)

    result = await executor.execute_async(plan)

    assert result.status is ExecutionStatus.SUCCEEDED
    assert json.loads(Path(result.stdout_ref.storage_locator).read_text()) == {
        "secret_visible": False, "network": False,
        "script_writable": False, "host_root_visible": False,
    }
    workspace = base / "executions" / str(plan.execution_id)
    assert (workspace / "outputs" / "written.txt").read_text() == "ok"
    for path in (workspace / "job").iterdir():
        if path.is_file():
            assert SENTINEL not in path.read_text(errors="replace")


@pytest.mark.asyncio
async def test_timeout_is_enforced_inside_the_job(cluster):
    executor, ledger, _, _ = cluster
    plan = _plan("import time\ntime.sleep(600)\n", timeout=20)
    started = time.monotonic()

    result = await executor.execute_async(plan)

    assert result.status is ExecutionStatus.TIMED_OUT
    assert time.monotonic() - started < 600
    accounting = _accounting(ledger.get(plan.execution_id).job_id)
    if accounting is not None:  # Operators see the timeout in Slurm's own records.
        assert accounting[0] == "FAILED" and accounting[1].split(":")[0] in {"124", "137"}, accounting


@pytest.mark.asyncio
@pytest.mark.parametrize("cluster, phase", [
    ({"extra_directives": ("--hold",)}, "queued"),  # Held, so it is still queued when lost.
    ({}, "running"),
], indirect=["cluster"], ids=["queued", "running"])
async def test_lost_control_plane_job_is_cancelled_by_reconciliation(cluster, phase):
    executor, ledger, _, base = cluster
    plan = _plan(
        "import os, time\n"
        "open(os.path.join(os.environ['LABBIO_OUTPUT_DIR'], 'started'), 'w').close()\n"
        "time.sleep(900)\n",
        timeout=1200,
    )
    workspace = base / "executions" / str(plan.execution_id)
    task = asyncio.create_task(executor.execute_async(plan))
    deadline = time.monotonic() + 300
    entry, reached = None, False
    while time.monotonic() < deadline and not reached:
        await asyncio.sleep(1)
        entry = ledger.get(plan.execution_id)
        if entry is None or entry.state is not JobLedgerState.SUBMITTED:
            continue
        if phase == "queued":
            reached = _squeue_state(entry.job_id) == "PENDING"
        else:  # The program itself has started inside the container.
            reached = (workspace / "outputs" / "started").exists()
    task.cancel()  # The control plane is lost; the job stays in Slurm.
    with pytest.raises(asyncio.CancelledError):
        await task
    assert entry is not None and _squeue_state(entry.job_id)

    restarted_ledger = JobLedger(base / "jobs.sqlite")
    restarted = SchedulerLauncher(template=BUILTIN_TEMPLATES["slurm"], policy=_policy(),
                                  workspace_root=base / "executions")
    try:
        results = await reconcile_orphan_jobs(
            ledger=restarted_ledger, launcher=restarted, run_id=plan.run_id,
            cleanup_paths=executor.command_builder.cleanup_paths,
        )
    finally:
        restarted_ledger.close()

    assert [(item.job_id, item.action) for item in results] == [(entry.job_id, OrphanAction.CANCELLED)]
    assert not _squeue_state(entry.job_id)
    assert not workspace.exists()
    assert not (base / "scratch" / plan.execution_id.hex).exists()
    if not reached:  # Cleaned up either way; only the intended phase was not observed.
        pytest.skip(f"the job was not observed {phase} within 300s on this partition")


def test_scheduler_preflight_accepts_the_configured_script(cluster):
    _, _, _, base = cluster

    report = check_scheduler_client(BUILTIN_TEMPLATES["slurm"], _policy(), base / "scratch")

    assert all(report["clients"].values()), report
    assert report["test_submission"] == "ACCEPTED", report
