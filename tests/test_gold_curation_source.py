"""Curation receives historical Agent plans, never host-authored science or RAW."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from labbioagentos import InformationAuthority, NextActionProposal, RunStatus, WorkflowStage
from labbioagentos.local_gold_source import stage_context_from_results
from labbioagentos.runtime.contracts import PlanStageBody, RuntimeStageResult
from labbioagentos.skills.models import SkillSourceBundle
from labbioagentos.skills.source import SkillSourceProjector
from labbioagentos.trace import TraceEventType


def _plan(text="Compare the supplied record schema before selecting a procedure."):
    return RuntimeStageResult(
        stage_id=WorkflowStage.PLAN, summary="Agent-authored fixture plan.",
        body=PlanStageBody(procedure_steps=(text,),
                           validation_expectations=("Check the declared output contract.",)),
        next_action=NextActionProposal(action="transition", target_stage="PREFLIGHT"),
    )


def _event(result, run_id, invocation_id):
    return SimpleNamespace(
        event_type=TraceEventType.RESULT_RECORDED, run_id=run_id,
        stage_id=result.stage_id,
        payload={"result": {"payload": {"runtime_result_id": str(result.result_id),
                                         "invocation_id": str(invocation_id)}}},
    )


def test_exact_agent_plan_survives_with_model_context_authority():
    result, run_id, invocation_id = _plan(), uuid4(), uuid4()
    context = stage_context_from_results(
        (result,), (_event(result, run_id, invocation_id),), run_id,
    )
    assert len(context) == 1
    assert context[0].result_id == result.result_id
    assert context[0].invocation_id == invocation_id
    assert context[0].authority is InformationAuthority.MODEL_CONTEXT
    assert context[0].model_body["procedure_steps"] == list(result.body.procedure_steps)
    assert context[0].model_body["validation_expectations"] == list(
        result.body.validation_expectations
    )
    bundle = SkillSourceBundle(
        source_run_id=run_id, final_status=RunStatus.COMPLETED, workflow_stage_path=(),
        stage_context=context, trace_event_ids=(),
    )
    restored = SkillSourceBundle.model_validate_json(bundle.model_dump_json())
    assert SkillSourceProjector.curation_view(restored).stage_context == context


@pytest.mark.parametrize("text", [
    "Read /media/private/other-user", "provider_response_body", "Bearer secret-token-value",
])
def test_unsafe_plan_is_rejected_not_redacted_into_new_guidance(text):
    result, run_id = _plan(text), uuid4()
    with pytest.raises(ValueError):
        stage_context_from_results((result,), (_event(result, run_id, uuid4()),), run_id)


def test_source_cannot_borrow_a_foreign_run_result_identity():
    result, run_id = _plan(), uuid4()
    with pytest.raises(ValueError):
        stage_context_from_results((result,), (_event(result, uuid4(), uuid4()),), run_id)


def test_missing_persistent_invocation_identity_is_not_invented():
    with pytest.raises(ValueError):
        stage_context_from_results((_plan(),), (), uuid4())


def test_legacy_source_without_plan_context_is_compatible():
    bundle = SkillSourceBundle.model_validate_json(
        '{"source_run_id":"00000000-0000-0000-0000-000000000001",'
        '"final_status":"COMPLETED","workflow_stage_path":[],"trace_event_ids":[]}'
    )
    assert SkillSourceProjector.curation_view(bundle).stage_context == ()


def test_legal_long_runtime_summary_is_preserved_without_a_smaller_new_limit():
    result = _plan().model_copy(update={"summary": "x" * 8000})
    run_id = uuid4()
    context = stage_context_from_results(
        (result,), (_event(result, run_id, uuid4()),), run_id,
    )
    assert context[0].model_summary == result.summary


def test_repeated_result_uuid_keeps_distinct_ordered_invocation_lineage():
    first, run_id, invocation1, invocation2 = _plan(), uuid4(), uuid4(), uuid4()
    second = first.model_copy(update={"summary": "A revised Agent fixture plan."})
    context = stage_context_from_results((first, second), (
        _event(first, run_id, invocation1), _event(second, run_id, invocation2),
    ), run_id)
    assert [item.invocation_id for item in context] == [invocation1, invocation2]
    assert context[0].result_id == context[1].result_id == first.result_id
    assert context[1].model_summary == second.summary


def test_reordered_results_are_not_matched_to_the_wrong_invocation():
    first, second, run_id = _plan(), _plan(), uuid4()
    with pytest.raises(ValueError):
        stage_context_from_results((second, first), (
            _event(first, run_id, uuid4()), _event(second, run_id, uuid4()),
        ), run_id)
