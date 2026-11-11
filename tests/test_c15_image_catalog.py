"""C15 step 3: the image catalog store, its merged view and what models may see."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from labbioagentos import ExecutionPlanDraft, ExecutionSubmissionService, WorkflowStage
from labbioagentos.execution import ImageNotApprovedError, JobLedger
from labbioagentos.execution.catalog import (
    CatalogError,
    CatalogStore,
    ExecutionBackendName,
    GpuSupport,
    ImageCatalog,
    ImageEvent,
    ImageRecord,
    ImageScope,
    ImageState,
    InventorySnapshot,
    Realization,
)
from labbioagentos.execution.identity import SourceIdentity, SourceKind
from labbioagentos.execution.inventory import InventoryOrigin

from test_execution_inspection import inspection, _submit  # noqa: F401
from test_runtime_milestone_b import boundary  # noqa: F401

OCI = SourceIdentity(kind=SourceKind.OCI_MANIFEST, digest="sha256:" + "c" * 64, platform="linux/amd64")
SIF = "sha256:" + "5" * 64
DOCKER = "sha256:" + "6" * 64
NOW = datetime(2026, 11, 11, tzinfo=timezone.utc)


def _record(key="tools", scope=ImageScope.LAB, owner=None, **updates) -> ImageRecord:
    values = dict(
        key=key, scope=scope, owner_user_id=owner, source=OCI,
        realizations=(Realization(backend=ExecutionBackendName.APPTAINER, resolved_image=SIF,
                                  produced_by="conversion-1"),),
        inventory=InventorySnapshot(origin=InventoryOrigin.CONDA_META, packages={"samtools": "1.21"}),
        purpose="Alignment and BAM utilities.", gpu=GpuSupport.NONE, size_bytes=300 * 1_048_576,
        admitted_by="admin-one", admitted_at=NOW,
    )
    values.update(updates)
    return ImageRecord(**values)


def _store(tmp_path, name="lab") -> CatalogStore:
    return CatalogStore(tmp_path / name).initialize()


def _event(key, state):
    return ImageEvent(key=key, state=state, actor="admin-one", reason="Superseded by a newer build.",
                      at=datetime.now(timezone.utc))


# --- Store: files are only ever added, and every file is checked ---


def test_a_record_round_trips_and_is_never_replaced(tmp_path):
    store = _store(tmp_path)
    store.add_record(_record())
    path = store.records_root / "tools.json"
    before = path.read_bytes()

    with pytest.raises(CatalogError):
        store.add_record(_record(purpose="A different purpose."))

    assert path.read_bytes() == before
    assert store.records() == {"tools": _record()}
    assert path.stat().st_mode & 0o777 == 0o640  # Readable by the lab group, never written.


def test_a_concurrent_writer_cannot_replace_a_record(tmp_path, monkeypatch):
    # Two writers can both pass the existence check; the final link must still refuse.
    store = _store(tmp_path)
    store.add_record(_record())
    before = (store.records_root / "tools.json").read_bytes()
    monkeypatch.setattr(store, "records", lambda: {})

    with pytest.raises(CatalogError):
        store.add_record(_record(purpose="Written by the second writer."))

    assert (store.records_root / "tools.json").read_bytes() == before
    assert [path.name for path in store.records_root.iterdir()] == ["tools.json"]


def test_a_tampered_record_fails_its_integrity_check(tmp_path):
    store = _store(tmp_path)
    store.add_record(_record())
    path = store.records_root / "tools.json"
    content = json.loads(path.read_text())
    content["record"]["purpose"] = "Rewritten after admission."
    path.write_text(json.dumps(content))

    with pytest.raises(CatalogError):
        store.records()


def test_a_record_stored_under_another_key_is_refused(tmp_path):
    store = _store(tmp_path)
    store.add_record(_record())
    (store.records_root / "tools.json").rename(store.records_root / "other.json")
    with pytest.raises(CatalogError):
        store.records()


@pytest.mark.parametrize("entry", ["notes.txt", "link.json"])
def test_unexpected_catalog_entries_fail_closed(tmp_path, entry):
    store = _store(tmp_path)
    store.add_record(_record())
    if entry == "link.json":
        (store.records_root / entry).symlink_to(store.records_root / "tools.json")
    else:
        (store.records_root / entry).write_text("x")
    with pytest.raises(CatalogError):
        store.records()


def test_an_interrupted_write_is_ignored(tmp_path):
    store = _store(tmp_path)
    (store.records_root / ".0123abcd.tmp").write_text("partial")
    assert store.records() == {}


@pytest.mark.parametrize("current, target, allowed", [
    (ImageState.ACTIVE, ImageState.DEPRECATED, True),
    (ImageState.DEPRECATED, ImageState.ACTIVE, True),
    (ImageState.ACTIVE, ImageState.REVOKED, True),
    (ImageState.DEPRECATED, ImageState.REVOKED, True),
    (ImageState.REVOKED, ImageState.ACTIVE, False),
    (ImageState.REVOKED, ImageState.DEPRECATED, False),
    (ImageState.ACTIVE, ImageState.ACTIVE, False),
])
def test_state_changes_follow_the_lifecycle(tmp_path, current, target, allowed):
    store = _store(tmp_path)
    if allowed:
        store.add_event(_event("tools", target), current=current)
        assert store.events()[-1].state is target
    else:
        with pytest.raises(CatalogError):
            store.add_event(_event("tools", target), current=current)


def test_an_illegal_history_written_around_the_store_fails_closed(tmp_path):
    store = _store(tmp_path)
    store.add_event(_event("tools", ImageState.REVOKED), current=ImageState.ACTIVE)
    store.add_event(_event("tools", ImageState.ACTIVE), current=ImageState.DEPRECATED)  # Lies about current.

    with pytest.raises(CatalogError):
        ImageCatalog(owner_user_id="u", backend=ExecutionBackendName.APPTAINER, lab=store)


@pytest.mark.parametrize("updates", [
    {"scope": ImageScope.PERSONAL, "owner_user_id": None},
    {"scope": ImageScope.LAB, "owner_user_id": "member"},
    {"realizations": (Realization(backend=ExecutionBackendName.APPTAINER, resolved_image=SIF, produced_by="a"),
                      Realization(backend=ExecutionBackendName.APPTAINER, resolved_image=SIF, produced_by="b"))},
    {"purpose": "Data lives in /home/member/project"},
    {"realizations": ()},
])
def test_inconsistent_records_are_refused(updates):
    with pytest.raises(ValidationError):
        _record(**updates)


# --- Merged view ---


def _catalog(tmp_path, *, lab_records=(), personal_records=(), configured=("python-base",),
             backend=ExecutionBackendName.APPTAINER, owner="member-one"):
    lab, personal = _store(tmp_path, "lab"), _store(tmp_path, "personal")
    for record in lab_records:
        lab.add_record(record)
    for record in personal_records:
        personal.add_record(record)
    return ImageCatalog(owner_user_id=owner, backend=backend, configured_keys=configured,
                        lab=lab, personal=personal)


@pytest.mark.parametrize("lab_records, personal_records", [
    ((_record(key="mine", scope=ImageScope.PERSONAL, owner="member-one"),), ()),
    ((), (_record(key="theirs", scope=ImageScope.PERSONAL, owner="member-two"),)),
    ((), (_record(key="python-base", scope=ImageScope.PERSONAL, owner="member-one"),)),
    ((_record(key="python-base"),), ()),
    ((_record(key="tools"),), (_record(key="tools", scope=ImageScope.PERSONAL, owner="member-one"),)),
])
def test_misplaced_or_doubly_held_images_fail_closed(tmp_path, lab_records, personal_records):
    with pytest.raises(CatalogError):
        _catalog(tmp_path, lab_records=lab_records, personal_records=personal_records)


def test_only_realizations_for_this_backend_execute(tmp_path):
    docker_only = _record(key="docker-only", realizations=(
        Realization(backend=ExecutionBackendName.DOCKER, resolved_image=DOCKER, produced_by="pull-1"),))
    catalog = _catalog(tmp_path, lab_records=(_record(), docker_only))

    images = {image.key: image for image in catalog.admitted_images()}

    assert set(images) == {"tools"}
    assert images["tools"].reference == SIF and images["tools"].source_identity == OCI
    assert images["tools"].installed_packages == {"samtools": "1.21"}


def test_a_revoked_admitted_image_never_executes(tmp_path):
    catalog = _catalog(tmp_path, lab_records=(_record(),))
    catalog.set_state("tools", ImageState.REVOKED, actor="admin-one", reason="Security finding.")

    assert catalog.admitted_images() == ()
    reopened = ImageCatalog(owner_user_id="member-one", backend=ExecutionBackendName.APPTAINER,
                            lab=CatalogStore(tmp_path / "lab"))
    assert reopened.state("tools") is ImageState.REVOKED and reopened.admitted_images() == ()


def test_states_persist_through_the_owning_store(tmp_path):
    catalog = _catalog(tmp_path, personal_records=(
        _record(key="mine", scope=ImageScope.PERSONAL, owner="member-one"),))
    catalog.set_state("mine", ImageState.DEPRECATED, actor="member-one", reason="Replaced.")
    catalog.set_state("python-base", ImageState.DEPRECATED, actor="admin-one", reason="Replaced.")

    reopened = ImageCatalog(owner_user_id="member-one", backend=ExecutionBackendName.APPTAINER,
                            configured_keys=("python-base",), lab=CatalogStore(tmp_path / "lab"),
                            personal=CatalogStore(tmp_path / "personal"))
    assert reopened.state("mine") is ImageState.DEPRECATED
    assert reopened.state("python-base") is ImageState.DEPRECATED
    assert [event.key for event in CatalogStore(tmp_path / "lab").events()] == ["python-base"]


def test_deprecated_images_serve_only_runs_that_already_used_them(tmp_path):
    catalog = _catalog(tmp_path)
    catalog.set_state("python-base", ImageState.DEPRECATED, actor="admin-one", reason="Replaced.")

    catalog.require_selectable("python-base", used_by_run=True)
    with pytest.raises(ImageNotApprovedError):
        catalog.require_selectable("python-base", used_by_run=False)
    assert not catalog.visible("python-base")

    catalog.set_state("python-base", ImageState.REVOKED, actor="admin-one", reason="Security finding.")
    with pytest.raises(ImageNotApprovedError):
        catalog.require_selectable("python-base", used_by_run=True)


def test_the_model_view_has_no_reference_registry_path_or_owner(tmp_path):
    personal = _record(key="mine", scope=ImageScope.PERSONAL, owner="member-one", gpu=GpuSupport.REQUIRED)
    catalog = _catalog(tmp_path, lab_records=(_record(),), personal_records=(personal,))

    lab, mine, configured, built = (catalog.describe(key) for key in ("tools", "mine", "python-base", "env-x"))

    assert lab == {"scope": "LAB", "purpose": "Alignment and BAM utilities.", "gpu": "NONE",
                   "inventory_origin": "CONDA_META", "size_mb": 300}
    assert mine["scope"] == "YOURS" and mine["gpu"] == "REQUIRED"
    assert configured["scope"] == "LAB" and configured["purpose"] is None
    assert built["scope"] == "YOURS"
    text = json.dumps([lab, mine, configured, built])
    for hidden in (SIF, "c" * 64, "member-one", "admin-one", str(tmp_path), "conversion-1"):
        assert hidden not in text


# --- Application and environment listing ---


def test_a_revoked_configured_image_never_reaches_the_registry(tmp_path, monkeypatch):
    from labbioagentos import cli
    from labbioagentos import local_config
    from test_c15_image_identity import _write

    settings = local_config.load_settings(_write(tmp_path))
    lab = _store(tmp_path, "lab-area")
    lab.add_event(_event("tools", ImageState.REVOKED), current=ImageState.ACTIVE)
    monkeypatch.setattr(local_config, "_image_catalog", lambda value: ImageCatalog(
        owner_user_id=value.principal.user_id, backend=ExecutionBackendName.DOCKER,
        configured_keys=("python-base", "tools"), lab=lab))

    application = local_config.build_application(settings, tmp_path / "run", load_provider=False)
    try:
        assert application.image_registry.resolve("python-base").key == "python-base"
        with pytest.raises(ImageNotApprovedError):
            application.image_registry.resolve("tools")
    finally:
        cli._close(application)


@pytest.mark.asyncio
async def test_the_listing_hides_deprecated_images_and_names_bases_by_key(tmp_path):
    from labbioagentos import ApprovedImageRegistry
    from labbioagentos.execution.environments import EnvironmentService
    from test_execution_environments import RecordingBuilder, _image

    catalog = ImageCatalog(owner_user_id="user-one", backend=ExecutionBackendName.DOCKER,
                           configured_keys=("base", "old"), lab=_store(tmp_path))
    registry = ApprovedImageRegistry((_image(), _image(key="old", digest="sha256:" + "7" * 64)))
    service = EnvironmentService(tmp_path / "environments", registry, RecordingBuilder(), "user-one",
                                 catalog=catalog)
    built = await service.build_environment("base", ("examplepkg==2.0",))
    catalog.set_state("old", ImageState.DEPRECATED, actor="admin-one", reason="Replaced.")

    page = service.list_environments()
    items = {item["image_key"]: item for item in page["items"]}

    assert set(items) == {"base", built["image_key"]}
    assert items["base"]["scope"] == "LAB" and items[built["image_key"]]["scope"] == "YOURS"
    assert items[built["image_key"]]["build_provenance"]["base_image_key"] == "base"
    text = json.dumps(page)
    assert "sha256:" not in text and "image_reference" not in text
    with pytest.raises(Exception) as raised:
        await service.build_environment("old", ("examplepkg==2.0",))
    assert getattr(raised.value, "code", None) == "UNKNOWN_IMAGE"


# --- Submission enforces the deprecated rule with the run's own history ---


def _deprecate(service, key="approved"):
    lab = CatalogStore(service.artifact_store.root.parent / "catalog-lab").initialize()
    catalog = ImageCatalog(owner_user_id="u", backend=ExecutionBackendName.DOCKER,
                           configured_keys=(key,), lab=lab)
    service.image_catalog = catalog
    return catalog


@pytest.mark.asyncio
async def test_a_run_that_used_a_deprecated_image_may_continue_and_a_new_run_may_not(
    inspection, boundary,
):
    service, executor, run_id = inspection
    await _submit(inspection, boundary)
    catalog = _deprecate(service)
    catalog.set_state("approved", ImageState.DEPRECATED, actor="admin-one", reason="Replaced.")

    await _submit(inspection, boundary)  # Same run: it already executed with this image.
    with pytest.raises(ImageNotApprovedError):
        await _submit((service, executor, uuid4()), boundary)  # A new run.
    assert len(executor.plans) == 2

    catalog.set_state("approved", ImageState.REVOKED, actor="admin-one", reason="Security finding.")
    with pytest.raises(ImageNotApprovedError):
        await _submit(inspection, boundary)
    assert len(executor.plans) == 2


@pytest.mark.asyncio
async def test_a_restarted_service_reads_the_run_history_from_the_ledger(inspection, boundary, tmp_path):
    _, executor, run_id = inspection
    _, recorder, access, principal, workspace, store, _ = boundary
    ledger = JobLedger(tmp_path / "submissions.sqlite")
    first = ExecutionSubmissionService(artifact_store=store, access_service=access, executor=executor,
                                       trace_recorder=recorder, job_ledger=ledger)
    await _submit((first, executor, run_id), boundary)

    restarted = ExecutionSubmissionService(artifact_store=store, access_service=access, executor=executor,
                                           trace_recorder=recorder, job_ledger=ledger)
    catalog = _deprecate(restarted)
    catalog.set_state("approved", ImageState.DEPRECATED, actor="admin-one", reason="Replaced.")

    await restarted.submit(
        ExecutionPlanDraft(image_key="approved", script_content="pass\n"), principal=principal,
        workspace=workspace, run_id=run_id, stage_id=WorkflowStage.EXECUTE, invocation_id=uuid4())
    assert len(executor.plans) == 2
    ledger.close()
