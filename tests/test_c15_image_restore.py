"""C15 step 9c-1: restoring a deleted image's file from its recorded source (DECIDED-C15-H, refined 2026-11-18)."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

from labbioagentos.execution.admission import (
    AdmissionError,
    AdmissionLimits,
    ApptainerRealizer,
    ImageAdmission,
)
from labbioagentos.execution.apptainer import SifStore
from labbioagentos.execution.catalog import (
    CatalogStore,
    ExecutionBackendName,
    GpuSupport,
    ImageScope,
    RealizationAction,
)
from labbioagentos.execution.identity import SourceIdentity, SourceKind
from labbioagentos.execution.oci import AcquiredImage

from test_c15_admission import FakeRealizer, _probe
from test_c15_image_delete import _cli, _delete, _deleted, _retire, lab  # noqa: F401 - the fixture
from test_c15_promotion import OCI, _record

RESTORED = "sha256:" + "e" * 64


class FakeRegistryClient:
    """Pulls any reference as the given source; records what was pulled."""

    def __init__(self, source=OCI, pulled=None):
        self.source, self.pulled = source, pulled if pulled is not None else []

    def pull(self, reference, *, platform, layout, max_bytes):
        self.pulled.append(f"{reference.host}/{reference.name}@{reference.digest}")
        layout.mkdir(parents=True)
        return AcquiredImage(layout=layout, source=self.source, size_bytes=1000, architecture="amd64")

    def close(self):
        pass


class RestoringRealizer(FakeRealizer):
    def realize_layout(self, image, staging):
        realized = super().realize_layout(image, staging)
        self.realized[-1] = realized = type(realized)(RESTORED, realized.produced_by)
        return realized


def _admission(tmp_path, *, source=OCI, probe=None, realizer=None, quota=10**9, used=0):
    store = CatalogStore(tmp_path / "personal").initialize()
    realizer = realizer or RestoringRealizer()
    return ImageAdmission(
        store=store, scope=ImageScope.PERSONAL, owner_user_id="admin", admitted_by="admin", realizer=realizer,
        probe=probe or (lambda image, runtime: _probe(python={"pip": "24.0"})), machine="x86_64",
        limits=AdmissionLimits(max_image_bytes=10**7, quota_bytes=quota), staging_root=tmp_path / "staging",
        registry_client=lambda: FakeRegistryClient(source), existing_keys=set, other_used_bytes=lambda: used,
    ), realizer, store


def _restore(admission, record=None, **form):
    committed = []
    form = form or {"oci": "registry.example/lab/tools@" + OCI.digest}
    realization = admission.restore(record or _record(key="mine"), **form, commit=committed.append)
    return realization, committed


# --- The admission pipeline ---


def test_a_restore_is_the_recorded_source_and_inventory_and_commits_a_new_realization(tmp_path):
    admission, realizer, store = _admission(tmp_path)

    realization, committed = _restore(admission)

    assert committed == [realization] and realization.resolved_image == RESTORED
    assert realization.backend is ExecutionBackendName.DOCKER and realization.produced_by == "restore-oci:load-1"
    assert store.records() == {} and realizer.discarded == []  # The record is never rewritten.
    assert list((tmp_path / "staging").iterdir()) == []


@pytest.mark.parametrize("setup, code, realized", [
    ({"source": SourceIdentity(kind=SourceKind.OCI_MANIFEST, digest="sha256:" + "d" * 64,
                               platform="linux/amd64")}, "SOURCE_MISMATCH", False),
    ({"probe": lambda image, runtime: _probe(python={"pip": "25.0"})}, "INVENTORY_MISMATCH", True),
    ({"probe": lambda image, runtime: _probe(machine="aarch64", python={"pip": "24.0"})},
     "ARCHITECTURE_MISMATCH", True),
    ({"quota": 1500, "used": 1000}, "MEMBER_QUOTA_EXCEEDED", False),
])
def test_a_restore_that_differs_from_the_record_commits_nothing(tmp_path, setup, code, realized):
    admission, realizer, _store = _admission(tmp_path, **setup)
    with pytest.raises(AdmissionError) as raised:
        _restore(admission)
    assert raised.value.code == code
    assert realizer.discarded == realizer.realized and bool(realizer.realized) is realized
    assert list((tmp_path / "staging").iterdir()) == []


def test_the_probe_runs_the_records_runtimes_and_gpu_check(tmp_path):
    from labbioagentos.execution.models import ExecutionRuntime

    probed = []

    def probe(image, runtime):
        probed.append((image.key, runtime))
        return _probe(python={"pip": "24.0"})

    admission, realizer, _store = _admission(tmp_path, probe=probe)
    record = _record(key="mine").model_copy(update={"gpu": GpuSupport.SUPPORTED})
    with pytest.raises(AdmissionError) as raised:
        _restore(admission, record)
    assert raised.value.code == "CUDA_RUNTIME_MISSING" and realizer.discarded == realizer.realized
    assert probed == [("mine", ExecutionRuntime.PYTHON)]


def test_a_commit_that_fails_discards_the_new_file(tmp_path):
    admission, realizer, _store = _admission(tmp_path)

    def fail(_realization):
        raise OSError("disk full")

    with pytest.raises(OSError):
        admission.restore(_record(key="mine"), oci="registry.example/lab/tools@" + OCI.digest, commit=fail)
    assert realizer.discarded == realizer.realized and len(realizer.realized) == 1


@pytest.mark.parametrize("form", [{}, {"oci": "registry.example/lab/tools@" + OCI.digest, "sif": Path("x")}])
def test_a_restore_takes_exactly_one_delivery(tmp_path, form):
    admission, realizer, _store = _admission(tmp_path)
    with pytest.raises(AdmissionError) as raised:
        admission.restore(_record(key="mine"), **form, commit=lambda _r: None)
    assert raised.value.code == "FORM_REQUIRED" and realizer.realized == []


def test_a_sif_is_delivered_again_into_the_store_on_apptainer(tmp_path):
    sif = tmp_path / "tools.sif"
    sif.write_bytes(b"SIF image bytes")
    store = SifStore(tmp_path / "sif")
    realizer = ApptainerRealizer(sif_store=store, build=lambda *_: None)
    admission, _realizer, _store = _admission(tmp_path, realizer=realizer)
    import hashlib

    source = SourceIdentity(kind=SourceKind.SIF, digest="sha256:" + hashlib.sha256(b"SIF image bytes").hexdigest())
    record = _record(key="from-sif", source=source, repository=None)

    realization, committed = _restore(admission, record, sif=sif)

    assert committed == [realization] and realization.backend is ExecutionBackendName.APPTAINER
    assert realization.produced_by.startswith("restore-sif:") and store.path_for(realization.resolved_image)

    other = tmp_path / "other.sif"
    other.write_bytes(b"another SIF")
    with pytest.raises(AdmissionError) as raised:
        _restore(admission, record, sif=other)
    assert raised.value.code == "SOURCE_MISMATCH"


# --- The command ---


@pytest.fixture
def restoring(lab, monkeypatch):
    """The lab fixture with admissions faked: a registry that serves the recorded source, a fake realizer."""
    from labbioagentos import local_images
    from labbioagentos.execution import admission as admission_module
    from labbioagentos.execution.admission_attempts import AdmissionAttempts

    lab.pulled, lab.jobs, lab.realizer, lab.archives = [], [], RestoringRealizer(), []
    lab.source = OCI

    @contextmanager
    def jobs(settings, member_root, *, sif_store=None, lock_builds=False):
        lab.jobs.append(sif_store)
        yield local_images._AdmissionJobs(
            attempts=AdmissionAttempts(member_root / "admissions"), realizer=lambda _attempt: lab.realizer,
            probe=lambda image, runtime: _probe(python={"pip": "24.0"}), builder=None)

    def read_archive(archive, *, platform, layout, max_bytes):
        lab.archives.append(archive)
        layout.mkdir(parents=True)
        return AcquiredImage(layout=layout, source=lab.source, size_bytes=1000, architecture="amd64")

    monkeypatch.setattr(local_images, "_admission_jobs", jobs)
    monkeypatch.setattr(local_images, "OciRegistryClient",
                        lambda credentials: FakeRegistryClient(lab.source, lab.pulled))
    monkeypatch.setattr(admission_module, "read_archive", read_archive)
    return lab


def _restore_cli(capsys, lab, key, *extra, reason="Needed again."):
    return _cli(capsys, lab, "image-restore", "--key", key, "--reason", reason, *extra)


def _admitted(lab):
    from labbioagentos.local_config import _image_catalog, load_settings

    return {image.key: image.reference for image in _image_catalog(load_settings(lab.path)).admitted_images()}


def test_a_deleted_oci_image_is_pulled_again_and_runs_from_the_new_file(restoring, capsys):
    lab = restoring
    _retire(capsys, lab, "mine")
    assert _delete(capsys, lab, "mine")[0] == 0 and "mine" not in _admitted(lab)

    code, output = _restore_cli(capsys, lab, "mine")

    assert code == 0 and output == {
        "event": "image_restored", "key": "mine", "scope": "PERSONAL", "state": "DEPRECATED",
        "backend": "DOCKER", "source": OCI.canonical, "delivery": "oci", "produced_by": "restore-oci:load-1",
        "reconciled": []}
    assert lab.pulled == ["registry.example/lab/tools@" + OCI.digest] and lab.jobs == [None]
    assert not _deleted(lab, "mine") and _admitted(lab)["mine"] == RESTORED
    events = CatalogStore(lab.tmp / "member" / "catalog").realization_events()
    assert [(event.action, event.resolved_image, event.produced_by, event.reason) for event in events] == [
        (RealizationAction.DELETED, OCI.digest, None, "Freeing space."),
        (RealizationAction.RESTORED, RESTORED, "restore-oci:load-1", "Needed again.")]
    assert _restore_cli(capsys, lab, "mine")[1]["error"] == "IMAGE_NOT_DELETED"

    # Deleting again removes the restored file, not the recorded one.
    lab.docker.clear()
    assert _delete(capsys, lab, "mine")[0] == 0 and lab.docker == [["docker", "image", "rm", RESTORED]]


def test_a_deleted_image_is_activated_only_after_its_restore(restoring, capsys):
    lab = restoring
    _retire(capsys, lab, "mine")
    _delete(capsys, lab, "mine")
    code, output = _cli(capsys, lab, "image-activate", "--key", "mine", "--reason", "Wanted.")
    assert code == 1 and output["error"] == "IMAGE_DELETED"
    assert _restore_cli(capsys, lab, "mine")[0] == 0
    assert _cli(capsys, lab, "image-activate", "--key", "mine", "--reason", "Wanted.")[0] == 0


def test_an_archive_delivers_an_oci_source_again(restoring, capsys, tmp_path):
    lab = restoring
    _retire(capsys, lab, "mine")
    _delete(capsys, lab, "mine")
    archive = tmp_path / "tools.tar"

    code, output = _restore_cli(capsys, lab, "mine", "--archive", str(archive))

    assert code == 0 and output["delivery"] == "archive" and output["produced_by"] == "restore-archive:load-1"
    assert lab.archives == [archive] and lab.pulled == []


def test_a_wrong_delivery_is_refused_before_anything_is_fetched(restoring, capsys, tmp_path):
    lab = restoring
    _retire(capsys, lab, "mine")
    _delete(capsys, lab, "mine")
    lab.source = SourceIdentity(kind=SourceKind.OCI_MANIFEST, digest="sha256:" + "d" * 64, platform="linux/amd64")
    assert _restore_cli(capsys, lab, "mine")[1]["error"] == "SOURCE_MISMATCH"
    assert lab.realizer.realized == [] and _deleted(lab, "mine")
    lab.pulled.clear()
    assert _restore_cli(capsys, lab, "mine", "--sif", str(tmp_path / "x.sif"))[1]["error"] == "RESTORE_FORM_INVALID"

    _retire(capsys, lab, "from-sif")
    assert _delete(capsys, lab, "from-sif", "--confirm-no-upstream")[0] == 0
    assert _restore_cli(capsys, lab, "from-sif")[1]["error"] == "RESTORE_FILE_REQUIRED"
    assert _restore_cli(capsys, lab, "from-sif", "--archive", str(tmp_path / "x.tar"))[1]["error"] == (
        "RESTORE_FORM_INVALID")
    # On Docker a SIF cannot be realized at all.
    assert _restore_cli(capsys, lab, "from-sif", "--sif", str(tmp_path / "x.sif"))[1]["error"] in {
        "SIF_INVALID", "FORM_UNSUPPORTED_ON_BACKEND"}
    assert lab.pulled == [] and _deleted(lab, "from-sif")


def test_an_archive_admission_without_a_registry_needs_its_archive(restoring, capsys):
    from labbioagentos.local_config import _image_catalog, load_settings

    lab = restoring
    catalog = _image_catalog(load_settings(lab.path))
    catalog.personal.add_record(_record(key="from-archive", repository=None))
    _retire(capsys, lab, "from-archive")
    assert _delete(capsys, lab, "from-archive")[0] == 0
    assert _restore_cli(capsys, lab, "from-archive")[1]["error"] == "RESTORE_FILE_REQUIRED"


@pytest.mark.parametrize("key, setup, error", [
    ("python-base", None, "IMAGE_CONFIGURED"),
    ("nobody", None, "IMAGE_KEY_UNKNOWN"),
    ("mine", None, "IMAGE_NOT_DELETED"),
    ("mine", "image-deprecate", "IMAGE_NOT_DELETED"),
    ("mine", "revoked", "IMAGE_REVOKED"),
])
def test_what_cannot_be_restored_is_refused(restoring, capsys, key, setup, error):
    lab = restoring
    if setup == "revoked":
        _retire(capsys, lab, key, "image-revoke")
        assert _delete(capsys, lab, key)[0] == 0
    elif setup:
        _retire(capsys, lab, key, setup)
    code, output = _restore_cli(capsys, lab, key)
    assert code == 1 and output["error"] == error and lab.pulled == []


@pytest.mark.parametrize("reason", ["", "two\nlines", "x" * 501])
def test_a_restore_needs_a_reason(restoring, capsys, reason):
    lab = restoring
    _retire(capsys, lab, "mine")
    _delete(capsys, lab, "mine")
    assert _restore_cli(capsys, lab, "mine", reason=reason)[1]["error"] == "REASON_INVALID"
    assert lab.pulled == [] and _deleted(lab, "mine")


def test_rebuilt_images_are_not_delivered_again(restoring, capsys):
    from labbioagentos.local_config import _image_catalog, load_settings

    lab = restoring
    settings = load_settings(lab.path)
    catalog = _image_catalog(settings)
    built = _record(key="locked", repository=None, source=SourceIdentity(
        kind=SourceKind.DERIVED, digest="sha256:" + "7" * 64))
    built = built.model_copy(update={"realizations": (built.realizations[0].model_copy(
        update={"produced_by": "lock:attempt"}),)})
    catalog.personal.add_record(built)
    environment = "env-" + "8" * 64
    records = settings.environment.root / "records"
    records.mkdir(parents=True, exist_ok=True)
    (records / ("8" * 64 + ".json")).write_text("{}")
    for key, error in (("locked", "RESTORE_LOCKS_REQUIRED"), (environment, "RESTORE_BY_REBUILD_UNAVAILABLE")):
        catalog.set_state(key, catalog.state(key).DEPRECATED, actor="admin", reason="Retired.")
        catalog.record_realization(key, resolved_image=RESTORED, action=RealizationAction.DELETED,
                                   actor="admin", reason="Freeing space.")
        assert _restore_cli(capsys, lab, key)[1]["error"] == error
    assert lab.pulled == []


def test_a_lab_image_is_restored_by_an_administrator_into_the_lab_stores(restoring, capsys, monkeypatch):
    from labbioagentos import local_lab

    lab = restoring
    _retire(capsys, lab, "lab-tools")
    assert _delete(capsys, lab, "lab-tools")[0] == 0
    lab.source = SourceIdentity(kind=SourceKind.OCI_MANIFEST, digest="sha256:" + "6" * 64, platform="linux/amd64")

    def refuse(self):
        raise local_lab.LabError("LAB_ADMIN_REQUIRED")

    with monkeypatch.context() as patched:
        patched.setattr(local_lab.LabArea, "require_admin", refuse)
        code, output = _restore_cli(capsys, lab, "lab-tools")
        assert code == 1 and output["error"] == "LAB_ADMIN_REQUIRED" and lab.pulled == []

    code, output = _restore_cli(capsys, lab, "lab-tools")
    assert code == 0 and output["scope"] == "LAB" and len(lab.jobs) == 1 and lab.jobs[0] is not None
    assert lab.jobs[0].root == lab.area.sif_store().root  # A lab SIF goes to the lab store.
    (_deleted_event, restored) = lab.area.catalog.realization_events()
    assert restored.action is RealizationAction.RESTORED and restored.actor == "admin"
    assert _admitted(lab)["lab-tools"] == RESTORED


def test_the_quota_counts_the_restored_image(restoring, capsys, monkeypatch):
    from labbioagentos import local_lab

    lab = restoring
    _retire(capsys, lab, "mine")
    _delete(capsys, lab, "mine")
    policy = lab.area.policy()
    monkeypatch.setattr(local_lab.LabArea, "policy", lambda self: policy.model_copy(update={
        "member_quota_bytes": 1500}))  # "from-sif" still holds 1000 bytes.
    assert _restore_cli(capsys, lab, "mine")[1]["error"] == "MEMBER_QUOTA_EXCEEDED"
    assert lab.realizer.realized == [] and _deleted(lab, "mine")


def test_crash_cleanup_keeps_restored_files(restoring, capsys):
    from labbioagentos import local_images
    from labbioagentos.local_config import load_settings

    lab = restoring
    _retire(capsys, lab, "mine")
    _delete(capsys, lab, "mine")
    _restore_cli(capsys, lab, "mine")
    assert RESTORED in local_images._admitted_references(load_settings(lab.path))


@pytest.mark.asyncio
async def test_an_agent_environment_runs_from_its_restored_file(tmp_path):
    from labbioagentos import ApprovedImageRegistry
    from labbioagentos.execution.catalog import ImageCatalog
    from labbioagentos.execution.environments import EnvironmentService
    from test_c15_environment_quota import SizedBuilder
    from test_execution_environments import _image

    catalog = ImageCatalog(owner_user_id="admin", backend=ExecutionBackendName.DOCKER,
                           personal=CatalogStore(tmp_path / "personal").initialize())
    builder = SizedBuilder(size=1000)
    service = EnvironmentService(tmp_path / "environments", ApprovedImageRegistry((_image(),)), builder,
                                 "admin", catalog=catalog)
    key = (await service.build_environment("base", ("examplepkg==2.0",), ("examplepkg",)))["image_key"]
    original = service.image_registry.resolve(key)
    catalog.record_realization(key, resolved_image=original.reference, action=RealizationAction.DELETED,
                               actor="admin", reason="Freeing space.")
    catalog.record_realization(key, resolved_image=RESTORED, action=RealizationAction.RESTORED,
                               actor="admin", reason="Needed again.", produced_by="rebuild")

    again = EnvironmentService(tmp_path / "environments", ApprovedImageRegistry((_image(),)), builder,
                               "admin", catalog=catalog)
    restored = again.image_registry.resolve(key)
    assert restored.reference == RESTORED and restored.source == original.source
    assert restored.installed_packages == original.installed_packages

    fresh = EnvironmentService(tmp_path / "environments", ApprovedImageRegistry((_image(),)), builder,
                               "admin", catalog=catalog)
    fresh.image_registry = ApprovedImageRegistry((_image(),))
    hit = await fresh.build_environment("base", ("examplepkg==2.0",), ("examplepkg",))
    assert hit["status"] == "SUCCEEDED" and hit["cache_hit"]
    assert fresh.image_registry.resolve(key).reference == RESTORED


@pytest.mark.asyncio
async def test_deletion_sees_an_environments_restored_file(restoring, capsys):
    from labbioagentos import ApprovedImageRegistry
    from labbioagentos.execution.environments import EnvironmentService
    from labbioagentos.local_config import _image_catalog, load_settings
    from test_c15_environment_quota import SizedBuilder
    from test_execution_environments import _image

    lab = restoring
    settings = load_settings(lab.path)
    catalog = _image_catalog(settings)
    service = EnvironmentService(settings.environment.root, ApprovedImageRegistry((_image(),)),
                                 SizedBuilder(size=1000), "admin", catalog=catalog)
    key = (await service.build_environment("base", ("examplepkg==2.0",), ("examplepkg",)))["image_key"]
    original = service.image_registry.resolve(key).reference
    catalog.record_realization(key, resolved_image=original, action=RealizationAction.DELETED,
                               actor="admin", reason="Freeing space.")
    catalog.record_realization(key, resolved_image=OCI.digest, action=RealizationAction.RESTORED,
                               actor="admin", reason="Needed again.", produced_by="rebuild")

    # "mine" runs from the file the environment was restored to, so that file stays.
    _retire(capsys, lab, "mine")
    assert _cli(capsys, lab, "image-delete", "--key", "mine")[1]["file"] == "KEEP_SHARED"
    lab.docker.clear()
    assert _delete(capsys, lab, "mine")[0] == 0 and lab.docker == []
    _retire(capsys, lab, key)
    assert _delete(capsys, lab, key)[0] == 0 and lab.docker == [["docker", "image", "rm", OCI.digest]]


def test_the_restored_image_is_counted_once_and_a_lab_image_against_the_lab_quota(restoring, capsys, monkeypatch):
    from labbioagentos import local_lab

    lab = restoring
    for key in ("mine", "lab-tools"):
        _retire(capsys, lab, key)
        assert _delete(capsys, lab, key)[0] == 0
    policy = lab.area.policy()
    # Personal: "from-sif" 1000 + the restored 1000 fit 2500; the deleted record itself is not counted again.
    # Lab: nothing live + the restored 1000 fit 1500, although the member's images hold 2000.
    monkeypatch.setattr(local_lab.LabArea, "policy", lambda self: policy.model_copy(update={
        "member_quota_bytes": 2500, "lab_quota_bytes": 1500}))
    assert _restore_cli(capsys, lab, "mine")[0] == 0
    lab.source = SourceIdentity(kind=SourceKind.OCI_MANIFEST, digest="sha256:" + "6" * 64, platform="linux/amd64")
    assert _restore_cli(capsys, lab, "lab-tools")[0] == 0


def test_another_file_of_the_same_image_drops_a_pinned_digest():
    from labbioagentos.execution.images import ApprovedImage

    pinned = ApprovedImage(key="k", reference="registry.example/a/b", digest="sha256:" + "a" * 64,
                           entry_points={"PYTHON": ("python",)})
    moved = pinned.with_reference(RESTORED)
    assert moved.reference == moved.resolved_reference == RESTORED and moved.digest is None
    assert moved.entry_points == pinned.entry_points
