"""C15 step 9a: Agent-built environments have sizes and count against the member's quota (DECIDED-C15-H)."""

from __future__ import annotations

import json
import os

import pytest

from labbioagentos import ApprovedImageRegistry
from labbioagentos.execution.apptainer import SifStore, SifStoreChain, SifStoreError
from labbioagentos.execution.environments import (
    EnvironmentBuildResult,
    EnvironmentLimits,
    EnvironmentRequestError,
    EnvironmentService,
    recorded_environments,
)

from test_execution_environments import DIGEST, _image


class SizedBuilder:
    def __init__(self, size=1000, created=True):
        self.size, self.created, self.discarded = size, created, []

    async def build(self, **kwargs):
        return EnvironmentBuildResult(image_id=DIGEST, installed_packages={"basepkg": "1.0", "examplepkg": "2.0"},
                                      available_python_modules=kwargs["import_modules"], size_bytes=self.size,
                                      created=self.created)

    def discard(self, result):
        self.discarded.append(result.image_id)


def _service(tmp_path, builder, *, max_image=10_000, quota=5_000, admitted=0):
    limits = None if max_image is None else EnvironmentLimits(limits=lambda: (max_image, quota),
                                                             admitted_bytes=lambda: admitted)
    return EnvironmentService(root=tmp_path / "environments", image_registry=ApprovedImageRegistry((_image(),)),
                              builder=builder, owner_user_id="user-one", limits=limits)


async def _build(service, package="examplepkg==2.0"):
    return await service.build_environment("base", (package,), ("examplepkg",))


# --- Limits at build time ---


@pytest.mark.asyncio
async def test_a_build_within_the_limits_records_its_size(tmp_path):
    builder = SizedBuilder(size=1000)
    service = _service(tmp_path, builder)

    receipt = await _build(service)

    assert receipt["status"] == "SUCCEEDED" and builder.discarded == []
    assert service.environment_bytes() == 1000
    assert recorded_environments(tmp_path / "environments") == [1000]


@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs, code", [
    (dict(max_image=999), "IMAGE_TOO_LARGE"),
    (dict(quota=999), "MEMBER_QUOTA_EXCEEDED"),
    (dict(quota=1500, admitted=600), "MEMBER_QUOTA_EXCEEDED"),  # Admitted images share the quota.
])
async def test_a_build_over_a_limit_is_discarded_and_not_registered(tmp_path, kwargs, code):
    builder = SizedBuilder(size=1000)
    service = _service(tmp_path, builder, **kwargs)

    receipt = await _build(service)

    assert receipt["status"] == "FAILED" and receipt["failure_code"] == code
    assert receipt["diagnostics"][0]["message"]  # A fixed, model-safe explanation.
    assert builder.discarded == [DIGEST]
    assert recorded_environments(tmp_path / "environments") == []
    with pytest.raises(Exception):
        service.image_registry.resolve("env-" + receipt["request_hash"])


@pytest.mark.asyncio
async def test_earlier_environments_count_against_the_quota(tmp_path):
    builder = SizedBuilder(size=1000)
    service = _service(tmp_path, builder, quota=1500)
    assert (await _build(service))["status"] == "SUCCEEDED"
    receipt = await _build(service, "examplepkg>=2")
    assert receipt["failure_code"] == "MEMBER_QUOTA_EXCEEDED"


@pytest.mark.asyncio
async def test_a_build_that_cannot_say_its_size_is_not_counted_as_free(tmp_path):
    builder = SizedBuilder(size=None)
    receipt = await _build(_service(tmp_path, builder))
    assert receipt["failure_code"] == "VERIFICATION_FAILED" and builder.discarded == [DIGEST]
    # Without a lab there are no limits, and nothing is checked.
    receipt = await _build(_service(tmp_path / "free", SizedBuilder(size=None), max_image=None))
    assert receipt["status"] == "SUCCEEDED"


def test_recorded_sizes_are_read_strictly(tmp_path):
    records = tmp_path / "records"
    records.mkdir()
    assert recorded_environments(tmp_path / "missing") == []
    (records / "a.json").write_text(json.dumps({"size_bytes": 7}))
    (records / "b.json").write_text(json.dumps({}))  # Recorded before sizes were kept.
    assert recorded_environments(tmp_path) == [7, None]
    for bad in ({"size_bytes": -1}, {"size_bytes": "7"}, {"size_bytes": True}, []):
        (records / "c.json").write_text(json.dumps(bad))
        with pytest.raises(EnvironmentRequestError):
            recorded_environments(tmp_path)


# --- Removing what a build made ---


def test_a_sif_store_says_whether_it_stored_a_new_file_and_its_holder_can_remove_it(tmp_path):
    store = SifStore(tmp_path / "sif")
    source = tmp_path / "x.sif"
    source.write_bytes(b"environment")
    reference, created = store.register_new(source)
    assert created and store.register_new(source) == (reference, False)

    store.remove(reference)

    with pytest.raises(SifStoreError):
        store.path_for(reference)
    with pytest.raises(SifStoreError):
        store.remove(reference)


def test_only_the_holder_removes_and_a_chain_removes_from_the_holding_store(tmp_path, monkeypatch):
    from labbioagentos.execution import apptainer

    first, second = SifStore(tmp_path / "first"), SifStore(tmp_path / "second")
    source = tmp_path / "x.sif"
    source.write_bytes(b"environment")
    reference = second.register(source)
    SifStoreChain((first, second)).remove(reference)
    assert not (tmp_path / "second" / (reference.removeprefix("sha256:") + ".sif")).exists()

    reference = first.register(source)
    other = os.getuid() + 7
    monkeypatch.setattr(apptainer.os, "getuid", lambda: other)
    with pytest.raises(SifStoreError):
        first.remove(reference)


@pytest.mark.asyncio
async def test_the_cluster_builder_reports_size_and_removes_only_a_sif_it_stored(cluster):  # noqa: F811
    service = cluster.service()
    builder = service.builder
    root = cluster.tmp_path / "attempts" / ("c" * 32)
    root.mkdir(parents=True)
    from uuid import uuid4

    from test_environment_builder import REQUEST_HASH

    result = await builder.build(base_image=cluster.base, requirements=("examplepkg>=1",),
                                 import_modules=("examplepkg",), build_root=root, request_hash=REQUEST_HASH,
                                 run_id=uuid4())

    assert result.failure_code is None and result.created
    assert result.size_bytes == cluster.sif_store.path_for(result.image_id).stat().st_size
    builder.discard(EnvironmentBuildResult(image_id=result.image_id, created=False))
    assert cluster.sif_store.path_for(result.image_id)  # Not stored by that build: kept.
    root.with_name("d" * 32).mkdir()
    again = await builder.build(base_image=cluster.base, requirements=("examplepkg>=1",),
                                import_modules=("examplepkg",), build_root=root.with_name("d" * 32),
                                request_hash=REQUEST_HASH, run_id=uuid4())
    assert again.image_id == result.image_id and not again.created  # The same SIF was already stored.
    builder.discard(again)
    assert cluster.sif_store.path_for(result.image_id)
    builder.discard(result)
    with pytest.raises(SifStoreError):
        cluster.sif_store.path_for(result.image_id)


def test_the_docker_builder_removes_only_an_image_it_made(monkeypatch):
    from labbioagentos.execution import environment_builder

    calls = []
    monkeypatch.setattr(environment_builder.subprocess, "run", lambda argv, **_: calls.append(argv))
    builder = environment_builder.DockerEnvironmentBuilder()
    builder.discard(EnvironmentBuildResult(image_id="sha256:" + "d" * 64, created=False))
    builder.discard(EnvironmentBuildResult(image_id="--force"))
    builder.discard(EnvironmentBuildResult(image_id="sha256:" + "d" * 64))
    assert calls == [["docker", "image", "rm", "sha256:" + "d" * 64]]


# --- Admissions and wiring ---


def test_a_personal_admission_counts_the_members_environments(tmp_path):
    from test_c15_admission import _admission, _admit

    admission, registry, _personal = _admission(tmp_path, quota=10**6)
    admission.other_used_bytes = lambda: 10**6
    with pytest.raises(Exception) as raised:
        _admit(admission, registry)
    assert raised.value.code == "MEMBER_QUOTA_EXCEEDED"


def test_environment_builds_get_the_lab_limits_only_with_a_lab(tmp_path):
    from labbioagentos.local_config import _environment_limits, _image_catalog, load_settings
    from labbioagentos.local_lab import LabArea
    from test_c15_lab_area import _settings_file
    from test_c15_promotion import _area, _record
    from labbioagentos.execution.catalog import CatalogStore
    from test_c15_image_identity import _write

    no_lab = load_settings(_write(tmp_path))
    assert _environment_limits(no_lab, _image_catalog(no_lab)) is None
    _area(tmp_path)
    (tmp_path / "member").mkdir(mode=0o700)
    CatalogStore(tmp_path / "member" / "catalog").initialize().add_record(_record(key="mine"))
    settings = load_settings(_settings_file(tmp_path))
    limits = _environment_limits(settings, _image_catalog(settings))
    policy = LabArea(tmp_path / "lab").policy()
    assert limits.limits() == (policy.max_image_bytes, policy.member_quota_bytes)
    assert limits.admitted_bytes() == 1000


@pytest.mark.asyncio
async def test_a_docker_build_whose_size_is_unreadable_fails(backend):  # noqa: F811
    from test_environment_builder import build

    backend.responses["image-size.log"] = (0, None, "unknown\n")
    result = await build(backend)
    assert result.failure_code == "VERIFICATION_FAILED"


def test_the_admission_command_counts_the_members_environments(tmp_path, monkeypatch, capsys):
    from labbioagentos import cli, local_images
    from test_c15_lab_area import _settings_file
    from test_c15_promotion import _area

    _area(tmp_path)
    path = _settings_file(tmp_path)
    path.write_text(path.read_text() + '[environment]\nroot = "envs"\n')
    (tmp_path / "member").mkdir(mode=0o700)
    (tmp_path / "envs" / "records").mkdir(parents=True)
    (tmp_path / "envs" / "records" / ("a" * 64 + ".json")).write_text(json.dumps({"size_bytes": 4321}))
    seen = []

    class Recording:
        def __init__(self, **kwargs):
            seen.append(kwargs["other_used_bytes"]())
            raise RuntimeError("stop")

    monkeypatch.setattr(local_images, "ImageAdmission", Recording)
    cli.main(["image-admit", "--config", str(path), "--key", "mine", "--purpose", "Mine.",
              "--oci", "registry.example/a/b@sha256:" + "a" * 64])
    assert seen == [4321]


from test_apptainer_environment_builder import cluster  # noqa: E402,F401 (fixture)
from test_environment_builder import backend  # noqa: E402,F401 (fixture)
