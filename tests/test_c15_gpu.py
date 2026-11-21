"""C15 step 8a: GPU requests, image GPU support and Docker GPUs (DECIDED-C15-F, refined 2026-11-16)."""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from labbioagentos import (
    ApprovedImage,
    ApprovedImageRegistry,
    ArtifactRegistrationPolicy,
    ExecutionPolicy,
    ExecutionRuntime,
    ExecutionWorkspaceManager,
    InMemoryTraceSink,
    LocalArtifactStore,
    MountResolver,
    OutputCollector,
    RequestedResources,
    RunTraceRecorder,
    TraceEventType,
)
from labbioagentos import ContainerExecutor
from labbioagentos.execution.docker import DockerCommandBuilder
from labbioagentos.execution.errors import ExecutionGpuImageMismatchError, ExecutionResourceLimitError
from labbioagentos.execution.gpu import DockerGpuInventory, GpuUnavailableError, parse_gpu_list
from labbioagentos.execution.models import GpuSupport

from test_phase6_docker_execution import FakeDockerRunner, _plan

UUID_A = "GPU-2da8c939-b98a-52ae-c45f-2ede2eb39349"
UUID_B = "GPU-0b1c2d3e-4f50-6172-8394-a5b6c7d8e9f0"
LISTING = (f"GPU 0: NVIDIA GeForce RTX 4060 Laptop GPU (UUID: {UUID_A})\n"
           f"GPU 1: NVIDIA A100-SXM4-80GB (UUID: {UUID_B})\n")


def _image(gpu=GpuSupport.NONE):
    return ApprovedImage(key="python-analysis", reference="local/python-fixture:3.11", digest="sha256:" + "1" * 64,
                         entry_points={ExecutionRuntime.PYTHON: ("python",)}, gpu=gpu)


def _run(stdout=LISTING, code=0, calls=None):
    def run(argv, **_options):
        if calls is not None:
            calls.append(argv)
        return SimpleNamespace(returncode=code, stdout=stdout, stderr="")
    return run


def _inventory(minimum=1, **kwargs):
    inventory = DockerGpuInventory("sha256:" + "1" * 64, minimum=minimum, run=_run(**kwargs))
    inventory.probe()
    return inventory


# --- The request and the policy ---


@pytest.mark.parametrize("value", [-1, 65, 1.0, "1", True])
def test_gpus_are_whole_non_negative_counts(value):
    with pytest.raises(ValidationError):
        RequestedResources(gpus=value)
    assert RequestedResources().gpus == 0


def test_gpus_are_bounded_by_the_policy_and_disabled_by_default():
    with pytest.raises(ExecutionResourceLimitError) as raised:
        ExecutionPolicy().validate_request(RequestedResources(gpus=1), network_required=False,
                                           image=_image(GpuSupport.SUPPORTED))
    assert raised.value.details.exceeded_fields == ("gpus",)
    assert raised.value.details.limits.gpus == 0
    ExecutionPolicy(max_gpus=2).validate_request(RequestedResources(gpus=2), network_required=False,
                                                 image=_image(GpuSupport.SUPPORTED))


@pytest.mark.parametrize("gpu, gpus, fits", [
    (GpuSupport.NONE, 0, True), (GpuSupport.NONE, 1, False),
    (GpuSupport.SUPPORTED, 0, True), (GpuSupport.SUPPORTED, 1, True),
    (GpuSupport.REQUIRED, 0, False), (GpuSupport.REQUIRED, 1, True),
])
def test_a_gpu_request_must_fit_the_image(gpu, gpus, fits):
    validate = lambda: ExecutionPolicy(max_gpus=1).validate_request(  # noqa: E731
        RequestedResources(gpus=gpus), network_required=False, image=_image(gpu))
    if fits:
        validate()
        return
    with pytest.raises(ExecutionGpuImageMismatchError) as raised:
        validate()
    assert (raised.value.requested_gpus, raised.value.image_gpu) == (gpus, gpu.value)


@pytest.mark.parametrize("gpu, phrase", [("NONE", "request gpus = 0"), ("REQUIRED", "gpus of 1 or more")])
def test_the_agent_is_told_which_way_the_request_does_not_fit(gpu, phrase):
    from labbioagentos.runtime.tooling import LabBioRuntimeToolSet

    error = LabBioRuntimeToolSet._safe_error(ExecutionGpuImageMismatchError(1 if gpu == "NONE" else 0, gpu))
    assert error.error_code == "EXECUTION_GPU_IMAGE_MISMATCH"
    assert phrase in error.safe_message and "environment_list" in error.safe_message


def test_an_image_carries_its_gpu_support_from_its_record_and_from_json():
    from test_c15_promotion import _record

    record = _record().model_copy(update={"gpu": GpuSupport.REQUIRED})
    from labbioagentos.execution.catalog import ExecutionBackendName

    assert record.approved_image(ExecutionBackendName.DOCKER).gpu is GpuSupport.REQUIRED
    image = ApprovedImage.model_validate_json(_image(GpuSupport.SUPPORTED).model_dump_json())
    assert image.gpu is GpuSupport.SUPPORTED
    # A stored record arrives as plain JSON values; the strict model still reads the flag.
    image = ApprovedImage.model_validate(_image(GpuSupport.REQUIRED).model_dump(mode="json"))
    assert image.gpu is GpuSupport.REQUIRED
    assert _image().gpu is GpuSupport.NONE


# --- The GPU inventory ---


def test_the_gpu_list_is_read_strictly():
    devices = parse_gpu_list(LISTING)
    assert [(device.index, device.uuid) for device in devices] == [(0, UUID_A), (1, UUID_B)]
    assert devices[0].record() == {"index": 0, "name": "NVIDIA GeForce RTX 4060 Laptop GPU", "uuid": UUID_A}
    long_listing = "".join(f"GPU {index}: NVIDIA A100-SXM4-80GB-with-a-long-model-name (UUID: GPU-{index:08d}"
                           "-0000-0000-0000-000000000000)\n" for index in range(800))
    for text in ("No devices were found\n", LISTING + LISTING.splitlines()[0] + "\n",
                 "GPU 0: x (UUID: GPU-$(reboot))\n", long_listing):
        with pytest.raises(ValueError):
            parse_gpu_list(text)


def test_the_probe_runs_nvidia_smi_in_a_closed_container_with_every_gpu():
    calls = []
    inventory = DockerGpuInventory("sha256:" + "1" * 64, minimum=2, run=_run(calls=calls))

    record = inventory.probe()

    argv = calls[0]
    assert argv[argv.index("--gpus") + 1] == "all" and argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--user") + 1] == "65534:65534" and "--read-only" in argv
    assert argv[-3:] == ["nvidia-smi", "sha256:" + "1" * 64, "-L"]
    assert argv[argv.index("--entrypoint") + 1] == "nvidia-smi"
    assert record == {"backend": "docker", "gpus": [device.record() for device in parse_gpu_list(LISTING)]}
    assert [device.uuid for device in inventory.assign(1)] == [UUID_A]
    assert inventory.assign(0) == ()
    with pytest.raises(GpuUnavailableError):
        inventory.assign(3)  # More than the host has.


@pytest.mark.parametrize("kwargs", [dict(code=1), dict(stdout="garbage\n"), dict(stdout=LISTING, minimum=3)])
def test_a_missing_or_short_gpu_runtime_refuses_executions(kwargs):
    minimum = kwargs.pop("minimum", 1)
    with pytest.raises(GpuUnavailableError):
        DockerGpuInventory("sha256:" + "1" * 64, minimum=minimum, run=_run(**kwargs)).probe()


def test_the_probe_failing_to_start_or_never_run_is_unavailable():
    def broken(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("docker", 120)

    with pytest.raises(GpuUnavailableError):
        DockerGpuInventory("i", minimum=1, run=broken).probe()
    with pytest.raises(GpuUnavailableError):
        DockerGpuInventory("i", minimum=1, run=_run()).assign(1)  # Not probed.
    with pytest.raises(ValueError):
        DockerGpuInventory("i", minimum=0)


# --- Docker executions ---


def test_docker_names_exactly_the_requested_devices_only_when_asked(tmp_path):
    builder = DockerCommandBuilder(gpu_inventory=_inventory(minimum=2))
    assert "--gpus" not in builder.build(_plan(), _image(), *_workspace(tmp_path, _plan()))
    plan = _plan(resources=RequestedResources(gpus=2))
    argv = builder.build(plan, _image(GpuSupport.SUPPORTED), *_workspace(tmp_path / "two", plan))
    assert argv[argv.index("--gpus") + 1] == f'"device={UUID_A},{UUID_B}"'
    plan = _plan(resources=RequestedResources(gpus=1))
    with pytest.raises(GpuUnavailableError):  # GPUs were never set up for this builder.
        DockerCommandBuilder().build(plan, _image(GpuSupport.SUPPORTED), *_workspace(tmp_path / "none", plan))


def _workspace(tmp_path, plan):
    return ExecutionWorkspaceManager(tmp_path / "executions").prepare(plan, ()), ()


def _executor(tmp_path, runner, *, gpu=GpuSupport.SUPPORTED, backend_check=None, builder=None):
    sink = InMemoryTraceSink()
    recorder = RunTraceRecorder(sink)
    store = LocalArtifactStore(tmp_path / "artifacts", trace_recorder=recorder)
    executor = ContainerExecutor(
        store=store, image_registry=ApprovedImageRegistry((_image(gpu),)),
        execution_policy=ExecutionPolicy(max_gpus=1),
        mount_resolver=MountResolver(store, approved_input_roots=(store.root,)),
        workspace_manager=ExecutionWorkspaceManager(tmp_path / "executions"),
        output_collector=OutputCollector(store, ArtifactRegistrationPolicy(()), trace_recorder=recorder),
        process_runner=runner, trace_recorder=recorder, command_builder=builder or DockerCommandBuilder(),
        backend_check=backend_check,
    )
    return executor, sink


def test_trace_records_the_request_and_the_exact_devices(tmp_path):
    inventory = DockerGpuInventory("sha256:" + "1" * 64, minimum=1, run=_run())
    runner = FakeDockerRunner()
    executor, sink = _executor(tmp_path, runner, builder=DockerCommandBuilder(gpu_inventory=inventory),
                               backend_check=inventory.probe)

    executor.execute(_plan(resources=RequestedResources(gpus=1)))

    planned = next(event for event in sink.read() if event.event_type is TraceEventType.EXECUTION_PLANNED
                   and event.status == "PLANNED")
    assert planned.payload["resources"]["gpus"] == 1
    assert planned.payload["gpu_devices"] == [parse_gpu_list(LISTING)[0].record()]
    assert planned.payload["isolation"]["gpus"][1]["uuid"] == UUID_B  # The whole inventory, once.
    argv = runner.calls[0][0]
    assert argv[argv.index("--gpus") + 1] == f'"device={UUID_A}"'


def test_without_a_gpu_runtime_nothing_runs_on_cpu(tmp_path):
    inventory = DockerGpuInventory("sha256:" + "1" * 64, minimum=1, run=_run(code=125))
    runner = FakeDockerRunner()
    executor, _sink = _executor(tmp_path, runner, builder=DockerCommandBuilder(gpu_inventory=inventory),
                                backend_check=inventory.probe)
    from labbioagentos import ContainerStartError

    with pytest.raises(ContainerStartError):
        executor.execute(_plan(resources=RequestedResources(gpus=1)))
    assert runner.calls == []


def test_an_execution_without_gpus_has_no_gpu_trace(tmp_path):
    runner = FakeDockerRunner()
    executor, sink = _executor(tmp_path, runner, gpu=GpuSupport.NONE)
    executor.execute(_plan())
    planned = next(event for event in sink.read() if event.status == "PLANNED")
    assert planned.payload["resources"]["gpus"] == 0 and "gpu_devices" not in planned.payload
    assert "--gpus" not in runner.calls[0][0]


# --- Configuration ---


def _settings(tmp_path, *, gpus=0, images=None, execution='default_image = "python-base"', extra=""):
    from labbioagentos.local_config import load_settings
    from test_c15_image_identity import TWO_IMAGES, _write

    path = _write(tmp_path, execution, images if images is not None else TWO_IMAGES)
    text = path.read_text().replace("timeout_seconds = 60.0", f"timeout_seconds = 60.0\ngpus = {gpus}")
    path.write_text(text + extra)
    return load_settings(path)


def test_configured_images_declare_gpu_support_and_the_default_never_requires_it(tmp_path):
    from labbioagentos.local_config import _image_catalog
    from test_c15_image_identity import TWO_IMAGES

    images = TWO_IMAGES.replace('key = "tools"', 'key = "tools"\ngpu = "SUPPORTED"')
    settings = _settings(tmp_path, images=images)
    assert {image.key: image.gpu for image in settings.execution.approved_images()} == {
        "python-base": GpuSupport.NONE, "tools": GpuSupport.SUPPORTED}
    assert _image_catalog(settings).describe("tools")["gpu"] == "SUPPORTED"
    with pytest.raises(ValidationError, match="must not require GPUs"):
        _settings(tmp_path, images=TWO_IMAGES.replace('key = "python-base"', 'key = "python-base"\ngpu = "REQUIRED"'))
    with pytest.raises(ValidationError):
        _settings(tmp_path, images=TWO_IMAGES.replace('key = "tools"', 'key = "tools"\ngpu = "MAYBE"'))


def test_the_configured_gpu_count_is_the_limit_and_the_default_request_uses_none(tmp_path):
    from labbioagentos import cli
    from labbioagentos.local_config import build_application

    settings = _settings(tmp_path, gpus=1)
    application = build_application(settings, tmp_path / "run", load_provider=False)
    try:
        assert application.execution_policy.max_gpus == 1
        assert application.configuration.execution_profile.resources.gpus == 0
        executor = application.container_executor
        assert isinstance(executor.command_builder.gpu_inventory, DockerGpuInventory)
        assert executor.backend_check == executor.command_builder.gpu_inventory.probe
    finally:
        cli._close(application)
    (tmp_path / "off").mkdir()
    application = build_application(_settings(tmp_path / "off"), tmp_path / "off-run", load_provider=False)
    try:
        assert application.execution_policy.max_gpus == 0 and application.container_executor.backend_check is None
    finally:
        cli._close(application)


def test_local_apptainer_cannot_be_given_gpus(tmp_path):
    from labbioagentos.local_config import load_settings
    from test_apptainer_configuration import _write

    path = _write(tmp_path, launcher="", enforcement="systemd-cgroups")
    path.write_text(path.read_text().replace("timeout_seconds = 60.0", "timeout_seconds = 60.0\ngpus = 1"))
    with pytest.raises(ValidationError, match="does not support GPUs"):
        load_settings(path)
