from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from labbioagentos import LabBioRuntimeToolSet, WorkflowStage
from labbioagentos.contracts import InformationAuthority
from labbioagentos.runtime.contracts import CapabilityEvidenceItem, CapabilityEvidenceStatus
from labbioagentos.runtime.tooling import ToolResult
from labbioagentos.runtime.observations import ObservationStore
from test_execution_inspection import inspection, _submit  # noqa: F401
from test_runtime_milestone_b import boundary, _toolset  # noqa: F401
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401


def tools_with_store(tmp_path, fixture):
    _, binding, ref, tools = fixture
    binding = replace(binding, stage_id=WorkflowStage.REPORT, capability_allowlist=(
        "artifact_query", "evidence_read", "prepare_handoff", "report_check"))
    services = replace(tools.services, observation_store=ObservationStore(tmp_path / "observations"))
    return LabBioRuntimeToolSet(binding, services), ref


@pytest.mark.asyncio
async def test_handoff_copies_only_agent_selected_delivered_evidence(tmp_path, artifact_query_boundary):
    tools, ref = tools_with_store(tmp_path, artifact_query_boundary)
    result = await tools.artifact_query(str(ref.artifact_id), "TOP_N", 2)
    identity = result["capability_invocation_id"]
    rejected = await tools.prepare_handoff([identity], "Agent-authored assessment")
    assert not rejected["success"]
    tools.mark_feedback_delivered((result,))
    accepted = await tools.prepare_handoff([identity], "Agent-authored assessment", ["Limited scope"])
    assert accepted["data"]["status"] == "READY"
    assert tools.prepared_handoff.summary == "Agent-authored assessment"
    assert tools.prepared_handoff.observations[0].public_response == result
    assert len(tools.prepared_handoff.observations) == 1


@pytest.mark.asyncio
async def test_partial_observation_read_grants_no_cross_stage_report_credit(tmp_path, artifact_query_boundary):
    original, ref = tools_with_store(tmp_path, artifact_query_boundary)
    result = await original.artifact_query(str(ref.artifact_id), "TOP_N", 2)
    identity = result["capability_invocation_id"]
    report = LabBioRuntimeToolSet(replace(original.binding, invocation_id=uuid4(),
                                          stage_id=WorkflowStage.REPORT), original.services)
    references = [{"artifact_id": str(ref.artifact_id), "observation_ids": [identity]}]
    first = await report.evidence_read(identity, limit=32)
    assert first["success"], first
    assert not first["data"]["complete_observation_delivered"]
    report.mark_feedback_delivered((first,))
    check = await report.report_check(evidence_references=references)
    assert not check["data"]["ready"]
    rest = await report.evidence_read(identity, offset=32, limit=8000)
    assert rest["data"]["complete_observation_delivered"]
    check = await report.report_check(evidence_references=references)
    assert not check["data"]["ready"]  # Not yet sent to the model.
    report.mark_feedback_delivered((rest,))
    check = await report.report_check(evidence_references=references)
    assert check["data"]["ready"]


@pytest.mark.asyncio
async def test_oversize_handoff_returns_sizes_without_dropping_selected_body(tmp_path, artifact_query_boundary):
    tools, _ = tools_with_store(tmp_path, artifact_query_boundary)
    ids = []
    responses = []
    for _ in range(2):
        item = CapabilityEvidenceItem(
            actor_profile_key="test", actor_agent_name="Test", capability_name="artifact_query",
            information_authority=InformationAuthority.AUTHORITATIVE_EVIDENCE,
            status=CapabilityEvidenceStatus.COMPLETED,
            safe_result={"values": ["a" * 7000 for _ in range(5)]},
        )
        response = ToolResult(
            success=True, capability_invocation_id=item.capability_invocation_id,
            information_authority=item.information_authority,
            data=item.safe_result,
        ).model_dump(mode="json")
        tools._record_observation(item, response)
        responses.append(response)
        ids.append(str(item.capability_invocation_id))
    tools.mark_feedback_delivered(tuple(responses))
    result = await tools.prepare_handoff(ids, "Agent summary")
    assert result["data"]["status"] == "TOO_LARGE"
    assert result["data"]["serialized_bytes"] > result["data"]["max_bytes"]
    assert tools.prepared_handoff is None
    result = await tools.prepare_handoff(ids[:1], "Narrower Agent summary")
    assert result["data"]["status"] == "READY"
    assert tools.prepared_handoff.observations[0].public_response["data"]["values"] == ["a" * 7000] * 5
    stored = tools.services.observation_store.load(tools.binding, UUID(ids[1]))
    assert stored.item.safe_result["values"] == ["a" * 7000] * 5
    result = await tools.prepare_handoff(ids, "Too wide again")
    assert result["data"]["status"] == "TOO_LARGE"
    assert tools.prepared_handoff is None


@pytest.mark.asyncio
async def test_handoff_rejects_empty_and_clears_previous_selection(tmp_path, artifact_query_boundary):
    tools, ref = tools_with_store(tmp_path, artifact_query_boundary)
    response = await tools.artifact_query(str(ref.artifact_id), "TOP_N", 2)
    tools.mark_feedback_delivered((response,))
    assert (await tools.prepare_handoff([response["capability_invocation_id"]], "Agent view"))["success"]
    assert tools.prepared_handoff is not None
    assert not (await tools.prepare_handoff([], "Summary without observations"))["success"]
    assert tools.prepared_handoff is None


@pytest.mark.asyncio
async def test_original_program_public_page_survives_archive_reread_and_handoff(
    tmp_path, inspection, boundary,
):
    source = "# explicitly public Agent program\n" * 400 + "pass\n"
    receipt = await _submit(inspection, boundary, source)
    service, _, run_id = inspection
    original = _toolset(boundary, WorkflowStage.EXECUTE,
                       ("execution_inspect", "evidence_read", "prepare_handoff"),
                       execution_submission=service)
    tools = LabBioRuntimeToolSet(replace(original.binding, run_id=run_id),
        replace(original.services, observation_store=ObservationStore(tmp_path / "observations")))
    response = await tools.execution_inspect(str(receipt.execution_id), source_limit=16000)
    assert response["observation_archived"] is True
    identity = UUID(response["capability_invocation_id"])
    saved = tools.services.observation_store.load(tools.binding, identity)
    assert saved.public_response == response
    assert len(saved.public_response["data"]["submitted_program"]["source"]) > 8000
    assert "source" not in saved.item.safe_result["submitted_program"]
    tools.mark_feedback_delivered((response,))
    result = await tools.prepare_handoff([str(identity)], "Agent-authored statement")
    assert result["data"]["status"] == "READY"
    assert tools.prepared_handoff.observations[0].public_response == response
    pieces, offset = [], 0
    while True:
        page = await tools.evidence_read(identity, offset=offset)
        pieces.append(page["data"]["text"])
        if page["data"]["next_offset"] is None:
            break
        offset = page["data"]["next_offset"]
    import json
    assert json.loads("".join(pieces)) == response
    assert saved.public_response["data"]["submitted_contract"] == response["data"]["submitted_contract"]


@pytest.mark.asyncio
async def test_only_exact_feedback_grants_observation_and_page_credit(tmp_path, artifact_query_boundary):
    tools, ref = tools_with_store(tmp_path, artifact_query_boundary)
    response = await tools.artifact_query(str(ref.artifact_id), "TOP_N", 2)
    identity = response["capability_invocation_id"]
    references = [{"artifact_id": str(ref.artifact_id), "observation_ids": [identity]}]
    # Execution alone and a projection losing the body both fail to grant credit.
    assert not (await tools.report_check(evidence_references=references))["data"]["ready"]
    tools.mark_feedback_delivered(({**response, "data": {"projection_failed": True}},))
    assert not (await tools.report_check(evidence_references=references))["data"]["ready"]
    tools.mark_feedback_delivered((response,))
    assert (await tools.report_check(evidence_references=references))["data"]["ready"]
    fresh = LabBioRuntimeToolSet(replace(tools.binding, invocation_id=uuid4()), tools.services)
    first = await fresh.evidence_read(identity, limit=100)
    rest = await fresh.evidence_read(identity, offset=100, limit=8000)
    fresh.mark_feedback_delivered((rest,))  # The first page was not sent.
    assert not (await fresh.report_check(evidence_references=references))["data"]["ready"]
    fresh.mark_feedback_delivered((first,))
    assert (await fresh.report_check(evidence_references=references))["data"]["ready"]


@pytest.mark.asyncio
async def test_projection_failure_cannot_gain_credit_from_later_delivered_result(
    tmp_path, artifact_query_boundary, monkeypatch,
):
    from labbioagentos.kernel import ToolError, ToolResult as KernelToolResult
    from labbioagentos.runtime.native_tools import NativeRuntimeCapabilityHost

    tools, ref = tools_with_store(tmp_path, artifact_query_boundary)
    failed_projection = await tools.artifact_query(str(ref.artifact_id), "TOP_N", 2)
    delivered = await tools.artifact_query(str(ref.artifact_id), "SUMMARY")
    monkeypatch.setattr("labbioagentos.runtime.tooling.LabBioRuntimeToolSet",
                        lambda _binding, _services: tools)
    host = NativeRuntimeCapabilityHost(tools.binding, tools.services)
    host.mark_feedback_delivered((
        KernelToolResult(tool_invocation_id=uuid4(), call_id="first", name="artifact_query",
                         status="FAILED", error=ToolError(code="PROJECTION_FAILED", message="Too large")),
        KernelToolResult(tool_invocation_id=uuid4(), call_id="second", name="artifact_query",
                         status="SUCCEEDED", content=delivered),
    ))
    assert UUID(delivered["capability_invocation_id"]) in tools._delivered_observations
    assert UUID(failed_projection["capability_invocation_id"]) not in tools._delivered_observations
    assert not (await tools.prepare_handoff(
        [failed_projection["capability_invocation_id"]], "Unread"))["success"]
    # The earlier page was never seen, so reading it again must remain possible.
    assert (await tools.artifact_query(str(ref.artifact_id), "TOP_N", 2))["success"]
    assert tools.stop_reason is None


@pytest.mark.asyncio
async def test_failed_public_observation_can_be_selected_without_claiming_success(
    tmp_path, artifact_query_boundary,
):
    tools, ref = tools_with_store(tmp_path, artifact_query_boundary)
    failure = await tools.artifact_query(str(ref.artifact_id), "SUMMARY", 2)
    assert not failure["success"]
    tools.mark_feedback_delivered((failure,))
    selected = await tools.prepare_handoff([failure["capability_invocation_id"]],
                                           "No valid data were returned.", ["Query failed."])
    assert selected["data"]["status"] == "READY"
    assert tools.prepared_handoff.observations[0].public_response == failure
    assert not tools.prepared_handoff.observations[0].public_response["success"]


@pytest.mark.asyncio
async def test_archive_failure_never_returns_an_unrecoverable_success_response(
    tmp_path, artifact_query_boundary, monkeypatch,
):
    tools, ref = tools_with_store(tmp_path, artifact_query_boundary)

    def fail_save(*_args):
        raise OSError("Synthetic archive failure")

    monkeypatch.setattr(tools.services.observation_store, "save", fail_save)
    with pytest.raises(OSError, match="Synthetic archive failure"):
        await tools.artifact_query(str(ref.artifact_id), "TOP_N", 2)
    assert not tools._public_responses
    assert not tools.evidence_items()


@pytest.mark.asyncio
async def test_host_without_durable_archive_does_not_claim_reread_available(artifact_query_boundary):
    _, _, ref, tools = artifact_query_boundary
    response = await tools.artifact_query(str(ref.artifact_id), "TOP_N", 2)
    assert response["success"]
    assert response["observation_archived"] is False
