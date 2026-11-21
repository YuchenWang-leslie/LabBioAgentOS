"""C15 step 2: backend-neutral image identity, provenance and multiple base images."""

from __future__ import annotations

import hashlib
import json

import pytest
from pydantic import ValidationError

from labbioagentos import ApprovedImageRegistry, ExecutionReceipt
from labbioagentos.execution.environments import (
    RECIPE_VERSION,
    EnvironmentBuildResult,
    EnvironmentRequestError,
    EnvironmentService,
)
from labbioagentos.execution.identity import (
    SourceIdentity,
    SourceKind,
    derived_identity,
    legacy_identity,
)
from labbioagentos.local_config import ProviderConfigurationError, load_settings

from test_execution_environments import DIGEST, RecordingBuilder, _image

OCI = SourceIdentity(kind=SourceKind.OCI_MANIFEST, digest="sha256:" + "c" * 64, platform="linux/amd64")
LOCK = [{"name": "examplepkg", "version": "2.0", "sha256": "d" * 64}]


# --- Source identity ---


def test_a_legacy_identity_is_its_realization_verbatim():
    identity = legacy_identity("registry.example/tool@sha256:" + "a" * 64)
    assert identity.kind is SourceKind.LEGACY
    assert identity.canonical == "registry.example/tool@sha256:" + "a" * 64


def test_an_oci_identity_names_its_platform_in_the_canonical_form():
    assert OCI.canonical == "oci_manifest:sha256:" + "c" * 64 + "@linux/amd64"


@pytest.mark.parametrize("values", [
    {"kind": SourceKind.OCI_MANIFEST, "digest": "sha256:" + "c" * 64},
    {"kind": SourceKind.SIF, "digest": "sha256:" + "c" * 64, "platform": "linux/amd64"},
    {"kind": SourceKind.SIF, "digest": "sha256:" + "C" * 64},
    {"kind": SourceKind.DERIVED, "digest": "c" * 64},
    {"kind": SourceKind.LEGACY, "digest": "two words"},
    {"kind": SourceKind.OCI_ARCHIVE, "digest": "sha256:" + "c" * 64, "platform": "Linux AMD64"},
])
def test_malformed_source_identities_are_refused(values):
    with pytest.raises(ValidationError):
        SourceIdentity(**values)


def test_a_derived_identity_depends_on_exactly_its_inputs():
    first = derived_identity(OCI, LOCK, RECIPE_VERSION, "x86_64")
    assert first.kind is SourceKind.DERIVED
    assert first == derived_identity(OCI, list(reversed(LOCK)), RECIPE_VERSION, "x86_64")
    other_base = SourceIdentity(kind=SourceKind.SIF, digest="sha256:" + "e" * 64)
    changed = [
        derived_identity(other_base, LOCK, RECIPE_VERSION, "x86_64"),
        derived_identity(OCI, [{**LOCK[0], "sha256": "f" * 64}], RECIPE_VERSION, "x86_64"),
        derived_identity(OCI, LOCK, "another-recipe", "x86_64"),
        derived_identity(OCI, LOCK, RECIPE_VERSION, "aarch64"),
    ]
    assert first not in changed and len(set(changed)) == len(changed)


def test_a_derived_identity_needs_a_lock_and_a_plain_machine():
    with pytest.raises(ValueError):
        derived_identity(OCI, [], RECIPE_VERSION, "x86_64")
    with pytest.raises(ValueError):
        derived_identity(OCI, LOCK, RECIPE_VERSION, "x86 64")


def test_an_image_without_a_recorded_source_is_legacy():
    image = _image()
    assert image.source_identity == legacy_identity(image.resolved_reference)
    assert _image().model_copy(update={"source": OCI}).source_identity == OCI


# --- Environment requests are keyed by the source, not the realization ---


def _service(tmp_path, base, builder=None, root="environments"):
    return EnvironmentService(tmp_path / root, ApprovedImageRegistry((base,)),
                              builder or RecordingBuilder(), "user-one")


@pytest.mark.asyncio
async def test_a_legacy_base_keeps_the_request_hash_used_before_c15(tmp_path):
    base = _image()
    result = await _service(tmp_path, base).build_environment("base", ("examplepkg==2.0",), ("examplepkg",))

    payload = {"base_image": base.resolved_reference, "requirements": ["examplepkg==2.0"],
               "import_modules": ["examplepkg"], "recipe_version": RECIPE_VERSION}
    before = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert result["request_hash"] == before


@pytest.mark.asyncio
async def test_two_realizations_of_one_source_share_one_environment_request(tmp_path):
    docker = _image(digest="sha256:" + "1" * 64).model_copy(update={"source": OCI})
    sif = _image(digest="sha256:" + "2" * 64).model_copy(update={"source": OCI})

    first = await _service(tmp_path, docker, root="docker").build_environment("base", ("examplepkg==2.0",))
    second = await _service(tmp_path, sif, root="sif").build_environment("base", ("examplepkg==2.0",))

    assert first["request_hash"] == second["request_hash"]
    assert first["image_key"] == second["image_key"]


@pytest.mark.asyncio
async def test_a_locked_build_gets_a_derived_identity_that_survives_restart(tmp_path):
    base = _image().model_copy(update={"source": OCI})
    builder = RecordingBuilder(EnvironmentBuildResult(
        image_id=DIGEST, installed_packages={"basepkg": "1.0", "examplepkg": "2.0"},
        available_python_modules=("basepkg", "examplepkg"), locked_wheels=tuple(LOCK), machine="x86_64"))
    result = await _service(tmp_path, base, builder).build_environment(
        "base", ("examplepkg==2.0",), ("examplepkg",))

    registry = ApprovedImageRegistry((base,))
    EnvironmentService(tmp_path / "environments", registry,
                       RecordingBuilder(error=AssertionError("No rebuild")), "user-one")
    image = registry.resolve(result["image_key"])
    assert image.source_identity == derived_identity(OCI, LOCK, RECIPE_VERSION, "x86_64")
    assert image.reference == DIGEST


@pytest.mark.asyncio
async def test_an_unlocked_build_claims_no_backend_neutral_identity(tmp_path):
    base = _image().model_copy(update={"source": OCI})
    service = _service(tmp_path, base)
    result = await service.build_environment("base", ("examplepkg==2.0",))

    image = service.image_registry.resolve(result["image_key"])
    assert image.source is None and image.source_identity.kind is SourceKind.LEGACY


@pytest.mark.asyncio
async def test_a_tampered_derived_identity_fails_closed_on_restart(tmp_path):
    base = _image().model_copy(update={"source": OCI})
    builder = RecordingBuilder(EnvironmentBuildResult(
        image_id=DIGEST, installed_packages={"basepkg": "1.0", "examplepkg": "2.0"},
        available_python_modules=("basepkg",), locked_wheels=tuple(LOCK), machine="x86_64"))
    result = await _service(tmp_path, base, builder).build_environment("base", ("examplepkg==2.0",))
    path = tmp_path / "environments" / "records" / (result["request_hash"] + ".json")
    record = json.loads(path.read_text())
    record["machine"] = "aarch64"
    path.write_text(json.dumps(record))

    with pytest.raises(EnvironmentRequestError):
        _service(tmp_path, base)


@pytest.mark.asyncio
async def test_a_record_written_before_c15_still_loads_as_legacy(tmp_path):
    base = _image()
    result = await _service(tmp_path, base).build_environment("base", ("examplepkg==2.0",))
    path = tmp_path / "environments" / "records" / (result["request_hash"] + ".json")
    record = json.loads(path.read_text())
    for name in ("base_source_identity", "machine"):
        record.pop(name)
    record["image"].pop("source")
    path.write_text(json.dumps(record))

    registry = ApprovedImageRegistry((base,))
    EnvironmentService(tmp_path / "environments", registry, RecordingBuilder(), "user-one")
    assert registry.resolve(result["image_key"]).source_identity.kind is SourceKind.LEGACY


# --- Provenance: trace and Gold lineage carry the source; models never see it ---


def test_execution_results_and_trace_carry_the_source_but_receipts_do_not(tmp_path):
    from labbioagentos import TraceEventType
    from test_phase6_docker_execution import FakeDockerRunner, _environment, _plan

    store, executor, sink = _environment(tmp_path, FakeDockerRunner(), traced=True)
    image = executor.image_registry.resolve("python-analysis")
    executor.image_registry = ApprovedImageRegistry((image.model_copy(update={"source": OCI}),))

    result = executor.execute(_plan())

    assert result.image_source == OCI
    planned = next(event for event in sink.read() if event.event_type is TraceEventType.EXECUTION_PLANNED)
    assert SourceIdentity.model_validate(planned.payload["image_source"], strict=False) == OCI
    receipt = json.dumps(ExecutionReceipt.from_result(result).model_dump(mode="json"))
    assert "c" * 64 not in receipt and "image_source" not in receipt


@pytest.mark.parametrize("payload, expected", [
    (OCI.model_dump(mode="json"), OCI.canonical),
    ({"kind": "OCI_MANIFEST", "digest": "sha256:" + "c" * 64}, None),
    ("not an identity", None),
    (None, None),
])
def test_gold_lineage_records_a_valid_source_and_never_repairs_a_malformed_one(payload, expected):
    from labbioagentos.skills.source import _source_identity

    assert _source_identity(payload) == expected


# --- Configuration: several base images, no silent single-image fallback ---

SETTINGS = '''
result_root = "results"
input_roots = ["inputs"]
[identity]
user_id = "u"
project_id = "p"
lab_id = "l"
[provider]
endpoint = "https://provider.invalid/v1"
env_file = "provider.env"
api_key_env = "KEY"
model_identifier = "m"
[execution]
{execution}
[execution.resources]
cpus = 1.0
memory_mb = 512
pids_limit = 64
timeout_seconds = 60.0
{images}
'''
TWO_IMAGES = '''
[[execution.images]]
key = "python-base"
reference = "sha256:{a}"
available_python_modules = ["numpy"]
[[execution.images]]
key = "tools"
reference = "sha256:{b}"
'''.format(a="a" * 64, b="b" * 64)


def _write(tmp_path, execution='default_image = "python-base"', images=TWO_IMAGES):
    (tmp_path / "inputs").mkdir(exist_ok=True)
    path = tmp_path / "runtime.toml"
    if "backend" not in execution:
        execution = 'backend = "docker"\n' + execution  # The source default is Apptainer since main a6da8a5.
    path.write_text(SETTINGS.format(execution=execution, images=images), encoding="utf-8")
    return path


def test_several_base_images_load_and_all_reach_the_registry(tmp_path):
    from labbioagentos import cli
    from labbioagentos.local_config import build_application

    settings = load_settings(_write(tmp_path))

    assert [image.key for image in settings.execution.approved_images()] == ["python-base", "tools"]
    assert settings.execution.default_approved_image().available_python_modules == ("numpy",)
    application = build_application(settings, tmp_path / "run", load_provider=False)
    try:
        assert application.image_registry.resolve("tools").reference == "sha256:" + "b" * 64
        assert application.execution_capability.image_key == "python-base"
    finally:
        cli._close(application)


@pytest.mark.parametrize("execution, images", [
    ('default_image = "missing"', TWO_IMAGES),
    ('default_image = "python-base"', TWO_IMAGES.replace('key = "tools"', 'key = "python-base"')),
    ('default_image = "python-base"', ""),
])
def test_an_inconsistent_image_list_fails_at_load(tmp_path, execution, images):
    with pytest.raises(ValidationError):
        load_settings(_write(tmp_path, execution, images))


def test_apptainer_needs_a_registered_sif_for_every_image(tmp_path):
    apptainer = ('backend = "apptainer"\ndefault_image = "python-base"\n'
                 '[execution.apptainer]\nbinary = "/usr/bin/apptainer"\nsif_store = "sif"\n'
                 'scratch_root = "scratch"\nresource_enforcement = "systemd-cgroups"')
    images = TWO_IMAGES.replace('reference = "sha256:' + "b" * 64 + '"', 'reference = "lab/tools:1.0"')
    with pytest.raises(ValidationError):
        load_settings(_write(tmp_path, apptainer, images))


@pytest.mark.parametrize("legacy", ["image_key", "image_reference", "available_python_modules"])
def test_the_single_image_settings_require_an_explicit_migration(tmp_path, legacy):
    values = {"image_key": '"python-base"', "image_reference": f'"sha256:{"a" * 64}"',
              "available_python_modules": "[]"}
    path = _write(tmp_path, f'{legacy} = {values[legacy]}\ndefault_image = "python-base"')

    with pytest.raises(ProviderConfigurationError) as raised:
        load_settings(path)
    assert raised.value.code == "EXECUTION_IMAGES_MIGRATION_REQUIRED"
    assert raised.value.fields == (f"execution.{legacy}",)


def test_the_cli_reports_the_migration_by_code_without_values(tmp_path, capsys):
    from labbioagentos import cli

    reference = "sha256:" + "9" * 64
    path = _write(tmp_path, f'image_key = "python-base"\nimage_reference = "{reference}"', images="")

    assert cli.main(["status", "--config", str(path), "--run-dir", str(tmp_path / "run")]) == 1
    emitted = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert emitted["error"] == "EXECUTION_IMAGES_MIGRATION_REQUIRED"
    assert emitted["fields"] == ["execution.image_key", "execution.image_reference"]
    assert reference not in json.dumps(emitted)


def test_the_examples_use_the_image_tables(tmp_path):
    import tomllib
    from pathlib import Path

    for name in ("local-runtime.toml", "cluster-runtime.toml"):
        raw = tomllib.loads((Path(__file__).parents[1] / "examples" / name).read_text())
        assert not set(raw["execution"]) & {"image_key", "image_reference"}
        assert raw["execution"]["default_image"] in {image["key"] for image in raw["execution"]["images"]}
