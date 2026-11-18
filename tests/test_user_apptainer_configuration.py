"""User-local locked cores are explicit; quota and scheduler modes remain distinct."""

from contextlib import closing

import pytest

from labbioagentos import ApprovedImageRegistry, ExecutionPlanRejected, RequestedResources, cli
from labbioagentos.execution.job_ledger import JobLedger
from labbioagentos.local_config import _environment_wiring, build_application, load_settings, runtime_manifest
from test_apptainer_configuration import _capabilities, _sif, _write


def test_user_affinity_wiring_and_identity_do_not_require_cpu_quota(tmp_path):
    settings = load_settings(_write(tmp_path, launcher="", enforcement="systemd-affinity"))
    app = build_application(settings, tmp_path / "run", load_provider=False)
    try:
        assert app.container_executor.launcher.resource_enforcement == "systemd-affinity"
        assert app.container_executor.command_builder.resource_enforcement == "systemd-affinity"
        assert app.execution_capability.cpu_limit_mode == "locked-affinity"
        manifest = runtime_manifest(settings)
        assert manifest["configuration"]["execution"]["apptainer"]["resource_enforcement"] == "systemd-affinity"
    finally:
        cli._close(app)


def test_user_builds_and_downloads_keep_the_selected_resource_owner(tmp_path):
    settings = load_settings(_write(
        tmp_path, launcher="", enforcement="systemd-affinity",
        extra='[environment]\nroot = "envs"\n[environment.local_apptainer]\nmachine = "x86_64"\n',
    ))
    root = tmp_path / "run"
    with closing(JobLedger(root / "jobs.sqlite")) as ledger:
        wiring = _environment_wiring(settings, root, ledger)
        builder = wiring["environment_service_factory"](ApprovedImageRegistry(())).builder
        assert builder.build_launcher.resource_enforcement == "systemd-affinity"
        assert builder.download_launcher is builder.build_launcher


@pytest.mark.parametrize("build", [False, True])
def test_locked_cores_refuse_fractional_requests_without_rounding(tmp_path, build):
    extra = ('[environment]\nroot = "envs"\n[environment.local_apptainer]\n'
             'machine = "x86_64"\ncpus = 0.5\n') if build else ""
    path = _write(tmp_path, launcher="", enforcement="systemd-affinity", extra=extra)
    if not build:
        path.write_text(path.read_text().replace("cpus = 1.0", "cpus = 0.5"))
    with pytest.raises(ValueError, match="integer"):
        load_settings(path)


def test_locked_cores_cannot_be_selected_for_scheduler_placement(tmp_path):
    with pytest.raises(ValueError, match="Local"):
        load_settings(_write(tmp_path, enforcement="systemd-affinity"))


def test_user_backend_check_remains_local_and_reports_its_resource_mode(tmp_path):
    from labbioagentos.execution.apptainer import SifStore
    from labbioagentos.local_backend import check_backend

    settings = load_settings(_write(tmp_path, launcher="", enforcement="systemd-affinity"))
    SifStore(settings.execution.apptainer.sif_store).register(_sif(tmp_path))
    report, passed = check_backend(
        settings, probe=lambda **_: _capabilities(),
        scheduler_check=lambda *_: pytest.fail("User-local execution cannot submit to a scheduler"),
        local_check=lambda: {"status": "OK", "failures": [], "cpu_enforcement": "locked-affinity"},
    )
    assert passed
    assert report["scheduler"] is None
    assert report["resource_enforcement"] == "systemd-affinity"
    assert report["local_resources"]["cpu_enforcement"] == "locked-affinity"


def test_builder_does_not_silently_round_an_agent_fractional_cpu_request(tmp_path):
    from apptainer_fixtures import apptainer_environment
    from test_phase6_docker_execution import FakeDockerRunner, _plan

    _, executor, _, _, _ = apptainer_environment(
        tmp_path, FakeDockerRunner(), resource_enforcement="systemd-affinity",
    )
    plan = _plan(resources=RequestedResources(cpus=0.5, memory_mb=256, pids_limit=32, timeout_seconds=20))
    with pytest.raises(ExecutionPlanRejected, match="integer"):
        executor.build_command(plan)
