"""C13: Apptainer configuration, Scheduler + Apptainer wiring, operator CLI."""

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from labbioagentos import ContainerStartError, cli
from labbioagentos.execution import ApptainerCommandBuilder, JobLedger
from labbioagentos.execution.apptainer_probe import ApptainerCapabilities
from labbioagentos.execution.launchers import SchedulerLauncher
from labbioagentos.local_backend import check_backend
from labbioagentos.local_config import (
    build_application,
    load_settings,
    runtime_manifest,
)

from test_phase6_docker_execution import _plan

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
endpoint = "https://provider.invalid/v1"
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
{launcher}
'''
APPTAINER = '''
[execution.apptainer]
binary = "{binary}"
sif_store = "sif"
scratch_root = "scratch"
cache_dir = "cache"
resource_enforcement = "{enforcement}"
'''


LAUNCHER = '''
[launcher]
kind = "template"
template = "slurm"
partition = "regular"
'''


def _write(tmp_path, *, backend='backend = "apptainer"', reference=SIF_REFERENCE,
           apptainer=True, binary="/opt/apptainer/bin/apptainer", enforcement="scheduler",
           launcher=None, extra=""):
    (tmp_path / "inputs").mkdir(exist_ok=True)
    path = tmp_path / "runtime.toml"
    if launcher is None:
        launcher = LAUNCHER if apptainer else ""
    path.write_text(BASE.format(
        execution=backend, reference=reference,
        apptainer=APPTAINER.format(binary=binary, enforcement=enforcement) if apptainer else "",
        launcher=launcher,
    ) + extra, encoding="utf-8")
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
    assert apptainer.verify_sif_on_execute is True
    assert runtime_manifest(settings)["configuration"]["execution"]["apptainer"]["resource_enforcement"] == (
        "scheduler"
    )


def test_docker_configuration_omits_the_absent_apptainer_section(tmp_path):
    settings = load_settings(_write(tmp_path, backend="", apptainer=False))
    dumped = settings.model_dump(mode="json")["execution"]
    assert dumped["backend"] == "docker" and "apptainer" not in dumped


@pytest.mark.parametrize("arguments", [
    {"apptainer": False, "launcher": LAUNCHER},
    {"reference": "registry.example/python@sha256:" + "1" * 64},
    {"binary": "-privileged"},
    {"binary": "apptainer exec"},
    {"binary": "apptainer"},  # Scheduler mode needs an absolute path.
    {"enforcement": "none"},
    {"launcher": ""},  # Local + Apptainer.
    {"launcher": '[launcher]\nkind = "local"\n'},
    {"launcher": '[launcher]\nkind = "template"\n'},
    {"launcher": LAUNCHER + 'extra_directives = ["--export=ALL"]\n'},
    {"launcher": LAUNCHER.replace('"slurm"', '"no-such-template.toml"')},
    {"extra": '[environment]\nroot = "envs"\n'},
])
def test_invalid_apptainer_configuration_is_rejected(tmp_path, arguments):
    with pytest.raises(ValueError):  # Includes pydantic ValidationError.
        load_settings(_write(tmp_path, **arguments))


def test_resource_enforcement_has_no_default(tmp_path):
    path = _write(tmp_path)
    path.write_text(path.read_text().replace('resource_enforcement = "scheduler"\n', ""))
    with pytest.raises(ValidationError):
        load_settings(path)


def test_scheduler_launcher_requires_the_apptainer_backend(tmp_path):
    with pytest.raises(ValidationError):  # Scheduler + Docker.
        load_settings(_write(tmp_path, backend="", apptainer=False, launcher=LAUNCHER))


def test_site_template_file_is_resolved_and_validated(tmp_path):
    from labbioagentos.execution.launchers import BUILTIN_TEMPLATES

    site = BUILTIN_TEMPLATES["slurm"].model_dump()
    lines = [f"{key} = {json.dumps(value)}" for key, value in site.items() if not isinstance(value, dict)]
    lines += [f"[{key}]\n" + "\n".join(f"{json.dumps(k)} = {json.dumps(v)}" for k, v in value.items())
              for key, value in site.items() if isinstance(value, dict)]
    (tmp_path / "site-slurm.toml").write_text("\n".join(lines) + "\n")

    settings = load_settings(_write(tmp_path, launcher=LAUNCHER.replace('"slurm"', '"site-slurm.toml"')))

    assert settings.launcher.template == str(tmp_path / "site-slurm.toml")
    assert settings.launcher.scheduler_template() == BUILTIN_TEMPLATES["slurm"]


@pytest.mark.asyncio
async def test_scheduler_apptainer_wiring_defers_the_probe_to_the_first_execution(tmp_path):
    settings = load_settings(_write(tmp_path, binary=str(tmp_path / "no-such-apptainer")))

    application = build_application(settings, tmp_path / "run", load_provider=False)
    try:
        executor = application.container_executor
        assert isinstance(executor.command_builder, ApptainerCommandBuilder)
        assert isinstance(executor.launcher, SchedulerLauncher)
        assert executor.launcher.workspace_root == (tmp_path / "run" / "executions").resolve()
        assert isinstance(executor.job_ledger, JobLedger)
        assert (tmp_path / "run" / "jobs.sqlite").is_file()
        assert application.docker_executor is executor
        # Building is cheap; the runtime is probed only when something would run.
        with pytest.raises(ContainerStartError):
            await executor.execute_async(_plan(image_key="python-local"))
        assert not (tmp_path / "run" / "executions").exists() or not any(
            (tmp_path / "run" / "executions").iterdir())
    finally:
        cli._close(application)


def test_docker_wiring_is_unchanged(tmp_path):
    settings = load_settings(_write(tmp_path, backend="", apptainer=False))
    application = build_application(settings, tmp_path / "run", load_provider=False)
    try:
        executor = application.container_executor
        assert executor.launcher is None and executor.job_ledger is None
        assert executor.backend_check is None and executor.isolation_record is None
        assert not (tmp_path / "run" / "jobs.sqlite").exists()
    finally:
        cli._close(application)


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

    report, ok = check_backend(load_settings(config), probe=probe, scheduler_check=_healthy_scheduler)

    assert ok is passed and report["security_failures"] == failures
    assert report["configuration_failures"] == []
    assert report["status"] == ("OK" if passed else "REFUSED")
    assert calls[0]["sif_path"] == tmp_path / "sif" / f"{SIF_REFERENCE.removeprefix('sha256:')}.sif"
    assert calls[0]["scratch_root"] == tmp_path / "scratch"


def _healthy_scheduler(template, policy, work_root):
    return {"template": template.name, "test_submission": "ACCEPTED",
            "clients": {"sbatch": True, "squeue": True, "scancel": True}}


@pytest.mark.parametrize("scheduler, expected", [
    ({"clients": {"sbatch": False, "squeue": True, "scancel": True},
      "test_submission": "UNSUPPORTED"}, ["scheduler_client"]),
    ({"clients": {"sbatch": True, "squeue": True, "scancel": True},
      "test_submission": "REJECTED", "detail": "invalid partition"}, ["scheduler_submission"]),
])
def test_backend_check_refuses_an_unusable_scheduler(tmp_path, scheduler, expected):
    config = _write(tmp_path)
    cli.main(["image-register", "--config", str(config), "--sif", str(_sif(tmp_path))])
    seen = []

    def scheduler_check(template, policy, work_root):
        seen.append((template.name, policy.partition, work_root))
        return {"template": template.name, **scheduler}

    report, ok = check_backend(load_settings(config), probe=lambda **_: _capabilities(),
                               scheduler_check=scheduler_check)

    assert not ok and report["status"] == "REFUSED"
    assert report["security_failures"] == [] and report["configuration_failures"] == expected
    assert seen == [("slurm", "regular", tmp_path / "scratch")]
