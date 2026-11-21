"""A rejected scoped citation identifies its first bad submitted member only."""

from dataclasses import replace
import json
from time import monotonic
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from labbioagentos import (
    ArtifactExposureClass, ArtifactExposureDenied, ArtifactReleaseBasis,
    ArtifactRepresentation, LabBioRuntimeToolSet, TraceEventType, WorkflowStage,
)
from labbioagentos.runtime.contracts import ReportEvidenceItem
from labbioagentos.runtime.observations import ObservationStore
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401
from test_execution_inspection import _submit, inspection  # noqa: F401
from test_report_scoped_evidence import report_tools
from test_runtime_milestone_b import _toolset, boundary  # noqa: F401


async def citation_pair(tmp_path, boundary, bad_kind):
    tools, ref, registered = report_tools(boundary)
    tools = LabBioRuntimeToolSet(tools.binding, replace(tools.services,
        observation_store=ObservationStore(tmp_path / "observations")))
    aggregate = await tools.artifact_aggregate(str(ref.artifact_id), "SUM", "row", "group", 1)
    tools.mark_feedback_delivered((aggregate,))
    good_id = aggregate["capability_invocation_id"]
    if bad_kind == "receiptless":
        bad = await tools.report_check(evidence_references=[
            {"artifact_id": str(ref.artifact_id), "observation_ids": [good_id]}])
        tools.mark_feedback_delivered((bad,))
        bad_id = bad["capability_invocation_id"]
    elif bad_kind == "other_source":
        bad = await tools.artifact_query(str(boundary[2].artifact_id), "TOP_N", 1)
        tools.mark_feedback_delivered((bad,))
        bad_id = bad["capability_invocation_id"]
    elif bad_kind == "duplicate":
        bad_id = good_id
    else:
        bad_id = str(uuid4())
    selected = [{"artifact_id": str(ref.artifact_id), "observation_ids": [good_id, bad_id]}]
    return tools, ref, registered, aggregate, selected


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_kind, status", [
    ("receiptless", "REPORT_OBSERVATION_SOURCE_MISMATCH"),
    ("other_source", "REPORT_OBSERVATION_SOURCE_MISMATCH"),
    ("not_delivered", "REPORT_OBSERVATION_NOT_DELIVERED"),
    ("duplicate", "DUPLICATE_REFERENCE"),
])
async def test_first_bad_member_path_survives_error_trace_and_immutable_observation(
    tmp_path, artifact_query_boundary, bad_kind, status,
):
    tools, _ref, registered, aggregate, selected = await citation_pair(
        tmp_path, artifact_query_boundary, bad_kind)
    original = tools.services.observation_store.load(tools.binding,
        UUID(aggregate["capability_invocation_id"]))
    checked = await tools.report_check(evidence_references=selected)
    assert checked["data"]["items"][0]["status"] == status
    assert checked["data"]["items"][0].get("failed_observation_index") == 1
    failed = await tools.report_submit("Synthetic", "Synthetic.", evidence_references=selected)
    assert failed["error"]["error_code"] == status and not registered
    diagnostic = failed["error"]["report_evidence"]
    assert diagnostic["items"][0]["failed_observation_index"] == 1
    assert diagnostic["items"][0]["read_receipts"] == []
    assert failed["data"]["report_reference_selections"] == [{"index": 0, **selected[0]}]
    if bad_kind == "other_source":
        assert str(artifact_query_boundary[2].artifact_id) not in json.dumps(failed)
    item = tools.evidence_items()[-1]
    stored = tools.services.observation_store.load(tools.binding, item.capability_invocation_id)
    assert stored.item == item and stored.public_response == failed
    terminal = artifact_query_boundary[0].read(tools.binding.run_id)[-1]
    assert terminal.event_type is TraceEventType.CAPABILITY_FAILED
    assert terminal.payload["error_details"]["report_evidence"] == diagnostic
    assert stored.item.error_details.report_evidence.items[0].failed_observation_index == 1
    assert tools.services.observation_store.load(tools.binding,
        original.item.capability_invocation_id) == original
    # Removing a bad selection in this fixture neither grants new reading credit
    # nor makes the untouched original observation a different source.
    valid = [{**selected[0], "observation_ids": selected[0]["observation_ids"][:1]}]
    accepted = await tools.report_submit("Synthetic", "Only the inspected scope.", evidence_references=valid)
    assert accepted["success"] and len(registered) == 1
    assert registered[0]["evidence_read_receipts"] == (original.item.artifact_read_receipt,)


@pytest.mark.asyncio
async def test_first_missing_member_is_not_replaced_by_later_diagnosis(tmp_path, artifact_query_boundary):
    tools, _ref, _registered, _aggregate, selected = await citation_pair(
        tmp_path, artifact_query_boundary, "not_delivered")
    selected[0]["observation_ids"].insert(0, str(uuid4()))
    result = await tools.report_check(evidence_references=selected)
    assert result["data"]["items"][0]["status"] == "REPORT_OBSERVATION_NOT_DELIVERED"
    assert result["data"]["items"][0].get("failed_observation_index") == 0


@pytest.mark.asyncio
async def test_sum_then_actual_execution_inspect_is_not_an_artifact_read_receipt(inspection, boundary):
    receipt = await _submit(inspection, boundary)
    service, _executor, run_id = inspection
    tools = _toolset(boundary, WorkflowStage.REPORT,
        ("artifact_aggregate", "execution_inspect", "report_submit"),
        execution_submission=service,
        report_submission=SimpleNamespace(submit=lambda **_kwargs: pytest.fail("Bad citation was accepted")))
    tools = LabBioRuntimeToolSet(replace(tools.binding, run_id=run_id), tools.services)
    ref = tools.services.artifact_store.register(artifact_type="synthetic-table",
        exposure_class=ArtifactExposureClass.DERIVED,
        release_basis=ArtifactReleaseBasis.TRUSTED_EXECUTION_DECLASSIFICATION,
        representation=ArtifactRepresentation(records=({"value": 21},), record_count=1),
        owner_user_id=tools.binding.principal.user_id,
        project_id=tools.binding.workspace.project_id, lab_id=tools.binding.workspace.lab_id,
        run_id=run_id)
    total = await tools.artifact_aggregate(str(ref.artifact_id), "SUM", "value")
    inspected = await tools.execution_inspect(str(receipt.execution_id))
    tools.mark_feedback_delivered((total, inspected))
    assert inspected["data"]["receipt"]["execution_id"] == str(receipt.execution_id)
    assert inspected["artifact_read_receipt"] is None
    failed = await tools.report_submit("Synthetic", "Synthetic.", evidence_references=[
        {"artifact_id": str(ref.artifact_id), "observation_ids": [
            total["capability_invocation_id"], inspected["capability_invocation_id"]]}])
    assert failed["error"]["error_code"] == "REPORT_OBSERVATION_SOURCE_MISMATCH"
    assert failed["error"]["report_evidence"]["items"][0]["failed_observation_index"] == 1


@pytest.mark.asyncio
async def test_last_member_at_shared_64_observation_limit_is_located(tmp_path, artifact_query_boundary):
    tools, ref, registered = report_tools(artifact_query_boundary)
    tools = LabBioRuntimeToolSet(tools.binding, replace(tools.services,
        observation_store=ObservationStore(tmp_path / "observations")))
    ids = []
    for offset in range(63):
        page = await tools.artifact_query(str(ref.artifact_id), "TOP_N", 1, offset=offset)
        tools.mark_feedback_delivered((page,))
        ids.append(page["capability_invocation_id"])
    ids.append(str(uuid4()))
    failed = await tools.report_submit("Synthetic", "Synthetic.", evidence_references=[
        {"artifact_id": str(ref.artifact_id), "observation_ids": ids}])
    assert failed["error"]["report_evidence"]["items"][0]["failed_observation_index"] == 63
    assert len(json.dumps(failed, separators=(",", ":")).encode()) < 60_000
    item = tools.evidence_items()[-1]
    assert tools.services.observation_store.load(tools.binding, item.capability_invocation_id).public_response == failed
    assert not registered


@pytest.mark.asyncio
async def test_changed_source_identifies_member_without_echoing_source(tmp_path, artifact_query_boundary, monkeypatch):
    tools, _ref, registered, _aggregate, selected = await citation_pair(
        tmp_path, artifact_query_boundary, "duplicate")

    def changed(_item):
        raise ValueError("PRIVATE_CHANGED_SOURCE_HASH")

    monkeypatch.setattr(tools, "_validate_report_observation", changed)
    failed = await tools.report_submit("Synthetic", "Synthetic.", evidence_references=selected)
    assert failed["error"]["error_code"] == "REPORT_OBSERVATION_SOURCE_CHANGED"
    assert failed["error"]["report_evidence"]["items"][0].get("failed_observation_index") == 0
    assert "PRIVATE_CHANGED_SOURCE_HASH" not in json.dumps(failed) and not registered


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke_after_artifact_check", [False, True])
async def test_authorization_precedes_member_feedback_and_reveals_no_extra_identity(
    tmp_path, artifact_query_boundary, monkeypatch, revoke_after_artifact_check,
):
    tools, _ref, registered, _aggregate, selected = await citation_pair(
        tmp_path, artifact_query_boundary, "duplicate")

    def denied(*_args):
        raise ArtifactExposureDenied("PRIVATE_REVOKED_SCOPE")

    if revoke_after_artifact_check:
        monkeypatch.setattr(tools, "_validate_report_observation", denied)
    else:
        from labbioagentos.governance import AuthorizationDenied

        def denied_artifact(*_args):
            raise AuthorizationDenied("PRIVATE_FOREIGN_ID")

        monkeypatch.setattr(tools, "_authorized_report_source", denied_artifact)
    failed = await tools.report_submit("Synthetic", "Synthetic.", evidence_references=selected)
    item = failed["error"]["report_evidence"]["items"][0]
    assert item["status"] == "AUTHORIZATION_DENIED"
    assert "failed_observation_index" not in item and not item["read_receipts"]
    assert "PRIVATE_" not in json.dumps(failed) and not registered


def test_member_index_is_bounded_and_absence_preserves_legacy_serialization():
    from pydantic import ValidationError

    fields = dict(index=0, status="REPORT_OBSERVATION_SOURCE_MISMATCH")
    old = ReportEvidenceItem(**fields)
    assert "failed_observation_index" not in old.model_dump(mode="json")
    assert ReportEvidenceItem(**fields, failed_observation_index=63).model_dump(mode="json")[
        "failed_observation_index"] == 63
    for value in (-1, 64, True):
        with pytest.raises(ValidationError):
            ReportEvidenceItem(**fields, failed_observation_index=value)


@pytest.mark.asyncio
async def test_next_native_request_sees_path_before_fixture_changes_selection(tmp_path, artifact_query_boundary):
    """Scripted model-boundary contract test, not a live-model success claim."""
    from labbioagentos.kernel import (
        AgentInvocation, AgentLoop, AgentMode, BudgetLedger, FakeProvider, FinishReason,
        ModelResultProjector, ModelTurn, PublicMessage, ToolCall, ToolDispatcher, ToolResult,
    )
    from labbioagentos.runtime.native_tools import (
        ArtifactAggregateArguments, NativeRuntimeCapabilityHost, build_labbio_tool_registry,
    )
    from native_kernel.test_agent_loop import _binding, _identity, _limits

    tools, _ref, registered, _aggregate, selected = await citation_pair(
        tmp_path, artifact_query_boundary, "not_delivered")
    host = NativeRuntimeCapabilityHost(tools.binding, tools.services)
    identity = _identity()
    host.begin_model_invocation(identity)
    aggregate = await host.invoke("artifact_aggregate", ArtifactAggregateArguments(
        artifact_id=UUID(selected[0]["artifact_id"]), operation="SUM", field="row"))
    host.mark_feedback_delivered((ToolResult(tool_invocation_id=uuid4(), call_id="read",
        name="artifact_aggregate", status="SUCCEEDED", content=aggregate),))
    selected[0]["observation_ids"][0] = aggregate["capability_invocation_id"]
    valid = [{**selected[0], "observation_ids": selected[0]["observation_ids"][:1]}]
    turns = tuple(ModelTurn(tool_calls=(ToolCall(call_id=f"report-{index}", name="report_submit",
        arguments_json=json.dumps({"title": "Synthetic", "report_text": "Synthetic.",
            "evidence_references": refs, "delivery_artifact_ids": []})),), finish_reason=FinishReason.TOOL_CALLS)
        for index, refs in enumerate((selected, valid)))
    provider = FakeProvider(binding=_binding(), turns=(*turns,
        ModelTurn(content="Fixture complete.", finish_reason=FinishReason.STOP)))
    budget = BudgetLedger(_limits())
    loop = AgentLoop(provider=provider, invocation=AgentInvocation(identity=identity,
        agent_id="leader", mode=AgentMode.CAPABILITY, tool_names=("report_submit",)), budget_ledger=budget,
        dispatcher=ToolDispatcher(build_labbio_tool_registry(host), ModelResultProjector(max_bytes=16_000),
            budget_ledger=budget, batch_observer=host.mark_feedback_delivered))
    await loop.run(messages=(PublicMessage(role="USER", content="Synthetic citation path test."),),
        deadline_monotonic=monotonic() + 60)
    observed = json.loads(provider.requests[1].messages[-1].content)["content"]
    assert observed["error"]["report_evidence"]["items"][0].get("failed_observation_index") == 1
    assert observed["data"]["report_reference_selections"] == [{"index": 0, **selected[0]}]
    accepted = json.loads(provider.requests[2].messages[-1].content)["content"]
    assert accepted["success"] and len(registered) == 1
