"""C15 step 9c-2a: keeping lock files and rebuilding a deleted lock-built image (DECIDED-C15-H, 2026-11-18)."""

from __future__ import annotations

import hashlib
import stat
from types import SimpleNamespace

import pytest

from labbioagentos.execution.catalog import CatalogError, CatalogStore, RealizationAction
from labbioagentos.execution.identity import SourceIdentity, SourceKind

from test_c15_lock_promotion import CONDA, WHEEL, _cli, lab  # noqa: F401 - the fixture
from test_c15_promotion import _record

DERIVED = SourceIdentity(kind=SourceKind.DERIVED, digest="sha256:" + "d" * 64)
BASE = SourceIdentity(kind=SourceKind.OCI_MANIFEST, digest="sha256:" + "b" * 64, platform="linux/amd64")


# --- Kept locks in a catalog store ---


def _keep(store, texts=None, private=True, source=DERIVED, base_key="python-base"):
    store.keep_locks(source=source, base_key=base_key, base_source=BASE,
                     texts=texts or {"wheel": WHEEL, "conda": CONDA}, private=private)


@pytest.mark.parametrize("private, mode", [(True, 0o600), (False, 0o640)])
def test_locks_are_kept_under_the_identity_they_rebuild(tmp_path, private, mode):
    store = CatalogStore(tmp_path / "catalog").initialize()
    _keep(store, private=private)

    kept, texts = store.kept_locks(DERIVED)

    assert (kept.base_key, kept.base_source, texts) == ("python-base", BASE, {"wheel": WHEEL, "conda": CONDA})
    name = "d" * 64
    assert sorted(path.name for path in store.locks_root.iterdir()) == [
        f"{name}.conda.lock", f"{name}.json", f"{name}.wheel.lock"]
    assert {stat.S_IMODE(path.stat().st_mode) for path in store.locks_root.iterdir()} == {mode}
    assert store.kept_locks(SourceIdentity(kind=SourceKind.DERIVED, digest="sha256:" + "e" * 64)) is None


def test_keeping_again_changes_nothing_and_the_first_locks_stay(tmp_path):
    store = CatalogStore(tmp_path / "catalog").initialize()
    _keep(store, {"wheel": WHEEL})
    _keep(store, {"wheel": WHEEL})
    _keep(store, {"wheel": WHEEL.replace("1.16.0", "1.17.0")}, base_key="tools")
    kept, texts = store.kept_locks(DERIVED)
    assert kept.base_key == "python-base" and texts == {"wheel": WHEEL}
    assert len(list(store.locks_root.iterdir())) == 2


@pytest.mark.parametrize("texts, source", [
    ({}, DERIVED), ({"other": "x"}, DERIVED), ({"wheel": WHEEL}, BASE),
])
def test_only_derived_identities_keep_known_lock_kinds(tmp_path, texts, source):
    store = CatalogStore(tmp_path / "catalog").initialize()
    with pytest.raises(CatalogError):
        store.keep_locks(source=source, base_key="python-base", base_source=BASE, texts=texts, private=True)
    assert list(store.locks_root.iterdir()) == []


@pytest.mark.parametrize("damage", ["edit", "link", "metadata", "renamed", "empty"])
def test_a_damaged_kept_lock_is_never_used(tmp_path, damage):
    store = CatalogStore(tmp_path / "catalog").initialize()
    _keep(store, {"wheel": WHEEL})
    lock = store.locks_root / ("d" * 64 + ".wheel.lock")
    if damage == "edit":
        lock.chmod(0o600)
        lock.write_text(WHEEL.replace("six", "evil"))
    elif damage == "link":
        lock.unlink()
        (tmp_path / "elsewhere").write_text(WHEEL)
        lock.symlink_to(tmp_path / "elsewhere")
    elif damage == "metadata":
        (store.locks_root / ("d" * 64 + ".json")).chmod(0o600)
        (store.locks_root / ("d" * 64 + ".json")).write_text("{}")
    else:
        # Well-formed metadata under another identity's name, or naming no lock at all.
        from labbioagentos.append_only import add_file

        other = SourceIdentity(kind=SourceKind.DERIVED, digest="sha256:" + "e" * 64)
        metadata = store.locks_root / ("d" * 64 + ".json")
        metadata.unlink()
        payload = {"source": (other if damage == "renamed" else DERIVED).model_dump(mode="json"),
                   "base_key": "python-base", "base_source": BASE.model_dump(mode="json"),
                   "wheel_sha256": None if damage == "empty" else hashlib.sha256(WHEEL.encode()).hexdigest(),
                   "conda_sha256": None}
        add_file(metadata, "locks", payload)
    with pytest.raises(CatalogError):
        store.kept_locks(DERIVED)


def test_a_different_lock_left_under_the_name_is_never_taken_for_the_kept_one(tmp_path):
    store = CatalogStore(tmp_path / "catalog").initialize()
    (store.locks_root / ("d" * 64 + ".wheel.lock")).write_text("left by an interrupted write\n")
    with pytest.raises(CatalogError):
        _keep(store, {"wheel": WHEEL})
    assert store.kept_locks(DERIVED) is None


def test_a_private_store_from_before_kept_locks_gets_the_directory_and_a_lab_one_needs_its_holder(tmp_path):
    store = CatalogStore(tmp_path / "personal").initialize()
    store.locks_root.rmdir()
    _keep(store)
    assert stat.S_IMODE(store.locks_root.stat().st_mode) == 0o700
    lab = CatalogStore(tmp_path / "lab").initialize()
    lab.locks_root.rmdir()
    with pytest.raises(CatalogError):
        _keep(lab, private=False)


def test_an_older_lab_area_gets_its_locks_directory_from_its_holder(tmp_path, monkeypatch):
    import os

    from labbioagentos import local_lab
    from test_c15_promotion import MY_GROUP, _area

    area = _area(tmp_path, group=MY_GROUP)
    assert area.catalog.locks_root.stat().st_mode & stat.S_ISGID
    area.catalog.locks_root.rmdir()
    me = os.getuid()
    monkeypatch.setattr(local_lab.os, "getuid", lambda: me + 7)
    monkeypatch.setattr(local_lab.os, "access", lambda *_: False)
    area.check_permissions()  # A member of an older area just finds no kept locks.
    assert not area.catalog.locks_root.exists()
    monkeypatch.undo()
    area.check_permissions()
    assert stat.S_IMODE(area.catalog.locks_root.stat().st_mode) == stat.S_IMODE(area.root.stat().st_mode)


# --- Admissions keep their locks ---


def _personal(tmp_path):
    return CatalogStore(tmp_path / "member" / "catalog")


def _admit(capsys, path, lock, key="six", base="python-base"):
    code, output = _cli(capsys, "image-admit", "--config", str(path), "--key", key, "--purpose", "six.",
                        "--base", base, "--lock", str(lock))
    assert code == 0, output
    return output


def test_a_lock_admission_keeps_its_locks_and_base(lab, capsys):
    area, path, lock, _builds = lab
    admitted = _admit(capsys, path, lock)
    record = _personal(area.root.parent).records()["six"]
    kept, texts = _personal(area.root.parent).kept_locks(record.source)
    assert record.source.canonical == admitted["source"]
    assert kept.base_key == "python-base" and texts == {"wheel": WHEEL}


def test_locks_that_cannot_be_kept_admit_nothing(lab, capsys, monkeypatch):
    area, path, lock, builds = lab

    def fail(self, **_kwargs):
        raise CatalogError("disk full")

    monkeypatch.setattr(CatalogStore, "keep_locks", fail)
    code, output = _cli(capsys, "image-admit", "--config", str(path), "--key", "six", "--purpose", "six.",
                        "--base", "python-base", "--lock", str(lock))
    assert code == 1 and len(builds) == 1 and "six" not in _personal(area.root.parent).records()


def test_an_approved_promotion_moves_the_requests_locks_to_the_lab_area(lab, capsys):
    area, path, lock, _builds = lab
    _admit(capsys, path, lock)
    config = ["--config", str(path)]
    _, requested = _cli(capsys, "image-promote-request", *config, "--key", "six", "--base", "python-base",
                        "--lock", str(lock))
    assert _cli(capsys, "image-promote-approve", *config, "--request", requested["request_id"],
                "--key", "lab-six", "--reason", "Useful.")[0] == 0
    record = area.catalog.records()["lab-six"]
    kept, texts = area.catalog.kept_locks(record.source)
    assert kept.base_key == "python-base" and texts == {"wheel": WHEEL}
    assert {stat.S_IMODE(item.stat().st_mode) for item in area.catalog.locks_root.iterdir()} == {0o640}
    assert list(area.requests_root.iterdir()) == []


# --- Restoring a lock-built image ---


@pytest.fixture
def retired(lab, capsys, monkeypatch):
    """`six` admitted from locks on python-base, then deprecated and deleted (docker calls recorded)."""
    from labbioagentos import local_images

    area, path, lock, builds = lab
    docker = []
    monkeypatch.setattr(local_images.subprocess, "run", lambda argv, **_kwargs: docker.append(argv) or
                        SimpleNamespace(returncode=0, stdout="", stderr=""))
    _admit(capsys, path, lock)
    _cli(capsys, "image-deprecate", "--config", str(path), "--key", "six", "--reason", "Retired.")
    code, output = _cli(capsys, "image-delete", "--config", str(path), "--key", "six", "--confirm",
                        "--reason", "Freeing space.")
    assert code == 0, output
    return SimpleNamespace(area=area, path=path, lock=lock, builds=builds, docker=docker,
                           tmp=area.root.parent)


def _restore(capsys, lab, key="six", *extra):
    return _cli(capsys, "image-restore", "--config", str(lab.path), "--key", key, "--reason", "Needed again.",
                *extra)


def _catalog(lab):
    from labbioagentos.local_config import _image_catalog, load_settings

    return _image_catalog(load_settings(lab.path))


def test_a_deleted_lock_built_image_is_rebuilt_from_its_kept_locks(retired, capsys):
    code, output = _restore(capsys, retired)

    assert code == 0, output
    assert output["delivery"] == "locks_kept" and output["base"] == "python-base"
    assert output["state"] == "DEPRECATED" and output["produced_by"].startswith("restore-lock:")
    assert [build["texts"] for build in retired.builds] == [[WHEEL], [WHEEL]]
    catalog = _catalog(retired)
    assert not catalog.deleted("six") and catalog.realization("six") == "sha256:" + "2" * 64
    events = _personal(retired.tmp).realization_events()
    assert [event.action for event in events] == [RealizationAction.DELETED, RealizationAction.RESTORED]


def test_an_image_from_before_kept_locks_needs_them_given_again_and_then_keeps_them(retired, capsys):
    for item in _personal(retired.tmp).locks_root.iterdir():
        item.unlink()
    assert _restore(capsys, retired)[1]["error"] == "RESTORE_LOCKS_REQUIRED" and len(retired.builds) == 1

    code, output = _restore(capsys, retired, "six", "--base", "python-base", "--lock", str(retired.lock))

    assert code == 0 and output["delivery"] == "locks_given"
    record = _personal(retired.tmp).records()["six"]
    assert _personal(retired.tmp).kept_locks(record.source)[1] == {"wheel": WHEEL}


def test_kept_locks_that_fail_their_check_are_reported(retired, capsys):
    lock = next(_personal(retired.tmp).locks_root.glob("*.wheel.lock"))
    lock.chmod(0o600)
    lock.write_text(WHEEL + "\n")
    assert _restore(capsys, retired)[1]["error"] == "KEPT_LOCKS_INVALID" and len(retired.builds) == 1


def test_a_rebuild_with_another_identity_restores_nothing(retired, capsys):
    code, output = _restore(capsys, retired, "six", "--base", "tools", "--lock", str(retired.lock))
    assert code == 1 and output["error"] == "SOURCE_MISMATCH"
    assert _catalog(retired).deleted("six") and len(retired.builds) == 2


@pytest.mark.parametrize("extra, error", [
    (("--base", "python-base"), "LOCK_REQUIRED"),
    (("--lock", "x.lock"), "LOCK_REQUIRED"),
    (("--archive", "x.tar"), "RESTORE_FORM_INVALID"),
    (("--base", "nobody", "--lock", "LOCK"), "BASE_NOT_FOUND"),
])
def test_wrong_restore_arguments_build_nothing(retired, capsys, extra, error):
    extra = tuple(str(retired.lock) if item == "LOCK" else item for item in extra)
    code, output = _restore(capsys, retired, "six", *extra)
    assert code == 1 and output["error"] == error and len(retired.builds) == 1


def test_base_and_locks_are_only_for_lock_built_images(retired, capsys):
    from labbioagentos.local_config import load_settings

    catalog = _catalog(retired)
    catalog.personal.add_record(_record(key="pulled", owner=load_settings(retired.path).principal.user_id))
    catalog.set_state("pulled", catalog.state("pulled").DEPRECATED, actor="admin", reason="Retired.")
    catalog.record_realization("pulled", resolved_image="sha256:" + "c" * 64, action=RealizationAction.DELETED,
                               actor="admin", reason="Freeing space.")
    code, output = _restore(capsys, retired, "pulled", "--base", "python-base", "--lock", str(retired.lock))
    assert code == 1 and output["error"] == "LOCK_NOT_ALLOWED" and len(retired.builds) == 1


@pytest.mark.parametrize("change, error", [
    ("DEPRECATED", None), ("REVOKED", "BASE_REVOKED"), ("DELETED", "BASE_DELETED"),
])
def test_a_deprecated_base_is_fine_but_a_revoked_or_deleted_one_is_not(lab, capsys, monkeypatch, change, error):
    from labbioagentos import local_images
    from labbioagentos.local_config import load_settings

    area, path, lock, builds = lab
    monkeypatch.setattr(local_images.subprocess, "run", lambda argv, **_kwargs: SimpleNamespace(
        returncode=0, stdout="", stderr=""))
    catalog = _catalog(SimpleNamespace(path=path))
    catalog.personal.add_record(_record(key="pbase", owner=load_settings(path).principal.user_id))
    _admit(capsys, path, lock, base="pbase")
    for key in ("six", "pbase"):
        _cli(capsys, "image-deprecate", "--config", str(path), "--key", key, "--reason", "Retired.")
    assert _cli(capsys, "image-delete", "--config", str(path), "--key", "six", "--confirm",
                "--reason", "Freeing space.")[0] == 0
    catalog = _catalog(SimpleNamespace(path=path))
    if change == "REVOKED":
        catalog.set_state("pbase", catalog.state("pbase").REVOKED, actor="admin", reason="Bad.")
    elif change == "DELETED":
        catalog.record_realization("pbase", resolved_image="sha256:" + "c" * 64,
                                   action=RealizationAction.DELETED, actor="admin", reason="Freeing space.")

    code, output = _restore(capsys, SimpleNamespace(path=path))

    assert (code, output.get("error")) == ((0, None) if error is None else (1, error))
    assert len(builds) == (2 if error is None else 1)


def test_a_lab_image_is_rebuilt_from_the_lab_areas_kept_locks_on_a_lab_base(lab, capsys, monkeypatch):
    from labbioagentos import local_images
    from labbioagentos.local_config import load_settings

    area, path, lock, builds = lab
    monkeypatch.setattr(local_images.subprocess, "run", lambda argv, **_kwargs: SimpleNamespace(
        returncode=0, stdout="", stderr=""))
    config = ["--config", str(path)]
    _admit(capsys, path, lock)
    _, requested = _cli(capsys, "image-promote-request", *config, "--key", "six", "--base", "python-base",
                        "--lock", str(lock))
    assert _cli(capsys, "image-promote-approve", *config, "--request", requested["request_id"],
                "--key", "lab-six", "--reason", "Useful.")[0] == 0
    _cli(capsys, "image-deprecate", *config, "--key", "lab-six", "--reason", "Retired.")
    assert _cli(capsys, "image-delete", *config, "--key", "lab-six", "--confirm", "--reason", "Freeing.")[0] == 0

    catalog = _catalog(SimpleNamespace(path=path))
    catalog.personal.add_record(_record(key="pbase", owner=load_settings(path).principal.user_id))
    code, output = _restore(capsys, SimpleNamespace(path=path), "lab-six", "--base", "pbase", "--lock", str(lock))
    assert code == 1 and output["error"] == "BASE_NOT_LAB" and len(builds) == 2

    # The member's own copy has the same identity; only the lab area's kept locks may be used.
    for item in (path.parent / "member" / "catalog" / "locks").iterdir():
        item.unlink()
    stores, real = [], local_images._admission_jobs

    def spy(*args, **kwargs):
        stores.append(kwargs.get("sif_store"))
        return real(*args, **kwargs)

    monkeypatch.setattr(local_images, "_admission_jobs", spy)
    code, output = _restore(capsys, SimpleNamespace(path=path), "lab-six")
    assert code == 0 and output["scope"] == "LAB" and output["delivery"] == "locks_kept" and len(builds) == 3
    assert [store.root for store in stores] == [area.sif_root]  # A cluster rebuild lands in the lab store.
    (_deleted, restored) = area.catalog.realization_events()
    assert restored.action is RealizationAction.RESTORED and not _catalog(SimpleNamespace(path=path)).deleted(
        "lab-six")


def test_locks_given_again_for_a_lab_image_are_kept_for_the_lab(lab, capsys, monkeypatch):
    from labbioagentos import local_images

    area, path, lock, builds = lab
    monkeypatch.setattr(local_images.subprocess, "run", lambda argv, **_kwargs: SimpleNamespace(
        returncode=0, stdout="", stderr=""))
    config = ["--config", str(path)]
    _admit(capsys, path, lock)
    _, requested = _cli(capsys, "image-promote-request", *config, "--key", "six", "--base", "python-base",
                        "--lock", str(lock))
    assert _cli(capsys, "image-promote-approve", *config, "--request", requested["request_id"],
                "--key", "lab-six", "--reason", "Useful.")[0] == 0
    _cli(capsys, "image-deprecate", *config, "--key", "lab-six", "--reason", "Retired.")
    assert _cli(capsys, "image-delete", *config, "--key", "lab-six", "--confirm", "--reason", "Freeing.")[0] == 0
    for item in area.catalog.locks_root.iterdir():
        item.unlink()

    code, output = _restore(capsys, SimpleNamespace(path=path), "lab-six", "--base", "python-base",
                            "--lock", str(lock))

    assert code == 0 and output["delivery"] == "locks_given"
    assert {stat.S_IMODE(item.stat().st_mode) for item in area.catalog.locks_root.iterdir()} == {0o640}


def test_without_a_builder_nothing_is_rebuilt(retired, capsys):
    retired.path.write_text(retired.path.read_text().replace('[environment]\nroot = "envs"\n', ""))
    assert _restore(capsys, retired)[1]["error"] == "LOCK_BUILDS_UNAVAILABLE" and len(retired.builds) == 1
