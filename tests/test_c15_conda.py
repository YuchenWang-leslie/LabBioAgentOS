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
async def test_the_docker_recipe_installs_offline_in_a_separate_stage_and_appends_path(tmp_path, monkeypatch):
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
    responses = {"base-inspect.log": BASE_ID + "\n", "verification.log": json.dumps({
        "installed_packages": {}, "conda_packages": {"samtools": "1.21", "_openmp_mutex": "4.5"},
        "failed_imports": []})}

    def run(argv, path, deadline):
        calls.append(argv)
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
    assert recipe.startswith(f"FROM {BASE_ID} AS labbio-conda\n")
    assert "--offline --file /labbio/explicit.txt" in recipe
    assert "COPY --from=labbio-conda /opt/labbio-env /opt/labbio-env\nENV PATH=$PATH:/opt/labbio-env/bin\n" in recipe
    assert (root / "context" / "micromamba").read_bytes() == policy.micromamba.read_bytes()
    assert (root / "context" / "explicit.txt").read_text().startswith("@EXPLICIT\nfile:///labbio/conda-pkgs/")
    build = next(argv for argv in calls if argv[:2] == ["docker", "build"])
    assert build[build.index("--network") + 1] == "none"
    assert result.locked_conda == ({"name": "conda::samtools", "version": "1.21", "sha256": "c" * 64},)
    assert result.conda_packages == {"samtools": "1.21", "_openmp_mutex": "4.5"} and result.machine == "x86_64"


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

