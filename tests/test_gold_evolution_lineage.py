"""Persistence and approval boundaries for evolving personal Gold procedures."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

import pytest
from pydantic import ValidationError

from labbioagentos import (
    AuthorizationDenied, RunStatus, SQLiteSkillStore, SkillDecisionError,
    SkillProposalContext, SkillSearchContext, SkillStoreError, SkillUseMode,
    SkillUserDecision,
)
from test_c9_gold_skill_lifecycle import (
    _context, _create_gold, _decide_use, _draft, _governed, _source_bundle, _submit_use,
)
from labbioagentos.skills.models import SkillDeliveryFile, SkillOutcomeEvidence, SkillSelectedDelivery


def _creation_context(parent=None, usage=None, **changes):
    fields = _context(parent=parent, source_usage_record_id=usage).model_dump()
    return SkillProposalContext.model_validate({**fields, **changes})


def _decision(proposal, principal, approved=True):
    return SkillUserDecision(subject_id=proposal.proposal_id, gate_id=proposal.approval_gate_id,
                             approved=approved, decided_by=principal.user_id)


def _adapt(service, principal, workspace, parent, run_id):
    use = _submit_use(service, principal, workspace, parent, run_id, mode=SkillUseMode.ADAPT)
    auth = _decide_use(service, principal, use)
    service.get_authorized_context(auth.authorization_id, run_id=run_id,
                                   principal=principal, project_id=workspace.project_id)
    return service.finalize_run_usage(run_id, RunStatus.COMPLETED)[0]


@pytest.mark.parametrize("selection", ["mixed", "imported_only", "current_only", "empty", "legacy"])
def test_update_program_provenance_follows_formal_delivery(tmp_path, selection):
    principal, workspace, artifacts, _, store, service, _, _ = _governed(tmp_path)
    _, _, parent = _create_gold(service, store, principal, artifacts)
    run_id = uuid4()
    usage = _adapt(service, principal, workspace, parent, run_id)
    source = _source_bundle(artifacts, run_id=run_id)
    audit_ref = source.execution_refs[0]
    retry_ref = audit_ref.model_copy(update={
        "execution_id": uuid4(), "script_artifact_id": uuid4(), "status": "FAILED", "exit_code": 1,
    })
    imported = SkillDeliveryFile(artifact_id=uuid4(), producer_run_id=uuid4(), execution_id=uuid4(),
                                 sha256="b" * 64, size_bytes=64)
    audit = SkillDeliveryFile(artifact_id=audit_ref.script_artifact_id, producer_run_id=run_id,
                              execution_id=audit_ref.execution_id, sha256=audit_ref.script_hash, size_bytes=32)
    programs = {"mixed": (imported, audit), "imported_only": (imported,),
                "current_only": (audit,), "empty": (), "legacy": None}[selection]
    delivery = None
    if programs is not None:
        outputs = tuple(program.model_copy(update={"artifact_id": uuid4()}) for program in programs)
        delivery = SkillSelectedDelivery(outputs=outputs, programs=programs)
    source = source.model_copy(update={"outcome_evidence": SkillOutcomeEvidence(selected_delivery=delivery),
                                       "execution_refs": (retry_ref, audit_ref)})
    _, proposal, updated = _create_gold(service, store, principal, artifacts, bundle=source,
        context=_creation_context(parent, usage.usage_id, evolution_kind="UPDATE"))
    expected = ((retry_ref.script_artifact_id, audit_ref.script_artifact_id) if programs is None
                else tuple(program.artifact_id for program in programs))
    assert proposal.procedure.script_artifact_ids == updated.procedure.script_artifact_ids == expected
    frozen = store.get_source_bundle(updated.source_bundle_id)
    assert frozen.outcome_evidence.selected_delivery == delivery
    assert frozen.execution_refs == (retry_ref, audit_ref)
    assert (updated.source_run_id, updated.source_usage_record_id) == (run_id, usage.usage_id)
    assert (updated.parent_skill_id, updated.parent_version) == (parent.skill_id, parent.version)


@pytest.mark.parametrize("wrong_parent", ["identity", "version"])
def test_adapt_receipt_must_belong_to_exact_parent(tmp_path, wrong_parent):
    principal, workspace, artifacts, _, store, service, _, _ = _governed(tmp_path)
    _, _, parent = _create_gold(service, store, principal, artifacts)
    other = _create_gold(service, store, principal, artifacts,
                         context=_context(parent=parent) if wrong_parent == "version" else None)[2]
    run_id = uuid4()
    usage = _adapt(service, principal, workspace, parent, run_id)
    source = _source_bundle(artifacts, run_id=run_id)
    store.save_source_bundle(source)
    with pytest.raises(SkillStoreError):
        service.create_proposal(source.bundle_id, _draft(), _context(
            parent=other, source_usage_record_id=usage.usage_id))
    assert service.parent_for_adapt(usage.usage_id, run_id=run_id, principal=principal,
                                    project_id=workspace.project_id) == parent
    with pytest.raises(AuthorizationDenied):
        service.parent_for_adapt(usage.usage_id, run_id=run_id, principal=principal,
                                 project_id="different-project")
    with pytest.raises(SkillStoreError):
        service.parent_for_adapt(usage.usage_id, run_id=uuid4(), principal=principal,
                                 project_id=workspace.project_id)


def test_two_forks_and_update_keep_parent_and_siblings_visible(tmp_path):
    path = tmp_path / "gold.sqlite"
    store = SQLiteSkillStore(path)
    try:
        principal, workspace, artifacts, _, _, service, _, _ = _governed(tmp_path, store=store)
        _, _, parent = _create_gold(service, store, principal, artifacts)
        children = []
        for kind in ("UPDATE", "FORK", "FORK"):
            run_id = uuid4()
            usage = _adapt(service, principal, workspace, parent, run_id)
            source = _source_bundle(artifacts, run_id=run_id)
            _, _, child = _create_gold(service, store, principal, artifacts, bundle=source,
                context=_creation_context(parent, usage.usage_id, evolution_kind=kind))
            assert (child.parent_skill_id, child.parent_version) == (parent.skill_id, 1)
            assert child.evolution_kind == kind
            children.append(child)
        updated, fork1, fork2 = children
        assert (updated.skill_id, updated.version) == (parent.skill_id, 2)
        assert len({parent.skill_id, fork1.skill_id, fork2.skill_id}) == 3
        assert fork1.version == fork2.version == 1
        assert store.get_gold(parent.skill_id, 1) == parent
        _, _, updated_fork = _create_gold(service, store, principal, artifacts,
                                            context=_context(parent=fork1))
        assert (updated_fork.skill_id, updated_fork.parent_skill_id,
                updated_fork.parent_version) == (fork1.skill_id, fork1.skill_id, 1)
    finally:
        store.close()
    reopened = SQLiteSkillStore(path)
    try:
        found = reopened.search(SkillSearchContext(user_id=principal.user_id, lab_id=principal.lab_id))
        assert {(v.skill_id, v.version) for v in found} == {
            (v.skill_id, v.version) for v in (parent, updated, fork1, fork2, updated_fork)}
    finally:
        reopened.close()


def test_fork_requires_parent_and_review_revision_requires_feedback():
    with pytest.raises(ValidationError):
        _creation_context(evolution_kind="FORK")
    with pytest.raises(ValidationError):
        _creation_context(revises_proposal_id=uuid4())


def test_old_json_snapshots_load_as_root_and_linear_update(tmp_path):
    path = tmp_path / "gold.sqlite"
    store = SQLiteSkillStore(path)
    try:
        principal, _, artifacts, _, _, service, _, _ = _governed(tmp_path, store=store)
        _, _, v1 = _create_gold(service, store, principal, artifacts)
        _, _, v2 = _create_gold(service, store, principal, artifacts, context=_context(parent=v1))
        row = store._connection.execute("SELECT payload FROM skill_store_state").fetchone()
        data = json.loads(row[0])
        for obj in [*data["proposals"], *data["gold"]]:
            for key in ("evolution_kind", "revises_proposal_id", "review_feedback"):
                obj.pop(key, None)
        store._connection.execute("UPDATE skill_store_state SET payload = ?", (json.dumps(data),))
        assert store.lineage(v1.skill_id) == (v1, v2)
    finally:
        store.close()


def test_revised_candidate_retires_exact_prior_gate_and_keeps_history(tmp_path):
    principal, _, artifacts, _, store, service, _, _ = _governed(tmp_path)
    source = _source_bundle(artifacts)
    store.save_source_bundle(source)
    old = service.create_proposal(source.bundle_id, _draft(), _context())
    revised_context = _creation_context(revises_proposal_id=old.proposal_id,
                                        review_feedback="Keep the applicability narrower.")
    another_source = _source_bundle(artifacts)
    store.save_source_bundle(another_source)
    with pytest.raises(SkillStoreError):
        service.create_proposal(another_source.bundle_id, _draft(), revised_context)
    revised = service.create_proposal(source.bundle_id, _draft("Narrower scope"), revised_context)
    assert store.get_proposal(old.proposal_id) == old
    assert service.pending_proposal(revised.proposal_id) == revised
    assert revised.review_feedback == revised_context.review_feedback
    for approved in (True, False):
        with pytest.raises(SkillDecisionError):
            service.decide_proposal(old.proposal_id, _decision(old, principal, approved),
                                    principal=principal)
    with pytest.raises(SkillDecisionError):
        service.pending_proposal(old.proposal_id)
    assert service.decide_proposal(revised.proposal_id, _decision(revised, principal, False),
                                   principal=principal) is None
    with pytest.raises(SkillDecisionError):
        service.pending_proposal(revised.proposal_id)
    with pytest.raises(SkillDecisionError):
        service.create_proposal(source.bundle_id, _draft(), _creation_context(
            revises_proposal_id=revised.proposal_id, review_feedback="Too late."))


def test_old_pending_candidate_with_mismatched_usage_can_only_be_rejected(tmp_path):
    path = tmp_path / "gold.sqlite"
    store = SQLiteSkillStore(path)
    try:
        principal, workspace, artifacts, _, _, service, _, _ = _governed(tmp_path, store=store)
        _, _, parent = _create_gold(service, store, principal, artifacts)
        _, _, other = _create_gold(service, store, principal, artifacts)
        run_id = uuid4()
        usage = _adapt(service, principal, workspace, parent, run_id)
        source = _source_bundle(artifacts, run_id=run_id)
        store.save_source_bundle(source)
        proposal = service.create_proposal(source.bundle_id, _draft(),
                                           _context(parent=parent, source_usage_record_id=usage.usage_id))
        # Old stores permitted a receipt from any Skill if its run matched.
        raw = json.loads(store._connection.execute("SELECT payload FROM skill_store_state").fetchone()[0])
        persisted = next(p for p in raw["proposals"] if p["proposal_id"] == str(proposal.proposal_id))
        persisted["parent_skill_id"] = str(other.skill_id)
        store._connection.execute("UPDATE skill_store_state SET payload = ?", (json.dumps(raw),))
        with pytest.raises(SkillStoreError, match="exact parent"):
            service.decide_proposal(proposal.proposal_id, _decision(proposal, principal), principal=principal)
        assert store.lineage(other.skill_id) == (other,)
        assert service.decide_proposal(proposal.proposal_id, _decision(proposal, principal, False),
                                       principal=principal) is None
    finally:
        store.close()


def test_two_sqlite_writers_cannot_both_revise_one_gate(tmp_path):
    path = tmp_path / "gold.sqlite"
    store = SQLiteSkillStore(path)
    try:
        principal, _, artifacts, _, _, service, _, _ = _governed(tmp_path, store=store)
        source = _source_bundle(artifacts)
        store.save_source_bundle(source)
        original = service.create_proposal(source.bundle_id, _draft(), _context())
        revised_context = _creation_context(revises_proposal_id=original.proposal_id,
                                            review_feedback="Recheck applicability.")
        barrier = Barrier(2)

        def revise(index):
            other = SQLiteSkillStore(path)
            try:
                _, _, _, _, _, writer, _, _ = _governed(tmp_path, store=other, trace=False)
                barrier.wait(timeout=10)
                return writer.create_proposal(source.bundle_id, _draft(f"Revision {index}"),
                                              revised_context)
            except SkillDecisionError:
                return None
            finally:
                other.close()

        with ThreadPoolExecutor(2) as workers:
            winners = [p for p in workers.map(revise, (1, 2)) if p is not None]
        assert len(winners) == 1
        winner = winners[0]
        approved = service.decide_proposal(winner.proposal_id, _decision(winner, principal),
                                           principal=principal)
        assert approved.source_proposal_id == winner.proposal_id
        with pytest.raises(SkillDecisionError):
            service.decide_proposal(winner.proposal_id, _decision(winner, principal),
                                    principal=principal)
    finally:
        store.close()
