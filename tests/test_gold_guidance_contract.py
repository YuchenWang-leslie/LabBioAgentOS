"""Gold retains Agent-authored reference guidance without becoming a workflow."""

import json
from types import MethodType, SimpleNamespace
from uuid import uuid4

import pytest
from pantheon.agent import Agent
from pydantic import ValidationError

from labbioagentos.contracts import InformationAuthority, RunStatus, WorkflowStage
from labbioagentos.skills.curator import (
    PantheonAuditedAdaptiveSkillCurator,
    SKILL_CURATOR_ADAPTIVE_INSTRUCTIONS,
    SKILL_CURATOR_AUDIT_INSTRUCTIONS,
    SKILL_CURATOR_REVISION_INSTRUCTIONS,
)
from labbioagentos.skills.models import (
    SkillAdaptiveCuratorDraft,
    SkillAdaptiveProcedureDraft,
    SkillCurationSourceView,
    SkillCuratorAudit,
    SkillCuratorDraft,
    SkillSourceBundle,
)
from test_gold_evidence_audit import checked_audit
from labbioagentos.skills.source import SkillSourceProjector


def _adaptive_payload():
    return {
        "proposed_name": "Document conversion reference",
        "description": "Source-grounded guidance for compatible document tasks.",
        "procedure": {
            "applicability": "When the current task has compatible document inputs.",
            "workflow_guidance": [
                "The source plan proposed inspecting the document then converting it; "
                "use this as a reference sequence only if current requirements fit."
            ],
            "reusable_principles": ["Verify the current output before delivery."],
            "adaptation_points": [{
                "decision": "Choose the current conversion approach.",
                "evidence_requirements": ["Current document format and objective."],
                "selection_considerations": ["Compatibility with required output."],
                "revalidation_requirements": ["Confirm the produced format."],
            }],
            "tags": ["document", "conversion"],
            "artifact_types": ["document"],
        },
    }


def _source_payload():
    return {
        "source_bundle_id": str(uuid4()),
        "source_run_id": str(uuid4()),
        "final_status": "COMPLETED",
        "workflow_stage_path": ["PLAN", "EXECUTE", "VALIDATE"],
    }


def _stage_payload(**updates):
    payload = {
        "result_id": str(uuid4()),
        "invocation_id": str(uuid4()),
        "stage_id": "PLAN",
        "model_summary": "The Agent proposed a document conversion approach.",
        "model_body": {"steps": ["Inspect document", "Convert", "Check output"]},
    }
    payload.update(updates)
    return payload


def _parse(model, payload):
    return model.model_validate_json(json.dumps(payload))


def test_legacy_adaptive_draft_and_source_view_remain_readable():
    draft = _parse(SkillAdaptiveCuratorDraft, _adaptive_payload())
    source = _parse(SkillCurationSourceView, _source_payload())
    assert source.final_status is RunStatus.COMPLETED
    assert source.stage_context == ()
    for field in (
        "agent_collaboration_guidance", "execution_guidance", "parameter_guidance",
        "debug_lessons",
    ):
        assert getattr(draft.procedure, field) == ()
        assert getattr(draft.to_curator_draft().procedure, field) == ()

    bundle = SkillSourceBundle(
        source_run_id=source.source_run_id,
        final_status=source.final_status,
        workflow_stage_path=source.workflow_stage_path,
        trace_event_ids=(),
    )
    assert bundle.stage_context == ()
    assert bundle.artifact_evidence_views == ()
    assert SkillSourceBundle.model_validate_json(bundle.model_dump_json()) == bundle


def test_adaptive_reference_guidance_survives_durable_conversion_verbatim():
    payload = _adaptive_payload()
    guidance = {
        "agent_collaboration_guidance": ["The source Agent requested an output check."],
        "execution_guidance": [
            "The source plan named converter A; recheck its suitability before reuse."
        ],
        "parameter_guidance": [
            "The source plan used option X under condition Y, not an unconditional default."
        ],
        "debug_lessons": ["No cause of failure was established in this source."],
    }
    payload["procedure"].update(guidance)
    adaptive = _parse(SkillAdaptiveCuratorDraft, payload)
    durable = adaptive.to_curator_draft()
    restored = SkillCuratorDraft.model_validate_json(durable.model_dump_json())
    for field, values in guidance.items():
        assert getattr(restored.procedure, field) == tuple(values)
    assert restored.procedure.workflow_outline == adaptive.procedure.workflow_guidance
    assert restored.procedure.adaptation_points[0].modifiable is True
    assert restored.procedure.tags == frozenset({"document", "conversion"})
    assert not hasattr(restored.procedure, "execute")


def test_stage_context_remains_model_context_not_execution_evidence():
    payload = _source_payload()
    payload["stage_context"] = [_stage_payload()]
    source = _parse(SkillCurationSourceView, payload)
    context = source.stage_context[0]
    assert context.authority is InformationAuthority.MODEL_CONTEXT
    assert context.stage_id is WorkflowStage.PLAN
    assert source.execution_refs == ()
    assert source.artifact_evidence_views == ()
    assert SkillCurationSourceView.model_validate_json(source.model_dump_json()) == source
    payload["stage_context"][0]["authority"] = "ARTIFACT_EVIDENCE"
    with pytest.raises(ValidationError):
        _parse(SkillCurationSourceView, payload)


@pytest.mark.parametrize("body", [
    {"script_body": "private code"},
    {"provider_response_body": "private transport"},
    {"steps": ["Read /media/private/input"]},
    {"steps": ["Bearer PRIVATE_SENTINEL_12345678"]},
    {"steps": ["reasoning_content is private"]},
    {"steps": ["/private/input"]},
    {"steps": ["x" * 8001]},
    {"steps": ["x" * 8000] * 9},
    {"steps": ["check"] * 257},
    ["not an object"],
])
def test_stage_context_rejects_unsafe_or_unbounded_material(body):
    payload = _source_payload()
    payload["stage_context"] = [_stage_payload(model_body=body)]
    with pytest.raises(ValidationError):
        _parse(SkillCurationSourceView, payload)


def test_stage_context_count_is_bounded():
    payload = _source_payload()
    payload["stage_context"] = [_stage_payload() for _ in range(65)]
    with pytest.raises(ValidationError):
        _parse(SkillCurationSourceView, payload)


@pytest.mark.parametrize("summary", [
    "/private/source", "Use Bearer PRIVATE_SENTINEL_12345678", "x" * 8001,
])
def test_stage_context_summary_is_also_bounded_and_safe(summary):
    payload = _source_payload()
    payload["stage_context"] = [_stage_payload(model_summary=summary)]
    with pytest.raises(ValidationError):
        _parse(SkillCurationSourceView, payload)


def test_adaptive_guidance_is_optional_in_model_facing_schema():
    schema = SkillAdaptiveProcedureDraft.model_json_schema()
    for field in (
        "agent_collaboration_guidance", "execution_guidance", "parameter_guidance",
        "debug_lessons",
    ):
        assert field in schema["properties"]
        assert field not in schema["required"]


@pytest.mark.asyncio
async def test_audited_boundary_preserves_agent_reference_steps_and_source_authority():
    source_payload = _source_payload()
    source_payload["stage_context"] = [_stage_payload()]
    source_payload["artifact_evidence_views"] = [{
        "artifact_id": str(uuid4()), "artifact_type": "JSON_RECORDS",
        "exposure_class": "DERIVED", "release_basis": "TRUSTED_EXECUTION_DECLASSIFICATION",
        "view_type": "TOP_N", "returned_count": 1, "available_count": 1,
        "effective_limit": 1, "records": [{"metric": "fixture_metric", "value": 7}],
        "provenance": {"owner_user_id": "fixture", "project_id": "p", "lab_id": "lab"},
    }]
    source = _parse(SkillCurationSourceView, source_payload)
    draft_payload = _adaptive_payload()
    draft_payload["procedure"]["execution_guidance"] = [
        "The source plan proposed converter A; this is not proof it was executed."
    ]
    draft = _parse(SkillAdaptiveCuratorDraft, draft_payload)
    audit = checked_audit(draft)
    captured = {}

    def agent(name, instructions, response_format, output):
        instance = Agent(
            name=name,
            instructions=instructions,
            model="openai/mock",
            response_format=response_format,
            use_memory=False,
        )

        async def run(_self, message, **_kwargs):
            captured[name] = json.loads(message)
            return SimpleNamespace(content=output)

        instance.run = MethodType(run, instance)
        return instance

    result = await PantheonAuditedAdaptiveSkillCurator(
        agent("draft", SKILL_CURATOR_ADAPTIVE_INSTRUCTIONS,
              SkillAdaptiveCuratorDraft, draft),
        agent("audit", SKILL_CURATOR_AUDIT_INSTRUCTIONS, SkillCuratorAudit, audit),
        agent("revision", SKILL_CURATOR_REVISION_INSTRUCTIONS,
              SkillAdaptiveCuratorDraft, draft),
    ).propose(source)

    assert result.procedure == draft.to_curator_draft().procedure
    assert result.review_notes == (audit.summary,)
    assert captured["draft"] == SkillSourceProjector.guidance_view(source)
    assert captured["audit"]["reference_material"] == SkillSourceProjector.review_view(source)
    assert captured["audit"]["reference_material"]["reference_plans"][0]["authority"] == "MODEL_CONTEXT"
    assert "fixture_metric" not in json.dumps(captured["draft"])
    assert captured["audit"]["reference_material"]["artifact_evidence_views"][0]["records"][0]["value"] == 7
    assert "revision" not in captured  # acceptable guidance needs no forced rewrite
    assert result.procedure.workflow_outline == draft.procedure.workflow_guidance
    assert result.procedure.execution_guidance == draft.procedure.execution_guidance
