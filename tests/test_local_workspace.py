"""Managed identities and path boundaries without a provider or analysis script."""

import hashlib
import os
import sqlite3

import pytest

from labbioagentos.local_workspace import WorkspaceRegistry


@pytest.fixture
def registry(tmp_path):
    with WorkspaceRegistry.initialize(tmp_path / "managed") as registry:
        registry.create_user("TEST1", tmp_path / "one.token")
        registry.create_user("TEST2", tmp_path / "two.token")
        registry.create_project("TEST1", "PRJ1")
        registry.create_project("TEST2", "PRJ1")
        yield registry, tmp_path


def test_identity_paths_and_restart(registry):
    registry, tmp = registry
    first = registry.resolve("TEST1", "PRJ1", tmp / "one.token")
    second = registry.resolve("TEST2", "PRJ1", tmp / "two.token")
    assert first.user_id == "TEST1" and first.lab_id == "local-lab"
    assert first.project_root == registry.root / "TEST1/projects/PRJ1"
    assert first.gold_root == registry.root / "TEST1/GoldSkills"
    assert first.gold_root != second.gold_root
    assert first.data_root == first.project_root / "data"
    assert first.result_root == first.project_root / "runs"
    with WorkspaceRegistry(registry.root) as reopened:
        assert reopened.resolve("TEST1", "PRJ1", tmp / "one.token") == first


def test_token_is_private_hashed_and_never_printed(registry, capsys):
    registry, tmp = registry
    token = (tmp / "one.token").read_text().strip()
    assert len(token) == 43
    with sqlite3.connect(registry.root / "registry.sqlite") as database:
        row = database.execute("SELECT token_hash FROM users WHERE user_id = 'TEST1'").fetchone()
    assert row == (hashlib.sha256(token.encode()).hexdigest(),)
    assert token.encode() not in (registry.root / "registry.sqlite").read_bytes()
    assert (tmp / "one.token").stat().st_mode & 0o777 == 0o600
    assert (registry.root / "TEST1").stat().st_mode & 0o777 == 0o700
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("user,credential", [("TEST2", "one.token"), ("UNKNOWN", "one.token"), ("TEST1", "missing.token")])
def test_impersonation_and_unknown_credentials_fail_the_same_way(registry, user, credential):
    registry, tmp = registry
    with pytest.raises(PermissionError, match="^Workspace authentication failed$"):
        registry.resolve(user, "PRJ1", tmp / credential)


def test_unknown_project_is_not_inferred_from_directory(registry):
    registry, tmp = registry
    registry.create_project("TEST2", "OTHER")
    (registry.root / "TEST1/projects/OTHER").mkdir()
    with pytest.raises(PermissionError, match="not registered"):
        registry.resolve("TEST1", "OTHER", tmp / "one.token")


@pytest.mark.parametrize("identifier", ["..", "../other", "user/path", "a.b", "用户", "a" * 65, "-bad", ""])
def test_identifier_paths_cannot_escape(registry, identifier):
    registry, tmp = registry
    with pytest.raises(ValueError, match="identifier"):
        registry.create_user(identifier, tmp / "new.token")
    with pytest.raises(ValueError, match="identifier"):
        registry.create_project("TEST1", identifier)


def test_only_current_project_data_is_an_input(registry):
    registry, tmp = registry
    workspace = registry.resolve("TEST1", "PRJ1", tmp / "one.token")
    source = workspace.data_root / "input.csv"
    source.write_text("fixture")
    assert workspace.check_input(source) == source
    assert workspace.validate_run(workspace.result_root / "new") == workspace.result_root / "new"
    other = registry.resolve("TEST2", "PRJ1", tmp / "two.token").data_root / "input.csv"
    other.write_text("private fixture")
    registry.create_project("TEST1", "PRJ2")
    sibling = registry.resolve("TEST1", "PRJ2", tmp / "one.token").data_root / "input.csv"
    sibling.write_text("different project")
    prefix_alias = workspace.data_root.with_name("data-other") / "input.csv"
    for denied in (other, sibling, prefix_alias, tmp / "one.token", workspace.gold_root / "skill.json", workspace.data_root):
        with pytest.raises(PermissionError):
            workspace.check_input(denied)
    with pytest.raises(PermissionError):
        workspace.validate_run(workspace.result_root)
    with pytest.raises(PermissionError):
        workspace.validate_run(other.parent / "run")


def test_input_symlinks_ancestors_hardlinks_and_traversal_are_rejected(registry):
    registry, tmp = registry
    workspace = registry.resolve("TEST1", "PRJ1", tmp / "one.token")
    source = workspace.data_root / "input"
    source.write_text("fixture")
    alias = workspace.data_root / "alias"
    alias.symlink_to(source)
    directory_alias = workspace.data_root / "directory-alias"
    directory_alias.symlink_to(workspace.data_root, target_is_directory=True)
    for denied in (alias, directory_alias / "input", workspace.data_root / ".." / "data/input"):
        with pytest.raises(ValueError):
            workspace.check_input(denied)
    hardlink = workspace.data_root / "hardlink"
    os.link(source, hardlink)
    with pytest.raises(PermissionError):
        workspace.check_input(hardlink)


def test_run_alias_and_replaced_workspace_directory_are_rejected(registry):
    registry, tmp = registry
    workspace = registry.resolve("TEST1", "PRJ1", tmp / "one.token")
    alias = workspace.result_root / "alias"
    alias.symlink_to(workspace.result_root, target_is_directory=True)
    with pytest.raises(ValueError):
        workspace.validate_run(alias / "new")
    workspace.gold_root.rmdir()
    workspace.gold_root.symlink_to(registry.root / "TEST2/GoldSkills", target_is_directory=True)
    with pytest.raises(ValueError):
        registry.resolve("TEST1", "PRJ1", tmp / "one.token")


@pytest.mark.parametrize("mutation", ["public", "symlink", "parent-symlink", "oversized", "hardlink"])
def test_credential_file_boundary(registry, mutation):
    registry, tmp = registry
    token = tmp / "one.token"
    if mutation == "public":
        token.chmod(0o644)
    elif mutation == "symlink":
        token = tmp / "alias.token"
        token.symlink_to(tmp / "one.token")
    elif mutation == "parent-symlink":
        (tmp / "alias").symlink_to(tmp, target_is_directory=True)
        token = tmp / "alias/one.token"
    elif mutation == "oversized":
        token.write_text("x" * 257)
    elif mutation == "hardlink":
        os.link(token, tmp / "linked.token")
    with pytest.raises(PermissionError, match="^Workspace authentication failed$"):
        registry.authenticate("TEST1", token)


def test_existing_paths_are_never_adopted_or_overwritten(registry):
    registry, tmp = registry
    old_token = (tmp / "one.token").read_bytes()
    with pytest.raises(FileExistsError):
        registry.create_user("TEST1", tmp / "unused.token")
    with pytest.raises(FileExistsError):
        registry.create_user("TEST3", tmp / "one.token")
    with pytest.raises(FileExistsError):
        registry.create_project("TEST1", "PRJ1")
    with pytest.raises(ValueError):
        WorkspaceRegistry.initialize(registry.root)
    assert (tmp / "one.token").read_bytes() == old_token
    assert not (registry.root / "TEST3").exists()


def test_registry_and_sidecar_aliases_are_rejected(registry):
    registry, tmp = registry
    alias = tmp / "registry-alias"
    alias.symlink_to(registry.root, target_is_directory=True)
    with pytest.raises(ValueError):
        WorkspaceRegistry(alias)
    sidecar = registry.root / "registry.sqlite-wal"
    sidecar.symlink_to(tmp / "outside")
    with pytest.raises(ValueError):
        WorkspaceRegistry(registry.root)


def test_credentials_cannot_be_created_in_the_managed_tree(registry):
    registry, _ = registry
    with pytest.raises(ValueError, match="outside the managed workspace"):
        registry.create_user("TEST3", registry.root / "new.token")
    with pytest.raises(PermissionError):
        registry.create_project("UNKNOWN", "PRJ1")


def test_failed_credential_creation_rolls_back_only_new_state(registry, monkeypatch):
    registry, tmp = registry
    old_token = (tmp / "one.token").read_bytes()

    def deny_create(*_args):
        raise OSError("fixture write denied")

    monkeypatch.setattr(os, "open", deny_create)
    with pytest.raises(OSError):
        registry.create_user("TEST3", tmp / "new.token")
    assert not (registry.root / "TEST3").exists()
    with sqlite3.connect(registry.root / "registry.sqlite") as database:
        assert database.execute("SELECT 1 FROM users WHERE user_id = 'TEST3'").fetchone() is None
    assert (tmp / "one.token").read_bytes() == old_token
