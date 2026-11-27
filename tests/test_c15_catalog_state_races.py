"""State changes must validate against disk while holding the catalog writer lock."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, BrokenBarrierError

import pytest

from labbioagentos.execution.catalog import (
    CatalogError, CatalogStore, ExecutionBackendName, ImageCatalog, ImageState,
)


def _open(root):
    return ImageCatalog(owner_user_id="member", backend=ExecutionBackendName.DOCKER,
                        configured_keys=("base",), lab=CatalogStore(root))


def _change(catalog, state):
    catalog.set_state("base", state, actor="admin", reason="Lifecycle update.")


@pytest.mark.parametrize("target", [ImageState.DEPRECATED, ImageState.REVOKED])
def test_a_stale_catalog_cannot_append_after_revocation(tmp_path, target):
    store = CatalogStore(tmp_path / "catalog").initialize()
    first, stale = _open(store.root), _open(store.root)
    _change(first, ImageState.REVOKED)

    with pytest.raises(CatalogError):
        _change(stale, target)

    assert _open(store.root).state("base") is ImageState.REVOKED
    assert len(store.events()) == 1


def test_a_stale_catalog_can_make_a_current_legal_transition(tmp_path):
    store = CatalogStore(tmp_path / "catalog").initialize()
    first, stale = _open(store.root), _open(store.root)
    _change(first, ImageState.DEPRECATED)

    _change(stale, ImageState.ACTIVE)

    assert _open(store.root).state("base") is ImageState.ACTIVE
    assert [event.state for event in store.events()] == [ImageState.DEPRECATED, ImageState.ACTIVE]


def test_simultaneous_writers_leave_one_replayable_transition(tmp_path):
    store = CatalogStore(tmp_path / "catalog").initialize()
    catalogs = [_open(store.root), _open(store.root)]
    ready = Barrier(2)

    def change(catalog):
        ready.wait(timeout=5)
        try:
            _change(catalog, ImageState.DEPRECATED)
        except CatalogError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(change, catalogs))

    assert sorted(outcomes) == [False, True]
    assert _open(store.root).state("base") is ImageState.DEPRECATED
    assert len(store.events()) == 1


def test_simultaneous_member_approvals_leave_registry_replayable(tmp_path, monkeypatch):
    from labbioagentos import local_lab
    from labbioagentos.local_lab import LabArea, LabError, MemberAction

    area = LabArea.initialize(tmp_path / "lab", lab_id="lab", admin_user_id="admin", group=None)
    validated = Barrier(2)
    replay = local_lab._replay

    def pause_after_validation(events):
        members = replay(events)
        if events[-1].user_id == "new-member" and events[-1].action is MemberAction.APPROVE:
            # Without a writer lock both commands validate the same preimage before appending.
            # With a lock only the first enters; the second sees its persisted approval instead.
            try:
                validated.wait(timeout=1)
            except BrokenBarrierError:
                pass
        return members

    monkeypatch.setattr(local_lab, "_replay", pause_after_validation)
    ready = Barrier(2)

    def approve(_):
        ready.wait(timeout=5)
        try:
            area.change(MemberAction.APPROVE, user_id="new-member", uid=12345, reason="Joining.")
        except LabError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(approve, range(2)))

    assert sorted(outcomes) == [False, True]
    assert area.members()["new-member"].uid == 12345


def test_simultaneous_policy_updates_preserve_both_changes(tmp_path, monkeypatch):
    from labbioagentos.local_lab import LabArea

    area = LabArea.initialize(tmp_path / "lab", lab_id="lab", admin_user_id="admin", group=None)
    snapshots = Barrier(2)
    policy = area.policy

    def pause_after_read():
        current = policy()
        try:
            snapshots.wait(timeout=1)
        except BrokenBarrierError:
            pass
        return current

    monkeypatch.setattr(area, "policy", pause_after_read)
    ready = Barrier(2)

    def update(limits):
        ready.wait(timeout=5)
        area.set_policy(reason="Updated limit.", **limits)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(update, ({"max_image_bytes": 100}, {"member_quota_bytes": 200})))

    assert policy().max_image_bytes == 100
    assert policy().member_quota_bytes == 200
