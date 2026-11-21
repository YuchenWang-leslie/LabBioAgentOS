"""Feedback editing carries the same request through both authored drafts and audits."""

import json

import pytest

from labbioagentos import SkillDecisionError, local_gold
from labbioagentos.skills.curator import TypedAuditedAdaptiveSkillCurator
from labbioagentos.skills.models import SkillCuratorAudit, SkillProcedureDraft
from test_gold_evolution_curation import approved_use, source
from test_gold_partial_update import RichDraftInvoker
from test_local_gold import principal, service


class FeedbackInvoker:
    def __init__(self):
        self.calls = []
        self.audit_count = 0

    async def invoke(self, *, instructions, request, response_model):
        payload = json.loads(request)
        self.calls.append((instructions, payload, response_model.__name__))
        if response_model.__name__ == "SkillUpdatePatch":
            field, value = (("parameter_guidance", ["Reconsider current input."])
                            if self.audit_count == 0 else
                            ("applicability", "Compatible inputs with a declared delimiter."))
            return response_model.model_validate_json(json.dumps({
                "edits": [{"field": field, "value": value}],
            }))
        assert response_model is SkillCuratorAudit
        self.audit_count += 1
        return SkillCuratorAudit.model_validate_json(json.dumps({
            "summary": "Advisory feedback disposition.",
            "checks": [{"draft_field": "procedure.applicability", "evidence_ids": [],
                        "rationale": ("Unresolved in the current field." if self.audit_count == 1
                                      else "Resolved in the current field; no unrelated scope change.")}],
            "findings": ([{"category": "UNSUPPORTED_BY_SOURCE",
                          "draft_field": "procedure.applicability",
                          "statement": "The requested field correction remains unresolved.",
                          "rationale": "An explanation in another field does not remove this conflict."}]
                         if self.audit_count == 1 else []),
        }))


def jsonl_observer(path):
    def observe(kind, value):
        payload = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
        with path.open("a") as handle:
            handle.write(json.dumps({"kind": kind, "payload": payload}, sort_keys=True) + "\n")
    return observe


@pytest.mark.parametrize("kind", ["ROOT", "FORK"])
@pytest.mark.asyncio
async def test_feedback_contract_reaches_both_current_drafts_and_persisted_audits(source, tmp_path, kind):
    parent = None
    arguments = {}
    if kind == "FORK":
        parent, usage = approved_use(source)
        arguments = {"from_usage": usage.usage_id, "evolution": "fork"}
    first = await local_gold.propose_from_completed_run(
        *source, TypedAuditedAdaptiveSkillCurator(RichDraftInvoker()), **arguments,
    )
    feedback = "Clarify the input scope in its own field; preserve unrelated guidance."
    invoker = FeedbackInvoker()
    boundary_path = tmp_path / "model-boundaries.jsonl"
    candidate = await local_gold.revise_personal_gold(
        *source, TypedAuditedAdaptiveSkillCurator(invoker, boundary_observer=jsonl_observer(boundary_path)),
        proposal_id=first.proposal_id, feedback=feedback,
    )
    assert [call[2] for call in invoker.calls] == [
        "SkillUpdatePatch", "SkillCuratorAudit", "SkillUpdatePatch", "SkillCuratorAudit",
    ]
    edit1, audit1, edit2, audit2 = invoker.calls
    for instructions, _, _ in (edit1, edit2):
        assert "review_feedback" in instructions and "edit_base" in instructions
        assert "affected fields themselves" in instructions
        assert "disagree" in instructions and "source" in instructions
    for instructions, _, _ in (audit1, audit2):
        assert "review_feedback" in instructions and "checks" in instructions
        assert "resolved" in instructions and "unresolved" in instructions and "disagree" in instructions
        assert "contradictions" in instructions

    initial_payload = edit1[1]
    assert initial_payload["edit_base"] == "previous_draft"
    old_content = initial_payload["requested_change"]["previous_draft"]
    assert old_content["procedure"]["applicability"] == first.procedure.applicability
    assert initial_payload["requested_change"]["review_feedback"] == feedback
    current_payload = edit2[1]
    assert current_payload["edit_base"] == "draft"
    assert current_payload["source"]["requested_change"]["review_feedback"] == feedback
    assert current_payload["source"]["requested_change"]["previous_draft"] == old_content
    assert current_payload["draft"]["procedure"]["parameter_guidance"] == ["Reconsider current input."]
    assert current_payload["draft"]["procedure"]["applicability"] == first.procedure.applicability
    for _, payload, _ in (audit1, audit2):
        assert payload["reference_material"]["requested_change"]["review_feedback"] == feedback
    assert audit1[1]["draft"] == current_payload["draft"]
    assert audit2[1]["draft"]["procedure"]["applicability"] == candidate.procedure.applicability
    assert candidate.procedure.applicability == "Compatible inputs with a declared delimiter."
    editable = set(SkillProcedureDraft.model_fields)
    assert candidate.procedure.model_dump(include=editable-{"applicability", "parameter_guidance"}) == (
        first.procedure.model_dump(include=editable-{"applicability", "parameter_guidance"}))
    assert len(candidate.procedure.known_failure_modes) == 10 and candidate.procedure.known_limitations

    persisted = [json.loads(line) for line in boundary_path.read_text().splitlines()]
    audits = [row for row in persisted if row["kind"] in {"curator_audit", "curator_final_audit"}]
    assert [row["payload"]["checks"] for row in audits] == [
        [{"draft_field": "procedure.applicability", "evidence_ids": [],
                       "rationale": "Unresolved in the current field."}],
        [{"draft_field": "procedure.applicability", "evidence_ids": [],
          "rationale": "Resolved in the current field; no unrelated scope change."}],
    ]
    assert source[0].configuration.skill_service.pending_proposal(candidate.proposal_id) == candidate
    assert candidate.revises_proposal_id == first.proposal_id
    lineage = ("source_bundle_id", "source_run_id", "scope", "owner_user_id", "project_id", "lab_id",
               "parent_skill_id", "parent_version", "source_usage_record_id", "evolution_kind")
    assert tuple(getattr(candidate, key) for key in lineage) == tuple(getattr(first, key) for key in lineage)
    with pytest.raises(SkillDecisionError):
        local_gold.decide_personal_gold(
            source[0].configuration.skill_service, first.proposal_id, first.approval_gate_id, True, source[2])
    if parent:
        assert source[0].configuration.skill_service.get_gold(
            parent.skill_id, parent.version, principal=source[2]) == parent


class CaptureRichInvoker(RichDraftInvoker):
    def __init__(self):
        super().__init__()
        self.instructions = []

    async def invoke(self, *, instructions, request, response_model):
        self.instructions.append(instructions)
        return await super().invoke(instructions=instructions, request=request, response_model=response_model)


@pytest.mark.parametrize("with_parent", [False, True])
@pytest.mark.asyncio
async def test_no_feedback_does_not_add_editorial_contract_to_normal_curation(source, with_parent):
    args = {}
    if with_parent:
        _, usage = approved_use(source)
        args = {"from_usage": usage.usage_id, "evolution": "update"}
    invoker = CaptureRichInvoker()
    await local_gold.propose_from_completed_run(
        *source, TypedAuditedAdaptiveSkillCurator(invoker), **args,
    )
    assert invoker.instructions
    assert all("affected fields themselves" not in text and
               "resolved, unresolved" not in text for text in invoker.instructions)


class CaptureAutoInvoker(CaptureRichInvoker):
    async def invoke(self, *, instructions, request, response_model):
        if response_model.__name__ == "SkillEvolutionDecision":
            self.instructions.append(instructions)
            return response_model(action="UPDATE", reason="Fixture model proposes a local refinement.")
        return await super().invoke(instructions=instructions, request=request, response_model=response_model)


@pytest.mark.asyncio
async def test_default_auto_without_feedback_keeps_decision_and_authoring_protocol(source):
    parent, usage = approved_use(source)
    invoker = CaptureAutoInvoker()
    recommendation, candidate = await local_gold.propose_evolution_from_usage(
        *source, TypedAuditedAdaptiveSkillCurator(invoker), from_usage=usage.usage_id,
    )
    assert recommendation.action == "UPDATE" and candidate is not None
    assert candidate.parent_skill_id == parent.skill_id
    assert candidate.parent_version == parent.version
    assert candidate.source_usage_record_id == usage.usage_id
    assert candidate.review_feedback is None
    assert all("affected fields themselves" not in text and
               "resolved, unresolved" not in text for text in invoker.instructions)


class CaptureFreshInvoker(RichDraftInvoker):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def invoke(self, *, instructions, request, response_model):
        self.calls.append((instructions, json.loads(request), response_model.__name__))
        return await super().invoke(instructions=instructions, request=request, response_model=response_model)


@pytest.mark.asyncio
async def test_fresh_fork_feedback_does_not_assume_an_existing_edit_base(source):
    parent, usage = approved_use(source)
    invoker = CaptureFreshInvoker()
    feedback = "Reconsider the target input scope."
    candidate = await local_gold.propose_from_completed_run(
        *source, TypedAuditedAdaptiveSkillCurator(invoker),
        from_usage=usage.usage_id, evolution="fork", feedback=feedback,
    )
    (_, payload, _) = next(call for call in invoker.calls if call[2] == "SkillAdaptiveCuratorDraft")
    author_instructions = next(call[0] for call in invoker.calls if call[2] == "SkillAdaptiveCuratorDraft")
    assert "edit_base" not in payload
    assert "previous_draft" not in payload["requested_change"]
    assert "affected fields themselves" not in author_instructions
    audit_instructions, audit_payload, _ = next(call for call in invoker.calls if call[2] == "SkillCuratorAudit")
    assert "resolved, unresolved" in audit_instructions
    assert audit_payload["reference_material"]["requested_change"]["review_feedback"] == feedback
    assert candidate.review_feedback == feedback and candidate.parent_skill_id == parent.skill_id
    assert candidate.evolution_kind == "FORK" and candidate.parent_version == parent.version
