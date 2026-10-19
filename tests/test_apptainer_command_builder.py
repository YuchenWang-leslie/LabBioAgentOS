"""C13 phase 2: Apptainer argv is fixed, isolated and refuses unsafe inputs."""

from __future__ import annotations

import json
import stat

import pytest

from labbioagentos import (
    ArtifactExposureClass,
    ArtifactRepresentation,
    ExecutionPlanRejected,
    ExecutionReceipt,
    RequestedResources,
    TraceEventType,
)
from labbioagentos.execution import ApptainerCommandBuilder, SifStoreError
from labbioagentos.execution.apptainer import ISOLATION_ARGUMENTS

from apptainer_fixtures import apptainer_environment, apptainer_image, bind_fields, register_sif
from test_phase6_docker_execution import FakeDockerRunner, _plan

EXPECTED_ENV = {
    "LABBIO_PARAMETERS_PATH=/labbio/parameters.json",
    "LABBIO_INPUT_MANIFEST_PATH=/labbio/input-manifest.json",
    "LABBIO_INPUT_IDENTITIES_PATH=/labbio/input-identities.json",
    "LABBIO_OUTPUT_DIR=/workspace/outputs",
    "LABBIO_INPUT_DIR=/labbio/inputs",
    "OMP_NUM_THREADS=1",
    "OPENBLAS_NUM_THREADS=1",
    "MKL_NUM_THREADS=1",
    "NUMEXPR_NUM_THREADS=1",
}


def _values(argv, flag):
    return [argv[index + 1] for index, item in enumerate(argv[:-1]) if item == flag]


def _input(store):
    return store.register(
        artifact_type="synthetic-input",
        exposure_class=ArtifactExposureClass.RAW,
        representation=ArtifactRepresentation(stored_content="fixture"),
    )


def test_argv_is_fixed_exec_with_isolation_defaults(tmp_path):
    store, executor, _, sif_store, reference = apptainer_environment(tmp_path, FakeDockerRunner())
    plan = _plan(input_artifact_ids=(_input(store).artifact_id,))

    argv = executor.build_command(plan)

    assert isinstance(argv, tuple)
    assert argv[:3] == ("apptainer", "--silent", "exec")
    start = argv.index(ISOLATION_ARGUMENTS[0])
    assert argv[start:start + len(ISOLATION_ARGUMENTS)] == ISOLATION_ARGUMENTS
    assert argv[argv.index("--network") + 1] == "none"
    scratch = tmp_path / "scratch" / plan.execution_id.hex
    assert _values(argv, "--workdir") == [str(scratch)]
    assert scratch.is_dir() and stat.S_IMODE(scratch.stat().st_mode) == 0o700
    assert _values(argv, "--pwd") == ["/workspace"]
    assert set(_values(argv, "--env")) == EXPECTED_ENV
    assert argv[-3:] == (str(sif_store.path_for(reference)), "python", "/labbio/script.py")
    for flag in ("--cpus", "--memory", "--pids-limit", "--writable", "--fakeroot", "--userns"):
        assert flag not in argv


def test_binds_are_read_only_except_the_output_directory(tmp_path):
    store, executor, _, _, _ = apptainer_environment(tmp_path, FakeDockerRunner())
    input_ref = _input(store)
    argv = executor.build_command(_plan(input_artifact_ids=(input_ref.artifact_id,)))

    binds = bind_fields(argv)
    writable = [(target, mode) for _, target, mode in binds if mode != "ro"]
    assert writable == [("/workspace/outputs", "rw")]
    targets = {target for _, target, _ in binds}
    assert {"/labbio/script.py", "/labbio/parameters.json", "/labbio/input-manifest.json",
            "/labbio/input-identities.json"} <= targets
    input_binds = [bind for bind in binds if str(input_ref.artifact_id) in bind[1]]
    assert len(input_binds) == 1 and input_binds[0][1].startswith("/labbio/inputs/")
    assert input_binds[0][2] == "ro"


def test_cgroup_flags_only_when_apptainer_enforces_limits(tmp_path):
    _, executor, _, _, _ = apptainer_environment(
        tmp_path, FakeDockerRunner(), resource_enforcement="apptainer-cgroups",
    )
    argv = executor.build_command(_plan(resources=RequestedResources(
        cpus=1.5, memory_mb=256, pids_limit=64, timeout_seconds=30,
    )))

    assert _values(argv, "--cpus") == ["1.5"]
    assert _values(argv, "--memory") == ["256M"]
    assert _values(argv, "--pids-limit") == ["64"]
    assert "OMP_NUM_THREADS=2" in _values(argv, "--env")


def test_import_probe_is_isolated_and_mounts_nothing(tmp_path):
    sif_store, reference = register_sif(tmp_path)
    builder = ApptainerCommandBuilder(
        sif_store, scratch_root=tmp_path / "scratch", resource_enforcement="scheduler",
    )
    from apptainer_fixtures import apptainer_image

    argv = builder.import_probe_argv(apptainer_image(reference), ("-I", "-S", "-c", "pass"))

    assert argv[:3] == ("apptainer", "--silent", "exec")
    assert argv[3:3 + len(ISOLATION_ARGUMENTS)] == ISOLATION_ARGUMENTS
    assert "--bind" not in argv and "--env" not in argv and "--workdir" not in argv
    assert argv[-6:] == (str(sif_store.path_for(reference)), "python", "-I", "-S", "-c", "pass")


def test_network_plan_is_rejected_even_when_policy_allows_it(tmp_path):
    _, executor, _, _, _ = apptainer_environment(
        tmp_path, FakeDockerRunner(), allow_network=True, image_network_allowed=True,
    )
    with pytest.raises(ExecutionPlanRejected):
        executor.build_command(_plan(network_required=True))


@pytest.mark.parametrize("character", [":", ","])
def test_bind_unsafe_paths_are_rejected(tmp_path, character):
    sif_store, _ = register_sif(tmp_path)
    with pytest.raises(ValueError):
        ApptainerCommandBuilder(
            sif_store, scratch_root=tmp_path / f"scratch{character}root",
            resource_enforcement="scheduler",
        )
    _, executor, _, _, _ = apptainer_environment(
        tmp_path / "env", FakeDockerRunner(), workspace_root=tmp_path / f"exec{character}root",
    )
    with pytest.raises(ValueError):
        executor.build_command(_plan())


@pytest.mark.parametrize("arguments", [
    {"apptainer_binary": "--privileged"},
    {"apptainer_binary": "apptainer exec"},
    {"apptainer_binary": ""},
    {"resource_enforcement": "none"},
])
def test_constructor_rejects_untrusted_configuration(tmp_path, arguments):
    sif_store, _ = register_sif(tmp_path)
    values = {"scratch_root": tmp_path / "scratch", "resource_enforcement": "scheduler", **arguments}
    with pytest.raises(ValueError):
        ApptainerCommandBuilder(sif_store, **values)


def test_verify_on_execute_rehashes_the_sif(tmp_path):
    _, executor, _, sif_store, reference = apptainer_environment(
        tmp_path, FakeDockerRunner(), verify_sif_on_execute=True,
    )
    stored = sif_store.path_for(reference)
    stored.chmod(0o644)
    stored.write_bytes(b"tampered")
    stored.chmod(0o444)

    with pytest.raises(SifStoreError):
        executor.build_command(_plan())


def test_default_builder_rejects_registered_sif_changed_after_registration(tmp_path):
    sif_store, reference = register_sif(tmp_path)
    stored = sif_store.path_for(reference)
    stored.chmod(0o644)
    stored.write_bytes(b"altered SIF fixture\n")
    stored.chmod(0o444)
    builder = ApptainerCommandBuilder(
        sif_store, scratch_root=tmp_path / "scratch", resource_enforcement="scheduler",
    )

    with pytest.raises(SifStoreError):
        builder.import_probe_argv(apptainer_image(reference), ("-I", "-S", "-c", "pass"))


def test_isolation_record_reaches_trusted_trace_but_not_the_receipt(tmp_path):
    record = {"backend": "apptainer", "resource_enforcement": "scheduler",
              "resource_limits_enforced_by_runtime": False}
    _, executor, sink, _, _ = apptainer_environment(
        tmp_path, FakeDockerRunner(), traced=True, isolation_record=record,
    )

    result = executor.execute(_plan())

    planned = [event for event in sink.read() if event.event_type is TraceEventType.EXECUTION_PLANNED]
    assert len(planned) == 1 and planned[0].payload["isolation"] == record
    receipt = json.dumps(ExecutionReceipt.from_result(result).model_dump(mode="json")).lower()
    for forbidden in ("apptainer", "sif", "scheduler", "isolation", str(tmp_path).lower()):
        assert forbidden not in receipt


def test_docker_trace_has_no_isolation_record(tmp_path):
    from test_phase6_docker_execution import _environment

    _, executor, sink = _environment(tmp_path, FakeDockerRunner(), traced=True)
    executor.execute(_plan())

    planned = [event for event in sink.read() if event.event_type is TraceEventType.EXECUTION_PLANNED]
    assert len(planned) == 1 and "isolation" not in planned[0].payload
