"""Local Gold curation follows the exact approved use, source and pending candidate."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from labbioagentos import (
    InMemoryTraceSink, LocalArtifactStore, Project, RunStatus, RunTraceRecorder,
    SkillDecisionError, SkillStoreError, SkillUseMode, TraceEventType, WorkspaceContext,
)
from labbioagentos import local_gold
from labbioagentos.run_state import RunRecoveryState
from labbioagentos.skills.curator import TypedAuditedAdaptiveSkillCurator
from labbioagentos.skills.models import SkillAdaptiveCuratorDraft, SkillCuratorAudit
from test_c9_gold_skill_lifecycle import _decide_use, _submit_use
from test_local_gold import _FixtureCurator, _gold, principal, service


@pytest.fixture
def source(service, principal, tmp_path):
    workspace = WorkspaceContext(user_id=principal.user_id, project_id="PRJ1", lab_id=principal.lab_id)
    service.access_service.projects.register(Project(project_id="PRJ1", owner_user_id=principal.user_id,
                                                     lab_id=principal.lab_id))
    run_id = uuid4()
    recorder = RunTraceRecorder(InMemoryTraceSink())
    recorder.emit(run_id, TraceEventType.RUN_STARTED, status="RUNNING")
    recorder.emit(run_id, TraceEventType.RUN_COMPLETED, status="COMPLETED")
    app = SimpleNamespace(
        configuration=SimpleNamespace(skill_service=service, boundary_observer=None, runtime_revision="candidate"),
        run_state_store=SimpleNamespace(get=lambda _: SimpleNamespace(
            owner_user_id=principal.user_id, project_id=workspace.project_id, lab_id=principal.lab_id,
            runtime_results=(), workflow_run=SimpleNamespace(status=RunStatus.COMPLETED),
            recovery_state=RunRecoveryState.STABLE, runtime_revision="source")),
        access_service=service.access_service, trace_recorder=recorder,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
    )
    return app, run_id, principal, workspace


class RecordingInvoker:
    def __init__(self):
        self.requests = []

    async def invoke(self, *, instructions, request, response_model):
        from labbioagentos.skills.models import SkillOutcomeSelection
        self.requests.append(json.loads(request))
        if response_model is SkillOutcomeSelection:
            return SkillOutcomeSelection(
                research_question="Review the user's requested outcome.",
                primary_result="No selected report was recorded.", report_anchor_id=None,
                assessment="INSUFFICIENT_EVIDENCE", explanation="No delivery evidence.",
            )
        if response_model is SkillCuratorAudit:
            return SkillCuratorAudit(summary="Advisory review", findings=())
        if response_model.__name__ == "SkillUpdatePatch":
            return response_model.model_validate_json(json.dumps({
                "edits": [{"field": "proposed_name", "value": "Agent-written change"}],
            }))
        assert response_model is SkillAdaptiveCuratorDraft
        return SkillAdaptiveCuratorDraft.model_validate({
            "proposed_name": "Agent-written change",
            "description": "Conditional update.",
            "procedure": {"applicability": "Compatible observations", "workflow_guidance": ("Inspect inputs.",),
                          "reusable_principles": ("Validate the target.",), "adaptation_points": ({
                              "decision": "Choose evidence", "evidence_requirements": ("Current inputs",),
                              "selection_considerations": ("Scope",),
                              "revalidation_requirements": ("Check new data.",)},)},
        })


def approved_use(source, *, mode=SkillUseMode.ADAPT, alternate=False):
    app, run_id, principal, workspace = source
    service = app.configuration.skill_service
    _, gold = _gold(service, principal)
    proposal = _submit_use(service, principal, workspace, gold, run_id, mode=mode)
    if alternate:
        from labbioagentos.skills.models import SkillUseProposal
        selected = SkillUseProposal.model_validate({
            **proposal.model_dump(exclude={"proposal_id", "approval_gate_id"}),
            "reason": "Use the audit steps and omit the optional archive.",
            "proposed_deviations": ("Deliver the summary only.", "Do not include /etc/passwd"),
        })
        service.submit_use_proposal(selected, principal=principal)
        proposal = selected
    authorization = _decide_use(service, principal, proposal)
    service.get_authorized_context(authorization.authorization_id, run_id=run_id,
                                   project_id=workspace.project_id, principal=principal)
    usage = service.finalize_run_usage(run_id, RunStatus.COMPLETED)[0]
    return gold, usage


def test_approved_adaptation_preserves_positions_under_text_budget(source):
    from labbioagentos.skills.curator import ApprovedAdaptationView

    app, run_id, principal, workspace = source
    _, usage = approved_use(source)
    _, proposal = app.configuration.skill_service.authorized_adaptation(
        usage.usage_id, run_id=run_id, project_id=workspace.project_id, principal=principal,
    )
    lines = tuple("场景" * 1500 for _ in range(8)) + ("Final exception.",)
    view = ApprovedAdaptationView.from_proposal(proposal.model_copy(update={"proposed_deviations": lines}))
    assert len(view.proposed_deviations) == len(lines)
    assert view.proposed_deviations[0].text == lines[0]
    assert view.proposed_deviations[-2].status == "LENGTH_LIMIT"
    assert view.proposed_deviations[-1].text == lines[-1]
    assert all(m.text == s or m.status == "LENGTH_LIMIT" for m, s in zip(view.proposed_deviations, lines))


@pytest.mark.parametrize("evolution", ["update", "fork"])
@pytest.mark.asyncio
async def test_native_curation_observes_exact_parent_and_intent(source, evolution):
    gold, usage = approved_use(source)
    invoker = RecordingInvoker()
    candidate = await local_gold.propose_from_completed_run(
        *source, TypedAuditedAdaptiveSkillCurator(invoker),
        from_usage=usage.usage_id, evolution=evolution, feedback="Recheck the input convention.",
    )
    assert candidate.parent_skill_id == gold.skill_id and candidate.parent_version == gold.version
    assert candidate.source_usage_record_id == usage.usage_id
    assert candidate.evolution_kind == evolution.upper()
    assert candidate.proposed_name == "Agent-written change" and candidate.review_feedback
    alignment, first, audit = invoker.requests
    assert "requested_change" not in alignment
    assert first["requested_change"]["outcome_review"]["assessment"] == "INSUFFICIENT_EVIDENCE"
    assert candidate.outcome_review.model_dump(exclude_none=True) == first["requested_change"]["outcome_review"]
    change = first["requested_change"]
    assert change["approved_adaptation"]["proposed_deviations"] == [
        {"status": "AVAILABLE", "text": "Use current task parameters."},
    ]
    assert audit["reference_material"]["requested_change"] == change
    assert first["source_run_id"] == str(usage.run_id)
    assert change["parent_guidance"]["procedure"]["workflow_outline"] == list(gold.procedure.workflow_outline)
    assert change["review_feedback"] == candidate.review_feedback
    assert "script_artifact_ids" not in change["parent_guidance"]["procedure"]
    assert "approval_gate_id" not in change["parent_guidance"]


@pytest.mark.asyncio
async def test_redrafting_reuses_frozen_bundle_and_supersedes_only_after_model_returns(source):
    app, run_id, principal, workspace = source
    service = app.configuration.skill_service
    root = await local_gold.propose_from_completed_run(*source, _FixtureCurator())
    class Unavailable(_FixtureCurator):
        async def propose_with_context(self, source, *, change):
            raise ConnectionError("fixture offline")
    with pytest.raises(ConnectionError):
        await local_gold.revise_personal_gold(*source, Unavailable(), proposal_id=root.proposal_id,
                                              feedback="Keep the clarification.")
    assert service.pending_proposal(root.proposal_id) == root
    invoker = RecordingInvoker()
    candidate = await local_gold.revise_personal_gold(
        *source, TypedAuditedAdaptiveSkillCurator(invoker), proposal_id=root.proposal_id,
        feedback="Keep the clarification.",
    )
    assert (candidate.source_bundle_id, candidate.source_run_id) == (root.source_bundle_id, run_id)
    assert candidate.revises_proposal_id == root.proposal_id and candidate.review_feedback
    change = invoker.requests[0]["requested_change"]
    assert change["previous_draft"]["proposed_name"] == root.proposed_name
    assert "parent_guidance" not in change
    with pytest.raises(SkillDecisionError):
        local_gold.decide_personal_gold(service, root.proposal_id, root.approval_gate_id, True, principal)
    assert service.store.get_proposal(root.proposal_id) == root
    assert service.pending_proposal(candidate.proposal_id) == candidate
    assert local_gold.decide_personal_gold(service, candidate.proposal_id, candidate.approval_gate_id,
                                          True, principal).name == candidate.proposed_name


@pytest.mark.asyncio
async def test_feedback_preserves_fork_edits_and_rejects_wrong_run_before_invocation(source):
    app, run_id, principal, workspace = source
    parent, usage = approved_use(source)
    invoker = RecordingInvoker()
    curator = TypedAuditedAdaptiveSkillCurator(invoker)
    first = await local_gold.propose_from_completed_run(
        *source, curator, from_usage=usage.usage_id, evolution="fork", feedback="Distinguish input formats."
    )
    calls = len(invoker.requests)
    with pytest.raises(PermissionError):
        await local_gold.revise_personal_gold(app, uuid4(), principal, workspace, curator,
                                              proposal_id=first.proposal_id, feedback="Wrong run")
    assert len(invoker.requests) == calls
    second = await local_gold.revise_personal_gold(
        *source, curator, proposal_id=first.proposal_id, feedback="Clarify the new branch."
    )
    assert (second.evolution_kind, second.parent_skill_id, second.parent_version,
            second.source_usage_record_id, second.source_bundle_id) == (
        "FORK", parent.skill_id, parent.version, usage.usage_id, first.source_bundle_id
    )
    changed = invoker.requests[calls]["requested_change"]
    assert changed["approved_adaptation"] == invoker.requests[1]["requested_change"]["approved_adaptation"]
    assert (changed["previous_review_feedback"], changed["review_feedback"]) == (
        first.review_feedback, second.review_feedback
    )
    assert changed["parent_guidance"]["proposed_name"] == parent.name
    assert changed["previous_draft"]["proposed_name"] == first.proposed_name
    calls = len(invoker.requests)
    with pytest.raises(SkillDecisionError):
        await local_gold.revise_personal_gold(*source, curator, proposal_id=first.proposal_id,
                                              feedback="Competing stale revision")
    assert len(invoker.requests) == calls


@pytest.mark.asyncio
async def test_unqualified_adapt_or_bad_feedback_never_reaches_provider(source):
    _, usage = approved_use(source, mode=SkillUseMode.REFERENCE)
    invoker = RecordingInvoker()
    curator = TypedAuditedAdaptiveSkillCurator(invoker)
    with pytest.raises(SkillStoreError):
        await local_gold.propose_from_completed_run(*source, curator, from_usage=usage.usage_id)
    with pytest.raises(ValueError):
        await local_gold.propose_from_completed_run(*source, curator, evolution="fork")
    with pytest.raises(ValueError):
        await local_gold.propose_from_completed_run(*source, curator, feedback="Open /media/private/raw")
    assert not invoker.requests


@pytest.mark.parametrize("action", ["KEEP", "UPDATE", "FORK"])
@pytest.mark.asyncio
async def test_agent_recommends_action_before_any_candidate_is_saved(source, action):
    from labbioagentos.skills import curator as curators
    parent, usage = approved_use(source, alternate=True)
    app = source[0]
    record = app.run_state_store.get(usage.run_id)
    record.task_text = "Compare the two input conventions."
    app.run_state_store.get = lambda _: record
    observations = []
    app.configuration.boundary_observer = lambda kind, value: observations.append((kind, value))

    class RecommendingInvoker(RecordingInvoker):
        async def invoke(self, *, instructions, request, response_model):
            if response_model is curators.SkillEvolutionDecision:
                self.requests.append(json.loads(request))
                return response_model(action=action, reason="Target inputs justify this choice.")
            return await super().invoke(instructions=instructions, request=request, response_model=response_model)

    invoker = RecommendingInvoker()
    curator = TypedAuditedAdaptiveSkillCurator(invoker, boundary_observer=app.configuration.boundary_observer)
    recommendation, proposal = await local_gold.propose_evolution_from_usage(
        *source, curator, from_usage=usage.usage_id, feedback="Review the new sample design."
    )
    assert recommendation.action == action
    approved = invoker.requests[1]["requested_change"]["approved_adaptation"]
    assert approved == {"reason": {"status": "AVAILABLE", "text": "Use the audit steps and omit the optional archive."},
                        "proposed_deviations": [{"status": "AVAILABLE", "text": "Deliver the summary only."},
                                                {"status": "WITHHELD"}]}
    assert "approved_adaptation" not in invoker.requests[0]
    assert "/etc/passwd" not in json.dumps(invoker.requests)
    if action != "KEEP":
        assert invoker.requests[2]["requested_change"]["approved_adaptation"] == approved
    assert sum(kind == "curation_runtime" for kind, _ in observations) == 1
    alignment, request = invoker.requests[:2]
    assert "requested_change" not in alignment
    assert "outcome_review" in request
    assert request["outcome_review"] == recommendation.outcome_review.model_dump(mode="json")
    assert request["source"]["source_run_id"] == str(usage.run_id)
    assert request["source"]["source_user_request"]["text"] == record.task_text
    assert "task_reference" not in request["source"]
    assert request["requested_change"]["parent_guidance"]["proposed_name"] == parent.name
    assert request["requested_change"]["review_feedback"] == "Review the new sample design."
    assert any(kind == "curator_evolution_recommendation" for kind, _ in observations)
    if action == "KEEP":
        assert proposal is None and len(invoker.requests) == 2
        proposals = app.configuration.skill_service.store._load()._snapshot().proposals
        assert all(item.source_run_id != usage.run_id for item in proposals)
    else:
        assert proposal.evolution_kind == action and proposal.source_usage_record_id == usage.usage_id
        assert str(proposal.source_bundle_id) == request["source"]["source_bundle_id"]
        assert invoker.requests[2]["requested_change"]["recommendation_reason"] == recommendation.reason
        assert proposal.outcome_review == recommendation.outcome_review
        assert invoker.requests[2]["requested_change"]["outcome_review"] == recommendation.outcome_review.model_dump(exclude_none=True)


@pytest.mark.parametrize("request_text,expected", [
    ("这是直接来自研究者的原任务。", "AVAILABLE"),
    ("Use the current input and describe limitations.", "AVAILABLE"),
    ("分析输入。" * 2500, "AVAILABLE"),
    ("x" * 32_001, "LENGTH_LIMIT"),
    ("Dataset location is /srv/restricted/data.csv", "WITHHELD"),
    ("检查数据，路径见/media/private/sensitive.txt", "WITHHELD"),
    ("The token=fixture-RESTRICTED must not travel", "WITHHELD"),
    (None, "NOT_RECORDED"),
], ids=["chinese", "english", "long-valid", "over-limit", "host-path", "adjacent-path", "credential", "missing"])
@pytest.mark.asyncio
async def test_original_request_is_a_safe_user_assertion_on_the_frozen_source(source, request_text, expected):
    from labbioagentos.skills.source import SkillSourceProjector
    app, run_id, _, _ = source
    rec = app.run_state_store.get(run_id)
    rec.task_text = request_text
    app.run_state_store.get = lambda _: rec
    fixture = _FixtureCurator()
    proposal = await local_gold.propose_from_completed_run(*source, fixture)
    bundle = app.configuration.skill_service.store.get_source_bundle(proposal.source_bundle_id)
    view = SkillSourceProjector.curation_view(bundle)
    for payload in (SkillSourceProjector.guidance_view(view), SkillSourceProjector.review_view(view)):
        intent = payload["source_user_request"]
        assert intent["authority"] == "USER_ASSERTION" and intent["status"] == expected
        if expected == "AVAILABLE":
            assert intent["text"] == request_text
        else:
            assert not intent.get("text")
            if request_text:
                assert request_text not in json.dumps(payload, ensure_ascii=False)
        assert "task_reference" not in payload
    # Reopening the evidence for a revision does not query today's source record.
    rec.task_text = "Different later text"
    fresh_view = app.configuration.skill_service.create_curation_view(proposal.source_bundle_id)
    assert fresh_view.source_user_request == view.source_user_request


@pytest.mark.asyncio
async def test_source_at_transport_capacity_omits_request_without_losing_other_evidence(source, monkeypatch):
    from labbioagentos.skills import models as skill_models, service as skill_service
    app, run_id, _, _ = source
    first = _FixtureCurator()
    await local_gold.propose_from_completed_run(*source, first)
    bound = len(first.source.model_dump_json().encode("utf-8")) + 16
    monkeypatch.setattr(skill_models, "MAX_CURATION_SOURCE_BYTES", bound)
    monkeypatch.setattr(skill_service, "MAX_CURATION_SOURCE_BYTES", bound)
    rec = app.run_state_store.get(run_id)
    rec.task_text = "An otherwise legal request with the same unmodified data. " * 500
    app.run_state_store.get = lambda _: rec
    second = _FixtureCurator()
    proposal = await local_gold.propose_from_completed_run(*source, second)
    assert second.source.source_user_request.status == "LENGTH_LIMIT"
    assert second.source.source_user_request.text is None
    assert second.source.execution_refs == first.source.execution_refs
    assert len(second.source.model_dump_json().encode("utf-8")) <= bound
    frozen = app.configuration.skill_service.store.get_source_bundle(proposal.source_bundle_id)
    assert frozen.source_user_request == second.source.source_user_request


def test_legacy_bundle_has_no_inferred_request_and_preserves_real_reference():
    from labbioagentos.skills.models import SkillSourceBundle
    from labbioagentos.skills.source import SkillSourceProjector
    old = SkillSourceBundle.model_validate_json(json.dumps({
        "source_run_id": str(uuid4()), "final_status": "COMPLETED", "workflow_stage_path": [],
        "trace_event_ids": [], "task_reference": "a-stable-external-task-reference",
    }))
    view = SkillSourceProjector.guidance_view(SkillSourceProjector.curation_view(old))
    assert view["source_user_request"]["status"] == "NOT_RECORDED"
    assert view["task_reference"] == "a-stable-external-task-reference"


def test_repair_lineage_survives_complementary_planned_events():
    from labbioagentos.skills.source import SkillSourceProjector
    recorder = RunTraceRecorder(InMemoryTraceSink())
    run_id, old, revised, unrelated = uuid4(), uuid4(), uuid4(), uuid4()
    for identity, parent in [(old, None), (revised, old), (unrelated, None)]:
        recorder.emit(run_id, TraceEventType.EXECUTION_PLANNED, status="SUBMITTED",
                      payload={"execution_id": str(identity), "revision_of": str(parent) if parent else None})
        recorder.emit(run_id, TraceEventType.EXECUTION_PLANNED, status="PLANNED",
                      payload={"execution_id": str(identity), "script_hash": str(identity)})
        failed = identity == old
        recorder.emit(run_id, TraceEventType.EXECUTION_FAILED if failed else TraceEventType.EXECUTION_COMPLETED,
                      status="FAILED" if failed else "SUCCEEDED",
                      payload={"execution_id": str(identity), "exit_code": int(failed)})
    refs = SkillSourceProjector._execution_refs(recorder.events(run_id))
    assert [(r.execution_id, r.revision_of, r.exit_code) for r in refs] == [
        (old, None, 1), (revised, old, 0), (unrelated, None, 0)]
