"""Full local provenance is referenced, not duplicated into bounded guidance."""

import asyncio
from uuid import uuid4

import pytest

from labbioagentos import (
    Principal, RunStatus, SkillCuratorDraft, SkillCuratorPort, SkillDecisionError,
    SkillProcedureDraft, SkillProposal, SkillProposalContext, SkillScope,
    SkillSearchContext, SkillSourceBundle, SkillUserDecision,
)
from labbioagentos.local_gold import build_personal_gold_service, close_personal_gold


class FixtureCurator(SkillCuratorPort):
    async def propose(self, source):
        self.seen = source
        return SkillCuratorDraft(
            proposed_name="Synthetic lineage fixture",
            description="Persistence test only; no scientific guidance.",
            procedure=SkillProcedureDraft(applicability="Synthetic fixture only.",
                workflow_outline=("Read the fixture statement.",)),
        )


@pytest.mark.parametrize("event_count", [1, 512, 513, 622, 1500])
def test_complete_trace_survives_curation_approval_and_restart(tmp_path, event_count):
    root = tmp_path / "GoldSkills"
    actor = Principal(user_id="fixture-user", lab_id="fixture-lab")
    service = build_personal_gold_service(root, actor.user_id)
    bundle = SkillSourceBundle(
        source_run_id=uuid4(), final_status=RunStatus.COMPLETED,
        workflow_stage_path=(), trace_event_ids=tuple(uuid4() for _ in range(event_count)),
    )
    curator = FixtureCurator()
    try:
        service.store.save_source_bundle(bundle)
        proposal = asyncio.run(service.curate_proposal(bundle.bundle_id, curator,
            SkillProposalContext(scope=SkillScope.PERSONAL,
                owner_user_id=actor.user_id, lab_id=actor.lab_id)))
        assert curator.seen.source_bundle_id == bundle.bundle_id
        assert "trace_event_ids" not in curator.seen.model_dump()
        assert proposal.procedure.source_trace_event_ids == ()
        assert proposal.source_bundle_id == bundle.bundle_id
        assert service.store.get_source_bundle(bundle.bundle_id) == bundle
        search = SkillSearchContext(user_id=actor.user_id, lab_id=actor.lab_id)
        assert service.search(search, principal=actor) == ()
        with pytest.raises(SkillDecisionError):
            service.decide_proposal(proposal.proposal_id, SkillUserDecision(
                subject_id=proposal.proposal_id, gate_id="wrong", approved=True,
                decided_by=actor.user_id), principal=actor)
        gold = service.decide_proposal(proposal.proposal_id, SkillUserDecision(
            subject_id=proposal.proposal_id, gate_id=proposal.approval_gate_id,
            approved=True, decided_by=actor.user_id), principal=actor)
    finally:
        close_personal_gold(service)
    reopened = build_personal_gold_service(root, actor.user_id)
    try:
        restored = reopened.get_gold(gold.skill_id, gold.version, principal=actor)
        assert restored == gold
        assert reopened.store.get_source_bundle(restored.source_bundle_id) == bundle
        assert reopened.search(search, principal=actor) == (gold,)
    finally:
        close_personal_gold(reopened)


def test_legacy_embedded_trace_ids_remain_unchanged(tmp_path):
    root = tmp_path / "GoldSkills"
    actor = Principal(user_id="fixture-user", lab_id="fixture-lab")
    service = build_personal_gold_service(root, actor.user_id)
    bundle = SkillSourceBundle(source_run_id=uuid4(), final_status=RunStatus.COMPLETED,
        workflow_stage_path=(), trace_event_ids=(uuid4(), uuid4()))
    try:
        service.store.save_source_bundle(bundle)
        proposal = SkillProposal(
            source_bundle_id=bundle.bundle_id, source_run_id=bundle.source_run_id,
            proposed_name="Legacy fixture", description="Legacy local record.",
            scope=SkillScope.PERSONAL, owner_user_id=actor.user_id, lab_id=actor.lab_id,
            procedure={"applicability": "Synthetic fixture only.",
                       "workflow_outline": ("Read the fixture statement.",),
                       "source_trace_event_ids": bundle.trace_event_ids},
        )
        service.store.save_proposal(proposal)
        gold = service.decide_proposal(proposal.proposal_id, SkillUserDecision(
            subject_id=proposal.proposal_id, gate_id=proposal.approval_gate_id,
            approved=True, decided_by=actor.user_id), principal=actor)
        before = gold.model_dump_json()
    finally:
        close_personal_gold(service)
    reopened = build_personal_gold_service(root, actor.user_id)
    try:
        restored = reopened.get_gold(gold.skill_id, gold.version, principal=actor)
        assert restored.model_dump_json() == before
        assert restored.procedure.source_trace_event_ids == bundle.trace_event_ids
    finally:
        close_personal_gold(reopened)
