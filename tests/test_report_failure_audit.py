"""Rejected mixed citations must keep truthful feedback and terminal audit."""

import asyncio
from dataclasses import replace
import json
from time import monotonic
from uuid import UUID, uuid4

import pytest

from labbioagentos import LabBioRuntimeToolSet, TraceEventType
from labbioagentos.runtime.observations import ObservationStore
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401
from test_report_scoped_evidence import report_tools


async def mixed_references(tmp_path, boundary):
    tools, ref, registered = report_tools(boundary)
    services = replace(tools.services, observation_store=ObservationStore(tmp_path / "observations"))
    tools = LabBioRuntimeToolSet(tools.binding, services)
    aggregate = await tools.artifact_aggregate(str(ref.artifact_id), "SUM", "row", "group", 1)
    tools.mark_feedback_delivered((aggregate,))
    valid = {"artifact_id": str(ref.artifact_id),
             "observation_ids": [aggregate["capability_invocation_id"]]}
    invalid = {"artifact_id": str(uuid4()), "observation_ids": [str(uuid4())]}
    return tools, registered, aggregate, valid, invalid


@pytest.mark.asyncio
async def test_failed_mixed_aggregate_and_unknown_citation_is_archived_with_terminal_event(
    tmp_path, artifact_query_boundary,
):
    tools, registered, aggregate, valid, invalid = await mixed_references(tmp_path, artifact_query_boundary)
    original = tools.services.observation_store.load(tools.binding, UUID(aggregate["capability_invocation_id"]))
    checked = await tools.report_check(evidence_references=[valid, invalid])
    assert checked["data"]["items"][0]["read_receipts"][0]["scope"]["query"]["operation"] == "SUM"
    response = await tools.report_submit("Synthetic", "Synthetic.", evidence_references=[valid, invalid])
    assert response["success"] is False
    assert response["error"]["error_code"] == "ARTIFACT_NOT_FOUND"
    selections = [{"index": index, **reference} for index, reference in enumerate((valid, invalid))]
    assert response["data"] == {"report_reference_selections": selections}
    diagnostic = response["error"]["report_evidence"]
    assert diagnostic["receipt_details"] == "OMITTED_FROM_FAILURE_DIAGNOSTICS"
    assert [(item["artifact_id"], item["status"]) for item in diagnostic["items"]] == [
        (valid["artifact_id"], "READY"), (invalid["artifact_id"], "ARTIFACT_NOT_FOUND")]
    assert all(item["read_receipts"] == [] for item in diagnostic["items"])
    assert not registered
    item = tools.evidence_items()[-1]
    restored = tools.services.observation_store.load(tools.binding, item.capability_invocation_id)
    assert restored.public_response == response and restored.item == item
    started, terminal = [event for event in artifact_query_boundary[0].read(tools.binding.run_id)
        if event.payload.get("capability_invocation_id") == response["capability_invocation_id"]]
    assert started.event_type is TraceEventType.CAPABILITY_INVOKED
    assert terminal.event_type is TraceEventType.CAPABILITY_FAILED
    assert started.payload["report_reference_selections"] == selections
    assert terminal.payload["report_reference_selections"] == selections
    assert [selection.model_dump(mode="json") for selection in item.report_reference_selections] == selections
    assert terminal.payload["error_details"]["report_evidence"] == diagnostic
    assert tools.services.observation_store.load(tools.binding, original.item.capability_invocation_id) == original
    # Failure diagnostics are not new reading credit. Original successful check
    # and observation retain the complete receipts, untouched.
    rechecked = await tools.report_check(evidence_references=[valid])
    assert rechecked["data"]["ready"]
    assert rechecked["data"]["receipt_details"] == "INCLUDED"
    assert rechecked["data"]["items"][0]["read_receipts"] == checked["data"]["items"][0]["read_receipts"]


@pytest.mark.asyncio
async def test_native_next_turn_receives_mixed_citation_error_and_can_submit_valid_reference(
    tmp_path, artifact_query_boundary,
):
    """Scripted transport test, not a new live-model success claim."""
    from labbioagentos.kernel import (
        AgentInvocation, AgentLoop, AgentMode, BudgetLedger, FakeProvider, FinishReason,
        ModelResultProjector, ModelTurn, PublicMessage, ToolCall, ToolDispatcher, ToolResult,
    )
    from labbioagentos.runtime.native_tools import (
        ArtifactAggregateArguments, NativeRuntimeCapabilityHost, build_labbio_tool_registry,
    )
    from native_kernel.test_agent_loop import _binding, _identity, _limits

    tools, registered, _aggregate, valid, invalid = await mixed_references(tmp_path, artifact_query_boundary)
    host = NativeRuntimeCapabilityHost(tools.binding, tools.services)
    identity = _identity()
    host.begin_model_invocation(identity)
    # Use the same real delivered aggregate in the new host-owned invocation.
    replayed = await host.invoke("artifact_aggregate", ArtifactAggregateArguments(
        artifact_id=UUID(valid["artifact_id"]), operation="SUM",
        field="row", match_field="group", match_value=1))
    host.mark_feedback_delivered((ToolResult(tool_invocation_id=uuid4(), call_id="aggregate",
        name="artifact_aggregate", status="SUCCEEDED", content=replayed),))
    valid = {**valid, "observation_ids": [replayed["capability_invocation_id"]]}
    turns = tuple(ModelTurn(tool_calls=(ToolCall(call_id=f"report-{index}", name="report_submit",
        arguments_json=json.dumps({"title": "Synthetic", "report_text": "Synthetic.",
            "evidence_references": refs, "delivery_artifact_ids": []})),), finish_reason=FinishReason.TOOL_CALLS)
        for index, refs in enumerate(([valid, invalid], [valid])))
    provider = FakeProvider(binding=_binding(), turns=(*turns,
        ModelTurn(content="Fixture complete.", finish_reason=FinishReason.STOP)))
    budget = BudgetLedger(_limits())
    loop = AgentLoop(provider=provider, invocation=AgentInvocation(identity=identity,
        agent_id="leader", mode=AgentMode.CAPABILITY, tool_names=("report_submit",)), budget_ledger=budget,
        dispatcher=ToolDispatcher(build_labbio_tool_registry(host), ModelResultProjector(max_bytes=16_000),
            budget_ledger=budget, batch_observer=host.mark_feedback_delivered))
    await loop.run(messages=(PublicMessage(role="USER", content="Synthetic citation contract test."),),
        deadline_monotonic=monotonic() + 60)
    failed = json.loads(provider.requests[1].messages[-1].content)
    assert failed["status"] == "SUCCEEDED"
    assert failed["content"]["error"]["error_code"] == "ARTIFACT_NOT_FOUND"
    assert failed["content"]["data"] == {"report_reference_selections": [
        {"index": index, **reference} for index, reference in enumerate((valid, invalid))]}
    failed_id = UUID(failed["content"]["capability_invocation_id"])
    archived = tools.services.observation_store.load(tools.binding, failed_id)
    assert archived.public_response == failed["content"]
    assert archived.item == next(item for item in host.evidence_items()
                                 if item.capability_invocation_id == failed_id)
    started, terminal = [event for event in artifact_query_boundary[0].read(tools.binding.run_id)
                        if event.payload.get("capability_invocation_id") == str(failed_id)]
    assert started.payload["report_reference_selections"] == terminal.payload["report_reference_selections"]
    assert terminal.payload["report_reference_selections"] == failed["content"]["data"]["report_reference_selections"]
    accepted = json.loads(provider.requests[2].messages[-1].content)
    assert accepted["content"]["success"] is True
    assert len(registered) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", ["report_check", "report_submit"])
async def test_report_selection_identity_is_recorded_before_check_can_fail(
    tmp_path, artifact_query_boundary, monkeypatch, capability,
):
    tools, registered, _aggregate, valid, invalid = await mixed_references(tmp_path, artifact_query_boundary)
    selections = [{"index": index, **reference} for index, reference in enumerate((valid, invalid))]
    observed = {}

    def fail_check(*_args):
        observed["started"] = artifact_query_boundary[0].read(tools.binding.run_id)[-1]
        raise RuntimeError("PRIVATE_EXCEPTION_SENTINEL")

    monkeypatch.setattr(tools, "_check_report_references", fail_check)
    if capability == "report_submit":
        response = await tools.report_submit("PRIVATE_TITLE_SENTINEL", "PRIVATE_PROSE_SENTINEL",
            evidence_references=[valid, invalid])
    else:
        response = await tools.report_check(evidence_references=[valid, invalid])
    assert not response["success"] and not registered
    assert observed["started"].event_type is TraceEventType.CAPABILITY_INVOKED
    assert observed["started"].payload["report_reference_selections"] == selections
    assert response["data"] == {"report_reference_selections": selections}
    item = tools.evidence_items()[-1]
    restored = tools.services.observation_store.load(tools.binding, item.capability_invocation_id)
    assert restored.item == item and restored.public_response == response
    for sentinel in ("PRIVATE_TITLE_SENTINEL", "PRIVATE_PROSE_SENTINEL", "PRIVATE_EXCEPTION_SENTINEL"):
        assert sentinel not in restored.model_dump_json()
        assert all(sentinel not in event.model_dump_json()
                   for event in artifact_query_boundary[0].read(tools.binding.run_id))


@pytest.mark.asyncio
async def test_failed_selection_preserves_duplicate_unknown_ids_without_replacement(
    tmp_path, artifact_query_boundary,
):
    tools, _registered, _aggregate, valid, _invalid = await mixed_references(tmp_path, artifact_query_boundary)
    selected_id = str(uuid4())
    references = [{**valid, "observation_ids": [selected_id, selected_id]}]
    failed = await tools.report_submit("Synthetic", "Synthetic.", evidence_references=references)
    assert failed["error"]["error_code"] == "REPORT_OBSERVATION_NOT_DELIVERED"
    assert failed["data"]["report_reference_selections"] == [{"index": 0, **references[0]}]
    assert failed["error"]["report_evidence"]["items"][0]["read_receipts"] == []


@pytest.mark.asyncio
async def test_cancelled_report_check_retains_selected_identities_in_started_event(
    tmp_path, artifact_query_boundary, monkeypatch,
):
    tools, registered, _aggregate, valid, invalid = await mixed_references(tmp_path, artifact_query_boundary)

    def cancel_check(*_args):
        raise asyncio.CancelledError()

    monkeypatch.setattr(tools, "_check_report_references", cancel_check)
    with pytest.raises(asyncio.CancelledError):
        await tools.report_submit("Synthetic", "Synthetic.", evidence_references=[valid, invalid])
    started = artifact_query_boundary[0].read(tools.binding.run_id)[-1]
    assert started.event_type is TraceEventType.CAPABILITY_INVOKED
    assert started.payload["report_reference_selections"] == [
        {"index": index, **reference} for index, reference in enumerate((valid, invalid))]
    assert not registered


@pytest.mark.asyncio
async def test_invalid_selection_syntax_remains_unknown_without_echoing_scalars(
    tmp_path, artifact_query_boundary,
):
    tools, _registered, _aggregate, valid, _invalid = await mixed_references(tmp_path, artifact_query_boundary)
    failed = await tools.report_submit("Synthetic", "Synthetic.", evidence_references=[
        {**valid, "observation_ids": ["PRIVATE_ID_SENTINEL"]}])
    assert not failed["success"]
    item = tools.evidence_items()[-1]
    assert item.report_reference_selections is None
    assert failed["data"] is None
    assert "PRIVATE_ID_SENTINEL" not in json.dumps(failed) + item.model_dump_json()


@pytest.mark.asyncio
async def test_selection_audit_distinguishes_64_observation_boundary_from_unknown_and_empty(
    tmp_path, artifact_query_boundary,
):
    tools, _registered, _aggregate, valid, _invalid = await mixed_references(tmp_path, artifact_query_boundary)
    selected = [str(uuid4()) for _ in range(64)]
    references = [{**valid, "observation_ids": selected}]
    failed = await tools.report_submit("Synthetic", "Synthetic.", evidence_references=references)
    assert failed["data"]["report_reference_selections"] == [{"index": 0, **references[0]}]
    assert len(tools.evidence_items()[-1].report_reference_selections[0].observation_ids) == 64
    too_many = [references[0], {**valid, "observation_ids": [str(uuid4())]}]
    rejected = await tools.report_submit("Synthetic", "Synthetic.", evidence_references=too_many)
    assert not rejected["success"] and rejected["data"] is None
    assert tools.evidence_items()[-1].report_reference_selections is None
    empty = await tools.report_check(evidence_references=[])
    assert empty["success"] and empty["data"]["ready"]
    assert tools.evidence_items()[-1].report_reference_selections == ()


@pytest.mark.asyncio
async def test_64_scoped_citations_preserve_every_selected_identity_within_response_bound(
    tmp_path, artifact_query_boundary,
):
    tools, registered, _aggregate, _valid, _invalid = await mixed_references(tmp_path, artifact_query_boundary)
    references = [{"artifact_id": str(uuid4()), "observation_ids": [str(uuid4())]}
                  for _ in range(64)]
    response = await tools.report_submit("Synthetic", "Synthetic.", evidence_references=references)
    assert not response["success"] and not registered
    expected = [{"index": index, **reference} for index, reference in enumerate(references)]
    assert response["data"]["report_reference_selections"] == expected
    assert len(response["error"]["report_evidence"]["items"]) == 64
    assert len(json.dumps(response, separators=(",", ":")).encode()) <= 64_000
    item = tools.evidence_items()[-1]
    assert tools.services.observation_store.load(tools.binding, item.capability_invocation_id).public_response == response
    started, terminal = [event for event in artifact_query_boundary[0].read(tools.binding.run_id)
                        if event.payload.get("capability_invocation_id") == response["capability_invocation_id"]]
    assert started.payload["report_reference_selections"] == terminal.payload["report_reference_selections"] == expected


@pytest.mark.asyncio
async def test_legacy_feedback_keeps_its_existing_shape_and_canonical_artifact_identities(
    tmp_path, artifact_query_boundary,
):
    tools, _registered, _aggregate, valid, invalid = await mixed_references(tmp_path, artifact_query_boundary)
    ids = [valid["artifact_id"].upper(), invalid["artifact_id"]]
    failed = await tools.report_submit("Synthetic", "Synthetic.", evidence_artifact_ids=ids)
    assert not failed["success"]
    assert failed["data"] is None and tools.evidence_items()[-1].report_reference_selections is None
    assert [item["artifact_id"] for item in failed["error"]["report_evidence"]["items"]] == [
        str(UUID(value)) for value in ids]
    # Invalid scalars remain unknown; even a partial audit must not imply an
    # exact selected identity or retain arbitrary values from an invalid list.
    failed = await tools.report_submit("Synthetic", "Synthetic.",
        evidence_artifact_ids=[valid["artifact_id"], "PRIVATE_ID_SENTINEL"])
    assert not failed["success"] and failed["data"] is None
    assert tools.evidence_items()[-1].report_reference_selections is None
    assert "PRIVATE_ID_SENTINEL" not in json.dumps(failed)


@pytest.mark.asyncio
async def test_legacy_256_citation_limit_does_not_lose_terminal_audit(
    tmp_path, artifact_query_boundary,
):
    tools, registered, _aggregate, _valid, _invalid = await mixed_references(tmp_path, artifact_query_boundary)
    response = await tools.report_submit("Synthetic", "Synthetic.",
        evidence_artifact_ids=[str(uuid4()) for _ in range(256)])
    assert not response["success"] and not registered
    item = tools.evidence_items()[-1]
    assert tools.services.observation_store.load(tools.binding, item.capability_invocation_id).public_response == response
    terminal = artifact_query_boundary[0].read(tools.binding.run_id)[-1]
    assert terminal.event_type is TraceEventType.CAPABILITY_FAILED


@pytest.mark.asyncio
@pytest.mark.parametrize("source_kind", ["COUNT", "TABLE_PAGE", "SUMMARY"])
async def test_scalar_page_and_summary_receipts_survive_rejected_neighbor(
    tmp_path, artifact_query_boundary, source_kind,
):
    tools, ref, registered = report_tools(artifact_query_boundary)
    tools = LabBioRuntimeToolSet(tools.binding, replace(tools.services,
        observation_store=ObservationStore(tmp_path / "observations")))
    if source_kind == "COUNT":
        source = await tools.artifact_aggregate(str(ref.artifact_id), "COUNT")
    elif source_kind == "TABLE_PAGE":
        source = await tools.artifact_query(str(ref.artifact_id), "TOP_N", 10, offset=200)
    else:
        source = await tools.artifact_query(str(ref.artifact_id), "SUMMARY")
    tools.mark_feedback_delivered((source,))
    original = tools.evidence_items()[-1]
    valid = {"artifact_id": str(ref.artifact_id), "observation_ids": [source["capability_invocation_id"]]}
    invalid = {"artifact_id": str(uuid4()), "observation_ids": [str(uuid4())]}
    failed = await tools.report_submit("Synthetic", "Synthetic.", evidence_references=[valid, invalid])
    assert failed["error"]["error_code"] == "ARTIFACT_NOT_FOUND"
    assert failed["error"]["report_evidence"]["receipt_details"] == "OMITTED_FROM_FAILURE_DIAGNOSTICS"
    assert tools.services.observation_store.load(tools.binding, original.capability_invocation_id).item == original
    accepted = await tools.report_submit("Synthetic", "Only the inspected scope.", evidence_references=[valid])
    assert accepted["success"] and len(registered) == 1
    submitted = registered[0]["evidence_read_receipts"]
    assert len(submitted) == 1 and submitted[0] == original.artifact_read_receipt
