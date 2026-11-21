"""Apptainer is the default; Docker compatibility requires explicit selection."""

import json
from pathlib import Path
import tomllib

import pytest

from labbioagentos import ContainerStartError, application as application_module, cli
from labbioagentos.execution.apptainer import SubprocessApptainerRunner
from labbioagentos.execution import ApptainerCommandBuilder, DockerProcessRunner
from labbioagentos.execution.launchers import SchedulerLauncher
from labbioagentos.execution.launchers.systemd import SystemdLocalLauncher, SystemdProbeRunner
from labbioagentos.local_config import LocalSettings, build_application, load_settings, runtime_manifest
from test_apptainer_configuration import _write
from test_phase6_docker_execution import _plan


@pytest.mark.parametrize("placement", ["local", "scheduler"])
def test_omitted_backend_selects_apptainer_without_inferring_from_placement(tmp_path, monkeypatch, placement):
    arguments = {"launcher": "", "enforcement": "systemd-cgroups"} if placement == "local" else {}
    settings = load_settings(_write(tmp_path, backend="", **arguments))
    assert settings.execution.backend == "apptainer"
    assert runtime_manifest(settings)["configuration"]["execution"]["backend"] == "apptainer"
    def docker_forbidden(*args, **kwargs):
        pytest.fail("The default Apptainer composition must not construct a Docker compatibility component")

    monkeypatch.setattr(application_module, "SubprocessDockerRunner", docker_forbidden)
    monkeypatch.setattr(application_module, "DockerCommandBuilder", docker_forbidden)
    application = build_application(settings, tmp_path / "run", load_provider=False)
    try:
        assert isinstance(application.container_executor.command_builder, ApptainerCommandBuilder)
        assert application.container_executor.command_builder is application.configuration.command_builder
        assert application.container_executor.process_runner is application.configuration.process_runner
        runner_type = SystemdProbeRunner if placement == "local" else SubprocessApptainerRunner
        assert isinstance(application.container_executor.process_runner, runner_type)
        launcher_type = SystemdLocalLauncher if placement == "local" else SchedulerLauncher
        assert isinstance(application.container_executor.launcher, launcher_type)
    finally:
        cli._close(application)


def test_legacy_implicit_docker_configuration_requires_explicit_migration(tmp_path):
    with pytest.raises(ValueError) as caught:
        load_settings(_write(tmp_path, backend="", apptainer=False))
    assert getattr(caught.value, "code", None) == "APPTAINER_SETTINGS_REQUIRED"
    assert "docker" in caught.value.hint


def test_explicit_docker_stays_docker_in_persisted_configuration(tmp_path):
    settings = load_settings(_write(tmp_path, backend='backend = "docker"', apptainer=False))
    manifest = runtime_manifest(settings)
    assert settings.execution.backend == "docker"
    assert manifest["configuration"]["execution"]["backend"] == "docker"
    assert "apptainer" not in manifest["configuration"]["execution"]


@pytest.mark.asyncio
async def test_default_apptainer_probe_failure_never_falls_back_to_docker(tmp_path, monkeypatch):
    settings = load_settings(_write(tmp_path, backend="", launcher="", enforcement="systemd-cgroups"))
    application = build_application(settings, tmp_path / "run", load_provider=False)
    docker_calls = []

    def docker_forbidden(*args, **kwargs):
        docker_calls.append(True)
        raise AssertionError("An Apptainer refusal cannot select Docker")

    def refused():
        raise RuntimeError("Synthetic unavailable Apptainer/resource boundary")

    monkeypatch.setattr(DockerProcessRunner, "run", docker_forbidden)
    application.container_executor.backend_check = refused
    try:
        assert isinstance(application.container_executor.command_builder, ApptainerCommandBuilder)
        with pytest.raises(ContainerStartError):
            await application.container_executor.execute_async(_plan(image_key="python-local"))
        assert isinstance(application.container_executor.command_builder, ApptainerCommandBuilder)
        assert docker_calls == []
    finally:
        cli._close(application)


@pytest.mark.parametrize("command", ["status", "export", "continue"])
def test_legacy_implicit_config_is_refused_before_reading_or_reinterpreting_history(
    tmp_path, monkeypatch, capsys, command,
):
    path = _write(tmp_path, backend="", apptainer=False)
    path.write_text(path.read_text().replace('image_key = "python-local"',
                                           'image_key = "private-config-canary"'))
    directory = tmp_path / "results" / "existing-run"
    directory.mkdir(parents=True)
    manifest = directory / "RUNTIME.json"
    manifest.write_text(json.dumps({"configuration": {"execution": {"backend": "docker"}}}))
    before = manifest.read_bytes()
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("Invalid external configuration must not reopen a historical run")

    monkeypatch.setattr(cli, "build_application", forbidden)
    arguments = [command, "--config", str(path)]
    if command == "continue":
        arguments += ["--conversation", "historical-thread", "--run-id",
                      "11111111-1111-4111-8111-111111111111"]
    else:
        arguments += ["--run-dir", str(directory)]
    assert cli.main(arguments) == 1
    output = json.loads(capsys.readouterr().out)
    assert output["error"] == "APPTAINER_SETTINGS_REQUIRED"
    assert output["fields"] == ["execution.backend", "execution.apptainer"]
    assert "docker" in output["detail"]
    assert "private-config-canary" not in json.dumps(output)
    assert str(tmp_path) not in json.dumps(output)
    assert manifest.read_bytes() == before
    assert calls == []


@pytest.mark.parametrize("name,backend", [
    ("local-runtime.toml", "apptainer"), ("docker-runtime.toml", "docker"),
    ("cluster-runtime.toml", "apptainer"),
])
def test_shipped_configuration_examples_select_the_documented_backend(name, backend):
    path = Path(__file__).resolve().parents[1] / "examples" / name
    settings = LocalSettings.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))
    assert settings.execution.backend == backend
    assert settings.runtime_backend == "native"
    if name == "local-runtime.toml":
        assert settings.provider.model_identifier == "mimo-v2.6-pro"
        assert settings.provider.max_model_phase_seconds == 300
