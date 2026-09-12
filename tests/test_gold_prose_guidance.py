"""A small curation envelope preserves free-form Agent guidance verbatim."""

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from labbioagentos.contracts import RunStatus
from labbioagentos.skills.curator import PantheonAuditedAdaptiveSkillCurator
from labbioagentos.skills.models import (
    SkillCurationSourceView, SkillCuratorAudit, SkillGuidanceDraft,
)
from test_gold_evidence_audit import _agent


def _draft(**updates):
    return SkillGuidanceDraft.model_validate_json(json.dumps({
        "proposed_name": "Document conversion reference",
        "description": "An adaptable document guide.",
        "applicability": "Compatible document tasks.",
        "guidance": "Inspect the current document.\n\nChoose a converter for its format. "
                    "Adjust the checks to the requested output; this is a reference, not a mandate.",
        "tags": ["document"], "artifact_types": ["document"], **updates,
    }))


def test_prose_envelope_does_not_require_procedural_subfields():
    schema = SkillGuidanceDraft.model_json_schema()
    assert set(schema["required"]) == {"proposed_name", "description", "applicability", "guidance"}
    assert schema["properties"]["guidance"]["type"] == "string"
    assert "procedure" not in schema["properties"]
    assert schema["additionalProperties"] is False
    draft = _draft()
    stored = draft.to_curator_draft()
    assert stored.procedure.workflow_outline == (draft.guidance,)
    assert stored.procedure.tags == draft.tags
    assert stored.procedure.adaptation_points == ()
    assert stored.procedure.debug_lessons == ()


@pytest.mark.parametrize("updates", [
    {"owner_user_id": "someone-else"}, {"guidance": "Bearer secret_token_fixture_12345"},
    {"guidance": ""}, {"guidance": "x" * 4001},
])
def test_free_prose_keeps_authority_safety_and_size_boundaries(updates):
    with pytest.raises(ValidationError):
        _draft(**updates)


@pytest.mark.asyncio
@pytest.mark.parametrize("revise", [False, True])
async def test_prose_agent_review_and_revision_remain_content_owners(revise):
    source = SkillCurationSourceView(source_bundle_id=uuid4(), source_run_id=uuid4(),
                                   final_status=RunStatus.COMPLETED, workflow_stage_path=())
    initial = _draft()
    revised = _draft(guidance="Agent-revised reference; adapt to current document needs.")
    good = SkillCuratorAudit.model_validate_json(json.dumps({
        "findings": [{"category": "UNSUPPORTED_BY_SOURCE", "draft_field": "guidance",
                      "statement": "Optional synthetic improvement.", "rationale": "Advisory only."}],
        "summary": "Useful advisory guidance with an optional improvement.",
    }))
    bad = SkillCuratorAudit.model_validate_json(json.dumps({
        "findings": [{"category": "UNSUPPORTED_BY_SOURCE", "draft_field": "guidance",
                      "statement": "Synthetic suggestion.", "rationale": "Agent may improve clarity."}]
                    if revise else [],
        "summary": "Fixture concern.",
    }))
    captured = []
    result = await PantheonAuditedAdaptiveSkillCurator(
        _agent(SkillGuidanceDraft, [initial], captured),
        _agent(SkillCuratorAudit, [bad, good] if revise else [bad], captured),
        _agent(SkillGuidanceDraft, [revised], captured),
    ).propose(source)
    assert result.procedure == (revised if revise else initial).to_curator_draft().procedure
    assert result.review_notes[0] == (good if revise else bad).summary
    assert len(captured) == (4 if revise else 2)
    if revise:
        assert captured[2]["audit"] == bad.model_dump(mode="json")
        assert captured[3]["draft"] == revised.model_dump(mode="json")


def test_review_has_no_hash_quote_severity_or_pointer_contract():
    schema = SkillCuratorAudit.model_json_schema()
    assert "draft_sha256" not in schema["properties"]
    finding = schema["$defs"]["SkillCuratorAuditFinding"]["properties"]
    assert not {"draft_quote", "severity"} & finding.keys()
    assert "JSON Pointer" not in json.dumps(schema)


@pytest.mark.asyncio
async def test_advisory_notes_survive_restart_but_do_not_approve_or_enter_gold(tmp_path):
    from labbioagentos.skills import SQLiteSkillStore, SkillUserDecision
    from test_c9_gold_skill_lifecycle import _context, _governed, _source_bundle

    path = tmp_path / "gold.sqlite"
    store = SQLiteSkillStore(path)
    principal, _, artifacts, _, _, service, _, _ = _governed(tmp_path, store=store)
    bundle = _source_bundle(artifacts)
    store.save_source_bundle(bundle)
    draft = _draft()
    audit = SkillCuratorAudit.model_validate_json(json.dumps({
        "summary": "Agent advisory review; details can change for the next task.",
        "findings": [{"category": "UNSUPPORTED_BY_SOURCE", "draft_field": "guidance",
                      "statement": "Consider more detail.", "rationale": "Optional advice."}],
    }))
    captured = []
    curator = PantheonAuditedAdaptiveSkillCurator(
        _agent(SkillGuidanceDraft, [draft], captured),
        _agent(SkillCuratorAudit, [audit, audit], captured),
        _agent(SkillGuidanceDraft, [draft], captured),
    )
    proposal = await service.curate_proposal(bundle.bundle_id, curator, _context())
    assert len(captured) == 4
    assert proposal.review_notes == (audit.summary, audit.findings[0].statement,
                                     audit.findings[0].rationale)
    store.close()
    restored = SQLiteSkillStore(path)
    try:
        assert restored.get_proposal(proposal.proposal_id) == proposal
        assert restored._load()._gold == {}
        gold = restored.decide_proposal(proposal.proposal_id, SkillUserDecision(
            subject_id=proposal.proposal_id, gate_id=proposal.approval_gate_id,
            approved=True, decided_by=principal.user_id,
        ))
        assert gold.procedure.workflow_outline == (_draft().guidance,)
        assert "review_notes" not in gold.model_dump()
    finally:
        restored.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("field, statement", [
    ("artifact_types", "The draft contains markdown_report."),
    ("guidance", "The draft cites local-flat-records-v1."),
])
async def test_source_draft_confusion_remains_visible_without_veto_or_host_rewrite(field, statement):
    draft = _draft()
    source = SkillCurationSourceView(source_bundle_id=uuid4(), source_run_id=uuid4(),
                                   final_status=RunStatus.COMPLETED, workflow_stage_path=())
    audit = SkillCuratorAudit.model_validate_json(json.dumps({
        "summary": "Synthetic reproduction of a mistaken review.",
        "findings": [{"category": "UNSUPPORTED_BY_SOURCE", "draft_field": field,
                      "statement": statement, "rationale": "Synthetic source/draft confusion."}],
    }))
    captured = []
    result = await PantheonAuditedAdaptiveSkillCurator(
        _agent(SkillGuidanceDraft, [draft], captured),
        _agent(SkillCuratorAudit, [audit, audit], captured),
        _agent(SkillGuidanceDraft, [draft], captured),
    ).propose(source)
    assert statement in result.review_notes
    assert result.procedure == draft.to_curator_draft().procedure
    assert captured[-1]["draft"] == draft.model_dump(mode="json")
    assert len(captured) == 4  # no review/rewrite loop and no fabricated correction
