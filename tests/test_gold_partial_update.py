"""An UPDATE changes only the fields its author names, including after review."""

import json

import pytest

from labbioagentos import SkillDecisionError
from labbioagentos import local_gold
from labbioagentos.skills.curator import SkillCuratorError, TypedAuditedAdaptiveSkillCurator
from labbioagentos.skills.models import SkillAdaptiveCuratorDraft, SkillCuratorAudit, SkillProcedureDraft
from test_gold_evolution_curation import RecordingInvoker, approved_use, source
from test_local_gold import principal, service


class PatchInvoker(RecordingInvoker):
    def __init__(self, edits, *, request_revision=False):
        super().__init__()
        self.responses = iter(edits)
        self.audit_count = 0
        self.request_revision = request_revision
        self.response_models = []

    async def invoke(self, *, instructions, request, response_model):
        self.response_models.append(response_model.__name__)
        if response_model.__name__ == "SkillUpdatePatch":
            self.requests.append(json.loads(request))
            return response_model.model_validate_json(json.dumps(next(self.responses)))
        if response_model is SkillCuratorAudit:
            self.audit_count += 1
            self.requests.append(json.loads(request))
            findings = [{"category": "UNSUPPORTED_BY_SOURCE", "draft_field": "description",
                         "statement": "Clarify this description.", "rationale": "It is ambiguous."}]
            return response_model.model_validate_json(json.dumps({
                "summary": "Review.", "findings": findings if self.request_revision and self.audit_count == 1 else [],
            }))
        return await super().invoke(instructions=instructions, request=request, response_model=response_model)


class RichDraftInvoker(RecordingInvoker):
    def __init__(self):
        super().__init__()
        self.response_models = []

    async def invoke(self, *, instructions, request, response_model):
        self.response_models.append(response_model.__name__)
        value = await super().invoke(instructions=instructions, request=request, response_model=response_model)
        if response_model is SkillAdaptiveCuratorDraft:
            return value.model_copy(update={
                "proposed_name": "Independent fixture guidance",
                "procedure": value.procedure.model_copy(update={
                    "known_limitations": ("Verification depends on complete source availability.",),
                    "known_failure_modes": tuple(f"Retain failure mode {i}." for i in range(10)),
                    "parameter_guidance": ("Reconsider the initial task choice.",),
                }),
            })
        return value


@pytest.mark.parametrize("kind", ["ROOT", "FORK"])
@pytest.mark.asyncio
async def test_pending_feedback_edits_exact_draft_without_changing_lineage(source, kind):
    service = source[0].configuration.skill_service
    parent = None
    arguments = {}
    if kind == "FORK":
        parent, usage = approved_use(source)
        arguments = {"from_usage": usage.usage_id, "evolution": "fork"}
    writer = RichDraftInvoker()
    first = await local_gold.propose_from_completed_run(
        *source, TypedAuditedAdaptiveSkillCurator(writer), **arguments,
    )
    assert writer.response_models == (["SkillOutcomeSelection"] if parent else []) + [
        "SkillAdaptiveCuratorDraft", "SkillCuratorAudit"]
    edits = [
        {"edits": [{"field": "description", "value": "Clarified fixture description."}]},
        {"edits": [{"field": "parameter_guidance", "value": ["Review the current task choice."]}]},
    ]
    editor = PatchInvoker(edits, request_revision=True)
    revised = await local_gold.revise_personal_gold(
        *source, TypedAuditedAdaptiveSkillCurator(editor), proposal_id=first.proposal_id,
        feedback="Clarify only the description and the task choice.",
    )
    assert editor.response_models == ["SkillUpdatePatch", "SkillCuratorAudit"] * 2
    request = editor.requests[0]
    assert request["edit_base"] == "previous_draft"
    assert request["requested_change"]["previous_draft"]["proposed_name"] == first.proposed_name
    assert request["requested_change"].get("evolution_kind") == ("FORK" if parent else None)
    assert revised.description == edits[0]["edits"][0]["value"]
    assert revised.procedure.parameter_guidance == tuple(edits[1]["edits"][0]["value"])
    assert revised.procedure.model_dump(exclude={"parameter_guidance"}) == first.procedure.model_dump(
        exclude={"parameter_guidance"})
    assert revised.procedure.known_limitations and len(revised.procedure.known_failure_modes) == 10
    lineage = ("source_bundle_id", "source_run_id", "scope", "owner_user_id", "project_id", "lab_id",
               "parent_skill_id", "parent_version", "source_usage_record_id", "evolution_kind", "outcome_review")
    assert tuple(getattr(revised, key) for key in lineage) == tuple(getattr(first, key) for key in lineage)
    assert revised.revises_proposal_id == first.proposal_id
    assert service.pending_proposal(revised.proposal_id) == revised

    clearer = PatchInvoker([{"edits": [{"field": "known_limitations", "value": []}]}])
    cleared = await local_gold.revise_personal_gold(
        *source, TypedAuditedAdaptiveSkillCurator(clearer), proposal_id=revised.proposal_id,
        feedback="Explicitly remove the obsolete limitation section.",
    )
    assert clearer.response_models == ["SkillUpdatePatch", "SkillCuratorAudit"]
    assert cleared.procedure == revised.procedure.model_copy(update={"known_limitations": ()})
    assert tuple(getattr(cleared, key) for key in lineage) == tuple(getattr(first, key) for key in lineage)
    assert cleared.revises_proposal_id == revised.proposal_id
    for stale in (first, revised):
        assert service.store.get_proposal(stale.proposal_id) == stale
        with pytest.raises(SkillDecisionError):
            local_gold.decide_personal_gold(service, stale.proposal_id, stale.approval_gate_id, True, source[2])
    approved = local_gold.decide_personal_gold(
        service, cleared.proposal_id, cleared.approval_gate_id, True, source[2],
    )
    assert approved.procedure == cleared.procedure and approved.version == 1
    assert approved.evolution_kind == first.evolution_kind
    if parent:
        assert approved.skill_id != parent.skill_id
        assert service.get_gold(parent.skill_id, parent.version, principal=source[2]) == parent
    else:
        assert approved.parent_skill_id is None and approved.source_usage_record_id is None


def rich_parent(source, monkeypatch):
    import test_local_gold

    base = test_local_gold._draft()
    procedure = base.procedure.model_copy(update={
        "known_failure_modes": tuple(f"Retain failure mode {i}." for i in range(8)),
        "validation_expectations": tuple(f"Retain validation step {i}." for i in range(8)),
        "input_contract_ids": ("legacy_count_table",),
    })
    monkeypatch.setattr(test_local_gold, "_draft", lambda: base.model_copy(update={"procedure": procedure}))
    return approved_use(source)


@pytest.mark.asyncio
async def test_update_merge_preserves_parent_sections_and_legacy_contract_through_review(source, monkeypatch):
    parent, usage = rich_parent(source, monkeypatch)
    edits = [
        {"edits": [{"field": "parameter_guidance", "value": ["Choose stability measures for this question."]}]},
        {"edits": [{"field": "description", "value": "Compare direction and stability."}]},
    ]
    recorder = PatchInvoker(edits, request_revision=True)
    result = await local_gold.propose_from_completed_run(
        *source, TypedAuditedAdaptiveSkillCurator(recorder), from_usage=usage.usage_id, evolution="update",
    )
    assert (result.parent_skill_id, result.parent_version) == (parent.skill_id, parent.version)
    assert result.proposed_name == parent.name and result.description == edits[1]["edits"][0]["value"]
    assert result.procedure.parameter_guidance == tuple(edits[0]["edits"][0]["value"])
    editable = set(SkillProcedureDraft.model_fields)
    assert result.procedure.model_dump(include=editable-{"parameter_guidance"}) == parent.procedure.model_dump(
        include=editable-{"parameter_guidance"})
    assert [r["draft"]["procedure"]["known_failure_modes"] for r in recorder.requests if "reference_material" in r] == [
        list(parent.procedure.known_failure_modes), list(parent.procedure.known_failure_modes)]
    assert source[0].configuration.skill_service.pending_proposal(result.proposal_id) == result
    revised = await local_gold.revise_personal_gold(
        *source, TypedAuditedAdaptiveSkillCurator(PatchInvoker([{"edits": [{"field": "known_failure_modes", "value": []}]}])),
        proposal_id=result.proposal_id, feedback="Remove the obsolete failure list.",
    )
    assert revised.procedure.known_failure_modes == ()
    assert revised.description == result.description
    assert revised.procedure.parameter_guidance == result.procedure.parameter_guidance
    assert revised.procedure.validation_expectations == parent.procedure.validation_expectations
    assert parent.procedure.known_failure_modes and revised.revises_proposal_id == result.proposal_id
    service = source[0].configuration.skill_service
    approved = local_gold.decide_personal_gold(service, revised.proposal_id, revised.approval_gate_id,
                                              True, source[2])
    assert approved.skill_id == parent.skill_id and approved.version == parent.version + 1
    assert approved.procedure == revised.procedure
    assert service.get_gold(parent.skill_id, parent.version, principal=source[2]) == parent


@pytest.mark.parametrize("edits", [
    [{"field": "approval_gate_id", "value": "untrusted"}],
    [{"field": "workflow_outline", "value": []}],
    [{"field": "adaptation_points", "value": ["wrong shape"]}],
    [{"field": "description", "value": "Open /media/forbidden/raw"}],
    [{"field": "description", "value": "First"}, {"field": "description", "value": "Second"}],
])
@pytest.mark.asyncio
async def test_invalid_update_never_creates_a_candidate(source, monkeypatch, edits):
    _, usage = rich_parent(source, monkeypatch)
    store = source[0].configuration.skill_service.store
    before = store._load()._snapshot().proposals
    with pytest.raises(SkillCuratorError):
        await local_gold.propose_from_completed_run(
            *source, TypedAuditedAdaptiveSkillCurator(PatchInvoker([{"edits": edits}])),
            from_usage=usage.usage_id, evolution="update",
        )
    assert store._load()._snapshot().proposals == before
