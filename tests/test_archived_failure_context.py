"""Archived business failures are rereadable history, not successful science."""

from dataclasses import replace
import json
from time import monotonic
from uuid import UUID, uuid4

import pytest

from labbioagentos import LabBioRuntimeToolSet
from labbioagentos.kernel import FinishReason, KernelContractError, ModelTurn, PublicMessage, ToolCall
from labbioagentos.runtime.observations import ObservationStore
from native_kernel.test_agent_loop import _enable_evidence_reader, _observed_batch, _tool_loop
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401
from test_report_fact_grounding import CheckingProvider
from test_report_scoped_evidence import report_tools
from test_working_note_continuation import call, latest_result, new_loop


@pytest.mark.asyncio
@pytest.mark.parametrize("business_success", [False, True])
async def test_old_archived_business_outcomes_keep_identity_and_latest_failure(business_success):
    observation_id, old = _observed_batch(1, failed=not business_success)
    _latest_id, latest = _observed_batch(2, failed=True)
    loop, provider, _ = _tool_loop((ModelTurn(content="Done.", finish_reason=FinishReason.STOP),))
    _enable_evidence_reader(loop)
    loop.max_request_bytes = 110_000

    await loop.run(messages=(PublicMessage(role="USER", content="Inspect retained errors."),
                             *old, *latest), deadline_monotonic=monotonic() + 60)

    sent = provider.requests[0].messages
    assert sent[-2:] == latest, "The current failure must remain unchanged."
    marker = json.loads(sent[1].content)
    assert marker["observations"][0]["observation_id"] == observation_id
    assert marker["observations"][0]["capability_request_succeeded"] is business_success
    assert marker["reread_tool"] == "evidence_read"
    assert "KNOWN_FAILURE" not in sent[1].content  # No invented error summary.


@pytest.mark.asyncio
@pytest.mark.parametrize("unreadable", [
    "not_archived", "no_reference", "protocol_failure", "success_zero", "success_one",
    "success_null", "success_missing", "archive_one",
])
async def test_failure_archive_eligibility_is_strict_and_does_not_hide_unknown(unreadable):
    _identity, old = _observed_batch(1, failed=True)
    _latest_id, latest = _observed_batch(2, failed=True)
    payload = json.loads(old[1].content)
    if unreadable == "not_archived":
        payload["content"]["observation_archived"] = False
    elif unreadable == "no_reference":
        del payload["content"]["capability_invocation_id"]
    elif unreadable == "protocol_failure":
        payload.update(status="FAILED", error={"code": "INTERNAL_TOOL_ERROR", "message": "Unknown."})
    elif unreadable == "archive_one":
        payload["content"]["observation_archived"] = 1
    elif unreadable == "success_missing":
        del payload["content"]["success"]
    else:
        payload["content"]["success"] = {
            "success_zero": 0, "success_one": 1, "success_null": None,
        }[unreadable]
    old = old[0], old[1].model_copy(update={"content": json.dumps(payload)})
    loop, provider, _ = _tool_loop((ModelTurn(content="Must not run.", finish_reason=FinishReason.STOP),))
    _enable_evidence_reader(loop)
    loop.max_request_bytes = 110_000

    with pytest.raises(KernelContractError, match="capacity"):
        await loop.run(messages=(PublicMessage(role="USER", content="Preserve unknown failures."),
                                 *old, *latest), deadline_monotonic=monotonic() + 60)

    assert provider.requests == ()
    assert loop.messages[-4:] == (*old, *latest)


@pytest.mark.asyncio
async def test_replacing_old_failures_does_not_bypass_unconfirmed_effect_stop():
    _old_id, old = _observed_batch(1, failed=True)
    _latest_id, latest = _observed_batch(2, failed=True)
    loop, provider, effects = _tool_loop((ModelTurn(tool_calls=(ToolCall(
        call_id="not-authorized-to-replay", name="record_value",
        arguments_json='{"value":"must not execute"}',
    ),), finish_reason=FinishReason.TOOL_CALLS),))
    _enable_evidence_reader(loop)
    loop.max_request_bytes = 110_000
    loop.dispatcher.stop_requested = lambda: "UNCONFIRMED_EFFECTS"

    result = await loop.run(messages=(PublicMessage(role="USER", content="Unknown effects stay blocked."),
                                    *old, *latest), deadline_monotonic=monotonic() + 60)

    assert len(provider.requests) == 1
    assert json.loads(provider.requests[0].messages[1].content)["observations"][0]["capability_request_succeeded"] is False
    assert effects == []
    assert result.termination == "RUNTIME_STOPPED"
    assert result.output == {"capability_stop_reason": "UNCONFIRMED_EFFECTS"}
    assert result.tool_results[-1].error.code == "TOOL_NOT_STARTED_RUNTIME_STOP"


@pytest.mark.asyncio
async def test_long_failed_arguments_are_archived_then_agent_rereads_and_changes_selection(
    tmp_path, artifact_query_boundary,
):
    """Real host/store/dispatcher, scripted Agent; not another live-model run."""
    original, ref, registered = report_tools(artifact_query_boundary)
    tools = LabBioRuntimeToolSet(replace(original.binding, capability_allowlist=(
        "artifact_aggregate", "report_submit", "evidence_read",
    )), replace(original.services, observation_store=ObservationStore(tmp_path / "observations")))
    seen = {}
    long_arguments = "Original Agent-authored synthetic payload. " * 300

    def respond(request, turn):
        if turn == 1:
            return call("artifact_aggregate", {"artifact_id": str(ref.artifact_id),
                "operation": "SUM", "field": "row", "match_field": "group", "match_value": 1}, turn)
        response = latest_result(request)
        if turn == 2:
            assert response["success"]
            seen["valid"] = {"artifact_id": str(ref.artifact_id),
                "observation_ids": [response["capability_invocation_id"]]}
        if turn in (2, 3):
            if turn == 3:
                assert response["success"] is False and response["observation_archived"] is True
                seen["original_failure"] = response
                seen["original_bytes"] = (tools.services.observation_store.root /
                    f'{response["capability_invocation_id"]}.json').read_bytes()
            return call("report_submit", {"title": "Synthetic", "report_text": long_arguments,
                "delivery_artifact_ids": [],
                "evidence_references": [seen["valid"], {
                    "artifact_id": str(uuid4()), "observation_ids": [str(uuid4())]}]}, turn)
        if turn == 4:
            assert response["success"] is False
            assert response["capability_invocation_id"] != seen["original_failure"]["capability_invocation_id"]
            assert request.messages[-1].role == "TOOL"  # Latest failure is not replaced.
            markers = [json.loads(message.content) for message in request.messages
                if message.role == "USER" and "COMPLETED_TOOL_BATCH" in message.content]
            archived = {entry["observation_id"]: entry for marker in markers
                        for entry in marker["observations"]}
            identity = seen["original_failure"]["capability_invocation_id"]
            assert archived[identity]["capability_request_succeeded"] is False
            assert not any(long_arguments in (message.content or "") for message in request.messages
                           if message.tool_calls and message.tool_calls[0].call_id == "report_submit-2")
            return call("evidence_read", {"observation_id": identity}, turn)
        if turn == 5:
            assert response["success"] and response["data"]["complete_observation_delivered"]
            reread = json.loads(response["data"]["text"])
            assert reread == seen["original_failure"]
            assert reread["error"]["error_code"] == "ARTIFACT_NOT_FOUND"
            # The scripted Agent chooses only the original READY citation after reading the error.
            ready_ids = {item["artifact_id"] for item in reread["error"]["report_evidence"]["items"]
                         if item["status"] == "READY"}
            corrected = [{"artifact_id": item["artifact_id"], "observation_ids": item["observation_ids"]}
                         for item in reread["data"]["report_reference_selections"]
                         if item["artifact_id"] in ready_ids]
            assert corrected == [seen["valid"]]
            return call("report_submit", {"title": "Synthetic", "report_text": "Bounded fixture only.",
                "delivery_artifact_ids": [],
                "evidence_references": corrected}, turn)
        assert turn == 6 and response["success"] is True
        return ModelTurn(content="Fixture completed from original error.", finish_reason=FinishReason.STOP)

    provider = CheckingProvider(respond)
    loop, host = new_loop(tools.binding, tools.services, provider, max_request_bytes=28_000)
    result = await loop.run(messages=(PublicMessage(role="USER", content="Retain and inspect exact failures."),),
                            deadline_monotonic=monotonic() + 60)

    assert result.termination == "MODEL_COMPLETED"
    assert len(provider.requests) == 6 and len(registered) == 1
    failure_id = UUID(seen["original_failure"]["capability_invocation_id"])
    assert (tools.services.observation_store.root / f"{failure_id}.json").read_bytes() == seen["original_bytes"]
    archived = tools.services.observation_store.load(tools.binding, failure_id)
    assert archived.public_response == seen["original_failure"]
    assert archived.item.status.value == "FAILED"
    assert host._host._delivered_observations[failure_id].artifact_read_receipt is None
    assert all(len(request.model_dump_json().encode()) <= 28_000 for request in provider.requests)
    foreign = LabBioRuntimeToolSet(replace(tools.binding, run_id=uuid4()), tools.services)
    denied = await foreign.evidence_read(str(failure_id))
    assert denied["success"] is False
    assert denied["error"]["error_code"] == "OBSERVATION_NOT_AVAILABLE"
    assert str(failure_id) not in json.dumps(denied.get("data"))
