"""Selected error bodies survive a bounded projection of repeated diagnostics."""

import json
from uuid import uuid4

import pytest

from labbioagentos import RuntimeIntegrationError
from labbioagentos.runtime.contracts import (
    CapabilityErrorDetails, CapabilityEvidenceItem, HandoffObservation, PreparedHandoff,
)
from labbioagentos.runtime.handoff import finalization_evidence
from labbioagentos.runtime.observations import ObservationStore
from labbioagentos.runtime.tooling import RuntimeCapabilityContext, ToolError, ToolResult
from test_finalization_completion_delivery import finalization_case


async def error_case(tmp_path, *, prepared, count=64):
    stage, evidence, invoker, calls = await finalization_case(
        tmp_path, "Agent reports an unresolved engineering failure.", prepared=False,
    )
    store = ObservationStore(tmp_path / "error-observations")
    binding = RuntimeCapabilityContext(
        principal=invoker.principal, workspace=invoker.workspace, run_id=stage.run_id,
        stage_id=stage.stage_id, invocation_id=stage.invocation_id,
        actor_profile_key="coordinator", actor_agent_name="CoordinatorAgent",
        capability_allowlist=("artifact_list",),
    )
    items = []
    responses = []
    for index in range(count):
        message = f"ENGINEERING_DIAGNOSTIC_{index:02d}_".ljust(1000, "x")
        item = CapabilityEvidenceItem(
            actor_profile_key=binding.actor_profile_key, actor_agent_name=binding.actor_agent_name,
            capability_name="artifact_list", information_authority="AUTHORITATIVE_EVIDENCE",
            status="FAILED", error_code="ENGINEERING_REJECTION", correlation_id=uuid4(),
            error_details=CapabilityErrorDetails(safe_message=message),
        )
        response = ToolResult(
            success=False, capability_invocation_id=item.capability_invocation_id,
            information_authority=item.information_authority,
            error=ToolError(error_code=item.error_code, safe_message=message,
                            correlation_id=item.correlation_id),
        ).model_dump(mode="json")
        store.save(binding, item, response)
        items.append(item)
        responses.append(response)
    handoff = PreparedHandoff(
        summary="Agent selected the first failed invocation; other diagnostics are not reviewed here.",
        observations=(HandoffObservation(
            observation_id=items[0].capability_invocation_id, capability_name="artifact_list",
            information_authority=items[0].information_authority, public_response=responses[0],
        ),),
    ) if prepared else None
    evidence = evidence.model_copy(update={"items": tuple(items), "prepared_handoff": handoff})
    return stage, evidence, invoker, calls, store, binding


@pytest.mark.asyncio
async def test_oversize_control_omits_diagnostic_copies_but_delivers_selected_original(tmp_path):
    stage, evidence, invoker, calls, store, binding = await error_case(tmp_path, prepared=True)
    original = evidence.model_dump_json()
    archived = [store.load(binding, item.capability_invocation_id).model_dump_json() for item in evidence.items]
    result = await invoker.finalize_recovered(stage, evidence)
    assert result.stage_id is stage.stage_id
    assert len(calls) == 1 and calls[0].tools == ()
    control = json.loads(calls[0].messages[-1].content)["capability_evidence"]
    assert "error_details" in control["projection"]["omitted_item_fields"]
    assert len(calls[0].messages[-1].content.encode("utf-8")) <= 60_000
    assert len(control["items"]) == len(evidence.items) == 64
    for projected, item in zip(control["items"], evidence.items, strict=True):
        assert "error_details" not in projected
        assert projected["capability_invocation_id"] == str(item.capability_invocation_id)
        assert projected["status"] == item.status.value
        assert projected["error_code"] == item.error_code
        assert projected["correlation_id"] == str(item.correlation_id)
    selected = json.loads(calls[0].messages[-2].content)
    assert selected == evidence.prepared_handoff.model_dump(mode="json", exclude_none=True)
    assert selected["observations"][0]["public_response"]["error"]["safe_message"] == evidence.items[0].error_details.safe_message
    all_messages = "".join(message.content for message in calls[0].messages)
    assert "ENGINEERING_DIAGNOSTIC_00_" in all_messages
    assert "ENGINEERING_DIAGNOSTIC_01_" not in all_messages
    assert "do not infer their contents" in calls[0].messages[-1].content
    assert evidence.model_dump_json() == original
    assert [store.load(binding, item.capability_invocation_id).model_dump_json() for item in evidence.items] == archived


@pytest.mark.asyncio
async def test_small_control_retains_error_details_without_unnecessary_compaction(tmp_path):
    stage, evidence, invoker, calls, _store, _binding = await error_case(tmp_path, prepared=True, count=1)
    await invoker.finalize_recovered(stage, evidence)
    control = json.loads(calls[0].messages[-1].content)["capability_evidence"]
    assert "projection" not in control
    assert control["items"][0]["error_details"]["safe_message"] == evidence.items[0].error_details.safe_message


@pytest.mark.asyncio
async def test_unselected_errors_cannot_be_omitted_without_an_agent_handoff(tmp_path):
    stage, evidence, invoker, calls, _store, _binding = await error_case(tmp_path, prepared=False)
    projected = finalization_evidence(evidence, include_safe_results=False, compact=True)
    assert "error_details" not in projected["projection"]["omitted_item_fields"]
    assert all(item["error_details"] for item in projected["items"])
    with pytest.raises(RuntimeIntegrationError) as caught:
        await invoker.finalize_recovered(stage, evidence)
    assert caught.value.error_code == "HANDOFF_REQUIRED"
    assert not calls
