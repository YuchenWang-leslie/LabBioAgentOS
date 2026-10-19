"""C13 phase 4: cluster environment build configuration, wiring and operator checks (DECIDED-C13-8)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from labbioagentos import ProcessOutcome, cli
from labbioagentos.execution.apptainer_build import DownloadRunner
from labbioagentos.execution.apptainer_environment_builder import ApptainerEnvironmentBuilder
from labbioagentos.execution.environment_builder import DockerEnvironmentBuilder
from labbioagentos.execution.launchers import SchedulerLauncher
from labbioagentos.local_backend import check_backend, check_environment_builds
from labbioagentos.local_config import build_application, load_settings, runtime_manifest

from test_apptainer_configuration import _capabilities, _healthy_scheduler, _sif, _write

PROXY = "http://proxy.example:3128"


def _cluster(tmp_path, cluster="", environment=""):
    return _write(tmp_path, extra=(
        '[environment]\nroot = "envs"\n' + environment
        + '[environment.cluster]\nmachine = "x86_64"\ncache_dir = "pip-cache"\n' + cluster
    ))


@pytest.mark.parametrize("arguments", [
    {"extra": '[environment]\nroot = "envs"\n'},  # Apptainer without cluster build settings.
    {"backend": "", "apptainer": False,
     "extra": '[environment]\nroot = "envs"\n[environment.cluster]\nmachine = "x86_64"\n'},  # Docker.
    {"extra": '[environment]\nroot = "envs"\n[environment.cluster]\ncpus = 2.0\n'},  # No machine.
])
def test_cluster_build_settings_go_with_the_apptainer_backend_only(tmp_path, arguments):
    with pytest.raises(ValueError):
        load_settings(_write(tmp_path, **arguments))


@pytest.mark.parametrize("cluster", [
    'machine_extra = "x"\n',
    'download_placement = "job"\n',  # A download job needs a partition with network.
    'download_partition = "transfer"\n',  # Only for job placement.
    'build_dir = "relative/tmp"\n',
    'download_work_root = "tmp"\n',
    'index_url = "https://user:secret@pypi.example/simple"\n',
    'index_url = "ftp://pypi.example/simple"\n',
    'extra_directives = ["--export=ALL"]\n',
    'partition = "regular;id"\n',
    'download_placement = "job"\ndownload_partition = "$(id)"\n',
])
def test_unsafe_or_incomplete_cluster_build_settings_are_refused(tmp_path, cluster):
    with pytest.raises(ValueError):
        load_settings(_cluster(tmp_path, cluster))


def test_cluster_build_settings_resolve_shared_paths_and_keep_node_local_ones(tmp_path):
    settings = load_settings(_cluster(tmp_path, 'build_dir = "/tmp"\n'))
    cluster = settings.environment.cluster

    assert cluster.cache_dir == tmp_path / "pip-cache"
    assert cluster.build_dir == Path("/tmp")  # Compute-node path, never resolved on the login node.
    assert settings.environment.root == tmp_path / "envs"
    manifest = runtime_manifest(settings)
    assert manifest["configuration"]["environment"]["cluster"]["machine"] == "x86_64"
    execute = next(stage for stage in manifest["profile"]["stages"] if stage["stage"] == "EXECUTE")
    assert "environment_build" in execute["capabilities"]


@pytest.mark.parametrize("placement", ["login", "job"])
def test_cluster_builds_are_wired_to_jobs_that_share_the_run_ledger(tmp_path, placement):
    cluster = ('download_placement = "job"\ndownload_partition = "transfer"\n' if placement == "job" else "")
    settings = load_settings(_cluster(tmp_path, cluster + 'max_file_bytes = 1073741824\n',
                                      environment=f'build_proxy = "{PROXY}"\n'))

    application = build_application(settings, tmp_path / "run", load_provider=False)
    try:
        builder = application.capability_services.environment_service.builder
        jobs_root = (tmp_path / "run" / "environment-jobs").resolve()
        assert isinstance(builder, ApptainerEnvironmentBuilder)
        assert builder.job_ledger is application.container_executor.job_ledger
        assert application.configuration.environment_job_launcher is builder.build_launcher
        assert isinstance(builder.build_launcher, SchedulerLauncher)
        assert builder.build_launcher.workspace_root == jobs_root
        assert builder.build_launcher.policy.max_output_file_bytes == 1073741824
        assert builder.build_launcher.policy.partition == "regular"  # The launcher's, by default.
        assert builder.policy.proxy_url == PROXY and builder.policy.machine == "x86_64"
        if placement == "login":
            assert isinstance(builder.download_runner, DownloadRunner) and builder.download_launcher is None
        else:
            assert builder.download_runner is None
            assert builder.download_launcher.policy.partition == "transfer"
            assert builder.download_launcher.workspace_root == jobs_root
    finally:
        cli._close(application)


def test_docker_environment_builds_are_unchanged(tmp_path):
    settings = load_settings(_write(tmp_path, backend="", apptainer=False,
                                    extra='[environment]\nroot = "envs"\n'))

    application = build_application(settings, tmp_path / "run", load_provider=False)
    try:
        assert isinstance(application.capability_services.environment_service.builder, DockerEnvironmentBuilder)
        assert application.configuration.environment_job_launcher is None
        assert not (tmp_path / "run" / "environment-jobs").exists()
    finally:
        cli._close(application)


def test_backend_check_includes_environment_build_failures(tmp_path):
    config = _cluster(tmp_path)
    cli.main(["image-register", "--config", str(config), "--sif", str(_sif(tmp_path))])
    seen = []

    def environment_check(settings, sif_path):
        seen.append(sif_path)
        return {"download_check": "PIP_TOO_OLD", "failures": ["environment_download"]}

    report, ok = check_backend(load_settings(config), probe=lambda **_: _capabilities(),
                               scheduler_check=_healthy_scheduler, environment_check=environment_check)

    assert not ok and report["status"] == "REFUSED"
    assert report["configuration_failures"] == ["environment_download"]
    assert report["environment"]["download_check"] == "PIP_TOO_OLD"
    assert [path.parent for path in seen] == [tmp_path / "sif"]  # The verified base SIF.


def test_backend_check_without_environment_builds_reports_none(tmp_path):
    config = _write(tmp_path)
    cli.main(["image-register", "--config", str(config), "--sif", str(_sif(tmp_path))])

    report, ok = check_backend(load_settings(config), probe=lambda **_: _capabilities(),
                               scheduler_check=_healthy_scheduler)

    assert ok and "environment" not in report


# The login-node environment check itself, with a recording runner.

class Runner:
    def __init__(self, machine=b"x86_64\n", check=(0, b""), report=True):
        self.machine, self.check, self.report, self.calls = machine, check, report, []

    def run(self, argv, *, timeout_seconds):
        self.calls.append(argv)
        if "platform.machine()" in argv[-1]:
            return ProcessOutcome(exit_code=0 if self.machine else 1, stdout=self.machine or b"",
                                  stderr=b"", duration_seconds=0.1)
        if self.report:
            work = Path(argv[argv.index("--workdir") + 1])
            (work / "report.json").write_text(json.dumps({"version": "1", "install": []}))
        code, output = self.check
        return ProcessOutcome(exit_code=code, stdout=b"", stderr=output, duration_seconds=0.1)


def _settings(tmp_path, cluster="", environment=""):
    (tmp_path / "login-tmp").mkdir(exist_ok=True)
    return load_settings(_cluster(tmp_path, f'download_work_root = "{tmp_path / "login-tmp"}"\n' + cluster,
                                  environment=environment))


def test_login_downloads_need_one_architecture_and_a_reachable_index(tmp_path):
    ca = tmp_path / "site-ca.pem"
    ca.write_text("certificate")
    settings = _settings(tmp_path, cluster=f'index_url = "https://pypi.example/simple"\nca_bundle = "{ca}"\n',
                         environment=f'build_proxy = "{PROXY}"\n')
    runner = Runner()

    report = check_environment_builds(settings, tmp_path / "base.sif", runner=runner, host_machine="x86_64")

    assert report["download_check"] == "OK" and report["failures"] == []
    assert report["build_capabilities"] == "CHECKED_BY_EACH_BUILD_JOB"
    machine, resolve = runner.calls
    assert "--network" in machine and "--network" not in resolve  # Only the index check has network.
    assert {"--dry-run", "--ignore-installed", "--report"} <= set(resolve)
    assert resolve[resolve.index("--proxy") + 1] == PROXY
    assert resolve[resolve.index("--index-url") + 1] == "https://pypi.example/simple"
    assert f"{ca}:/labbio/ca.pem:ro" in resolve and "--cert" in resolve
    # The check binds the pip cache exactly as the download does, creating it first.
    assert f"{tmp_path / 'pip-cache'}:/labbio/cache:rw" in resolve and "--no-cache-dir" not in resolve
    assert stat.S_IMODE((tmp_path / "pip-cache").stat().st_mode) == 0o700
    assert list((tmp_path / "login-tmp").iterdir()) == []


@pytest.mark.parametrize("host, image", [("aarch64", b"x86_64\n"), ("x86_64", b"aarch64\n"), ("x86_64", None)])
def test_an_architecture_mismatch_refuses_login_downloads(tmp_path, host, image):
    runner = Runner(machine=image)

    report = check_environment_builds(_settings(tmp_path), tmp_path / "base.sif", runner=runner, host_machine=host)

    assert report["failures"] == ["environment_architecture"] and report["download_check"] == "SKIPPED"
    assert len(runner.calls) == 1  # The index was not contacted.


@pytest.mark.parametrize("check, report_written, expected", [
    ((2, b"ERROR: no such option: --dry-run\n"), False, "PIP_TOO_OLD"),
    ((1, b"ProxyError('Cannot connect to proxy.')\n"), False, "NETWORK_FAILURE"),
    ((1, b"SSLError: certificate verify failed\n"), False, "TLS_FAILURE"),
    ((0, b""), False, "VERIFICATION_FAILED"),
])
def test_unreachable_index_or_old_pip_refuses_login_downloads(tmp_path, check, report_written, expected):
    runner = Runner(check=check, report=report_written)

    report = check_environment_builds(_settings(tmp_path), tmp_path / "base.sif", runner=runner,
                                      host_machine="x86_64")

    assert report["download_check"] == expected and report["failures"] == ["environment_download"]


def test_an_unusable_pip_cache_is_reported_before_any_check(tmp_path):
    (tmp_path / "pip-cache").mkdir()
    (tmp_path / "pip-cache").chmod(0o777)
    runner = Runner()

    report = check_environment_builds(_settings(tmp_path), tmp_path / "base.sif", runner=runner,
                                      host_machine="x86_64")

    assert report["failures"] == ["environment_cache_dir"] and runner.calls == []


def test_job_downloads_are_reported_as_unchecked(tmp_path):
    runner = Runner()
    settings = _settings(tmp_path, 'download_placement = "job"\ndownload_partition = "transfer"\n')

    report = check_environment_builds(settings, tmp_path / "base.sif", runner=runner, host_machine="x86_64")

    assert report["download_check"] == "UNCHECKED_JOB_PLACEMENT" and report["failures"] == []
    assert runner.calls == []


def test_a_missing_download_work_root_is_reported(tmp_path):
    settings = load_settings(_cluster(tmp_path, f'download_work_root = "{tmp_path / "missing"}"\n'))

    report = check_environment_builds(settings, tmp_path / "base.sif", runner=Runner(), host_machine="x86_64")

    assert report["failures"] == ["environment_download_work_root"]


def test_the_example_cluster_configuration_is_valid():
    import tomllib

    from labbioagentos.local_config import LocalSettings

    example = Path(__file__).parents[1] / "examples" / "cluster-runtime.toml"
    settings = LocalSettings.model_validate(tomllib.loads(example.read_text(encoding="utf-8")))
    assert settings.environment.cluster.download_placement == "login"
