"""C11 durable immutable Memory-store acceptance tests."""

from __future__ import annotations

from uuid import uuid4

import pytest

from labbioagentos.memory import (
    InMemoryMemoryStore,
    MemoryConflictError,
    MemoryDecision,
    MemoryKind,
    MemoryProposalAction,
    MemoryScope,
    MemoryStaleUpdateError,
    MemoryStatus,
    MemoryStore,
    MemoryUpdateProposal,
    SQLiteMemoryStore,
)


def _proposal(*, target=None, version=None, action=MemoryProposalAction.UPSERT):
    values = {
        "action": action,
        "target_scope": MemoryScope.PERSONAL,
        "owner_user_id": "user-a",
        "lab_id": "lab-a",
        "target_memory_id": target,
        "target_version": version,
        "proposed_kind": MemoryKind.OPERATING_NOTE,
        "proposed_content": "Prefer concise reports with explicit limitations.",
        "reason": "The user explicitly requested this durable preference.",
        "source_run_id": uuid4(),
    }
    if action is MemoryProposalAction.RETIRE:
        values["proposed_kind"] = None
        values["proposed_content"] = None
    return MemoryUpdateProposal(**values)


def _decision(proposal, approved=True):
    return MemoryDecision(
        proposal_id=proposal.proposal_id,
        gate_id=proposal.approval_gate_id,
        approved=approved,
        decided_by="user-a",
    )


def test_memory_store_protocol_and_proposal_conflict():
    store = InMemoryMemoryStore()
    assert isinstance(store, MemoryStore)
    proposal = _proposal()
    store.save_proposal(proposal)
    with pytest.raises(MemoryConflictError):
        store.save_proposal(proposal)


def test_approval_and_rejection_are_single_store_decisions():
    store = InMemoryMemoryStore()
    rejected = _proposal()
    store.save_proposal(rejected)
    assert store.decide_proposal(rejected.proposal_id, _decision(rejected, False)) is None
    assert store.get_decision(rejected.proposal_id).approved is False
    assert store.entries() == ()

    approved = _proposal()
    store.save_proposal(approved)
    entry = store.decide_proposal(approved.proposal_id, _decision(approved))
    assert entry is not None
    assert store.get_decision(approved.proposal_id).approved is True
    assert store.get(entry.memory_id, 1) == entry


def test_sqlite_restart_update_stale_guard_and_retirement(tmp_path):
    path = tmp_path / "memory.sqlite3"
    first = _proposal()
    store = SQLiteMemoryStore(path)
    assert isinstance(store, MemoryStore)
    store.save_proposal(first)
    v1 = store.decide_proposal(first.proposal_id, _decision(first))
    assert v1 is not None
    snapshot = v1.model_dump_json()
    store.close()

    store = SQLiteMemoryStore(path)
    assert store.get(v1.memory_id, 1).model_dump_json() == snapshot
    update = _proposal(target=v1.memory_id, version=1)
    store.save_proposal(update)
    v2 = store.decide_proposal(update.proposal_id, _decision(update))
    assert v2 is not None and v2.version == 2 and v2.previous_version == 1
    assert store.get(v1.memory_id, 1).model_dump_json() == snapshot

    stale = _proposal(target=v1.memory_id, version=1)
    store.save_proposal(stale)
    with pytest.raises(MemoryStaleUpdateError):
        store.decide_proposal(stale.proposal_id, _decision(stale))
    assert store.get_decision(stale.proposal_id) is None

    retire = _proposal(
        target=v1.memory_id,
        version=2,
        action=MemoryProposalAction.RETIRE,
    )
    store.save_proposal(retire)
    v3 = store.decide_proposal(retire.proposal_id, _decision(retire))
    assert v3 is not None and v3.status is MemoryStatus.RETIRED
    assert store.latest(v1.memory_id) == v3
    assert store.active_latest() == ()
    assert store.lineage(v1.memory_id) == (v1, v2, v3)
    store.close()

    reopened = SQLiteMemoryStore(path)
    assert reopened.lineage(v1.memory_id) == (v1, v2, v3)
    reopened.close()
