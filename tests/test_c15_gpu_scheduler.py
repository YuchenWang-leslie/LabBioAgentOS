"""C15 step 8b: GPUs under a scheduler and Apptainer (DECIDED-C15-F, refined 2026-11-16)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from labbioagentos import (
    ExecutionWorkspaceManager,
    ProcessOutcome,
    RequestedResources,
    TraceEventType,
)
from labbioagentos.execution import ApptainerCommandBuilder
from labbioagentos.execution.gpu import parse_gpu_allocation
from labbioagentos.execution.launchers import BUILTIN_TEMPLATES, JobSpec, SchedulerLauncher, SchedulerTemplate
from labbioagentos.execution.launchers.job_script import SchedulerPolicy, render_job_script
from labbioagentos.execution.launchers.scheduler import check_scheduler_client
from labbioagentos.execution.models import GpuSupport

from apptainer_fixtures import apptainer_image, register_sif
from scheduler_fixtures import FakeSlurm, outcome
from test_c15_gpu import LISTING, UUID_A, UUID_B
from test_phase6_docker_execution import FakeDockerRunner, _plan
from test_scheduler_launcher import _policy
from test_scheduler_templates import CheckClient, _bin

POLICY = SchedulerPolicy(max_output_file_bytes=1 << 20, partition="cpu", gpu_partition="gpu")


def _script(template="slurm", gpus=0, policy=POLICY, cpus=4.0):
    spec = JobSpec(execution_id=uuid4(), argv=("/opt/apptainer", "exec", "x.sif"), timeout_seconds=60.0,
                   resources=RequestedResources(gpus=gpus, cpus=cpus), workspace_root=Path("/w/x"))
    return render_job_script(BUILTIN_TEMPLATES[template] if isinstance(template, str) else template,
                             policy, spec, Path("/w/x/job"), {"PATH": "/usr/bin"})


def _directives(script, prefix):
    return [line.removeprefix(prefix + " ") for line in script.splitlines() if line.startswith(prefix + " ")]


# --- Directives ---


def test_a_slurm_gpu_job_asks_for_its_gpus_in_the_gpu_partition():
    directives = _directives(_script(gpus=2), "#SBATCH")
    assert "--gpus=2" in directives and "--partition=gpu" in directives and "--partition=cpu" not in directives
    plain = _directives(_script(), "#SBATCH")
    assert not any(item.startswith("--gpus") for item in plain) and "--partition=cpu" in plain
    # Without a GPU partition, a GPU job goes to the ordinary one.
    default = _directives(_script(gpus=1, policy=SchedulerPolicy(max_output_file_bytes=1, partition="cpu")),
                          "#SBATCH")
    assert "--partition=cpu" in default and "--gpus=1" in default


def test_pbs_states_gpus_in_the_same_select_chunk_and_lsf_with_its_gpu_option():
    pbs = _directives(_script("pbs", gpus=2), "#PBS")
    assert "-l select=1:ncpus=4:mem=512mb:ngpus=2" in pbs
    assert "-l select=1:ncpus=4:mem=512mb" not in pbs  # Replaced, not stated twice.
    assert "-l select=1:ncpus=4:mem=512mb" in _directives(_script("pbs"), "#PBS")
    assert '-gpu "num=2"' in _directives(_script("lsf", gpus=2), "#BSUB")
    assert not any(item.startswith("-gpu") for item in _directives(_script("lsf"), "#BSUB"))


def test_a_template_that_cannot_request_gpus_never_runs_a_gpu_job_without_them():
    directives = {key: value for key, value in BUILTIN_TEMPLATES["slurm"].directives.items() if key != "gpus"}
    site = BUILTIN_TEMPLATES["slurm"].model_copy(update={"directives": directives})
    _script(site)
    with pytest.raises(ValueError, match="cannot request GPUs"):
        _script(site, gpus=1)


@pytest.mark.parametrize("changes", [{"gpu_replaces": ("cpus",)}, {"gpu_replaces": ("gpus",)}])
def test_a_template_replacing_directives_must_state_gpus_elsewhere(changes):
    values = BUILTIN_TEMPLATES["slurm"].model_dump()
    if changes["gpu_replaces"] == ("cpus",):
        values["directives"].pop("gpus")
    with pytest.raises(ValidationError):
        SchedulerTemplate.model_validate({**values, **changes})


def test_only_a_gpu_job_records_its_allocation_and_passes_its_devices_on():
    script = _script(gpus=1)
    assert "/w/x/job/gpus.txt" in script and "nvidia-smi -L" in script
    assert '${CUDA_VISIBLE_DEVICES+"APPTAINERENV_CUDA_VISIBLE_DEVICES=$CUDA_VISIBLE_DEVICES"}' in script
    record_line = next(index for index, line in enumerate(script.splitlines()) if "gpus.txt" in line)
    run_line = next(index for index, line in enumerate(script.splitlines()) if line.startswith("timeout "))
    assert record_line < run_line  # Recorded before the program starts.
    plain = _script()
    assert "gpus.txt" not in plain and "CUDA_VISIBLE_DEVICES" not in plain


# --- The job, run for real by a fake scheduler ---


def _fake_nvidia_smi(scheduler):
    path = scheduler.shim_dir / "nvidia-smi"
    path.write_text("#!/bin/sh\n[ \"$1\" = -L ] || exit 2\nprintf '%s' \"$LISTING\"\n")
    path.chmod(0o755)


@pytest.mark.parametrize("visible", ["1", None])
def test_a_scheduler_job_reports_what_the_node_allocated(tmp_path, visible):
    environment = {"LISTING": LISTING.splitlines()[1] + "\n"}
    if visible is not None:
        environment["CUDA_VISIBLE_DEVICES"] = visible
    scheduler = FakeSlurm(tmp_path, job_environment=environment)
    _fake_nvidia_smi(scheduler)
    launcher = SchedulerLauncher(template=BUILTIN_TEMPLATES["slurm"], policy=_policy(),
                                 workspace_root=tmp_path / "jobs", command_runner=scheduler)
    execution_id = uuid4()
    workspace = tmp_path / "jobs" / str(execution_id)
    workspace.mkdir(parents=True)
    spec = JobSpec(execution_id=execution_id, argv=("/usr/bin/env",), timeout_seconds=30.0,
                   resources=RequestedResources(gpus=1), workspace_root=workspace)

    async def run():
        handle = await launcher.submit(spec)
        await launcher.wait(handle)
        return await launcher.collect(handle)

    result = asyncio.run(run())

    program_environment = result.stdout.decode()
    # The program's environment is cleared; the device list reaches it only through Apptainer.
    assert not any(line.startswith("CUDA_VISIBLE_DEVICES=") for line in program_environment.splitlines())
    if visible is None:
        assert "APPTAINERENV_CUDA_VISIBLE_DEVICES" not in program_environment
        assert result.gpu_allocation == {"cuda_visible_devices": "", "valid": True,
                                         "devices": [parse_gpu_allocation("CUDA_VISIBLE_DEVICES=\n" + LISTING)
                                                     ["devices"][1]]}
        return
    assert "APPTAINERENV_CUDA_VISIBLE_DEVICES=1" in program_environment
    assert result.gpu_allocation["cuda_visible_devices"] == "1" and result.gpu_allocation["valid"]
    assert [device["uuid"] for device in result.gpu_allocation["devices"]] == [UUID_B]


def test_a_job_without_gpus_reports_no_allocation_and_a_linked_record_is_ignored(tmp_path):
    scheduler = FakeSlurm(tmp_path)
    launcher = SchedulerLauncher(template=BUILTIN_TEMPLATES["slurm"], policy=_policy(),
                                 workspace_root=tmp_path / "jobs", command_runner=scheduler)

    async def run(resources, link=False):
        execution_id = uuid4()
        workspace = tmp_path / "jobs" / str(execution_id)
        workspace.mkdir(parents=True)
        handle = await launcher.submit(JobSpec(execution_id=execution_id, argv=("/bin/true",),
                                               timeout_seconds=30.0, resources=resources,
                                               workspace_root=workspace))
        await launcher.wait(handle)
        if link:
            target = tmp_path / "forged.txt"
            target.write_text("CUDA_VISIBLE_DEVICES=7\n")
            (workspace / "job" / "gpus.txt").unlink(missing_ok=True)
            (workspace / "job" / "gpus.txt").symlink_to(target)
        return await launcher.collect(handle)

    assert asyncio.run(run(RequestedResources())).gpu_allocation is None
    assert asyncio.run(run(RequestedResources(gpus=1), link=True)).gpu_allocation is None


def test_an_allocation_record_is_read_strictly():
    assert parse_gpu_allocation("CUDA_VISIBLE_DEVICES=0,1\n" + LISTING) == {
        "cuda_visible_devices": "0,1", "valid": True,
        "devices": [{"index": 0, "name": "NVIDIA GeForce RTX 4060 Laptop GPU", "uuid": UUID_A},
                    {"index": 1, "name": "NVIDIA A100-SXM4-80GB", "uuid": UUID_B}]}
    assert parse_gpu_allocation("CUDA_VISIBLE_DEVICES=MIG-1/2:3\n")["cuda_visible_devices"] == "MIG-1/2:3"
    unsafe = parse_gpu_allocation("CUDA_VISIBLE_DEVICES=$(reboot)\n" + LISTING)
    assert unsafe["cuda_visible_devices"] is None and not unsafe["valid"]
    garbled = parse_gpu_allocation("CUDA_VISIBLE_DEVICES=0\nnvidia-smi: command failed\n")
    assert garbled["devices"] is None and not garbled["valid"]
    assert parse_gpu_allocation("") == {"cuda_visible_devices": None, "devices": None, "valid": False}
    headless = parse_gpu_allocation(LISTING)  # A listing without the device line is not trusted either.
    assert headless["devices"] is None and not headless["valid"]


# --- Trace ---


class AllocatingRunner(FakeDockerRunner):
    def __init__(self, allocation, **kwargs):
        super().__init__(**kwargs)
        self.allocation = allocation

    def run(self, argv, *, timeout_seconds):
        result = super().run(argv, timeout_seconds=timeout_seconds)
        return ProcessOutcome(**{**result.__dict__, "gpu_allocation": self.allocation})


@pytest.mark.parametrize("exit_code, event_type", [(0, TraceEventType.EXECUTION_COMPLETED),
                                                   (3, TraceEventType.EXECUTION_FAILED)])
def test_the_allocation_joins_the_executions_final_record(tmp_path, exit_code, event_type):
    from test_c15_gpu import _executor

    allocation = parse_gpu_allocation("CUDA_VISIBLE_DEVICES=0\n" + LISTING.splitlines()[0])
    executor, sink = _executor(tmp_path, AllocatingRunner(allocation, exit_code=exit_code), gpu=GpuSupport.NONE)
    executor.execute(_plan())

    final = next(event for event in sink.read() if event.event_type is event_type)
    assert final.payload["gpu_allocation"] == allocation
    assert all("gpu_allocation" not in event.payload for event in sink.read() if event.event_type is not event_type)
    assert executor._gpu_allocations == {}
    executor, sink = _executor(tmp_path / "plain", FakeDockerRunner(), gpu=GpuSupport.NONE)
    executor.execute(_plan())
    assert all("gpu_allocation" not in event.payload for event in sink.read())


# --- Apptainer ---


def test_apptainer_adds_nv_only_for_a_gpu_request(tmp_path):
    sif_store, reference = register_sif(tmp_path)
    builder = ApptainerCommandBuilder(sif_store, scratch_root=tmp_path / "scratch", resource_enforcement="scheduler")
    image = apptainer_image(reference).model_copy(update={"gpu": GpuSupport.SUPPORTED})
    manager = ExecutionWorkspaceManager(tmp_path / "executions")
    for gpus in (0, 1):
        plan = _plan(resources=RequestedResources(gpus=gpus))
        argv = builder.build(plan, image, manager.prepare(plan, ()), ())
        assert ("--nv" in argv) is bool(gpus)
        assert argv.index("--containall") < (argv.index("--nv") if gpus else len(argv))


# --- Configuration and the backend check ---


def test_the_gpu_partition_is_a_scheduler_setting(tmp_path):
    from labbioagentos.local_config import load_settings, scheduler_policy
    from test_apptainer_configuration import LAUNCHER, _write

    settings = load_settings(_write(tmp_path, launcher=LAUNCHER + 'gpu_partition = "gpu"\n'))
    assert scheduler_policy(settings).gpu_partition == "gpu"
    with pytest.raises(ValueError):
        load_settings(_write(tmp_path, launcher=LAUNCHER + 'gpu_partition = "gpu; rm -rf"\n'))
    with pytest.raises(ValidationError):
        load_settings(_write(tmp_path, launcher='[launcher]\nkind = "local"\ngpu_partition = "gpu"\n',
                             enforcement="systemd-cgroups"))


def test_gpus_need_a_template_that_can_request_them(tmp_path, monkeypatch):
    from labbioagentos.execution import launchers
    from labbioagentos.local_config import load_settings
    from test_apptainer_configuration import _write

    directives = {key: value for key, value in BUILTIN_TEMPLATES["slurm"].directives.items() if key != "gpus"}
    monkeypatch.setitem(launchers.BUILTIN_TEMPLATES, "slurm",
                        BUILTIN_TEMPLATES["slurm"].model_copy(update={"directives": directives}))
    path = _write(tmp_path)
    load_settings(path)
    path.write_text(path.read_text().replace("timeout_seconds = 60.0", "timeout_seconds = 60.0\ngpus = 1"))
    with pytest.raises(ValueError, match="no gpus directive"):
        load_settings(path)


@pytest.mark.parametrize("result, expected", [(outcome(), "ACCEPTED"),
                                              (outcome(1, stderr=b"bad gres\n"), "REJECTED")])
def test_the_scheduler_check_validates_a_gpu_job_when_gpus_are_allowed(tmp_path, result, expected):
    client = CheckClient(_bin(tmp_path, ("sbatch", "squeue", "scancel")), result)

    report = check_scheduler_client(BUILTIN_TEMPLATES["slurm"], POLICY, tmp_path, client, gpus=True)

    assert report["gpu_test_submission"] == expected and len(client.scripts) == 2
    assert "#SBATCH --gpus=1" in client.scripts[1] and "#SBATCH --partition=gpu" in client.scripts[1]
    assert "--gpus" not in client.scripts[0]
    if expected == "REJECTED":
        assert report["gpu_detail"] == "bad gres"
    assert "gpu_test_submission" not in check_scheduler_client(BUILTIN_TEMPLATES["slurm"], POLICY, tmp_path,
                                                                CheckClient(_bin(tmp_path, ()), outcome()))


def test_backend_check_fails_on_a_rejected_gpu_job(tmp_path):
    from labbioagentos import cli
    from labbioagentos.local_backend import check_backend
    from labbioagentos.local_config import load_settings
    from test_apptainer_configuration import _capabilities, _sif, _write

    config = _write(tmp_path)
    config.write_text(config.read_text().replace("timeout_seconds = 60.0", "timeout_seconds = 60.0\ngpus = 1"))
    cli.main(["image-register", "--config", str(config), "--sif", str(_sif(tmp_path))])
    seen = []

    def scheduler_check(template, policy, work_root, *, gpus=False):
        seen.append(gpus)
        return {"template": "slurm", "clients": {"sbatch": True}, "test_submission": "ACCEPTED",
                "gpu_test_submission": "REJECTED"}

    report, ok = check_backend(load_settings(config), probe=lambda **_: _capabilities(),
                               scheduler_check=scheduler_check)
    assert seen == [True] and not ok and "scheduler_gpu_submission" in report["configuration_failures"]
