"""C13 phase 2: Docker and Apptainer argv satisfy one shared sandbox contract."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from labbioagentos import ArtifactExposureClass, ArtifactRepresentation

from apptainer_fixtures import apptainer_environment, apptainer_image, bind_fields, register_sif
from test_phase6_docker_execution import FakeDockerRunner, _environment, _image, _plan

SECRET = "sk-contract-sentinel-0123456789abcdef"
ALLOWED_ENV = {
    "LABBIO_PARAMETERS_PATH", "LABBIO_INPUT_MANIFEST_PATH", "LABBIO_INPUT_IDENTITIES_PATH",
    "LABBIO_OUTPUT_DIR", "LABBIO_INPUT_DIR",
    "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS",
}


@dataclass(frozen=True)
class Sandbox:
    network_disabled: bool
    mounts: tuple[tuple[str, str, bool], ...]  # (source, target, read_only)
    env_names: frozenset[str]
    program: tuple[str, ...]


def _values(argv, flag):
    return [argv[index + 1] for index, item in enumerate(argv[:-1]) if item == flag]


def _docker(argv) -> Sandbox:
    mounts = []
    for value in _values(argv, "--mount"):
        fields = dict(part.split("=", 1) for part in value.split(",") if "=" in part)
        mounts.append((fields["source"], fields["target"], value.endswith(",readonly")))
    image = argv.index(_image().resolved_reference)
    return Sandbox(
        network_disabled=_values(argv, "--network") == ["none"],
        mounts=tuple(mounts),
        env_names=frozenset(item.split("=", 1)[0] for item in _values(argv, "--env")),
        program=tuple(argv[image + 1:]),
    )


def _apptainer(argv) -> Sandbox:
    sif = next(index for index, item in enumerate(argv) if item.endswith(".sif"))
    return Sandbox(
        network_disabled="--net" in argv and _values(argv, "--network") == ["none"],
        mounts=tuple((source, target, mode == "ro") for source, target, mode in bind_fields(argv)),
        env_names=frozenset(item.split("=", 1)[0] for item in _values(argv, "--env")),
        program=tuple(argv[sif + 1:]),
    )


def _build(backend, tmp_path, *, workspace_root=None):
    runner = FakeDockerRunner()
    if backend == "docker":
        store, executor, _ = _environment(tmp_path, runner)
        if workspace_root is not None:
            from labbioagentos import ExecutionWorkspaceManager
            executor.workspace_manager = ExecutionWorkspaceManager(workspace_root)
        return store, executor, _docker
    store, executor, *_ = apptainer_environment(tmp_path, runner, workspace_root=workspace_root)
    return store, executor, _apptainer


BACKENDS = ("docker", "apptainer")


@pytest.mark.parametrize("backend", BACKENDS)
def test_shared_sandbox_contract(backend, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    monkeypatch.setenv("APPTAINERENV_OPENAI_API_KEY", SECRET)
    store, executor, parse = _build(backend, tmp_path)
    input_ref = store.register(
        artifact_type="synthetic-input",
        exposure_class=ArtifactExposureClass.RAW,
        representation=ArtifactRepresentation(stored_content="fixture"),
    )

    argv = executor.build_command(_plan(input_artifact_ids=(input_ref.artifact_id,)))
    sandbox = parse(argv)

    assert sandbox.network_disabled
    writable = [target for _, target, read_only in sandbox.mounts if not read_only]
    assert writable == ["/workspace/outputs"]
    inputs = [mount for mount in sandbox.mounts if str(input_ref.artifact_id) in mount[1]]
    assert len(inputs) == 1 and inputs[0][1].startswith("/labbio/inputs/") and inputs[0][2]
    assert sandbox.env_names == ALLOWED_ENV
    assert sandbox.program == ("python", "/labbio/script.py")
    flattened = "\0".join(argv)
    assert SECRET not in flattened and "OPENAI_API_KEY" not in flattened
    assert "/var/run/docker.sock" not in flattened and "--privileged" not in argv


@pytest.mark.parametrize("backend, character", [
    ("docker", ","), ("apptainer", ","), ("apptainer", ":"),
])
def test_mount_syntax_injection_is_rejected(backend, character, tmp_path):
    _, executor, _ = _build(backend, tmp_path, workspace_root=tmp_path / f"exec{character}root")
    with pytest.raises(ValueError):
        executor.build_command(_plan())


@pytest.mark.parametrize("backend", BACKENDS)
def test_import_probe_is_network_disabled_and_mounts_nothing(backend, tmp_path):
    if backend == "docker":
        from labbioagentos import DockerCommandBuilder
        image, builder = _image(), DockerCommandBuilder()
    else:
        from labbioagentos.execution import ApptainerCommandBuilder
        sif_store, reference = register_sif(tmp_path)
        image = apptainer_image(reference)
        builder = ApptainerCommandBuilder(
            sif_store, scratch_root=tmp_path / "scratch", resource_enforcement="scheduler",
        )

    argv = builder.import_probe_argv(image, ("-I", "-S", "-c", "pass"))

    assert _values(argv, "--network") == ["none"]
    assert not {"--mount", "--volume", "--bind", "--env"} & set(argv)
    assert argv[-4:] == ("-I", "-S", "-c", "pass")
