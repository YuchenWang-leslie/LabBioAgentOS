"""Curation keeps execution facts and requires evidence-backed Agent audits."""

import json
from types import MethodType, SimpleNamespace
from uuid import uuid4

import pytest
from pantheon.agent import Agent

from labbioagentos.contracts import RunStatus
from labbioagentos.skills.curator import PantheonAuditedAdaptiveSkillCurator, SkillCuratorError
from labbioagentos.skills.models import (
    SkillAdaptiveCuratorDraft, SkillCurationSourceView, SkillCuratorAudit,
)
from labbioagentos.skills.source import SkillSourceProjector
from labbioagentos.trace import TraceEventType


def _event(kind, execution_id, sequence, **payload):
    return SimpleNamespace(event_type=kind, event_id=uuid4(), sequence=sequence,
                           status="FAILED" if kind is TraceEventType.EXECUTION_FAILED
                           else "SUCCEEDED", payload={"execution_id": str(execution_id), **payload})


def test_complementary_terminal_events_preserve_exit_code_and_safe_failure_facts():
    first, second = uuid4(), uuid4()
    events = (
        _event(TraceEventType.EXECUTION_FAILED, first, 1, exit_code=0,
               error_class="OUTPUT_CONTRACT_FAILURE", error_message="Bearer PRIVATE_SENTINEL_12345"),
        _event(TraceEventType.EXECUTION_FAILED, first, 2,
               issue_codes=["OUTPUT_CONTRACT_FAILURE"],
               issue_detail_codes=["INVALID_DOCUMENT", "QUERYABLE_OUTPUT_REQUIRED"]),
        _event(TraceEventType.EXECUTION_COMPLETED, second, 3, exit_code=0),
        _event(TraceEventType.EXECUTION_COMPLETED, second, 4, output_artifact_ids=[str(uuid4())]),
    )
    refs = SkillSourceProjector._execution_refs(events)
    assert [r.execution_id for r in refs] == [first, second]
    assert [r.exit_code for r in refs] == [0, 0]
    assert refs[0].status == "FAILED"  # exit zero is not contract success
    assert refs[0].issue_detail_codes == ("INVALID_DOCUMENT", "QUERYABLE_OUTPUT_REQUIRED")
    assert refs[0].terminal_event_ids == (events[0].event_id, events[1].event_id)
    assert "PRIVATE_SENTINEL" not in refs[0].model_dump_json()


def test_legacy_unknown_execution_is_not_inferred_as_success():
    ref, = SkillSourceProjector._execution_refs((
        _event(TraceEventType.EXECUTION_FAILED, uuid4(), 1),
    ))
    assert ref.exit_code is None
    assert ref.issue_codes == ()


def _draft():
    return SkillAdaptiveCuratorDraft.model_validate_json(json.dumps({
        "proposed_name": "Document summary reference",
        "description": "Conditional source reference, not a fixed method.",
        "procedure": {"applicability": "Compatible document summary tasks.",
                      "workflow_guidance": ["Check current output."],
                      "reusable_principles": ["Preserve original inputs."],
                      "adaptation_points": [{"decision": "Choose current output format.",
                          "evidence_requirements": ["Current requested format."],
                          "selection_considerations": ["Current input structure."],
                          "revalidation_requirements": ["Check delivered output."]}]},
    }))


def checked_audit(draft, **updates):
    values = draft.model_dump(mode="json")
    fields = ["proposed_name", "description"] + [
        "procedure." + k for k, v in values["procedure"].items() if v
    ]
    return SkillCuratorAudit.model_validate_json(json.dumps({
        "findings": [], "summary": "Synthetic field-by-field review.",
        "checks": [{"draft_field": f, "evidence_ids": ["E003"],
                    "rationale": "Fixture checks structural citation plumbing, not science."}
                   for f in fields], **updates,
    }))


def _agent(schema, outputs, captured):
    agent = Agent(name="fixture", instructions="Synthetic protocol fixture.",
                  model="openai/mock", response_format=schema, use_memory=False)
    async def run(_self, message, **kwargs):
        captured.append(json.loads(message))
        return SimpleNamespace(content=outputs.pop(0))
    agent.run = MethodType(run, agent)
    return agent


async def _curate(audits, observed=None):
    draft, source = _draft(), SkillCurationSourceView(
        source_bundle_id=uuid4(), source_run_id=uuid4(), final_status=RunStatus.COMPLETED,
        workflow_stage_path=(),
    )
    captured = []
    curator = PantheonAuditedAdaptiveSkillCurator(
        _agent(SkillAdaptiveCuratorDraft, [draft], captured),
        _agent(SkillCuratorAudit, audits, captured),
        _agent(SkillAdaptiveCuratorDraft, [draft], captured),
        boundary_observer=lambda k, v: observed.append(k) if observed is not None else None,
    )
    return await curator.propose(source), captured


@pytest.mark.asyncio
async def test_advisory_review_accepts_without_exhaustive_field_checks_or_revision():
    result, captured = await _curate([checked_audit(_draft(), checks=[])])
    assert result.procedure == _draft().to_curator_draft().procedure
    assert result.review_notes == (checked_audit(_draft()).summary,)
    assert len(captured) == 2  # author and reviewer; no unnecessary rewrite


@pytest.mark.asyncio
async def test_malformed_review_still_cannot_pass():
    with pytest.raises(SkillCuratorError):
        await _curate([{"findings": "not a list", "summary": "Fixture"}])


@pytest.mark.asyncio
async def test_final_revision_is_audited_before_candidate_can_leave_boundary():
    audit = checked_audit(_draft())
    initial = checked_audit(_draft(), findings=[{
        "category": "UNSUPPORTED_BY_SOURCE", "draft_field": "description",
        "statement": "Fixture issue.", "rationale": "Requires Agent correction.",
    }])
    observed = []
    result, captured = await _curate([initial, audit], observed)
    assert result.procedure == _draft().to_curator_draft().procedure
    assert observed[-2:] == ["curator_final_audit", "curator_guidance_ready_for_user_review"]
    assert captured[-1]["draft"] == _draft().model_dump(mode="json")


@pytest.mark.asyncio
async def test_final_opinion_is_visible_advice_not_an_automatic_veto():
    audit = checked_audit(_draft(), findings=[{
        "category": "OVERSTATED_FAILURE_CAUSE", "draft_field": "description",
        "statement": "Unproven failure cause.", "rationale": "No causal evidence in source.",
    }])
    observed = []
    result, captured = await _curate([audit, audit], observed)
    assert result.review_notes == (audit.summary, audit.findings[0].statement,
                                   audit.findings[0].rationale)
    assert result.procedure == _draft().to_curator_draft().procedure
    assert len(captured) == 4
    assert observed.count("curator_revised_adaptive_draft") == 1
    assert observed[-1] == "curator_guidance_ready_for_user_review"


@pytest.mark.asyncio
async def test_partial_optional_checks_are_not_an_advisory_approval_gate():
    audit = checked_audit(_draft())
    partial = audit.model_copy(update={"checks": audit.checks[:1]})
    result, _ = await _curate([partial])
    assert result.procedure == _draft().to_curator_draft().procedure


def test_diagnostics_are_typed_and_unsafe_extra_fields_rejected():
    from pydantic import ValidationError
    event = _event(TraceEventType.EXECUTION_FAILED, uuid4(), 1, diagnostics=[{
        "code": "PYTHON_EXCEPTION", "exception_type": "ImportError", "script_line_numbers": [12],
    }])
    ref, = SkillSourceProjector._execution_refs((event,))
    assert ref.diagnostics[0].exception_type == "ImportError"
    event.payload["diagnostics"][0]["stderr"] = "private process output"
    with pytest.raises(ValidationError):
        SkillSourceProjector._execution_refs((event,))


def test_guidance_context_omits_result_previews_without_altering_source_evidence():
    from labbioagentos.skills.models import SkillSourceBundle, SkillStageContext
    from labbioagentos.contracts import WorkflowStage
    from labbioagentos.artifacts import ArtifactView
    artifact_id = str(uuid4())
    common = {"artifact_id": artifact_id, "artifact_type": "JSON_RECORDS",
              "exposure_class": "DERIVED", "release_basis": "TRUSTED_EXECUTION_DECLASSIFICATION",
              "returned_count": 0, "available_count": 0,
              "provenance": {"owner_user_id": "fixture", "project_id": "p", "lab_id": "lab"}}
    views = tuple(ArtifactView.model_validate_json(json.dumps(common | body)) for body in (
        {"view_type": "SUMMARY"},
        {"view_type": "TOP_N", "available_count": 39, "returned_count": 10,
         "effective_limit": 10, "truncated": True,
         "records": [{"metric": "synthetic", "value": i} for i in range(10)]},
    ))
    bundle = SkillSourceBundle(source_run_id=uuid4(), final_status=RunStatus.COMPLETED,
        workflow_stage_path=(), trace_event_ids=(), artifact_evidence_views=views,
        stage_context=(SkillStageContext(result_id=uuid4(), stage_id=WorkflowStage.VALIDATE,
            model_summary="Fixture Agent incorrectly claims the artifact is empty.", model_body={}),))
    source = SkillSourceProjector.curation_view(bundle)
    context = SkillSourceProjector.guidance_view(source)
    assert "stage_context" not in context
    assert source.stage_context[0].authority.value == "MODEL_CONTEXT"
    assert "artifact_evidence_views" not in context
    assert source.artifact_evidence_views == views
    assert source.artifact_evidence_views[1].available_count == 39
    assert source.artifact_evidence_views[1].truncated is True


def test_source_boundary_excludes_curation_queries_without_deleting_history():
    from labbioagentos.trace import InMemoryTraceSink, RunTraceRecorder
    from labbioagentos.skills.source import SkillSourceProjectionError
    recorder, run = RunTraceRecorder(InMemoryTraceSink()), uuid4()
    for kind in (TraceEventType.RUN_CREATED, TraceEventType.RUN_STARTED, TraceEventType.RUN_COMPLETED):
        recorder.emit(run, kind, status="COMPLETED" if kind is TraceEventType.RUN_COMPLETED else "RUNNING")
    expected = SkillSourceProjector().project(recorder.events(run), run_id=run)
    for _ in range(520):
        recorder.emit(run, TraceEventType.ARTIFACT_VIEW_REQUESTED, payload={"artifact_id": str(uuid4())})
    projected = SkillSourceProjector().project(recorder.events(run), run_id=run)
    assert projected.trace_event_ids == expected.trace_event_ids
    assert projected.artifact_ids == expected.artifact_ids
    assert len(recorder.events(run)) == 523
    recorder.emit(run, TraceEventType.RUN_FAILED, status="FAILED")
    with pytest.raises(SkillSourceProjectionError):
        SkillSourceProjector().project(recorder.events(run), run_id=run)
