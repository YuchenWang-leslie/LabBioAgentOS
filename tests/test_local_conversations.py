"""Durable conversation identities, not inferred task-completion history."""

import json
import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from labbioagentos.contracts import WorkflowRun
from labbioagentos.governance.models import Principal, WorkspaceContext
from labbioagentos.local_conversations import (
    ConversationConflictError,
    ConversationNotFoundError,
    ConversationStore,
    read_run_record,
)
from labbioagentos.run_state import ApplicationRunRecord, SQLiteRunStateStore


@pytest.fixture
def scope(tmp_path):
    root = tmp_path / "runs"
    root.mkdir(mode=0o700)
    principal = Principal(user_id="owner", lab_id="lab")
    workspace = WorkspaceContext(user_id="owner", project_id="project", lab_id="lab")
    return root, principal, workspace


def _record(scope):
    _, principal, workspace = scope
    run = WorkflowRun(
        workflow_id="fixture-workflow", owner_user_id=principal.user_id,
        project_id=workspace.project_id, lab_id=workspace.lab_id,
    )
    return ApplicationRunRecord(
        run_id=run.run_id, task_text="User's generic task.",
        owner_user_id=principal.user_id, project_id=workspace.project_id,
        lab_id=workspace.lab_id, workflow_run=run, runtime_revision="fixture-v1",
    )


def _persist(root, record):
    directory = root / str(record.run_id)
    directory.mkdir(mode=0o700)
    (directory / "RUN.json").write_text(json.dumps({"run_id": str(record.run_id)}))
    store = SQLiteRunStateStore(directory / "state.sqlite")
    store.create(record)
    store.close()
    return directory


def test_restart_parent_identity_and_idempotent_add(scope):
    root, principal, workspace = scope
    first, second = _record(scope), _record(scope)
    first_directory, second_directory = _persist(root, first), _persist(root, second)
    with ConversationStore(*scope) as store:
        original = store.add_run("conversation", first, first_directory)
        assert store.add_run("conversation", first, first_directory) == original
        revised = store.add_run(
            "conversation", second, second_directory, parent_run_id=first.run_id,
        )
        assert original.sequence == 1 and revised.sequence == 2
        assert revised.parent_run_id == first.run_id
    with ConversationStore(root, principal, workspace) as restarted:
        assert restarted.get_run("conversation", second.run_id) == revised
        assert restarted.list_runs("conversation")["items"] == [
            original.model_dump(mode="json"), revised.model_dump(mode="json"),
        ]
    assert read_run_record(first_directory) == first
    assert read_run_record(second_directory) == second
    assert (root / "conversations.sqlite").stat().st_mode & 0o777 == 0o600


def test_existing_run_or_directory_cannot_be_reassigned(scope):
    root, _, _ = scope
    first, second = _record(scope), _record(scope)
    first_directory, second_directory = _persist(root, first), _persist(root, second)
    with ConversationStore(*scope) as store:
        store.add_run("one", first, first_directory)
        for conversation, record, directory in (
            ("two", first, first_directory),
            ("one", first, second_directory),
            ("one", second, first_directory),
        ):
            with pytest.raises(ConversationConflictError):
                store.add_run(conversation, record, directory)
        with pytest.raises(ConversationNotFoundError):
            store.get_run("two", first.run_id)
        assert store.list_runs()["total"] == 1


def test_parent_must_exist_in_exact_conversation(scope):
    root, _, _ = scope
    first, second = _record(scope), _record(scope)
    first_directory, second_directory = _persist(root, first), _persist(root, second)
    with ConversationStore(*scope) as store:
        store.add_run("one", first, first_directory)
        for parent in (first.run_id, uuid4(), second.run_id):
            with pytest.raises(ConversationConflictError):
                store.add_run("two", second, second_directory, parent_run_id=parent)
        assert store.list_runs()["total"] == 1


def test_existing_revision_parent_cannot_change(scope):
    root, _, _ = scope
    first, second = _record(scope), _record(scope)
    with ConversationStore(*scope) as store:
        store.add_run("one", first, _persist(root, first))
        directory = _persist(root, second)
        store.add_run("one", second, directory, parent_run_id=first.run_id)
        with pytest.raises(ConversationConflictError):
            store.add_run("one", second, directory)
        assert store.get_run("one", second.run_id).parent_run_id == first.run_id


def test_concurrent_writers_assign_distinct_monotonic_sequence(scope):
    root, _, _ = scope
    records = [_record(scope) for _ in range(6)]
    directories = [_persist(root, record) for record in records]
    with ConversationStore(*scope) as first, ConversationStore(*scope) as second:
        def add(index):
            return (first if index % 2 else second).add_run(
                "one", records[index], directories[index],
            )
        with ThreadPoolExecutor(max_workers=2) as pool:
            links = list(pool.map(add, range(len(records))))
        assert sorted(link.sequence for link in links) == list(range(1, 7))
        assert first.list_runs("one")["count"] == 6


@pytest.mark.parametrize("field", ["user_id", "project_id", "lab_id"])
def test_catalog_scope_cannot_be_reopened_as_another_identity(scope, field):
    root, principal, workspace = scope
    with ConversationStore(*scope):
        pass
    other_workspace = workspace.model_copy(update={field: "other"})
    other_principal = principal
    if field in ("user_id", "lab_id"):
        other_principal = principal.model_copy(update={field: "other"})
    before = (root / "conversations.sqlite").read_bytes()
    with pytest.raises(PermissionError):
        ConversationStore(root, other_principal, other_workspace)
    assert (root / "conversations.sqlite").read_bytes() == before


def test_principal_workspace_and_record_scope_are_required(scope):
    root, principal, workspace = scope
    with pytest.raises(PermissionError):
        ConversationStore(root, principal.model_copy(update={"user_id": "other"}), workspace)
    foreign = (root, principal, workspace.model_copy(update={"project_id": "other"}))
    record = _record(foreign)
    directory = _persist(root, record)
    with ConversationStore(*scope) as store:
        with pytest.raises(PermissionError):
            store.add_run("one", record, directory)
        assert store.list_runs()["total"] == 0


def test_bounded_deterministic_pagination(scope):
    root, _, _ = scope
    with ConversationStore(*scope) as store:
        for conversation in ("one", "two", "one", "one"):
            record = _record(scope)
            store.add_run(conversation, record, _persist(root, record))
        first = store.list_runs("one", limit=2)
        assert first["total"] == 3 and first["count"] == 2
        assert first["truncated"] is True and first["next_offset"] == 2
        second = store.list_runs("one", offset=2, limit=2)
        assert second["count"] == 1 and second["next_offset"] is None
        assert [item["sequence"] for item in first["items"] + second["items"]] == [1, 2, 3]
        assert store.list_runs()["total"] == 4
        assert store.list_runs("absent")["items"] == []
        for kwargs in ({"offset": -1}, {"limit": 0}, {"limit": 101}, {"limit": True}):
            with pytest.raises(ValueError):
                store.list_runs(**kwargs)
        assert "User's generic task" not in json.dumps(store.list_runs())


@pytest.mark.parametrize("identifier", ["", "../other", "a/b", "has space", "x" * 65])
def test_conversation_ids_are_bounded_not_paths(scope, identifier):
    root, _, _ = scope
    record = _record(scope)
    directory = _persist(root, record)
    with ConversationStore(*scope) as store:
        with pytest.raises(ValueError):
            store.add_run(identifier, record, directory)
        with pytest.raises(ValueError):
            store.list_runs(identifier)


def test_directory_must_be_existing_unaliased_child(scope, tmp_path):
    root, _, _ = scope
    record = _record(scope)
    directory = _persist(root, record)
    alias = root / "alias"
    alias.symlink_to(directory, target_is_directory=True)
    with ConversationStore(*scope) as store:
        for path in (root, tmp_path, alias, directory / ".." / directory.name, root / "missing"):
            with pytest.raises((ValueError, PermissionError)):
                store.add_run("one", record, path)
        assert store.list_runs()["total"] == 0


@pytest.mark.parametrize("suffix", ["", "-journal", "-wal", "-shm"])
@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_catalog_rejects_aliased_database_and_sidecars(scope, tmp_path, suffix, alias_kind):
    root, _, _ = scope
    outside = tmp_path / "outside"
    outside.write_bytes(b"untouched")
    outside.chmod(0o600)
    target = root / ("conversations.sqlite" + suffix)
    if alias_kind == "symlink":
        target.symlink_to(outside)
    else:
        os.link(outside, target)
    with pytest.raises((ValueError, PermissionError)):
        ConversationStore(*scope)
    assert outside.read_bytes() == b"untouched"


def test_catalog_rejects_aliased_root(scope, tmp_path):
    root, principal, workspace = scope
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError):
        ConversationStore(alias, principal, workspace)


@pytest.mark.parametrize("suffix", ["", "-journal", "-wal", "-shm"])
def test_catalog_rejects_nonprivate_persistence(scope, suffix):
    root, _, _ = scope
    target = root / ("conversations.sqlite" + suffix)
    target.write_bytes(b"not private")
    target.chmod(0o644)
    with pytest.raises(PermissionError):
        ConversationStore(*scope)


def test_read_run_record_preserves_authority_and_database_bytes(scope):
    root, _, _ = scope
    record = _record(scope)
    directory = _persist(root, record)
    database = directory / "state.sqlite"
    before = database.read_bytes()
    (directory / "run-trace.jsonl").write_text('{"status":"COMPLETED"}\n')
    assert read_run_record(directory) == record
    assert database.read_bytes() == before
    assert record.workflow_run.status.value == "CREATED"


def test_read_run_record_observes_committed_wal_without_checkpoint(scope):
    root, _, _ = scope
    record = _record(scope)
    directory = _persist(root, record)
    writer = SQLiteRunStateStore(directory / "state.sqlite")
    try:
        newer = writer.update(
            record.model_copy(update={"task_text": "Updated durable fixture task."}),
            expected_version=record.record_version,
        )
        assert (directory / "state.sqlite-wal").is_file()
        before = (directory / "state.sqlite").read_bytes()
        assert read_run_record(directory) == newer
        assert (directory / "state.sqlite").read_bytes() == before
    finally:
        writer.close()


@pytest.mark.parametrize("target_name", ["RUN.json", "state.sqlite", "state.sqlite-wal", "state.sqlite-shm"])
@pytest.mark.parametrize("alias_kind", ["symlink", "hardlink"])
def test_read_run_rejects_aliased_persistence(scope, tmp_path, target_name, alias_kind):
    root, _, _ = scope
    directory = _persist(root, _record(scope))
    target = directory / target_name
    outside = tmp_path / "outside"
    if target.exists():
        target.rename(outside)
    else:
        outside.write_bytes(b"untouched")
    before = outside.read_bytes()
    if alias_kind == "symlink":
        target.symlink_to(outside)
    else:
        os.link(outside, target)
    with pytest.raises((ValueError, PermissionError)):
        read_run_record(directory)
    assert outside.read_bytes() == before


@pytest.mark.parametrize("damage", ["missing_manifest", "oversize_manifest", "missing_database", "missing_row", "wrong_version", "invalid_payload", "wrong_id"])
def test_read_run_rejects_incomplete_or_inconsistent_state(scope, damage):
    root, _, _ = scope
    record = _record(scope)
    directory = _persist(root, record)
    manifest = directory / "RUN.json"
    database = directory / "state.sqlite"
    if damage == "missing_manifest":
        manifest.unlink()
    elif damage == "oversize_manifest":
        manifest.write_text(" " * 4097)
    elif damage == "missing_database":
        database.unlink()
    elif damage == "wrong_id":
        manifest.write_text(json.dumps({"run_id": str(uuid4())}))
    else:
        with sqlite3.connect(database) as connection:
            if damage == "missing_row":
                connection.execute("DELETE FROM application_run_state")
            elif damage == "wrong_version":
                connection.execute("UPDATE application_run_state SET record_version=999")
            else:
                connection.execute("UPDATE application_run_state SET payload='{}'")
    with pytest.raises((ValueError, PermissionError, FileNotFoundError)):
        read_run_record(directory)
