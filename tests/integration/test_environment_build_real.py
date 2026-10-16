"""Opt-in C13 phase 4 check: a real cluster environment build, end to end (acceptance 20 and 21).

Run on a login node with the same variables as test_slurm_real.py; every path
must be on a filesystem shared by login and compute nodes. The base SIF needs
pip 22.2 or later (python:3.11-slim qualifies), and the login node needs to
reach the package index.

    LABBIO_RUN_ENV_BUILD=1 \
    LABBIO_APPTAINER_SIF=/shared/path/python.sif \
    LABBIO_APPTAINER_BINARY=/absolute/path/to/apptainer \
    LABBIO_SHARED_DIR=/shared/path/labbio-test \
    LABBIO_SLURM_PARTITION=<partition> \
        python -m pytest tests/integration/test_environment_build_real.py -v

Optional: LABBIO_SLURM_ACCOUNT, LABBIO_SLURM_QOS; LABBIO_BUILD_PROXY and
LABBIO_BUILD_INDEX_URL for the download; LABBIO_BUILD_DIR (node-local on compute
nodes, default /tmp); LABBIO_DOWNLOAD_PARTITION (a partition with network,
enables the download-job test); LABBIO_BUILD_REQUIREMENT and
LABBIO_BUILD_MODULE (default requests==2.32.3 / requests, a package with
dependencies and a compiled wheel); LABBIO_KEEP_TEST_FILES=1 keeps each
test's directory, including the build logs, for inspection.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID, uuid4

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
    WorkflowStage,
)
from labbioagentos.execution import (
    ApptainerCommandBuilder,
    JobLedger,
    JobLedgerState,
    JobPurpose,
    OrphanAction,
    SifStore,
    SubprocessApptainerRunner,
    reconcile_orphan_jobs,
)
from labbioagentos.execution.apptainer import ISOLATION_ARGUMENTS
from labbioagentos.execution.apptainer_build import (
    DownloadRunner,
    classify_download_check,
    download_check_argv,
)
from labbioagentos.execution.apptainer_environment_builder import (
    ApptainerBuildPolicy,
    ApptainerEnvironmentBuilder,
)
from labbioagentos.execution.environments import EnvironmentService
from labbioagentos.execution.launchers import BUILTIN_TEMPLATES, SchedulerLauncher, SchedulerPolicy

pytestmark = pytest.mark.skipif(
    os.environ.get("LABBIO_RUN_ENV_BUILD") != "1" or not sys.platform.startswith("linux"),
    reason="set LABBIO_RUN_ENV_BUILD=1 and the LABBIO_* paths on a Linux login node",
)

BINARY = os.environ.get("LABBIO_APPTAINER_BINARY", "")
REQUIREMENT = os.environ.get("LABBIO_BUILD_REQUIREMENT", "requests==2.32.3")
MODULE = os.environ.get("LABBIO_BUILD_MODULE", "requests")
PROXY = os.environ.get("LABBIO_BUILD_PROXY") or None
INDEX_URL = os.environ.get("LABBIO_BUILD_INDEX_URL") or None
MACHINE = platform.machine()
SIF_BYTES = 8 << 30


def _scheduler_policy(**overrides) -> SchedulerPolicy:
    return SchedulerPolicy(**{
        "max_output_file_bytes": SIF_BYTES,
        "partition": os.environ["LABBIO_SLURM_PARTITION"],
        "account": os.environ.get("LABBIO_SLURM_ACCOUNT") or None,
        "qos": os.environ.get("LABBIO_SLURM_QOS") or None,
        "walltime_margin_seconds": 120, "poll_interval_seconds": 5, "completion_grace_seconds": 60,
        **overrides,
    })


@pytest.fixture
def site(request):
    options = getattr(request, "param", {})
    if options.get("download") == "job" and not os.environ.get("LABBIO_DOWNLOAD_PARTITION"):
        pytest.skip("set LABBIO_DOWNLOAD_PARTITION to a partition whose nodes can reach the index")
    base = Path(os.environ["LABBIO_SHARED_DIR"]) / f"env-{uuid4().hex[:12]}"
    base.mkdir(parents=True)
    ledger = None
    try:  # The directory is removed even when setup fails.
        sif_store = SifStore(base / "sif")
        reference = sif_store.register(Path(os.environ["LABBIO_APPTAINER_SIF"]))
        image = ApprovedImage(key="python-base", reference=reference, runtime=ExecutionRuntime.PYTHON)
        ledger = JobLedger(base / "jobs.sqlite")
        jobs_root = base / "environment-jobs"
        jobs_root.mkdir()
        launcher = SchedulerLauncher(template=BUILTIN_TEMPLATES["slurm"],
                                     policy=_scheduler_policy(**options.get("build", {})), workspace_root=jobs_root)
        policy = ApptainerBuildPolicy(
            apptainer_binary=Path(BINARY), machine=MACHINE,
            build_dir=Path(os.environ.get("LABBIO_BUILD_DIR", "/tmp")), download_work_root=Path("/tmp"),
            resources=RequestedResources(cpus=2.0, memory_mb=8192, timeout_seconds=3600),
            # Not created here: the builder must create it, as for a fresh site.
            cache_dir=base / "pip-cache", index_url=INDEX_URL, proxy_url=PROXY,
        )
        if options.get("download") == "job":
            placement = {"download_launcher": SchedulerLauncher(
                template=BUILTIN_TEMPLATES["slurm"],
                policy=_scheduler_policy(partition=os.environ["LABBIO_DOWNLOAD_PARTITION"]),
                workspace_root=jobs_root)}
        else:
            placement = {"download_runner": DownloadRunner(max_file_bytes=SIF_BYTES, cpu_seconds=1800)}
        builder = ApptainerEnvironmentBuilder(sif_store=sif_store, policy=policy, build_launcher=launcher,
                                              job_ledger=ledger, **placement)
        service = EnvironmentService(root=base / "environments", image_registry=ApprovedImageRegistry((image,)),
                                     builder=builder, owner_user_id="real-test-user")
        yield service, ledger, launcher, sif_store, base
    finally:
        if ledger is not None:
            ledger.close()
        if os.environ.get("LABBIO_KEEP_TEST_FILES") != "1":
            shutil.rmtree(base, ignore_errors=True)


def _jobs(base, ledger, run_id):
    """Every ledger entry of one run, in submission order."""
    with sqlite3.connect(base / "jobs.sqlite") as connection:
        rows = connection.execute("SELECT execution_id FROM jobs WHERE run_id = ? ORDER BY created_at",
                                  (str(run_id),)).fetchall()
    return [ledger.get(UUID(row[0])) for row in rows]


def _record(base, receipt):
    return json.loads((base / "environments" / "records" / f"{receipt['request_hash']}.json").read_text())


def _logs(base):
    """Build logs to show when a real build fails; they never reach the model."""
    lines = []
    for path in sorted((base / "environments" / "attempts").glob("*/logs/*")):
        lines.append(f"--- {path.name}\n{path.read_text(errors='replace')[-2000:]}")
    return "\n".join(lines)


def test_login_node_can_download_for_this_base_image(site):
    service, _, _, sif_store, base = site
    sif = sif_store.path_for(service.image_registry.resolve("python-base").resolved_reference)
    work = base / "check"
    work.mkdir()
    runner = DownloadRunner(max_file_bytes=SIF_BYTES, cpu_seconds=600)

    machine = runner.run((BINARY, "--silent", "exec", *ISOLATION_ARGUMENTS, "--workdir", str(work), str(sif),
                          "python", "-I", "-c", "import platform; print(platform.machine())"), timeout_seconds=120)
    outcome = runner.run(download_check_argv(apptainer_binary=Path(BINARY), base_sif=sif, work_dir=work,
                                             index_url=INDEX_URL, proxy_url=PROXY), timeout_seconds=600)

    assert machine.stdout.decode().strip() == MACHINE
    output = (outcome.stdout + outcome.stderr).decode(errors="replace")
    assert classify_download_check(outcome.exit_code, timed_out=outcome.timed_out, output=output,
                                   report=work / "report.json") == "OK", output[-2000:]


@pytest.mark.asyncio
async def test_environment_is_built_in_a_job_and_runs_a_task(site):
    service, ledger, _, sif_store, base = site
    run_id = uuid4()

    receipt = await service.build_environment("python-base", (REQUIREMENT,), (MODULE,), run_id=run_id)

    assert receipt["status"] == "SUCCEEDED", (receipt, _logs(base))
    image = service.image_registry.resolve(receipt["image_key"])
    assert sif_store.verify(image.resolved_reference)
    locked = {item["name"] for item in _record(base, receipt)["locked_wheels"]}
    assert REQUIREMENT.split("==")[0] in locked
    jobs = _jobs(base, ledger, run_id)
    assert [(job.purpose, job.state) for job in jobs] == [(JobPurpose.ENVIRONMENT_BUILD, JobLedgerState.COLLECTED)]
    assert list((base / "environment-jobs").iterdir()) == []
    attempt = next((base / "environments" / "attempts").iterdir())
    assert not (attempt / "download" / "wheels").exists() and (attempt / "lock.txt").is_file()
    assert ((base / "pip-cache").stat().st_mode & 0o777) == 0o700  # Created private by the builder.

    # The built environment executes like any approved image: scheduled, isolated, no network.
    store = LocalArtifactStore(base / "artifacts", trace_recorder=RunTraceRecorder(InMemoryTraceSink()))
    runner = SubprocessApptainerRunner(max_output_file_bytes=16 << 20)
    executor = ContainerExecutor(
        store=store, image_registry=service.image_registry,
        execution_policy=ExecutionPolicy(include_error_context=True),
        mount_resolver=MountResolver(store, approved_input_roots=(store.root,)),
        workspace_manager=ExecutionWorkspaceManager(base / "executions"),
        output_collector=OutputCollector(store, ArtifactRegistrationPolicy()),
        process_runner=runner,
        command_builder=ApptainerCommandBuilder(sif_store, scratch_root=base / "scratch",
                                                resource_enforcement="scheduler", apptainer_binary=BINARY),
        launcher=SchedulerLauncher(template=BUILTIN_TEMPLATES["slurm"], policy=_scheduler_policy(),
                                   workspace_root=base / "executions"),
        job_ledger=ledger,
    )
    plan = ExecutionPlan(run_id=run_id, stage_id=WorkflowStage.EXECUTE, image_key=receipt["image_key"],
                         script_content=f"import {MODULE}\nprint({MODULE}.__name__)\n",
                         resources=RequestedResources(cpus=1.0, memory_mb=1024, timeout_seconds=300))

    result = await executor.execute_async(plan)

    assert result.status is ExecutionStatus.SUCCEEDED, result
    assert Path(result.stdout_ref.storage_locator).read_text() == f"{MODULE}\n"


@pytest.mark.asyncio
async def test_requirements_the_base_image_satisfies_build_without_downloading(site):
    service, ledger, _, _, base = site

    receipt = await service.build_environment("python-base", ("pip",), ("json",), run_id=uuid4())

    assert receipt["status"] == "SUCCEEDED", (receipt, _logs(base))
    assert _record(base, receipt)["locked_wheels"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("site", [{"download": "job"}], indirect=True)
async def test_download_can_run_as_a_job_on_a_network_partition(site):
    service, ledger, _, _, base = site
    run_id = uuid4()

    receipt = await service.build_environment("python-base", (REQUIREMENT,), (MODULE,), run_id=run_id)

    assert receipt["status"] == "SUCCEEDED", (receipt, _logs(base))
    assert [job.state for job in _jobs(base, ledger, run_id)] == [JobLedgerState.COLLECTED] * 2


@pytest.mark.asyncio
@pytest.mark.parametrize("site", [{"build": {"extra_directives": ("--hold",)}}], indirect=True)
async def test_a_lost_build_job_is_cancelled_by_its_run(site):
    service, ledger, launcher, _, base = site
    run_id = uuid4()
    task = asyncio.create_task(service.build_environment("python-base", (REQUIREMENT,), (MODULE,), run_id=run_id))
    deadline = time.monotonic() + 600  # The download runs first.
    jobs = []
    while time.monotonic() < deadline and not task.done():
        jobs = _jobs(base, ledger, run_id)
        if jobs and jobs[0].state is JobLedgerState.SUBMITTED:
            break
        await asyncio.sleep(2)
    assert jobs and jobs[0].state is JobLedgerState.SUBMITTED, (
        task.done() and (task.exception() or task.result()), _logs(base))
    task.cancel()  # The control plane is lost while the build job is held in the queue.
    with pytest.raises(asyncio.CancelledError):
        await task
    job = jobs[0]

    restarted = JobLedger(base / "jobs.sqlite")
    try:
        results = await reconcile_orphan_jobs(ledger=restarted, launcher=launcher, run_id=run_id,
                                              environment_launcher=launcher)
    finally:
        restarted.close()

    assert [(item.job_id, item.action) for item in results] == [(job.job_id, OrphanAction.CANCELLED)]
    assert not job.workspace_root.exists()
    squeue = subprocess.run(["squeue", "--noheader", "--jobs", job.job_id, "--format", "%T"],
                            capture_output=True, text=True, check=False)
    assert not (squeue.returncode == 0 and squeue.stdout.strip())
