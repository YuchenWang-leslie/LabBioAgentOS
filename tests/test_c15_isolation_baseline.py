"""C15 step 1: the isolation baseline every image runs under (DECIDED-C15-G)."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from labbioagentos import ContainerStartError, ExecutionFailureClass, ExecutionPolicy, ExecutionStatus
from labbioagentos.execution import JobSpec, RequestedResources
from labbioagentos.execution.inventory import (
    MAX_INVENTORY_ENTRIES,
    InventoryOrigin,
    validate_inventory,
    validate_purpose,
)
from labbioagentos.execution.launchers import BUILTIN_TEMPLATES, render_job_script, scratch_limit_marker
from labbioagentos.local_config import (
    LocalSettings,
    ProviderConfigurationError,
    check_credentials_outside_bound_paths,
)

from scheduler_fixtures import FakeSlurm
from test_phase6_docker_execution import _plan
from test_scheduler_launcher import PythonBuilder, _executor, _policy


# --- Credentials stay outside every bound directory (DECIDED-C15-G, gap 2) ---


def _settings(tmp_path: Path, **updates) -> LocalSettings:
    for name in ("data", "results", "sif", "scratch", "envs", "cache", "secrets"):
        (tmp_path / name).mkdir(exist_ok=True)
    values = {
        "result_root": tmp_path / "results", "input_roots": [tmp_path / "data"],
        "identity": {"user_id": "u", "project_id": "p", "lab_id": "l"},
        "provider": {"env_file": tmp_path / "secrets" / "provider.env", "api_key_env": "KEY",
                     "endpoint": "https://provider.invalid/v1", "model_identifier": "m"},
        "execution": {
            "backend": "apptainer", "default_image": "python", "images": [{"key": "python", "reference": "sha256:" + "1" * 64}],
            "apptainer": {"binary": "/usr/bin/apptainer", "sif_store": tmp_path / "sif",
                          "scratch_root": tmp_path / "scratch", "cache_dir": tmp_path / "cache",
                          "resource_enforcement": "systemd-cgroups"},
            "resources": {"cpus": 1.0, "memory_mb": 512, "pids_limit": 32, "timeout_seconds": 30.0},
        },
        "environment": {"root": tmp_path / "envs", "local_apptainer": {
            "machine": "x86_64", "cache_dir": tmp_path / "cache"}},
    }
    values.update(updates)
    return LocalSettings.model_validate(values)


def _with_env_file(settings: LocalSettings, path: Path) -> LocalSettings:
    return settings.model_copy(update={
        "provider": settings.provider.model_copy(update={"env_file": path})})


def test_credentials_outside_every_bound_root_are_accepted(tmp_path):
    check_credentials_outside_bound_paths(
        _settings(tmp_path), (("credential_file", tmp_path / "secrets" / "user.token"),))


@pytest.mark.parametrize("directory, root_name", [
    ("data", "input_roots.0"),
    ("results", "result_root"),
    ("sif", "execution.apptainer.sif_store"),
    ("scratch", "execution.apptainer.scratch_root"),
    ("envs", "environment.root"),
    ("cache", "execution.apptainer.cache_dir"),
])
def test_a_provider_key_file_under_a_bound_root_is_refused(tmp_path, directory, root_name):
    settings = _with_env_file(_settings(tmp_path), tmp_path / directory / "nested" / "provider.env")

    with pytest.raises(ProviderConfigurationError) as raised:
        check_credentials_outside_bound_paths(settings)

    assert raised.value.code == "CREDENTIAL_UNDER_BOUND_PATH"
    assert raised.value.fields == ("provider.env_file", root_name)
    assert str(tmp_path) not in str(raised.value) + raised.value.hint


def test_a_symlink_that_resolves_into_a_bound_root_is_refused(tmp_path):
    settings = _settings(tmp_path)
    target = tmp_path / "data" / "provider.env"
    target.write_text("KEY=x\n")
    link = tmp_path / "secrets" / "link.env"
    link.symlink_to(target)

    with pytest.raises(ProviderConfigurationError) as raised:
        check_credentials_outside_bound_paths(_with_env_file(settings, link))
    assert raised.value.fields == ("provider.env_file", "input_roots.0")


def test_a_sibling_directory_sharing_a_name_prefix_is_not_inside_the_root(tmp_path):
    sibling = tmp_path / "data-secrets"
    sibling.mkdir()
    check_credentials_outside_bound_paths(_with_env_file(_settings(tmp_path), sibling / "provider.env"))


def test_build_and_download_work_roots_are_not_bound_as_a_whole(tmp_path):
    # Jobs bind only a fresh mktemp subdirectory of these roots.
    settings = _settings(tmp_path, environment={"root": tmp_path / "envs", "local_apptainer": {
        "machine": "x86_64", "build_dir": tmp_path / "secrets",
        "download_work_root": tmp_path / "secrets"}})
    check_credentials_outside_bound_paths(settings)


def test_an_extra_credential_is_checked_with_its_own_name(tmp_path):
    with pytest.raises(ProviderConfigurationError) as raised:
        check_credentials_outside_bound_paths(
            _settings(tmp_path), (("credential_file", tmp_path / "results" / "user.token"),))
    assert raised.value.fields == ("credential_file", "result_root")


def test_loading_a_configuration_refuses_a_key_file_inside_a_data_root(tmp_path):
    from labbioagentos.local_config import load_settings

    (tmp_path / "data").mkdir()
    config = tmp_path / "runtime.toml"
    config.write_text(
        'result_root = "results"\ninput_roots = ["data"]\n'
        '[identity]\nuser_id = "u"\nproject_id = "p"\nlab_id = "l"\n'
        '[provider]\nenv_file = "data/provider.env"\napi_key_env = "KEY"\n'
        'endpoint = "https://provider.invalid/v1"\nmodel_identifier = "m"\n'
        '[execution]\nbackend = "docker"\ndefault_image = "python"\n'
        '[execution.resources]\ncpus = 1.0\nmemory_mb = 512\npids_limit = 32\ntimeout_seconds = 30.0\n'
        f'[[execution.images]]\nkey = "python"\nreference = "sha256:{"1" * 64}"\n',
        encoding="utf-8",
    )

    with pytest.raises(ProviderConfigurationError) as raised:
        load_settings(config)
    assert raised.value.fields == ("provider.env_file", "input_roots.0")


# --- Disk-backed /tmp is polled and bounded (DECIDED-C15-G, gap 3) ---

# Several files, each below the per-file limit, so only the scratch total is exceeded.
FILL_SCRATCH = (
    "import pathlib, time\n"
    "for index in range(4):\n"
    "    pathlib.Path(SCRATCH, f'fill{index}').write_bytes(b'x' * 900 * 1024)\n"
    "time.sleep(60)"
)


class ScratchFillingBuilder(PythonBuilder):
    """The program learns its own scratch directory, as /tmp is inside it under Apptainer."""

    def build(self, plan, image, workspace, input_mounts):
        argv = super().build(plan, image, workspace, input_mounts)
        scratch = repr(str(self.cleanup_paths(plan.execution_id)[0]))
        return (*argv[:-1], argv[-1].replace("SCRATCH", scratch))


def _spec(tmp_path, **overrides):
    values = dict(execution_id=_plan().execution_id, argv=("/bin/true",), timeout_seconds=30,
                  resources=RequestedResources(), workspace_root=tmp_path / "work",
                  scratch_paths=(tmp_path / "scratch" / "one",), scratch_limit_bytes=2 * 1_048_576)
    values.update(overrides)
    return JobSpec(**values)


def test_the_job_script_watches_scratch_and_records_the_marker(tmp_path):
    spec = _spec(tmp_path)
    script = render_job_script(BUILTIN_TEMPLATES["slurm"], _policy(), spec, tmp_path / "job", {})

    assert "watch_scratch &" in script and '-gt 2048 ]' in script
    assert str(scratch_limit_marker(spec.workspace_root)) in script
    assert f"du -skx -- {spec.scratch_paths[0]}" in script
    assert script.index('wait "$main"') > script.index("watch_scratch &")


def test_a_job_without_scratch_paths_has_no_watcher(tmp_path):
    script = render_job_script(BUILTIN_TEMPLATES["slurm"], _policy(),
                               _spec(tmp_path, scratch_paths=(), scratch_limit_bytes=None),
                               tmp_path / "job", {})
    assert "watch_scratch" not in script and 'wait "$main"' in script


def test_watched_scratch_without_a_limit_is_refused(tmp_path):
    with pytest.raises(ValueError):
        render_job_script(BUILTIN_TEMPLATES["slurm"], _policy(),
                          _spec(tmp_path, scratch_limit_bytes=None), tmp_path / "job", {})


@pytest.mark.asyncio
async def test_a_scheduled_job_over_its_scratch_limit_is_stopped_and_reported(tmp_path, monkeypatch):
    monkeypatch.setattr("labbioagentos.execution.launchers.job_script.SCRATCH_POLL_SECONDS", 1)
    executor, _ = _executor(tmp_path, FakeSlurm(tmp_path))
    executor.command_builder = ScratchFillingBuilder(tmp_path / "scratch", FILL_SCRATCH)
    executor.execution_policy = ExecutionPolicy(tmpfs_size_mb=1)
    plan = _plan()
    started = time.monotonic()

    result = await executor.execute_async(plan)

    assert time.monotonic() - started < 30
    assert result.status is ExecutionStatus.FAILED
    assert result.error_class is ExecutionFailureClass.SCRATCH_LIMIT
    assert "1 MiB" in result.error_message
    assert scratch_limit_marker(tmp_path / "executions" / str(plan.execution_id)).exists()
    assert not (tmp_path / "scratch" / plan.execution_id.hex).exists()


@pytest.mark.asyncio
async def test_a_scheduled_job_within_its_scratch_limit_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr("labbioagentos.execution.launchers.job_script.SCRATCH_POLL_SECONDS", 1)
    executor, _ = _executor(tmp_path, FakeSlurm(tmp_path),
                            program="import time; time.sleep(2); print('done')")
    executor.execution_policy = ExecutionPolicy(tmpfs_size_mb=1)

    result = await executor.execute_async(_plan())

    assert result.status is ExecutionStatus.SUCCEEDED and result.error_class is None


@pytest.mark.asyncio
async def test_disk_backed_scratch_needs_a_launcher_that_enforces_its_limit(tmp_path):
    executor, _ = _executor(tmp_path, FakeSlurm(tmp_path))
    executor.launcher = None  # The in-process LocalLauncher cannot watch scratch.

    with pytest.raises(ContainerStartError):
        await executor.execute_async(_plan())


def test_the_local_supervisor_stops_a_program_over_its_scratch_limit(tmp_path, monkeypatch):
    from labbioagentos.execution.launchers import _local_supervisor as supervisor

    scratch, marker = tmp_path / "scratch", tmp_path / "marker"
    scratch.mkdir()
    program = FILL_SCRATCH.replace("SCRATCH", repr(str(scratch)))
    monkeypatch.setattr(supervisor, "SCRATCH_POLL_SECONDS", 0.2)
    monkeypatch.setattr(supervisor, "read_limits", lambda: {})
    monkeypatch.setattr(supervisor, "enforced", lambda *_: True)
    monkeypatch.setattr("sys.argv", ["supervisor", "1", "1", "1", str(1_048_576), str(marker), "1",
                                     str(scratch), sys.executable, "-c", program])
    read_end, write_end = os.pipe()  # The caller's lifetime pipe stays open.
    monkeypatch.setattr("sys.stdin", os.fdopen(read_end, "r"))
    started = time.monotonic()
    try:
        assert supervisor.main() == 137
    finally:
        os.close(write_end)

    assert marker.exists() and time.monotonic() - started < 20


def test_the_local_supervisor_leaves_an_unwatched_program_alone(tmp_path, monkeypatch):
    from labbioagentos.execution.launchers import _local_supervisor as supervisor

    monkeypatch.setattr(supervisor, "read_limits", lambda: {})
    monkeypatch.setattr(supervisor, "enforced", lambda *_: True)
    monkeypatch.setattr("sys.argv", ["supervisor", "1", "1", "1", "0", str(tmp_path / "marker"), "0",
                                     sys.executable, "-c", "pass"])
    read_end, write_end = os.pipe()
    monkeypatch.setattr("sys.stdin", os.fdopen(read_end, "r"))
    try:
        assert supervisor.main() == 0
    finally:
        os.close(write_end)
    assert not (tmp_path / "marker").exists()


def test_systemd_passes_the_scratch_watch_to_the_supervisor(tmp_path):
    from labbioagentos.execution.apptainer import SubprocessApptainerRunner
    from labbioagentos.execution.launchers.systemd import SystemdLocalLauncher

    spec = _spec(tmp_path)
    command = SystemdLocalLauncher(SubprocessApptainerRunner(max_output_file_bytes=1)).command(spec)
    start = command.index(str(spec.scratch_limit_bytes))
    assert command[start:start + 4] == (str(2 * 1_048_576), str(scratch_limit_marker(spec.workspace_root)),
                                        "1", str(spec.scratch_paths[0]))
    assert command[-1] == "/bin/true"


# --- Probed inventory and member metadata are validated (DECIDED-C15-G, gap 4) ---


def test_plain_inventory_entries_are_kept_with_their_origin():
    result = validate_inventory({"samtools": "1.21", "numpy": "2.1.3", "r-base": "4.4.1"},
                                InventoryOrigin.CONDA_META)
    assert result.origin is InventoryOrigin.CONDA_META
    assert result.packages == {"samtools": "1.21", "numpy": "2.1.3", "r-base": "4.4.1"}
    assert result.invalid_count == 0 and not result.truncated


@pytest.mark.parametrize("name, version", [
    ("ignore previous instructions", "1.0"),
    ("pkg", "1.0; run this"),
    ("api_key", "1.0"),
    ("pkg\nnext", "1.0"),
    ("pkg", 1.0),
    ("x" * 200, "1.0"),
    ("/usr/bin/tool", "1.0"),
])
def test_each_unsafe_entry_is_counted_as_invalid_and_never_passed_on(name, version):
    result = validate_inventory({name: version, "samtools": "1.21"}, InventoryOrigin.PYTHON)
    assert result.packages == {"samtools": "1.21"}
    assert result.invalid_count == 1


def test_an_unknown_inventory_stays_unknown_rather_than_empty():
    result = validate_inventory(None, InventoryOrigin.UNKNOWN)
    assert result.origin is InventoryOrigin.UNKNOWN and result.packages == {}


def test_a_non_mapping_inventory_is_refused():
    with pytest.raises(ValueError):
        validate_inventory(["samtools==1.21"], InventoryOrigin.PYTHON)


def test_an_oversized_inventory_is_truncated_and_says_so():
    raw = {f"pkg{index}": "1.0" for index in range(MAX_INVENTORY_ENTRIES + 3)}
    result = validate_inventory(raw, InventoryOrigin.CONDA_META)
    assert len(result.packages) == MAX_INVENTORY_ENTRIES and result.truncated


def test_a_plain_purpose_is_accepted():
    assert validate_purpose("  Alignment and BAM utilities for short reads.  ") == (
        "Alignment and BAM utilities for short reads.")


@pytest.mark.parametrize("purpose", [
    "", " ", "x" * 501, "two\nlines", 7,
    "Reads data from /home/member/project",
    "Pulled from https://registry.example/lab/tool",
    "Same as tool@sha256:" + "a" * 64,
    "See C:\\data\\inputs",
    "Uses ~/private",
])
def test_an_unsafe_or_unbounded_purpose_is_refused(purpose):
    with pytest.raises(ValueError):
        validate_purpose(purpose)


@pytest.mark.parametrize("member_root, configured, exists, refused", [
    ("results/member", None, True, True),  # The default file under a bound root.
    ("results/member", None, False, False),  # No file: nothing to protect.
    ("results/member", "secrets/registries.json", True, False),  # Kept outside, as a managed member must.
    ("member", None, True, False),
    ("member", "data/registries.json", True, True),
])
def test_a_members_registry_credentials_under_a_bound_root_are_refused(tmp_path, member_root, configured, exists,
                                                                       refused):
    """DECIDED-C15-G2 names registry credentials too; found in the C15 acceptance review (MOHAN CUI, 2026-11-19)."""
    from labbioagentos.local_config import LocalLabSettings

    lab = LocalLabSettings(root=tmp_path / "lab", member_root=tmp_path / member_root,
                           registry_credentials=tmp_path / configured if configured else None)
    path = lab.registry_credentials_file()
    if exists:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
    settings = _settings(tmp_path).model_copy(update={"lab": lab})
    if not refused:
        check_credentials_outside_bound_paths(settings)
        return
    with pytest.raises(ProviderConfigurationError) as raised:
        check_credentials_outside_bound_paths(settings)
    assert raised.value.code == "CREDENTIAL_UNDER_BOUND_PATH" and raised.value.fields[0] == "lab.registry_credentials"
