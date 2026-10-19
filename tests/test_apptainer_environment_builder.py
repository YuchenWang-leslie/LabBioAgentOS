"""C13 phase 4: ApptainerEnvironmentBuilder end to end (DECIDED-C13-8, acceptance 20 and 21).

The scheduler is FakeSlurm, which runs the generated job scripts with bash.
The fake apptainer keeps image content as JSON: a SIF file holds the package
state, a sandbox directory holds it while packages are installed, and the
import probe reads it back from the packed SIF. Jobs clear their
environment, so the fake reads its settings from a file beside it.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
import stat
import sys
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from labbioagentos import ApprovedImage, ApprovedImageRegistry, ExecutionRuntime, RequestedResources
from labbioagentos.execution import JobLedger, JobLedgerState, JobPurpose, OrphanAction, reconcile_orphan_jobs
from labbioagentos.execution.apptainer import SifStore
from labbioagentos.execution.apptainer_build import DownloadRunner
from labbioagentos.execution.apptainer_environment_builder import (
    ApptainerBuildPolicy,
    ApptainerEnvironmentBuilder,
)
from labbioagentos.execution.environment_builder import DockerEnvironmentBuilder
from labbioagentos.execution.environments import EnvironmentBuilder, EnvironmentService
from labbioagentos.execution.launchers import BUILTIN_TEMPLATES, SchedulerLauncher, SchedulerPolicy

from scheduler_fixtures import FakeSlurm

PROXY = "http://proxy.example:3128"

FAKE_APPTAINER = f"""#!{sys.executable}
import json, os, subprocess, sys, time
from pathlib import Path
here = Path(sys.argv[0]).parent
config = json.loads((here / "fake.json").read_text())
args = sys.argv[1:]
with open(config["log"], "a") as stream:
    stream.write(json.dumps(args) + "\\n")

def step(stage):
    if config.get("fail") == stage:
        print(config.get("output", ""))
        sys.exit(1)
    if config.get("hang") == stage:
        (here / "hanging").touch()
        time.sleep(120)

if args[1] == "build":
    if args[2] == "--sandbox":
        step("sandbox")
        Path(args[3]).mkdir()
        (Path(args[3]) / "state.json").write_text(Path(args[4]).read_text())
    else:
        stage = "pack-check" if args[2].endswith("pack-check.sif") else "pack"
        step(stage)
        Path(args[2]).write_text((Path(args[3]) / "state.json").read_text() if stage == "pack" else "{{}}")
    sys.exit(0)

binds, index = [], 2
while args[index].startswith("--"):
    if args[index] == "--bind":
        source, target = args[index + 1].split(":")[:2]
        if not os.path.exists(source):  # Like Apptainer: a missing bind source stops the container.
            sys.stderr.write("FATAL: mount source " + source + " doesn't exist\\n")
            sys.exit(255)
        binds.append((target, source))
    index += 2 if args[index] in ("--bind", "--workdir", "--no-mount", "--network") else 1
image = Path(args[index])

def host(value):
    for target, source in binds:
        if value == target or value.startswith(target + "/"):
            return source + value[len(target):]
    return value

command = [host(value) for value in args[index + 1:]]
if "-m" in command:
    action = command[command.index("-m") + 2]
    if action == "install" and "--dry-run" in command:
        step("resolve")
        base = json.loads(image.read_text())["packages"]
        names = [line.split(">")[0].split("=")[0].split("<")[0] for line in open(command[command.index("-r") + 1])]
        report = {{"version": "1", "install": [
            {{"metadata": {{"name": name, "version": "2.0"}}}} for name in names if name not in base]}}
        Path(command[command.index("--report") + 1]).write_text(json.dumps(report))
    elif action == "download":
        step("download")
        for line in open(command[command.index("-r") + 1]):
            name, version = line.strip().split("==")
            wheel = Path(command[command.index("--dest") + 1]) / f"{{name.replace('-', '_')}}-{{version}}-py3-none-any.whl"
            wheel.write_text(line)
    elif action == "install":
        step("install")
        state = json.loads((image / "state.json").read_text())
        for line in open(command[command.index("-r") + 1]):
            name, version = line.split()[0].split("==")
            state["packages"][name] = version
        (image / "state.json").write_text(json.dumps(state))
    elif action == "check":
        step("check")
    sys.exit(0)
code = command[command.index("-c") + 1]
if "TemporaryFile" in code:
    step("writable")
    sys.exit(0)
if "name_pattern" in code:
    sys.exit(subprocess.call([sys.executable, "-I", *command[command.index("-c"):]]))
step("probe")
packages = json.loads(image.read_text())["packages"]
importable = {{name.replace("-", "_") for name in packages}}
print(json.dumps({{"installed_packages": packages,
                  "failed_imports": [module for module in json.loads(command[-1]) if module not in importable]}}))
"""


def _scheduler_policy(**overrides):
    return SchedulerPolicy(**{"max_output_file_bytes": 64 << 20, "poll_interval_seconds": 0.05,
                              "completion_grace_seconds": 0.5, "kill_after_seconds": 1, **overrides})


@pytest.fixture
def cluster(tmp_path):
    fake = tmp_path / "apptainer-bin" / "apptainer"
    fake.parent.mkdir()
    fake.write_text(FAKE_APPTAINER)
    fake.chmod(0o755)
    log = tmp_path / "apptainer-calls.log"
    sif_store = SifStore(tmp_path / "sif")
    base_file = tmp_path / "base.sif"
    base_file.write_text(json.dumps({"packages": {"basepkg": "1.0"}}))
    base = ApprovedImage(key="base", reference=sif_store.register(base_file), runtime=ExecutionRuntime.PYTHON,
                         available_python_modules=("basepkg",), installed_packages={"basepkg": "1.0"})
    for name in ("node-local", "login-tmp", "jobs"):
        (tmp_path / name).mkdir()
    ledger = JobLedger(tmp_path / "jobs.sqlite")
    state = SimpleNamespace(tmp_path=tmp_path, fake=fake, log=log, sif_store=sif_store, base=base,
                            ledger=ledger, jobs=tmp_path / "jobs")

    def configure(**settings):
        (fake.parent / "fake.json").write_text(json.dumps({"log": str(log), **settings}))

    def service(*, scheduler=None, download="login", timeout=60.0, **policy_options):
        scheduler = scheduler or FakeSlurm(tmp_path)
        launcher = SchedulerLauncher(template=BUILTIN_TEMPLATES["slurm"], policy=_scheduler_policy(),
                                     workspace_root=state.jobs, command_runner=scheduler)
        policy = ApptainerBuildPolicy(
            apptainer_binary=fake, machine="x86_64", build_dir=tmp_path / "node-local",
            download_work_root=tmp_path / "login-tmp",
            resources=RequestedResources(cpus=2.0, memory_mb=4096, timeout_seconds=timeout),
            proxy_url=PROXY, **policy_options,
        )
        placement = ({"download_runner": DownloadRunner(max_file_bytes=64 << 20, cpu_seconds=60)}
                     if download == "login" else {"download_launcher": launcher})
        builder = ApptainerEnvironmentBuilder(sif_store=sif_store, policy=policy, build_launcher=launcher,
                                              job_ledger=ledger, **placement)
        state.scheduler, state.launcher = scheduler, launcher
        return EnvironmentService(root=tmp_path / "environments", image_registry=ApprovedImageRegistry((base,)),
                                  builder=builder, owner_user_id="user-one")

    def calls():
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    configure()
    state.configure, state.service, state.calls = configure, service, calls
    yield state
    ledger.close()


def _attempt(cluster):
    attempts = list((cluster.tmp_path / "environments" / "attempts").iterdir())
    assert len(attempts) == 1
    return attempts[0]


def _jobs(cluster, run_id):
    """Every ledger entry of one run, in submission order."""
    # closing(): a connection used as a context manager only commits; it stays open.
    with closing(sqlite3.connect(cluster.tmp_path / "jobs.sqlite")) as connection:
        rows = connection.execute("SELECT execution_id FROM jobs WHERE run_id = ? ORDER BY created_at",
                                  (str(run_id),)).fetchall()
    return [cluster.ledger.get(UUID(row[0])) for row in rows]


@pytest.mark.asyncio
@pytest.mark.parametrize("download", ["login", "job"])
async def test_environment_is_downloaded_locked_built_in_a_job_and_registered(cluster, download):
    service, run_id = cluster.service(download=download), uuid4()

    receipt = await service.build_environment("base", ("examplepkg>=1",), ("examplepkg",), run_id=run_id)

    assert receipt["status"] == "SUCCEEDED", receipt
    reference = receipt["image_reference"]
    assert json.loads(cluster.sif_store.path_for(reference).read_text())["packages"] == {
        "basepkg": "1.0", "examplepkg": "2.0"}
    assert service.image_registry.resolve(receipt["image_key"]).resolved_reference == reference
    record = json.loads((cluster.tmp_path / "environments" / "records" / f"{receipt['request_hash']}.json").read_text())
    assert [(item["name"], item["version"]) for item in record["locked_wheels"]] == [("examplepkg", "2.0")]
    assert "locked_wheels" not in json.dumps(receipt) and "sha256:" + record["locked_wheels"][0]["sha256"] != reference
    jobs = _jobs(cluster, run_id)
    assert len(jobs) == (1 if download == "login" else 2)
    assert all(job.purpose is JobPurpose.ENVIRONMENT_BUILD and job.state is JobLedgerState.COLLECTED for job in jobs)
    attempt = _attempt(cluster)
    assert not (attempt / "download" / "wheels").exists()  # Wheels are not kept.
    assert (attempt / "lock.txt").read_text().startswith("examplepkg==2.0 --hash=sha256:")
    assert (attempt / "logs" / "probe.json").is_file()
    for directory in ("jobs", "node-local", "login-tmp"):
        assert list((cluster.tmp_path / directory).iterdir()) == []


@pytest.mark.asyncio
async def test_build_steps_never_have_network_and_only_the_download_sees_the_proxy(cluster):
    service = cluster.service(download="job")

    await service.build_environment("base", ("examplepkg>=1",), ("examplepkg",), run_id=uuid4())

    for call in cluster.calls():
        if call[1] == "exec":
            networked = "--dry-run" in call or "download" in call or any("name_pattern" in item for item in call)
            assert ("--network" in call) is not networked
            assert (PROXY in call) is ("--dry-run" in call or "download" in call)
    assert PROXY in (_attempt(cluster) / "download.sh").read_text()


@pytest.mark.asyncio
async def test_requirements_already_in_the_base_image_build_without_downloading(cluster):
    service = cluster.service()

    receipt = await service.build_environment("base", (), ("basepkg",), run_id=uuid4())

    assert receipt["status"] == "SUCCEEDED"
    actions = [call[call.index("-m") + 2] for call in cluster.calls() if "-m" in call]
    assert actions == ["check"]  # No resolution, download or install.


@pytest.mark.asyncio
@pytest.mark.parametrize("stage, output, code", [
    ("resolve", "ERROR: No matching distribution found for examplepkg>=1", "PIP_NO_MATCH"),
    ("download", "ProxyError: cannot connect", "NETWORK_FAILURE"),
    ("sandbox", "", "BUILDER_UNAVAILABLE"),
    ("writable", "", "BUILDER_UNAVAILABLE"),
    ("pack-check", "", "BUILDER_UNAVAILABLE"),
    ("install", "ERROR: THESE PACKAGES DO NOT MATCH THE HASHES", "BUILD_FAILED"),
    ("check", "examplepkg 2.0 has requirement other>=3", "PIP_CHECK_FAILED"),
    ("pack", "", "BUILD_FAILED"),
    ("probe", "", "VERIFICATION_FAILED"),
])
async def test_each_failure_is_reported_with_its_code_and_leaves_nothing_registered(cluster, stage, output, code):
    service, run_id = cluster.service(), uuid4()
    cluster.configure(fail=stage, output=output)

    receipt = await service.build_environment("base", ("examplepkg>=1",), ("examplepkg",), run_id=run_id)

    assert receipt["status"] == "FAILED" and receipt["failure_code"] == code, receipt
    assert len(list((cluster.tmp_path / "sif").iterdir())) == 1  # Only the base SIF.
    assert all(job.state is JobLedgerState.COLLECTED for job in _jobs(cluster, run_id))
    assert len(_jobs(cluster, run_id)) == (0 if stage in {"resolve", "download"} else 1)
    for directory in ("jobs", "node-local", "login-tmp"):
        assert list((cluster.tmp_path / directory).iterdir()) == []


@pytest.mark.asyncio
async def test_a_missing_pip_cache_is_created_private_before_the_download(cluster):
    cache = cluster.tmp_path / "shared" / "pip-cache"
    service = cluster.service(cache_dir=cache)

    receipt = await service.build_environment("base", ("examplepkg>=1",), ("examplepkg",), run_id=uuid4())

    assert receipt["status"] == "SUCCEEDED", receipt
    assert stat.S_IMODE(cache.stat().st_mode) == 0o700
    resolve = next(call for call in cluster.calls() if "--dry-run" in call)
    assert f"{cache}:/labbio/cache:rw" in resolve


@pytest.mark.asyncio
async def test_a_pip_cache_others_can_write_makes_the_builder_unavailable(cluster):
    cache = cluster.tmp_path / "open-cache"
    cache.mkdir()
    cache.chmod(0o777)
    service = cluster.service(cache_dir=cache)

    receipt = await service.build_environment("base", ("examplepkg>=1",), ("examplepkg",), run_id=uuid4())

    assert receipt["failure_code"] == "BUILDER_UNAVAILABLE" and cluster.calls() == []


@pytest.mark.asyncio
async def test_a_requested_module_that_does_not_import_is_reported(cluster):
    service = cluster.service()

    receipt = await service.build_environment("base", ("examplepkg>=1",), ("missingmodule",), run_id=uuid4())

    assert receipt["failure_code"] == "IMPORT_CHECK_FAILED"
    assert receipt["diagnostics"][0]["module"] == "missingmodule"


@pytest.mark.asyncio
async def test_a_refused_submission_means_the_builder_is_unavailable(cluster):
    service, run_id = cluster.service(scheduler=FakeSlurm(cluster.tmp_path, refuse=True)), uuid4()

    receipt = await service.build_environment("base", ("examplepkg>=1",), ("examplepkg",), run_id=run_id)

    assert receipt["failure_code"] == "BUILDER_UNAVAILABLE"
    assert [job.state for job in _jobs(cluster, run_id)] == [JobLedgerState.NOT_SUBMITTED]
    assert list(cluster.jobs.iterdir()) == []


@pytest.mark.asyncio
async def test_a_build_over_its_time_limit_is_stopped(cluster):
    service = cluster.service(timeout=2.0)
    cluster.configure(hang="install")
    started = time.monotonic()

    receipt = await service.build_environment("base", ("examplepkg>=1",), ("examplepkg",), run_id=uuid4())

    assert receipt["failure_code"] == "BUILD_TIMEOUT"
    assert time.monotonic() - started < 60
    assert list((cluster.tmp_path / "node-local").iterdir()) == []


@pytest.mark.asyncio
async def test_a_crashed_build_job_is_cleaned_up_by_its_run_and_its_wheels_on_restart(cluster):
    held = FakeSlurm(cluster.tmp_path, hold=True)
    service, run_id = cluster.service(scheduler=held), uuid4()
    task = asyncio.create_task(service.build_environment("base", ("examplepkg>=1",), ("examplepkg",), run_id=run_id))
    deadline = time.monotonic() + 30
    while not held.calls:
        assert time.monotonic() < deadline
        await asyncio.sleep(0.05)
    task.cancel()  # The control plane is lost while the build job is queued.
    with pytest.raises(asyncio.CancelledError):
        await task
    [job] = _jobs(cluster, run_id)
    assert job.state is JobLedgerState.SUBMITTED and job.workspace_root.exists()
    for path in job.workspace_root.rglob("*"):
        if path.is_file():
            assert PROXY not in path.read_text(errors="replace")  # The build job never sees the proxy.

    results = await reconcile_orphan_jobs(ledger=cluster.ledger, launcher=cluster.launcher, run_id=run_id,
                                          environment_launcher=cluster.launcher)

    assert [(item.job_id, item.action) for item in results] == [(job.job_id, OrphanAction.CANCELLED)]
    assert held.jobs[job.job_id].cancelled and not job.workspace_root.exists()
    assert len(held.calls) == 1  # Nothing was resubmitted.
    crashed = _attempt(cluster)
    assert (crashed / "download" / "wheels").is_dir()
    receipt = await cluster.service().build_environment("base", ("examplepkg>=1",), ("examplepkg",), run_id=uuid4())
    assert receipt["status"] == "SUCCEEDED"
    assert not (crashed / "download" / "wheels").exists()  # Removed by the next build.


@pytest.mark.asyncio
async def test_builds_without_a_run_are_refused_before_any_job(cluster):
    service = cluster.service()

    receipt = await service.build_environment("base", ("examplepkg>=1",), ("examplepkg",))

    assert receipt["failure_code"] == "BUILD_FAILED"
    assert cluster.calls() == [] and list(cluster.jobs.iterdir()) == []


@pytest.mark.asyncio
async def test_a_changed_base_sif_is_refused(cluster):
    service = cluster.service()
    stored = cluster.sif_store.path_for(cluster.base.resolved_reference)
    stored.chmod(0o644)
    stored.write_text(stored.read_text() + " ")
    stored.chmod(0o444)

    receipt = await service.build_environment("base", ("examplepkg>=1",), ("examplepkg",), run_id=uuid4())

    assert receipt["failure_code"] == "VERIFICATION_FAILED" and cluster.calls() == []


def test_both_builders_implement_the_environment_builder_protocol():
    expected = inspect.signature(EnvironmentBuilder.build)
    assert inspect.signature(ApptainerEnvironmentBuilder.build) == expected
    assert inspect.signature(DockerEnvironmentBuilder.build) == expected


@pytest.mark.parametrize("change", [
    {"machine": "x86_64; id"}, {"proxy_url": "http://user:secret@proxy.example"},
    {"build_dir": "relative"}, {"download_timeout_seconds": 0},
])
def test_build_policy_refuses_unsafe_settings(change):
    values = dict(apptainer_binary=Path("/opt/apptainer/bin/apptainer"), machine="x86_64",
                  build_dir=Path("/tmp"), download_work_root=Path("/tmp"), resources=RequestedResources())
    values.update({key: Path(value) if key == "build_dir" else value for key, value in change.items()})
    with pytest.raises(ValueError):
        ApptainerBuildPolicy(**values)


def test_exactly_one_download_placement_is_required(cluster):
    builder = cluster.service().builder
    with pytest.raises(ValueError):
        ApptainerEnvironmentBuilder(sif_store=cluster.sif_store, policy=builder.policy,
                                    build_launcher=cluster.launcher, job_ledger=cluster.ledger)
    with pytest.raises(ValueError):
        ApptainerEnvironmentBuilder(sif_store=cluster.sif_store, policy=builder.policy,
                                    build_launcher=cluster.launcher, job_ledger=cluster.ledger,
                                    download_runner=builder.download_runner, download_launcher=cluster.launcher)
