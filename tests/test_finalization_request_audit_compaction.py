"""Archived request audits must not make a bounded selected handoff unreachable."""

import json
from uuid import uuid4

import pytest

from labbioagentos import WorkflowStage
from labbioagentos.runtime.contracts import (
    ArtifactAggregateRequestAudit, ExecutionAuditWireType, RuntimePriorResultView,
)
from labbioagentos.runtime.handoff import finalization_evidence
from test_finalization_completion_delivery import finalization_case


def audit_heavy_history(stage, evidence):
    audit = ArtifactAggregateRequestAudit(
        artifact_id=uuid4(), operation="COUNT", field="e" * 128,
        field_type=ExecutionAuditWireType.STRING, match_field="m" * 128,
        match_field_type=ExecutionAuditWireType.STRING,
        match_value_type=ExecutionAuditWireType.STRING, selector_resolution="AUTHORIZED_SOURCE",
    )
    item = evidence.items[0]
    evidence = evidence.model_copy(update={"items": (
        item,
        *(item.model_copy(update={"capability_invocation_id": uuid4(),
            "capability_name": "artifact_aggregate", "artifact_aggregate_request": audit,
            "safe_result": {"count": index}}) for index in range(63)),
    )})
    prior = RuntimePriorResultView(
        result_id=uuid4(), stage_id=WorkflowStage.INTAKE, body_kind="INTAKE",
        model_summary="Earlier bounded engineering context, not scientific evidence.",
        model_body={"engineering_segments": ["e" * 7000 for _ in range(4)]},
    )
    return stage.model_copy(update={"prior_results": (prior,)}), evidence


@pytest.mark.asyncio
@pytest.mark.parametrize("prepared", (False, True))
@pytest.mark.parametrize("include_safe_results", (False, True))
async def test_request_audits_only_leave_compact_prepared_working_view(
    tmp_path, prepared, include_safe_results,
):
    stage, evidence, _invoker, _calls = await finalization_case(
        tmp_path, "Synthetic completion.", prepared=prepared,
    )
    _stage, evidence = audit_heavy_history(stage, evidence)
    original = evidence.model_dump_json()
    projected = finalization_evidence(evidence, compact=True, include_safe_results=include_safe_results)
    omitted = prepared and not include_safe_results
    assert ("artifact_aggregate_request" not in projected['items'][1]) is omitted
    assert ("artifact_aggregate_request" in projected['projection']['omitted_item_fields']) is omitted
    assert len(projected['items']) == len(evidence.items)
    assert evidence.model_dump_json() == original
    assert projected['projection']['full_evidence_id'] == str(evidence.evidence_id)


@pytest.mark.asyncio
async def test_legal_request_audits_do_not_prevent_finalization_of_selected_bodies(tmp_path):
    stage, evidence, invoker, calls = await finalization_case(
        tmp_path, "Synthetic completion.", prepared=True,
    )
    stage, evidence = audit_heavy_history(stage, evidence)
    original = evidence.model_dump_json()
    selected = evidence.prepared_handoff.model_dump(mode="json", exclude_none=True)
    status = invoker._handoff_capacity_status(stage, evidence)
    assert status['within_message_limits']
    result = await invoker.finalize_recovered(stage, evidence)
    assert result.stage_id is WorkflowStage.INTAKE
    assert len(calls) == 1 and calls[0].tools == ()
    assert len(calls[0].messages[-1].content.encode()) <= 60_000
    assert json.loads(calls[0].messages[-2].content) == selected
    assert evidence.model_dump_json() == original
