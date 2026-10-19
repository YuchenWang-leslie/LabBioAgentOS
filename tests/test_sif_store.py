"""C13 phase 2: the SIF store is content-addressed, read-only and fails closed."""

from __future__ import annotations

import hashlib
import os
import stat

import pytest

from labbioagentos.execution import SifStore, SifStoreError


def _source(tmp_path, content: bytes, name: str = "image.sif"):
    path = tmp_path / name
    path.write_bytes(content)
    return path


def test_register_streams_hash_into_private_read_only_store(tmp_path):
    content = os.urandom(3 * 1_048_576 + 17)  # More than one streaming chunk.
    store = SifStore(tmp_path / "store")

    reference = store.register(_source(tmp_path, content))

    assert reference == "sha256:" + hashlib.sha256(content).hexdigest()
    stored = store.path_for(reference)
    assert stored == store.root / f"{reference.removeprefix('sha256:')}.sif"
    assert stored.read_bytes() == content
    assert stat.S_IMODE(stored.stat().st_mode) == 0o444
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert [path.name for path in store.root.iterdir()] == [stored.name]


def test_register_is_idempotent_for_identical_content(tmp_path):
    store = SifStore(tmp_path / "store")
    first = store.register(_source(tmp_path, b"same", "a.sif"))
    second = store.register(_source(tmp_path, b"same", "b.sif"))

    assert first == second
    assert len(list(store.root.iterdir())) == 1


def test_register_rejects_symlink_and_non_regular_sources(tmp_path):
    store = SifStore(tmp_path / "store")
    target = _source(tmp_path, b"real")
    link = tmp_path / "link.sif"
    link.symlink_to(target)

    with pytest.raises(SifStoreError):
        store.register(link)
    with pytest.raises(SifStoreError):
        store.register(tmp_path)
    with pytest.raises(SifStoreError):
        store.register(tmp_path / "missing.sif")
    assert list(store.root.iterdir()) == []


def test_store_root_must_be_private_and_unaliased(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    alias = tmp_path / "alias"
    alias.symlink_to(real)
    with pytest.raises(SifStoreError):
        SifStore(alias)
    with pytest.raises(SifStoreError):
        SifStore(alias / "nested")

    shared = tmp_path / "shared"
    shared.mkdir()
    shared.chmod(0o755)
    with pytest.raises(SifStoreError):
        SifStore(shared)


@pytest.mark.parametrize("reference", [
    "", "sha256:abc", "sha256:" + "A" * 64, "local/image@sha256:" + "1" * 64,
    "sha256:" + "1" * 64 + "/../x", None,
])
def test_path_for_rejects_malformed_references(tmp_path, reference):
    with pytest.raises(SifStoreError):
        SifStore(tmp_path / "store").path_for(reference)


def test_path_for_rejects_missing_aliased_writable_or_linked_files(tmp_path):
    store = SifStore(tmp_path / "store")
    missing = "sha256:" + "2" * 64
    with pytest.raises(SifStoreError):
        store.path_for(missing)

    alias = store.root / f"{'3' * 64}.sif"
    alias.symlink_to(_source(tmp_path, b"elsewhere"))
    with pytest.raises(SifStoreError):
        store.path_for("sha256:" + "3" * 64)

    reference = store.register(_source(tmp_path, b"stored"))
    stored = store.path_for(reference)
    stored.chmod(0o644)
    with pytest.raises(SifStoreError):
        store.path_for(reference)
    stored.chmod(0o444)

    os.link(stored, tmp_path / "hardlink.sif")
    with pytest.raises(SifStoreError):
        store.path_for(reference)


def test_verify_detects_content_tampering_that_path_for_cannot_see(tmp_path):
    store = SifStore(tmp_path / "store")
    reference = store.register(_source(tmp_path, b"original"))
    stored = store.path_for(reference)
    stored.chmod(0o644)
    stored.write_bytes(b"tampered")
    stored.chmod(0o444)

    assert store.path_for(reference) == stored  # Mode-only checks still pass.
    with pytest.raises(SifStoreError):
        store.verify(reference)


def test_register_refuses_to_reuse_a_tampered_existing_entry(tmp_path):
    store = SifStore(tmp_path / "store")
    reference = store.register(_source(tmp_path, b"payload"))
    stored = store.path_for(reference)
    stored.chmod(0o644)
    stored.write_bytes(b"replaced")
    stored.chmod(0o444)

    with pytest.raises(SifStoreError):
        store.register(_source(tmp_path, b"payload", "again.sif"))
    assert not any(path.name.endswith(".tmp") for path in store.root.iterdir())
