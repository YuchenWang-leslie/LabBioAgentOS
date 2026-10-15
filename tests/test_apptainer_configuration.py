"""C13 phase 2: Apptainer configuration, refusal before a launcher exists, operator CLI."""

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from labbioagentos import cli
from labbioagentos.execution.apptainer_probe import ApptainerCapabilities
from labbioagentos.local_backend import check_backend
from labbioagentos.local_config import (
    UnsupportedExecutionBackend,
    build_application,
    load_settings,
    runtime_manifest,
)

SIF_CONTENT = b"configured SIF fixture"
SIF_REFERENCE = "sha256:" + hashlib.sha256(SIF_CONTENT).hexdigest()
BASE = '''
result_root = "results"
input_roots = ["inputs"]
[identity]
user_id = "user-local"
project_id = "project-local"
lab_id = "lab-local"
[provider]
env_file = "provider.env"
api_key_env = "LOCAL_TEST_KEY"
base_url_env = "LOCAL_TEST_URL"
model_identifier = "test-provider-model"
[execution]
{execution}
image_key = "python-local"
image_reference = "{reference}"
[execution.resources]
cpus = 1.0
memory_mb = 512
pids_limit = 64
timeout_seconds = 60.0
{apptainer}
'''
APPTAINER = '''
[execution.apptainer]
binary = "{binary}"
sif_store = "sif"
scratch_root = "scratch"
cache_dir = "cache"
resource_enforcement = "{enforcement}"
'''


def _write(tmp_path, *, backend='backend = "apptainer"', reference=SIF_REFERENCE,
           apptainer=True, binary="apptainer", enforcement="scheduler"):
    (tmp_path / "inputs").mkdir(exist_ok=True)
    path = tmp_path / "runtime.toml"
    path.write_text(BASE.format(
        execution=backend, reference=reference,
        apptainer=APPTAINER.format(binary=binary, enforcement=enforcement) if apptainer else "",
    ), encoding="utf-8")
    return path


def _sif(tmp_path):
    source = tmp_path / "image.sif"
    source.write_bytes(SIF_CONTENT)
    return source


def test_apptainer_settings_resolve_paths_and_enter_the_runtime_identity(tmp_path):
    settings = load_settings(_write(tmp_path))
    apptainer = settings.execution.apptainer

    assert settings.execution.backend == "apptainer"
    assert apptainer.sif_store == tmp_path / "sif" and apptainer.scratch_root == tmp_path / "scratch"
    assert apptainer.cache_dir == tmp_path / "cache" and apptainer.tmp_dir is None
    assert runtime_manifest(settings)["configuration"]["execution"]["apptainer"]["resource_enforcement"] == (
        "scheduler"
    )


def test_docker_configuration_omits_the_absent_apptainer_section(tmp_path):
    settings = load_settings(_write(tmp_path, backend="", apptainer=False))
    dumped = settings.model_dump(mode="json")["execution"]
    assert dumped["backend"] == "docker" and "apptainer" not in dumped


@pytest.mark.parametrize("arguments", [
    {"apptainer": False},
    {"reference": "registry.example/python@sha256:" + "1" * 64},
    {"binary": "-privileged"},
    {"binary": "apptainer exec"},
    {"enforcement": "none"},
])
def test_invalid_apptainer_configuration_is_rejected(tmp_path, arguments):
    with pytest.raises(ValidationError):
        load_settings(_write(tmp_path, **arguments))


def test_resource_enforcement_has_no_default(tmp_path):
    path = _write(tmp_path)
    path.write_text(path.read_text().replace('resource_enforcement = "scheduler"\n', ""))
    with pytest.raises(ValidationError):
        load_settings(path)


def test_apptainer_backend_is_refused_until_a_scheduler_launcher_exists(tmp_path):
    settings = load_settings(_write(tmp_path))
    with pytest.raises(UnsupportedExecutionBackend):
        build_application(settings, tmp_path / "run", load_provider=False)
    assert not (tmp_path / "run").exists()


def test_image_register_prints_the_immutable_reference(tmp_path, capsys):
    config = _write(tmp_path)
    assert cli.main(["image-register", "--config", str(config), "--sif", str(_sif(tmp_path))]) == 0
    assert json.loads(capsys.readouterr().out) == {"image_reference": SIF_REFERENCE}
    assert (tmp_path / "sif" / f"{SIF_REFERENCE.removeprefix('sha256:')}.sif").is_file()


def test_operator_commands_require_the_apptainer_backend(tmp_path, capsys):
    config = _write(tmp_path, backend="", apptainer=False)
    assert cli.main(["image-register", "--config", str(config), "--sif", str(_sif(tmp_path))]) == 1
    assert cli.main(["backend-check", "--config", str(config)]) == 1
    assert "LOCAL_COMMAND_FAILED" in capsys.readouterr().out


def test_backend_check_refuses_an_unregistered_image(tmp_path, capsys):
    assert cli.main(["backend-check", "--config", str(_write(tmp_path))]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "REFUSED" and report["security_failures"] == ["image_store"]


def test_backend_check_refuses_when_the_runtime_is_missing(tmp_path, capsys):
    config = _write(tmp_path, binary=str(tmp_path / "no-such-apptainer"))
    cli.main(["image-register", "--config", str(config), "--sif", str(_sif(tmp_path))])
    capsys.readouterr()

    assert cli.main(["backend-check", "--config", str(config)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "REFUSED"
    assert {"version", "network_isolation", "bind_mounts"} <= set(report["security_failures"])


def _capabilities(**overrides):
    values = dict(version="1.3.4", version_supported=True, network_isolated=True,
                  bind_mounts=True, cgroup_limits=True, no_new_privileges=True)
    values.update(overrides)
    return ApptainerCapabilities(**values)


@pytest.mark.parametrize("enforcement, capabilities, passed, failures", [
    ("scheduler", _capabilities(), True, []),
    ("scheduler", _capabilities(cgroup_limits=False), True, []),
    ("apptainer-cgroups", _capabilities(cgroup_limits=False), False, ["cgroup_limits"]),
    ("scheduler", _capabilities(network_isolated=False), False, ["network_isolation"]),
])
def test_backend_check_report(tmp_path, enforcement, capabilities, passed, failures):
    config = _write(tmp_path, enforcement=enforcement)
    cli.main(["image-register", "--config", str(config), "--sif", str(_sif(tmp_path))])
    calls = []

    def probe(**arguments):
        calls.append(arguments)
        return capabilities

    report, ok = check_backend(load_settings(config), probe=probe)

    assert ok is passed and report["security_failures"] == failures
    assert report["status"] == ("OK" if passed else "REFUSED")
    assert calls[0]["sif_path"] == tmp_path / "sif" / f"{SIF_REFERENCE.removeprefix('sha256:')}.sif"
    assert calls[0]["scratch_root"] == tmp_path / "scratch"
