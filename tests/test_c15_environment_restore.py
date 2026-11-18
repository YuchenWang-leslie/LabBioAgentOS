"""C15 step 9c-2b: rebuilding a deleted Agent environment from its record (DECIDED-C15-H, refined 2026-11-18)."""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from labbioagentos import ApprovedImageRegistry
from labbioagentos.execution.catalog import CatalogStore, RealizationAction
from labbioagentos.execution.conda import LockedPackage
from labbioagentos.execution.environments import (
    EnvironmentBuildResult,
    EnvironmentRequestError,
    EnvironmentService,
    check_restored,
    load_environment_record,
    restore_locks,
)

from test_c15_conda import _policy
from test_c15_image_delete import _cli, _delete, _retire, lab  # noqa: F401 - the fixture

CONDA_URL = "https://conda.anaconda.org/conda-forge/linux-64/samtools-1.21-h_0.conda"
WHEEL = {"name": "examplepkg", "version": "2.0", "sha256": "e" * 64}
CONDA_LOCK = {"name": "conda::samtools", "version": "1.21", "sha256": "c" * 64}
CONDA_FILE = {"name": "samtools", "version": "1.21", "build": "h_0", "url": CONDA_URL, "sha256": "c" * 64}


class LockingBuilder:
    """Builds exactly what it is asked for, reporting the locks an identity is derived from."""

    supports_conda = True

    def __init__(self, image_id="sha256:" + "b" * 64, *, conda=False, size=1000, policy=None, **changes):
        self.image_id, self.with_conda, self.size, self.conda = image_id, conda, size, policy
        self.changes, self.calls, self.discarded = changes, [], []

    async def build(self, **kwargs):
        self.calls.append(kwargs)
        values = dict(
            image_id=self.image_id, installed_packages={"examplepkg": "2.0"},
            available_python_modules=kwargs["import_modules"], locked_wheels=(WHEEL,), machine="x86_64",
            size_bytes=self.size,
            **({"locked_conda": (CONDA_LOCK,), "conda_files": (CONDA_FILE,), "conda_packages": {"samtools": "1.21"}}
               if self.with_conda else {}))
        values.update(self.changes)
        return EnvironmentBuildResult(**values)

    def discard(self, result):
        self.discarded.append(result.image_id)


# --- Records keep their conda files ---


async def _built(tmp_path, builder, *, conda=False, owner="admin", registry=None, catalog=None):
    from test_execution_environments import _image

    service = EnvironmentService(tmp_path / "environments", registry or ApprovedImageRegistry((_image(),)),
                                 builder, owner, catalog=catalog)
    built = await service.build_environment("base" if registry is None else "python-base", ("examplepkg==2.0",),
                                            (), conda_packages=("samtools=1.21",) if conda else ())
    assert built["status"] == "SUCCEEDED", built
    return service, built["image_key"], tmp_path / "environments" / "records" / (built["request_hash"] + ".json")


@pytest.mark.asyncio
async def test_a_conda_environment_records_its_files_and_a_wheel_one_none(tmp_path):
    _service, _key, path = await _built(tmp_path / "conda", LockingBuilder(conda=True), conda=True)
    record = json.loads(path.read_text())
    assert record["conda_files"] == [CONDA_FILE] and record["locked_conda"] == [CONDA_LOCK]
    _service, _key, path = await _built(tmp_path / "wheels", LockingBuilder())
    assert "conda_files" not in json.loads(path.read_text())


@pytest.mark.asyncio
@pytest.mark.parametrize("files", [(), ({**CONDA_FILE, "sha256": "d" * 64},), ({**CONDA_FILE, "url": "ftp://x/y"},)])
async def test_a_conda_build_without_matching_files_is_not_registered(tmp_path, files):
    from test_execution_environments import _image

    service = EnvironmentService(tmp_path / "environments", ApprovedImageRegistry((_image(),)),
                                 LockingBuilder(conda=True, conda_files=files), "admin")
    built = await service.build_environment("base", (), (), conda_packages=("samtools=1.21",))
    assert built["status"] == "FAILED" and built["failure_code"] == "VERIFICATION_FAILED"


@pytest.mark.asyncio
async def test_a_record_whose_files_disagree_with_its_lock_is_refused(tmp_path):
    _service, _key, path = await _built(tmp_path, LockingBuilder(conda=True), conda=True)
    record = json.loads(path.read_text())
    record["conda_files"][0]["version"] = "1.22"
    path.chmod(0o600)
    path.write_text(json.dumps(record))
    with pytest.raises(EnvironmentRequestError) as raised:
        load_environment_record(path, "admin")
    assert raised.value.code == "ENVIRONMENT_CACHE_INVALID"


# --- The locks a record restores from ---


def _record(**changes):
    record = {"locked_wheels": [WHEEL]}
    record.update(changes)
    return record


def test_wheels_are_pinned_to_their_recorded_hash(tmp_path):
    pinned = restore_locks(_record(), None, tmp_path / "work")
    (wheel,) = pinned.wheels
    assert (wheel.name, wheel.version, wheel.hashes, pinned.conda) == ("examplepkg", "2.0", frozenset({"e" * 64}), ())


def test_recorded_conda_files_are_used_without_a_solve(tmp_path, monkeypatch):
    from labbioagentos.execution import environments

    monkeypatch.setattr(environments, "conda_solve", lambda *_args: pytest.fail("no solve"))
    record = _record(requested_conda=["samtools=1.21"], locked_conda=[CONDA_LOCK], conda_files=[CONDA_FILE])
    pinned = restore_locks(record, _policy(tmp_path), tmp_path / "work")
    assert pinned.conda == (LockedPackage(name="samtools", version="1.21", build="h_0", url=CONDA_URL,
                                          sha256="c" * 64),)


@pytest.mark.parametrize("url", ["https://conda.anaconda.org/defaults/linux-64/samtools-1.21-h_0.conda",
                                 "https://conda.anaconda.org/conda-forge/osx-64/samtools-1.21-h_0.conda"])
def test_recorded_files_outside_the_allowed_channels_are_refused(tmp_path, url):
    record = _record(requested_conda=["samtools=1.21"], locked_conda=[CONDA_LOCK],
                     conda_files=[{**CONDA_FILE, "url": url}])
    with pytest.raises(EnvironmentRequestError) as raised:
        restore_locks(record, _policy(tmp_path), tmp_path / "work")
    assert raised.value.code == "CONDA_LOCK_INVALID"


@pytest.mark.parametrize("sha, code", [("c" * 64, None), ("f" * 64, "CONDA_LOCK_MISMATCH")])
def test_an_older_record_is_solved_once_and_must_reproduce_its_lock(tmp_path, monkeypatch, sha, code):
    from labbioagentos.execution import environments

    solved = []

    def solve(policy, specs, work):
        solved.append((specs, work.is_dir()))
        (work / "repodata").write_text("large")
        return (LockedPackage(name="samtools", version="1.21", build="h_0", url=CONDA_URL, sha256=sha),)

    monkeypatch.setattr(environments, "conda_solve", solve)
    record = _record(requested_conda=["samtools=1.21"], locked_conda=[CONDA_LOCK])
    if code is None:
        assert restore_locks(record, _policy(tmp_path), tmp_path / "work").conda[0].sha256 == sha
    else:
        with pytest.raises(EnvironmentRequestError) as raised:
            restore_locks(record, _policy(tmp_path), tmp_path / "work")
        assert raised.value.code == code
    assert solved == [(("samtools=1.21",), True)] and not (tmp_path / "work").exists()


@pytest.mark.parametrize("files", [
    [{**CONDA_FILE, "sha256": "C" * 64}],
    [CONDA_FILE, {**CONDA_FILE, "url": CONDA_URL.replace("1.21-h_0", "1.21-h_1"), "build": "h_1"}],
    [{key: value for key, value in CONDA_FILE.items() if key != "build"}],
])
def test_recorded_files_are_checked_on_their_own(tmp_path, files):
    from labbioagentos.execution.conda import CondaError, packages_from_files

    with pytest.raises(CondaError) as raised:
        packages_from_files(files, _policy(tmp_path))
    assert raised.value.code == "CONDA_LOCK_INVALID"


@pytest.mark.parametrize("record, policy, code", [
    ({"locked_wheels": []}, False, "RESTORE_UNVERIFIABLE"),
    ({"requested_conda": ["samtools=1.21"], "locked_conda": [CONDA_LOCK], "conda_files": [CONDA_FILE]}, False,
     "CONDA_UNAVAILABLE"),
])
def test_what_cannot_be_pinned_is_refused(tmp_path, record, policy, code):
    with pytest.raises(EnvironmentRequestError) as raised:
        restore_locks(record, _policy(tmp_path) if policy else None, tmp_path / "work")
    assert raised.value.code == code


# --- Checking the rebuilt environment ---


async def _checked(tmp_path, **changes):
    from test_execution_environments import _image

    service, key, path = await _built(tmp_path, LockingBuilder(conda=True), conda=True)
    record, image = load_environment_record(path, "admin")
    result = await LockingBuilder(conda=True, **changes).build(import_modules=image.available_python_modules)
    return record, image, _image(), result


@pytest.mark.asyncio
async def test_the_same_locks_on_the_same_base_are_the_recorded_environment(tmp_path):
    check_restored(*await _checked(tmp_path, image_id="sha256:" + "9" * 64))


@pytest.mark.asyncio
@pytest.mark.parametrize("changes, code", [
    ({"failure_code": "BUILD_FAILED"}, "BUILD_FAILED"),
    ({"machine": None}, "VERIFICATION_FAILED"),
    ({"machine": "aarch64"}, "SOURCE_MISMATCH"),
    ({"locked_wheels": ({**WHEEL, "sha256": "f" * 64},)}, "SOURCE_MISMATCH"),
    ({"installed_packages": {"examplepkg": "2.1"}}, "INVENTORY_MISMATCH"),
    ({"conda_packages": {"samtools": "1.22"}}, "INVENTORY_MISMATCH"),
    ({"available_python_modules": ("other",)}, "INVENTORY_MISMATCH"),
])
async def test_a_rebuild_that_differs_from_the_record_is_refused(tmp_path, changes, code):
    with pytest.raises(EnvironmentRequestError) as raised:
        check_restored(*await _checked(tmp_path, **changes))
    assert raised.value.code == code


@pytest.mark.asyncio
async def test_a_rebuild_on_another_base_is_another_environment(tmp_path):
    from test_execution_environments import _image

    record, image, _base, result = await _checked(tmp_path)
    with pytest.raises(EnvironmentRequestError) as raised:
        check_restored(record, image, _image(digest="sha256:" + "7" * 64), result)
    assert raised.value.code == "SOURCE_MISMATCH"


# --- The command ---


@pytest.fixture
def environment(lab, monkeypatch):
    """The lab fixture with an Agent environment built on python-base, deprecated and deleted."""
    import asyncio

    from labbioagentos import local_images
    from labbioagentos.execution.admission_attempts import AdmissionAttempts
    from labbioagentos.local_config import _image_catalog, load_settings

    settings = load_settings(lab.path)
    registry = ApprovedImageRegistry(settings.execution.approved_images())
    service = EnvironmentService(settings.environment.root, registry, LockingBuilder(), "admin",
                                 catalog=_image_catalog(settings))
    built = asyncio.run(service.build_environment("python-base", ("examplepkg==2.0",), ()))
    assert built["status"] == "SUCCEEDED", built
    lab.key = built["image_key"]
    lab.record = settings.environment.root / "records" / (built["request_hash"] + ".json")
    _retire(lab.capsys, lab, lab.key)
    assert _delete(lab.capsys, lab, lab.key)[0] == 0
    lab.builder, lab.jobs = LockingBuilder("sha256:" + "9" * 64), []

    @contextmanager
    def jobs(settings, member_root, *, sif_store=None, lock_builds=False):
        lab.jobs.append((sif_store, lock_builds))
        yield local_images._AdmissionJobs(attempts=AdmissionAttempts(member_root / "admissions"),
                                          realizer=None, probe=None, builder=lab.builder)

    monkeypatch.setattr(local_images, "_admission_jobs", jobs)
    return lab


@pytest.fixture(autouse=True)
def _capsys_on_lab(request, capsys):
    if "lab" in request.fixturenames:
        request.getfixturevalue("lab").capsys = capsys


def _restore(lab, *extra):
    return _cli(lab.capsys, lab, "image-restore", "--key", lab.key, "--reason", "Needed again.", *extra)


def _catalog(lab):
    from labbioagentos.local_config import _image_catalog, load_settings

    return _image_catalog(load_settings(lab.path))


def test_a_deleted_environment_is_rebuilt_from_its_record(environment):
    lab = environment
    code, output = _restore(lab)

    assert code == 0, output
    assert output["delivery"] == "environment_record" and output["conda"] is None
    assert output["state"] == "DEPRECATED" and output["produced_by"].startswith("restore-env:")
    (call,) = lab.builder.calls
    assert call["requirements"] == () and call["base_image"].key == "python-base"
    assert [(wheel.name, wheel.hashes) for wheel in call["pinned"].wheels] == [("examplepkg", frozenset({"e" * 64}))]
    assert call["request_hash"] == lab.key.removeprefix("env-") and lab.jobs == [(None, True)]
    catalog = _catalog(lab)
    assert not catalog.deleted(lab.key) and catalog.realization(lab.key) == "sha256:" + "9" * 64
    events = CatalogStore(lab.tmp / "member" / "catalog").realization_events()
    assert [event.action for event in events] == [RealizationAction.DELETED, RealizationAction.RESTORED]
    assert lab.builder.discarded == []


@pytest.mark.parametrize("changes, code", [
    ({"installed_packages": {"examplepkg": "2.1"}}, "INVENTORY_MISMATCH"),
    ({"failure_code": "BUILD_FAILED"}, "BUILD_FAILED"),
    ({"size_bytes": 10**15}, "IMAGE_TOO_LARGE"),
    ({"size_bytes": None}, "VERIFICATION_FAILED"),
])
def test_a_rebuild_that_is_not_the_environment_restores_nothing_and_is_discarded(environment, changes, code):
    lab = environment
    lab.builder = LockingBuilder("sha256:" + "9" * 64, **changes)
    code_, output = _restore(lab)
    assert code_ == 1 and output["error"] == code and _catalog(lab).deleted(lab.key)
    assert lab.builder.discarded == ["sha256:" + "9" * 64]


def test_the_rebuilt_environment_counts_against_the_members_quota(environment, monkeypatch):
    from labbioagentos import local_lab

    lab = environment
    policy = lab.area.policy()
    # "mine" and "from-sif" hold 1000 bytes each; the deleted environment itself is not counted.
    monkeypatch.setattr(local_lab.LabArea, "policy", lambda self: policy.model_copy(update={
        "member_quota_bytes": 2999}))
    assert _restore(lab)[1]["error"] == "MEMBER_QUOTA_EXCEEDED" and lab.builder.discarded
    monkeypatch.setattr(local_lab.LabArea, "policy", lambda self: policy.model_copy(update={
        "member_quota_bytes": 3000}))
    lab.builder.discarded.clear()
    assert _restore(lab)[0] == 0 and lab.builder.discarded == []


@pytest.mark.parametrize("extra", [("--archive", "x.tar"), ("--sif", "x.sif"),
                                   ("--base", "python-base", "--lock", "x.lock")])
def test_an_environment_takes_no_delivery(environment, extra):
    lab = environment
    assert _restore(lab, *extra)[1]["error"] == "RESTORE_FORM_INVALID" and lab.builder.calls == []


@pytest.mark.parametrize("base_gone", [False, True])
def test_an_environment_built_before_lock_lists_is_not_restored(environment, base_gone):
    lab = environment
    record = json.loads(lab.record.read_text())
    image = record["image"]
    image.pop("source", None)
    record.update(locked_wheels=[], machine=None, image=image)
    lab.record.chmod(0o600)
    lab.record.write_text(json.dumps(record))
    if base_gone:  # Refused for what it is, before any base is looked for.
        lab.path.write_text(lab.path.read_text().replace("sha256:" + "a" * 64, "sha256:" + "4" * 64))
    assert _restore(lab)[1]["error"] == "RESTORE_UNVERIFIABLE" and lab.builder.calls == []


@pytest.mark.parametrize("files", [True, False])
def test_a_conda_environment_is_rebuilt_from_its_files_or_an_exact_solve(environment, monkeypatch, tmp_path, files):
    import asyncio

    from labbioagentos.execution import environments
    from labbioagentos.local_config import _image_catalog, load_settings

    lab = environment
    settings = load_settings(lab.path)
    catalog = _image_catalog(settings)
    registry = ApprovedImageRegistry(settings.execution.approved_images())
    service = EnvironmentService(settings.environment.root, registry,
                                 LockingBuilder("sha256:" + "8" * 64, conda=True), "admin", catalog=catalog)
    built = asyncio.run(service.build_environment("python-base", ("examplepkg==2.0",), (),
                                                  conda_packages=("samtools=1.21",)))
    lab.key = built["image_key"]
    path = settings.environment.root / "records" / (built["request_hash"] + ".json")
    if not files:  # Recorded before conda files were kept.
        record = json.loads(path.read_text())
        del record["conda_files"]
        path.chmod(0o600)
        path.write_text(json.dumps(record))
    solved = []
    monkeypatch.setattr(environments, "conda_solve", lambda policy, specs, work: solved.append(specs) or (
        LockedPackage(name="samtools", version="1.21", build="h_0", url=CONDA_URL, sha256="c" * 64),))
    catalog.set_state(lab.key, catalog.state(lab.key).DEPRECATED, actor="admin", reason="Retired.")
    catalog.record_realization(lab.key, resolved_image="sha256:" + "8" * 64, action=RealizationAction.DELETED,
                               actor="admin", reason="Freeing space.")
    lab.builder = LockingBuilder("sha256:" + "9" * 64, conda=True, policy=_policy(tmp_path))

    code, output = _restore(lab)

    assert code == 0, output
    assert output["conda"] == ("files" if files else "solved") and solved == ([] if files else [("samtools=1.21",)])
    (call,) = lab.builder.calls
    assert [package.url for package in call["pinned"].conda] == [CONDA_URL]


def test_a_damaged_record_is_not_rebuilt(environment):
    lab = environment
    lab.record.chmod(0o600)
    lab.record.write_text("{}")
    assert _restore(lab)[1]["error"] == "ENVIRONMENT_RECORD_INVALID" and lab.builder.calls == []


@pytest.mark.parametrize("change, error", [
    ("image-deprecate", None), ("image-revoke", "BASE_REVOKED"), ("missing", "BASE_NOT_FOUND"),
])
def test_the_base_is_found_by_its_source(environment, change, error, monkeypatch):
    from labbioagentos.execution.identity import SourceIdentity, SourceKind

    lab = environment
    if change == "missing":
        record = json.loads(lab.record.read_text())
        assert record["base_source_identity"] == SourceIdentity(
            kind=SourceKind.LEGACY, digest="sha256:" + "a" * 64).model_dump(mode="json")
        lab.path.write_text(lab.path.read_text().replace("sha256:" + "a" * 64, "sha256:" + "4" * 64))
    else:
        assert _cli(lab.capsys, lab, change, "--key", "python-base", "--reason", "Retired.")[0] == 0
    code, output = _restore(lab)
    assert (code, output.get("error")) == ((0, None) if error is None else (1, error))


def test_an_admitted_base_whose_file_was_deleted_must_be_restored_first(environment):
    import asyncio

    from labbioagentos.local_config import _image_catalog, load_settings
    from test_c15_promotion import OCI

    lab = environment
    settings = load_settings(lab.path)
    catalog = _image_catalog(settings)
    registry = ApprovedImageRegistry((*settings.execution.approved_images(), *catalog.admitted_images()))
    service = EnvironmentService(settings.environment.root, registry, LockingBuilder("sha256:" + "8" * 64),
                                 "admin", catalog=catalog)
    built = asyncio.run(service.build_environment("mine", ("examplepkg==2.0",), ()))
    lab.key = built["image_key"]  # Built on the admitted "mine", found by its OCI source.
    catalog.set_state(lab.key, catalog.state(lab.key).DEPRECATED, actor="admin", reason="Retired.")
    catalog.record_realization(lab.key, resolved_image="sha256:" + "8" * 64, action=RealizationAction.DELETED,
                               actor="admin", reason="Freeing space.")
    catalog.set_state("mine", catalog.state("mine").DEPRECATED, actor="admin", reason="Retired.")
    catalog.record_realization("mine", resolved_image=OCI.digest, action=RealizationAction.DELETED,
                               actor="admin", reason="Freeing space.")
    assert _restore(lab)[1]["error"] == "BASE_DELETED" and lab.builder.calls == []
    catalog.record_realization("mine", resolved_image=OCI.digest, action=RealizationAction.RESTORED,
                               actor="admin", reason="Back.", produced_by="test")
    code, output = _restore(lab)
    assert code == 0, output
    assert lab.builder.calls[0]["base_image"].key == "mine"  # Deprecated, but its file is back.


def test_an_apptainer_rebuild_registers_in_the_configured_store(environment, monkeypatch, tmp_path):
    from labbioagentos import local_images
    from labbioagentos.local_config import load_settings

    lab = environment
    real = load_settings(lab.path)
    settings = SimpleNamespace(
        environment=real.environment, principal=real.principal, lab=real.lab,
        execution=SimpleNamespace(backend="apptainer", apptainer=SimpleNamespace(sif_store=tmp_path / "sif"),
                                  approved_images=real.execution.approved_images))
    stores = []

    @contextmanager
    def jobs(settings, member_root, *, sif_store=None, lock_builds=False):
        stores.append(sif_store)
        raise RuntimeError("stop")
        yield

    monkeypatch.setattr(local_images, "_admission_jobs", jobs)
    with pytest.raises(RuntimeError):
        local_images._restore_environment(settings, local_images._image_catalog(real), lab.area, lab.key,
                                          actor="admin", reason="Needed again.")
    (store,) = stores
    assert store.root == tmp_path / "sif"


def test_an_environment_built_on_another_environment_finds_it_as_its_base(environment):
    import asyncio

    from labbioagentos.local_config import _image_catalog, load_settings

    lab = environment
    settings = load_settings(lab.path)
    catalog = _image_catalog(settings)
    registry = ApprovedImageRegistry(settings.execution.approved_images())
    service = EnvironmentService(settings.environment.root, registry, LockingBuilder("sha256:" + "7" * 64), "admin",
                                 catalog=catalog)
    lower = asyncio.run(service.build_environment("python-base", ("examplepkg==2.0",), ("examplepkg",)))
    base_key = lower["image_key"]
    upper = asyncio.run(service.build_environment(base_key, ("examplepkg==2.0",), ()))
    assert upper["status"] == "SUCCEEDED", upper
    lab.key = upper["image_key"]
    for key in (lab.key, base_key):
        catalog.set_state(key, catalog.state(key).DEPRECATED, actor="admin", reason="Retired.")
        catalog.record_realization(key, resolved_image="sha256:" + "7" * 64, action=RealizationAction.DELETED,
                                   actor="admin", reason="Freeing space.")
    assert _restore(lab)[1]["error"] == "BASE_DELETED" and lab.builder.calls == []

    catalog.record_realization(base_key, resolved_image="sha256:" + "6" * 64, action=RealizationAction.RESTORED,
                               actor="admin", reason="Back.", produced_by="test")
    (settings.environment.root / "records" / ("1" * 64 + ".json")).write_text("{}")  # Damaged: never a base.
    code, output = _restore(lab)
    assert code == 0, output
    base = lab.builder.calls[0]["base_image"]
    assert base.key == base_key and base.reference == "sha256:" + "6" * 64  # Its restored file.
