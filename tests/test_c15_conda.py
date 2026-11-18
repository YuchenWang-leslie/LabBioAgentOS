"""C15 step 7b: locked conda packages in derived environments (DECIDED-C15-E1)."""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from labbioagentos import ApprovedImage, ApprovedImageRegistry, ExecutionRuntime
from labbioagentos.execution.conda import (
    CondaError,
    CondaPolicy,
    LockedPackage,
    download,
    lock_from_solution,
    normalize_conda_specs,
    render_explicit,
    solve,
    validate_channels,
    verify_micromamba,
)
from labbioagentos.execution.environments import (
    EnvironmentBuildResult,
    EnvironmentRequestError,
    EnvironmentService,
    _request_hash,
)

from test_apptainer_environment_builder import _attempt, cluster  # noqa: F401 (fixture)
from test_execution_environments import RecordingBuilder, _image

BASE = "https://conda.anaconda.org/conda-forge/linux-64/"


def _policy(tmp_path, program="#!/bin/sh\nexit 1\n", **updates) -> CondaPolicy:
    tool = tmp_path / "micromamba"
    if not tool.exists():
        tool.write_text(program)
        tool.chmod(0o555)
    values = dict(micromamba=tool, micromamba_sha256=hashlib.sha256(tool.read_bytes()).hexdigest(),
                  channels=("conda-forge", "bioconda"), machine="x86_64")
    values.update(updates)
    return CondaPolicy(**values)


def _entry(name="samtools", version="1.21", url=None, sha="a" * 64):
    return {"name": name, "version": version, "build_string": "h_0",
            "url": url or f"{BASE}{name}-{version}-h_0.conda", "sha256": sha}


# --- Requests are plain names and constraints from allowed channels ---


@pytest.mark.parametrize("spec", ["samtools", "samtools=1.21", "SAMTOOLS==1.21", "htslib>=1.20,<2"])
def test_plain_specs_are_accepted(spec):
    assert normalize_conda_specs((spec,)) == (spec.lower(),)


@pytest.mark.parametrize("spec", ["bioconda::samtools", "https://x/samtools.conda", "./samtools", "--override",
                                  "samtools 1.21", "samtools=1.21; rm -rf /", ""])
def test_channels_urls_paths_and_options_are_refused(spec):
    with pytest.raises(CondaError):
        normalize_conda_specs((spec,))


def test_a_package_may_be_named_once():
    with pytest.raises(CondaError):
        normalize_conda_specs(("samtools", "samtools=1.21"))


@pytest.mark.parametrize("channels", [("defaults",), ("conda-forge", "main"), ("conda-forge", "conda-forge"), (),
                                      ("https://repo.anaconda.com/pkgs/main",)])
def test_the_anaconda_defaults_and_malformed_channels_are_refused(channels):
    with pytest.raises(ValueError):
        validate_channels(channels)


# --- The platform's own micromamba ---


def test_only_the_configured_micromamba_runs(tmp_path):
    policy = _policy(tmp_path)
    assert verify_micromamba(policy) == policy.micromamba
    with pytest.raises(CondaError):
        verify_micromamba(_policy(tmp_path, micromamba_sha256="b" * 64))
    policy.micromamba.chmod(0o755)
    with pytest.raises(CondaError):
        verify_micromamba(policy)  # A writable tool could have been replaced.


def test_the_solver_runs_without_user_configuration_or_inherited_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "PRIVATE-SECRET")
    record = tmp_path / "observed.json"
    program = ("#!/usr/bin/python3\nimport json, os, sys\n"
               f"json.dump({{'argv': sys.argv[1:], 'env': dict(os.environ)}}, open({str(record)!r}, 'w'))\n"
               f"print(json.dumps({{'success': True, 'actions': {{'LINK': [{json.dumps(_entry())}]}}}}))\n")
    policy = _policy(tmp_path, program)
    (tmp_path / "work").mkdir()

    locked = solve(policy, ("samtools=1.21",), tmp_path / "work")

    observed = json.loads(record.read_text())
    assert locked[0].name == "samtools" and locked[0].sha256 == "a" * 64
    assert {"--dry-run", "--json", "--no-rc", "--override-channels", "--strict-channel-priority"} <= set(observed["argv"])
    assert observed["argv"][observed["argv"].index("--platform") + 1] == "linux-64"
    channels = [observed["argv"][index + 1] for index, item in enumerate(observed["argv"]) if item == "-c"]
    assert channels == ["conda-forge", "bioconda"] and observed["argv"][-1] == "samtools=1.21"
    assert "OPENAI_API_KEY" not in observed["env"] and set(observed["env"]) <= {"PATH", "HOME", "LC_CTYPE"}


def test_a_failed_solve_reports_a_fixed_code(tmp_path):
    (tmp_path / "work").mkdir()
    with pytest.raises(CondaError) as raised:
        solve(_policy(tmp_path), ("samtools",), tmp_path / "work")
    assert raised.value.code == "CONDA_NO_SOLUTION"


# --- The lock: allowed channels and sha256 only ---


def test_a_lock_keeps_every_package_with_its_hash_and_allows_underscore_names(tmp_path):
    noarch = _entry("tzdata", "2025b", url="https://conda.anaconda.org/conda-forge/noarch/tzdata-2025b-h_0.conda")
    payload = {"actions": {"LINK": [_entry(), _entry("_openmp_mutex", "4.5"), noarch]}}
    locked = lock_from_solution(payload, _policy(tmp_path))
    assert [item.name for item in locked] == ["_openmp_mutex", "samtools", "tzdata"]


@pytest.mark.parametrize("entry", [
    _entry(url="https://conda.anaconda.org/main/linux-64/samtools-1.21-h_0.conda"),
    _entry(url="https://repo.anaconda.com/pkgs/main/linux-64/samtools-1.21-h_0.conda"),
    _entry(url=BASE + "../evil/samtools.conda"),
    _entry(url="https://conda.anaconda.org/conda-forge/osx-arm64/samtools-1.21-h_0.conda"),
    _entry(url="https://conda.anaconda.org/conda-forge/linux-64/sub/samtools-1.21-h_0.conda"),
    _entry(url="https://conda.anaconda.org/conda-forge/linux-64/samtools-1.21-h_0.conda/x.conda"),
    _entry(sha=None),
    _entry(sha="A" * 64),
])
def test_a_lock_naming_another_channel_or_lacking_a_hash_is_refused(tmp_path, entry):
    with pytest.raises(CondaError) as raised:
        lock_from_solution({"actions": {"LINK": [entry]}}, _policy(tmp_path))
    assert raised.value.code == "CONDA_LOCK_INVALID"


def test_downloads_are_kept_only_when_their_hash_matches(tmp_path):
    body = b"conda package bytes"
    package = LockedPackage("samtools", "1.21", "h_0", BASE + "samtools-1.21-h_0.conda",
                            hashlib.sha256(body).hexdigest())
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
    download((package,), tmp_path / "pkgs", proxy_url=None, transport=transport)
    assert (tmp_path / "pkgs" / "samtools-1.21-h_0.conda").read_bytes() == body

    tampered = httpx.MockTransport(lambda request: httpx.Response(200, content=body + b"x"))
    with pytest.raises(CondaError) as raised:
        download((package,), tmp_path / "again", proxy_url=None, transport=tampered)
    assert raised.value.code == "VERIFICATION_FAILED"
    missing = httpx.MockTransport(lambda request: httpx.Response(404))
    with pytest.raises(CondaError) as raised:
        download((package,), tmp_path / "third", proxy_url=None, transport=missing)
    assert raised.value.code == "NETWORK_FAILURE"


def test_the_explicit_lock_repeats_every_hash_for_the_installer():
    package = LockedPackage("samtools", "1.21", "h_0", BASE + "samtools-1.21-h_0.conda", "c" * 64)
    assert render_explicit((package,), "/labbio/conda-pkgs") == (
        "@EXPLICIT\nfile:///labbio/conda-pkgs/samtools-1.21-h_0.conda#" + "c" * 64 + "\n")


# --- The Docker recipe ---


@pytest.mark.asyncio
@pytest.mark.parametrize("on_path", [True, False])
async def test_the_docker_recipe_installs_offline_in_a_separate_stage_and_appends_path(tmp_path, monkeypatch,
                                                                                         on_path):
    from labbioagentos.execution import environment_builder as module
    from test_environment_builder import BASE_ID, IMAGE_ID, REQUEST_HASH

    root = tmp_path / ("a" * 32)
    root.mkdir(mode=0o700)
    policy = _policy(tmp_path)
    builder = module.DockerEnvironmentBuilder(conda=policy)
    locked = (LockedPackage("samtools", "1.21", "h_0", BASE + "samtools-1.21-h_0.conda", "c" * 64),)
    monkeypatch.setattr(module, "conda_solve", lambda *_: locked)
    monkeypatch.setattr(module, "conda_download",
                        lambda packages, destination, **_: destination.mkdir() or (destination / "x.conda").write_bytes(b"x"))
    calls = []
    responses = {"base-inspect.log": BASE_ID + "\n", "image-size.log": "1000\n", "verification.log": json.dumps({
        "installed_packages": {}, "conda_packages": {"samtools": "1.21", "_openmp_mutex": "4.5"},
        "conda_on_path": on_path, "failed_imports": []}),
        # The classic builder keeps the conda stage's last image beside the final one.
        "stage-images-0.log": f"{IMAGE_ID}\nsha256:{'f' * 64}\n",
        "stage-images-1.log": f"{IMAGE_ID}\nsha256:{'d' * 64}\n", "stage-images-2.log": f"{IMAGE_ID}\n"}
    context_files = {}

    def run(argv, path, deadline):
        calls.append(argv)
        if argv[:2] == ["docker", "build"]:
            context_files.update({item.name: item for item in (root / "context").iterdir()})
            context_files["micromamba-bytes"] = (root / "context" / "micromamba").read_bytes()
        path.write_text(responses.get(path.name, ""))
        if path.name == "build.log":
            (root / "image-id.txt").write_text(IMAGE_ID)
        return 0, None

    monkeypatch.setattr(builder, "_run", run)
    base = ApprovedImage(key="base", reference="example/base", digest="sha256:" + "e" * 64,
                         runtime=ExecutionRuntime.PYTHON)
    result = await builder.build(base_image=base, requirements=(), import_modules=(), build_root=root,
                                 request_hash=REQUEST_HASH, conda_packages=("samtools=1.21",))

    recipe = (root / "context" / "Dockerfile").read_text()
    assert recipe.startswith(f"FROM {BASE_ID} AS labbio-conda\nLABEL labbio.environment.build={root.name}\n")
    assert "--offline --file /labbio/explicit.txt" in recipe
    assert f"FROM {BASE_ID}\nLABEL labbio.environment.build={root.name}\nUSER root\n" in recipe  # Labelled first.
    assert "COPY --from=labbio-conda /opt/labbio-env /opt/labbio-env\nENV PATH=$PATH:/opt/labbio-env/bin\n" in recipe
    assert context_files["micromamba-bytes"] == policy.micromamba.read_bytes()
    assert {"conda-pkgs", "explicit.txt", "Dockerfile"} <= set(context_files)
    assert (root / "context" / "explicit.txt").read_text().startswith("@EXPLICIT\nfile:///labbio/conda-pkgs/")
    build = next(argv for argv in calls if argv[:2] == ["docker", "build"])
    assert build[build.index("--network") + 1] == "none"
    listing = next(argv for argv in calls if argv[:3] == ["docker", "image", "ls"])
    assert f"label=labbio.environment.build={root.name}" in listing and "dangling=true" in listing
    # Round by round, until only the environment image is left; never the image itself.
    assert [argv for argv in calls if argv[:3] == ["docker", "image", "rm"]] == [
        ["docker", "image", "rm", "sha256:" + "f" * 64], ["docker", "image", "rm", "sha256:" + "d" * 64]]
    # The downloaded packages, the micromamba copy and the solver's repodata cache are not kept.
    assert {path.name for path in (root / "context").iterdir()} == {"Dockerfile", "explicit.txt"}
    assert not (root / "conda").exists()
    if not on_path:
        assert result.failure_code == "VERIFICATION_FAILED"
        return
    assert result.locked_conda == ({"name": "conda::samtools", "version": "1.21", "sha256": "c" * 64},)
    (file,) = result.conda_files  # Recorded so the environment can be installed again without a solve.
    assert (file["name"], file["version"], file["sha256"]) == ("samtools", "1.21", "c" * 64)
    assert file["url"].startswith("https://conda.anaconda.org/") and file["build"]
    assert result.conda_packages == {"samtools": "1.21", "_openmp_mutex": "4.5"} and result.machine == "x86_64"


@pytest.mark.asyncio
async def test_a_failed_docker_conda_build_removes_its_stage_images(tmp_path, monkeypatch):
    from labbioagentos.execution import environment_builder as module
    from test_environment_builder import BASE_ID, REQUEST_HASH

    root = tmp_path / ("a" * 32)
    root.mkdir(mode=0o700)
    builder = module.DockerEnvironmentBuilder(conda=_policy(tmp_path))
    locked = (LockedPackage("samtools", "1.21", "h_0", BASE + "samtools-1.21-h_0.conda", "c" * 64),)
    monkeypatch.setattr(module, "conda_solve", lambda *_: locked)
    monkeypatch.setattr(module, "conda_download", lambda packages, destination, **_: destination.mkdir())
    stage = "sha256:" + "f" * 64
    calls = []

    def run(argv, path, deadline):
        calls.append(argv)
        path.write_text({"base-inspect.log": BASE_ID + "\n", "stage-images-0.log": stage + "\n"}.get(path.name, ""))
        return (1 if path.name == "build.log" else 0), None

    monkeypatch.setattr(builder, "_run", run)
    base = ApprovedImage(key="base", reference="example/base", digest="sha256:" + "e" * 64,
                         runtime=ExecutionRuntime.PYTHON)
    result = await builder.build(base_image=base, requirements=(), import_modules=(), build_root=root,
                                 request_hash=REQUEST_HASH, conda_packages=("samtools",))

    assert result.failure_code == "BUILD_FAILED"
    assert [argv for argv in calls if argv[:3] == ["docker", "image", "rm"]] == [["docker", "image", "rm", stage]]
    assert calls[-1][:3] == ["docker", "image", "ls"]  # Stops once nothing of the stage is left.


# --- The environment service ---


class CondaBuilder(RecordingBuilder):
    supports_conda = True

    def __init__(self, installed=None, **kwargs):
        super().__init__(**kwargs)
        self.installed = {"samtools": "1.21"} if installed is None else installed

    async def build(self, **kwargs):
        self.calls.append(kwargs)
        return EnvironmentBuildResult(
            image_id="sha256:" + "b" * 64, installed_packages={"basepkg": "1.0"},
            available_python_modules=kwargs["import_modules"], machine="x86_64",
            locked_conda=({"name": "conda::samtools", "version": "1.21", "sha256": "c" * 64},),
            conda_files=({"name": "samtools", "version": "1.21", "build": "h_0", "sha256": "c" * 64,
                          "url": "https://conda.anaconda.org/bioconda/linux-64/samtools-1.21-h_0.conda"},),
            conda_packages=self.installed)


def _service(tmp_path, builder, base=None, root="environments"):
    return EnvironmentService(tmp_path / root, ApprovedImageRegistry((base or _image(),)), builder, "user-one")


def test_conda_packages_enter_the_request_hash_only_when_present():
    base = _image().resolved_reference
    assert _request_hash(base, ("x==1",), ()) == _request_hash(base, ("x==1",), (), ())
    assert _request_hash(base, ("x==1",), ()) != _request_hash(base, ("x==1",), (), ("samtools",))


@pytest.mark.asyncio
async def test_a_conda_environment_is_locked_identified_and_survives_restart(tmp_path):
    base = _image().model_copy(update={"entry_points": {ExecutionRuntime.PYTHON: ("python",),
                                                        ExecutionRuntime.SHELL: ("sh",)}})
    builder = CondaBuilder()
    result = await _service(tmp_path, builder, base).build_environment(
        "base", (), conda_packages=("samtools=1.21",))

    assert result["status"] == "SUCCEEDED" and builder.calls[0]["conda_packages"] == ("samtools=1.21",)
    registry = ApprovedImageRegistry((base,))
    EnvironmentService(tmp_path / "environments", registry, RecordingBuilder(error=AssertionError()), "user-one")
    image = registry.resolve(result["image_key"])
    assert image.source_identity.kind.value == "DERIVED" and image.conda_packages == {"samtools": "1.21"}
    assert image.runtimes == {ExecutionRuntime.PYTHON, ExecutionRuntime.SHELL}  # Inherited from the base.


@pytest.mark.asyncio
async def test_a_requested_conda_package_missing_from_the_image_fails_verification(tmp_path):
    result = await _service(tmp_path, CondaBuilder(installed={})).build_environment(
        "base", (), conda_packages=("samtools",))
    assert result["status"] == "FAILED" and result["failure_code"] == "VERIFICATION_FAILED"


@pytest.mark.asyncio
@pytest.mark.parametrize("builder, packages, code", [
    (RecordingBuilder(), ("samtools",), "CONDA_UNAVAILABLE"),
    (CondaBuilder(), ("bioconda::samtools",), "INVALID_CONDA_PACKAGES"),
])
async def test_conda_requests_that_cannot_be_served_have_no_effect(tmp_path, builder, packages, code):
    with pytest.raises(EnvironmentRequestError) as raised:
        await _service(tmp_path, builder).build_environment("base", (), conda_packages=packages)
    assert raised.value.code == code and builder.calls == []


@pytest.mark.asyncio
async def test_a_tampered_conda_lock_fails_closed_on_restart(tmp_path):
    result = await _service(tmp_path, CondaBuilder()).build_environment("base", (), conda_packages=("samtools",))
    path = tmp_path / "environments" / "records" / (result["request_hash"] + ".json")
    record = json.loads(path.read_text())
    record["locked_conda"][0]["sha256"] = "d" * 64
    path.write_text(json.dumps(record))
    with pytest.raises(EnvironmentRequestError):
        _service(tmp_path, CondaBuilder())


# --- Configuration and catalog ---


def test_conda_settings_load_and_refuse_the_defaults_channel(tmp_path):
    from pydantic import ValidationError
    from labbioagentos.local_config import load_settings
    from test_c15_image_identity import TWO_IMAGES, _write

    tool = tmp_path / "micromamba"
    tool.write_text("x")
    conda = (f'[environment]\nroot = "envs"\n[environment.conda]\nmicromamba = "micromamba"\n'
             f'micromamba_sha256 = "{"a" * 64}"\n')
    settings = load_settings(_write(tmp_path, images=TWO_IMAGES + conda))
    assert settings.environment.conda.micromamba == tool and settings.environment.conda.channels == (
        "conda-forge", "bioconda")
    with pytest.raises(ValidationError):
        load_settings(_write(tmp_path, images=TWO_IMAGES + conda + 'channels = ["conda-forge", "defaults"]\n'))


def test_a_catalog_record_keeps_conda_and_python_inventories_apart():
    from test_c15_image_catalog import _record
    from labbioagentos.execution.catalog import ExecutionBackendName

    image = _record().approved_image(ExecutionBackendName.APPTAINER)
    assert image.conda_packages == {"samtools": "1.21"} and image.installed_packages == {}



# --- The Apptainer builder: solved on the control plane, installed in the build job ---


def _cluster_conda(cluster, monkeypatch, **updates):
    from labbioagentos.execution import apptainer_environment_builder as module

    policy = _policy(cluster.tmp_path, **updates)
    locked = (LockedPackage("samtools", "1.21", "h_0", BASE + "samtools-1.21-h_0.conda", "c" * 64),)
    solved = []

    def fake_solve(policy, specs, work):
        solved.append((specs, work, work.is_dir()))
        (work / "root").mkdir()  # Stands in for the solver's repodata cache.
        return locked

    def fake_download(packages, destination, **_):
        # The repodata cache is gone before the download, not only after the job.
        assert not solved[-1][1].exists()
        destination.mkdir(mode=0o700)
        (destination / packages[0].filename).write_bytes(b"package")

    monkeypatch.setattr(module, "conda_solve", fake_solve)
    monkeypatch.setattr(module, "conda_download", fake_download)
    return policy, solved


@pytest.mark.asyncio
async def test_a_cluster_conda_environment_is_installed_in_the_build_job_and_identified(cluster, monkeypatch):
    from uuid import uuid4

    policy, solved = _cluster_conda(cluster, monkeypatch)
    service = cluster.service(conda=policy)

    receipt = await service.build_environment("base", (), (), run_id=uuid4(), conda_packages=("samtools=1.21",))

    assert receipt["status"] == "SUCCEEDED", receipt
    assert solved[0][0] == ("samtools=1.21",) and solved[0][2]
    image = service.image_registry.resolve(receipt["image_key"])
    assert image.conda_packages == {"samtools": "1.21"}
    assert image.source_identity.kind.value == "DERIVED"
    record = json.loads((cluster.tmp_path / "environments" / "records" / f"{receipt['request_hash']}.json").read_text())
    assert record["locked_conda"] == [{"name": "conda::samtools", "version": "1.21", "sha256": "c" * 64}]
    assert record["machine"] == "x86_64"
    attempt = _attempt(cluster)
    conda = attempt / "conda"
    # The lock and the checked micromamba copy stay; packages and the repodata cache do not.
    assert (conda / "explicit.txt").read_text() == (
        "@EXPLICIT\nfile:///.labbio-build/conda/pkgs/samtools-1.21-h_0.conda#" + "c" * 64 + "\n")
    assert (conda / "micromamba").read_bytes() == policy.micromamba.read_bytes()
    assert not (conda / "pkgs").exists() and not (conda / "solve").exists()
    install = next(call for call in cluster.calls() if "/.labbio-build/conda/micromamba" in call)
    assert "--network" in install and "--writable" in install
    assert f"{conda / 'pkgs'}:/.labbio-build/conda/pkgs:ro" in install


@pytest.mark.asyncio
@pytest.mark.parametrize("settings, code", [
    ({"fail": "conda"}, "CONDA_INSTALL_FAILED"),
    ({"path_script": False}, "VERIFICATION_FAILED"),  # The image would not put the tools on PATH.
])
async def test_a_cluster_conda_install_that_fails_or_misses_path_registers_nothing(cluster, monkeypatch,
                                                                                  settings, code):
    from uuid import uuid4

    policy, _ = _cluster_conda(cluster, monkeypatch)
    cluster.configure(**settings)
    service = cluster.service(conda=policy)
    stored = set(cluster.sif_store.root.rglob("*"))

    receipt = await service.build_environment("base", (), (), run_id=uuid4(), conda_packages=("samtools",))

    assert receipt["status"] == "FAILED" and receipt["failure_code"] == code
    assert set(cluster.sif_store.root.rglob("*")) == stored


@pytest.mark.asyncio
async def test_a_failed_solve_submits_no_job(cluster, monkeypatch):
    from uuid import uuid4

    from labbioagentos.execution import apptainer_environment_builder as module

    policy, _ = _cluster_conda(cluster, monkeypatch)

    def no_solution(*_):
        raise CondaError("CONDA_NO_SOLUTION")

    monkeypatch.setattr(module, "conda_solve", no_solution)
    receipt = await cluster.service(conda=policy).build_environment(
        "base", (), (), run_id=uuid4(), conda_packages=("samtools",))

    assert receipt["failure_code"] == "CONDA_NO_SOLUTION"
    assert cluster.calls() == []


@pytest.mark.asyncio
async def test_a_micromamba_copy_that_differs_is_refused(cluster, monkeypatch):
    from uuid import uuid4

    from labbioagentos.execution import apptainer_environment_builder as module

    policy, _ = _cluster_conda(cluster, monkeypatch)
    monkeypatch.setattr(module.shutil, "copyfile", lambda source, target: target.write_bytes(b"other"))

    receipt = await cluster.service(conda=policy).build_environment(
        "base", (), (), run_id=uuid4(), conda_packages=("samtools",))

    assert receipt["failure_code"] == "CONDA_UNAVAILABLE"
    assert cluster.calls() == []


@pytest.mark.asyncio
async def test_a_cluster_builder_without_conda_refuses_conda_requests(cluster):
    from uuid import uuid4

    service = cluster.service()
    assert service.builder.supports_conda is False
    with pytest.raises(EnvironmentRequestError) as raised:
        await service.build_environment("base", (), (), run_id=uuid4(), conda_packages=("samtools",))
    assert raised.value.code == "CONDA_UNAVAILABLE" and cluster.calls() == []


def test_conda_must_be_solved_for_the_compute_nodes(cluster, tmp_path):
    with pytest.raises(ValueError):
        cluster.service(conda=_policy(tmp_path, machine="aarch64"))


def test_cluster_wiring_solves_for_the_configured_machine(tmp_path):
    from contextlib import closing

    from labbioagentos.execution.job_ledger import JobLedger
    from labbioagentos.local_config import _environment_wiring, _image_catalog, load_settings
    from test_apptainer_configuration import _write

    tool = tmp_path / "micromamba"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o555)
    settings = load_settings(_write(
        tmp_path, launcher="", enforcement="systemd-cgroups",
        extra=('[environment]\nroot = "envs"\n[environment.local_apptainer]\nmachine = "aarch64"\n'
               f'[environment.conda]\nmicromamba = "{tool}"\n'
               f'micromamba_sha256 = "{hashlib.sha256(tool.read_bytes()).hexdigest()}"\n'),
    ))
    run = tmp_path / "run"
    with closing(JobLedger(run / "jobs.sqlite")) as ledger:
        wiring = _environment_wiring(settings, run, ledger, _image_catalog(settings))
        builder = wiring["environment_service_factory"](ApprovedImageRegistry(())).builder
    assert builder.supports_conda and builder.conda.machine == "aarch64"
    assert builder.conda.channels == ("conda-forge", "bioconda")


def test_unheld_attempts_lose_their_conda_downloads_and_solver_cache(tmp_path):
    from labbioagentos.execution.apptainer_build import attempt_lease, remove_stale_downloads

    held, stale = tmp_path / ("a" * 32), tmp_path / ("b" * 32)
    for attempt in (held, stale):
        for name in ("pkgs", "solve"):
            (attempt / "conda" / name).mkdir(parents=True)
        (attempt / "conda" / "explicit.txt").write_text("@EXPLICIT\n")
    with attempt_lease(held):
        assert remove_stale_downloads(tmp_path) == 1
    assert sorted(path.name for path in (stale / "conda").iterdir()) == ["explicit.txt"]  # The lock stays.
    assert sorted(path.name for path in (held / "conda").iterdir()) == ["explicit.txt", "pkgs", "solve"]


# --- Cluster builds start from any store in the chain (found in 7b-2) ---


@pytest.mark.asyncio
async def test_a_cluster_build_may_start_from_a_lab_or_personal_sif(cluster, tmp_path):
    from uuid import uuid4

    from labbioagentos.execution.apptainer import SifStore, SifStoreChain

    configured, personal = cluster.sif_store, SifStore(tmp_path / "personal-sif")
    source = tmp_path / "personal.sif"
    source.write_text(json.dumps({"packages": {"basepkg": "1.0"}}))
    base = cluster.base.model_copy(update={"key": "mine", "reference": personal.register(source)})
    service = cluster.service()
    service.builder.sif_store = SifStoreChain((configured, personal))
    service.image_registry = ApprovedImageRegistry((base,))

    receipt = await service.build_environment("mine", ("examplepkg>=1",), ("examplepkg",), run_id=uuid4())

    assert receipt["status"] == "SUCCEEDED", receipt
    reference = service.image_registry.resolve(receipt["image_key"]).resolved_reference
    assert configured.verify(reference)  # The build itself is registered in the configured store.


def test_cluster_wiring_verifies_bases_through_the_store_chain(tmp_path, monkeypatch):
    from contextlib import closing

    from labbioagentos import local_config
    from labbioagentos.execution.job_ledger import JobLedger
    from test_apptainer_configuration import _write

    chain = object()
    monkeypatch.setattr(local_config, "sif_stores", lambda settings: chain)
    settings = local_config.load_settings(_write(
        tmp_path, launcher="", enforcement="systemd-cgroups",
        extra='[environment]\nroot = "envs"\n[environment.local_apptainer]\nmachine = "x86_64"\n'))
    run = tmp_path / "run"
    with closing(JobLedger(run / "jobs.sqlite")) as ledger:
        wiring = local_config._environment_wiring(settings, run, ledger, local_config._image_catalog(settings))
        assert wiring["environment_service_factory"](ApprovedImageRegistry(())).builder.sif_store is chain
