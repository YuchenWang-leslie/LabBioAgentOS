"""C10 deterministic acceptance for durable, restart-safe control state."""

from __future__ import annotations

from uuid import uuid4

import pytest

from labbioagentos import (
    ApplicationRunRecord,
    InMemoryRunStateStore,
    RunRecoveryState,
    RunStatus,
    RunStateVersionConflictError,
    SQLiteRunStateStore,
    WorkflowRun,
    runtime_workflow_definition,
)


def _record() -> ApplicationRunRecord:
    definition = runtime_workflow_definition()
    run = WorkflowRun(
        workflow_id=definition.workflow_id,
        owner_user_id="user-c10",
        project_id="project-c10",
        lab_id="lab-c10",
    )
    return ApplicationRunRecord(
        run_id=run.run_id,
        task_text="Exercise durable generic control state.",
        owner_user_id=run.owner_user_id,
        project_id=run.project_id,
        lab_id=run.lab_id,
        workflow_run=run,
        runtime_revision="runtime-c10-test-v1",
        recovery_state=RunRecoveryState.STABLE,
    )


def test_d1_sqlite_run_state_roundtrip_uses_validated_json(tmp_path):
    database = tmp_path / "run-state.sqlite3"
    expected = _record()
    first = SQLiteRunStateStore(database)
    created = first.create(expected)
    first.close()

    reopened = SQLiteRunStateStore(database)
    loaded = reopened.get(expected.run_id)
    assert loaded == created == expected
    assert reopened.list() == (expected,)
    assert '"runtime_revision":"runtime-c10-test-v1"' in loaded.model_dump_json()
    assert "pickle" not in database.read_bytes().lower().decode(
        "utf-8", errors="ignore"
    )
    reopened.close()


@pytest.mark.parametrize("store_kind", ["memory", "sqlite"])
def test_d2_optimistic_record_version_conflict_is_explicit(tmp_path, store_kind):
    store = (
        InMemoryRunStateStore()
        if store_kind == "memory"
        else SQLiteRunStateStore(tmp_path / "state.sqlite3")
    )
    original = store.create(_record())
    replacement = original.model_copy(
        update={"task_text": "Updated only through the versioned store."}
    )
    current = store.update(replacement, expected_version=1)
    assert current.record_version == 2
    assert current.created_at == original.created_at
    assert current.updated_at >= original.updated_at

    with pytest.raises(RunStateVersionConflictError):
        store.update(original, expected_version=1)
    assert store.get(original.run_id) == current
    close = getattr(store, "close", None)
    if close is not None:
        close()


def test_d1_in_memory_store_does_not_share_mutable_workflow_objects():
    store = InMemoryRunStateStore()
    original = store.create(_record())
    loaded = store.get(original.run_id)
    loaded.workflow_run.status = RunStatus.RUNNING
    assert store.get(original.run_id).workflow_run.status.value == "CREATED"
