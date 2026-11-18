"""Host failures are operation facts, not Agent prose or scientific evidence."""

import json
from dataclasses import replace
from time import monotonic
from uuid import UUID, uuid4

import pytest

from labbioagentos import LabBioRuntimeToolSet
from labbioagentos.kernel import (
    AgentInvocation, AgentLoop, AgentMode, BudgetLedger, FinishReason,
    ModelResultProjector, ModelTurn, PublicMessage, ToolCall, ToolDispatcher,
)
from labbioagentos.runtime.contracts import RuntimeEvidenceGroundingControl
from labbioagentos.runtime.native_tools import (
    NativeCapabilityDispatcher, NativeRuntimeCapabilityHost, build_labbio_tool_registry,
)
from native_kernel.test_agent_loop import _binding, _identity, _limits
from native_kernel.test_labbio_native_tools import (
    RecordingHost, VALID_ARGUMENTS, _binding as dispatcher_binding,
)
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401
from test_runtime_working_notes import note_tools


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["note_source", "note_read", "evidence_read", "artifact_query"])
async def test_failure_authority_matches_public_response_audit_and_archive(
    tmp_path, artifact_query_boundary, operation,
):
    tools, ref = note_tools(tmp_path, artifact_query_boundary)
    missing = str(uuid4())
    if operation == "note_source":
        response = await tools.working_note_write("Unverified Agent assessment.", [missing])
    elif operation == "note_read":
        response = await tools.working_note_read(missing)
    elif operation == "evidence_read":
        response = await tools.evidence_read(missing)
    else:
        response = await tools.artifact_query(str(ref.artifact_id), "SUMMARY", 2)
    assert response["success"] is False
    assert response["information_authority"] == "CONTROL_STATE"
    assert response["error"]["error_code"] and response["data"] is None
    item = tools.evidence_items()[-1]
    assert item.information_authority.value == "CONTROL_STATE"
    assert item.status.value == "FAILED" and item.safe_result is None
    saved = tools.services.observation_store.load(tools.binding, item.capability_invocation_id)
    assert saved.item == item and saved.public_response == response
    assert item.artifact_read_receipt is None


@pytest.mark.asyncio
async def test_successful_notes_and_sources_keep_their_own_authority(tmp_path, artifact_query_boundary):
    tools, ref = note_tools(tmp_path, artifact_query_boundary)
    source = await tools.artifact_query(str(ref.artifact_id), "SUMMARY")
    assert source["information_authority"] == "AUTHORITATIVE_EVIDENCE"
    tools.mark_feedback_delivered((source,))
    note = await tools.working_note_write("Agent interpretation is not proof.", [source["capability_invocation_id"]])
    assert note["success"] and note["information_authority"] == "MODEL_CONTEXT"
    assert note["data"]["authority"] == "MODEL_CONTEXT"
    assert note["data"]["source_read_credit"] is False
    read = await tools.working_note_read(note["capability_invocation_id"])
    assert read["success"] and read["information_authority"] == "MODEL_CONTEXT"
    assert read["data"]["text"] == "Agent interpretation is not proof."
    assert tools.evidence_items()[-1].information_authority.value == "MODEL_CONTEXT"


@pytest.mark.asyncio
async def test_author_mismatch_is_host_control_not_a_change_to_the_original_note(
    tmp_path, artifact_query_boundary,
):
    tools, _ = note_tools(tmp_path, artifact_query_boundary)
    note = await tools.working_note_write("Original Agent assessment.")
    note_id = note["capability_invocation_id"]
    other = LabBioRuntimeToolSet(replace(tools.binding, actor_profile_key="other-agent"), tools.services)
    failure = await other.working_note_write("Unapproved replacement.", previous_note_id=note_id)
    assert not failure["success"]
    assert failure["error"]["error_code"] == "WORKING_NOTE_AUTHOR_MISMATCH"
    assert failure["information_authority"] == "CONTROL_STATE"
    assert other.evidence_items()[-1].information_authority.value == "CONTROL_STATE"
    original = await other.working_note_read(note_id)
    assert original["data"]["text"] == "Original Agent assessment."
    assert original["information_authority"] == "MODEL_CONTEXT"


@pytest.mark.asyncio
async def test_native_model_receives_host_failure_without_promoting_note_content(
    tmp_path, artifact_query_boundary,
):
    tools, _ = note_tools(tmp_path, artifact_query_boundary)
    host = NativeRuntimeCapabilityHost(tools.binding, tools.services)

    class Provider:
        binding = _binding()

        async def generate(self, request):
            if request.turn_index == 1:
                name, args = "working_note_read", {"note_id": str(uuid4())}
            else:
                outer = next(json.loads(message.content) for message in reversed(request.messages)
                             if message.role == "TOOL")
                previous = outer["content"]
                if request.turn_index == 2:
                    self.failure = previous
                    name, args = "working_note_write", {"text": "An unresolved Agent assessment."}
                elif request.turn_index == 3:
                    assert previous["success"] is True
                    assert previous["information_authority"] == "MODEL_CONTEXT"
                    self.note = previous
                    name, args = "working_note_read", {"note_id": previous["capability_invocation_id"]}
                else:
                    assert previous["information_authority"] == "MODEL_CONTEXT"
                    assert previous["data"]["source_read_credit"] is False
                    return ModelTurn(content="The lookup failed; the assessment remains unverified.",
                                     finish_reason=FinishReason.STOP)
            return ModelTurn(tool_calls=(ToolCall(call_id=f"call-{request.turn_index}", name=name,
                                                  arguments_json=json.dumps(args)),),
                             finish_reason=FinishReason.TOOL_CALLS)

    provider = Provider()
    identity = _identity()
    host.begin_model_invocation(identity)
    budget = BudgetLedger(_limits())
    loop = AgentLoop(provider=provider,
        invocation=AgentInvocation(identity=identity, agent_id="test", mode=AgentMode.CAPABILITY,
                                   tool_names=("working_note_read", "working_note_write")),
        budget_ledger=budget,
        dispatcher=ToolDispatcher(build_labbio_tool_registry(host), ModelResultProjector(max_bytes=60_000),
                                  budget_ledger=budget, batch_observer=host.mark_feedback_delivered))
    await loop.run(messages=(PublicMessage(role="USER", content="Synthetic authority test."),),
                   deadline_monotonic=monotonic() + 60)
    assert provider.failure["success"] is False
    assert provider.failure["information_authority"] == "CONTROL_STATE"
    archived = tools.services.observation_store.load(tools.binding,
        UUID(provider.failure["capability_invocation_id"]))
    assert archived.item.information_authority.value == "CONTROL_STATE"
    assert archived.public_response == provider.failure
    assert provider.note["data"]["source_observation_ids"] == []


@pytest.mark.asyncio
async def test_control_failure_is_not_a_report_source(tmp_path, artifact_query_boundary):
    tools, ref = note_tools(tmp_path, artifact_query_boundary)
    failure = await tools.working_note_read(str(uuid4()))
    tools.mark_feedback_delivered((failure,))
    checked = await tools.report_check(evidence_references=[{
        "artifact_id": str(ref.artifact_id),
        "observation_ids": [failure["capability_invocation_id"]],
    }])
    assert checked["success"] and not checked["data"]["ready"]
    assert not checked["data"]["items"][0]["read_receipts"]


def test_operational_rule_does_not_promote_scientific_or_model_claims():
    rule = RuntimeEvidenceGroundingControl().model_dump(mode="json").get("operational_fact_rule", "")
    for text in ("CONTROL_STATE", "host", "diagnostics", "permissions", "status", "scientific", "MODEL_CONTEXT"):
        assert text in rule


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", ["artifact_list", "working_note_read", "report_check"])
async def test_standalone_native_dispatch_failure_uses_host_control_authority(capability):
    host = RecordingHost()
    host.fail_with = RuntimeError("PRIVATE_DIAGNOSTIC_SENTINEL")
    dispatcher = NativeCapabilityDispatcher(binding=dispatcher_binding(capability),
                                             registry=build_labbio_tool_registry(host))
    outcomes = await dispatcher.dispatch_batch((ToolCall(
        call_id="host-failed", name=capability, arguments_json=json.dumps(VALID_ARGUMENTS[capability]),
    ),), finish_reason=FinishReason.TOOL_CALLS)
    result = outcomes[0].result
    item = dispatcher.evidence_items()[0]
    assert result.status == "FAILED" and result.error.code == "INTERNAL_TOOL_ERROR"
    assert item.status.value == "FAILED" and item.information_authority.value == "CONTROL_STATE"
    assert item.safe_result is None and item.artifact_read_receipt is None
    assert "PRIVATE_DIAGNOSTIC_SENTINEL" not in repr(outcomes) + repr(item)
