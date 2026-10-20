"""Handoff selection feedback is actionable without choosing evidence for Agents."""

import json
from dataclasses import replace
from time import monotonic
from uuid import UUID, uuid4

import pytest

from labbioagentos import LabBioRuntimeToolSet, TraceEventType
from labbioagentos.kernel import (
    AgentInvocation, AgentLoop, AgentMode, BudgetLedger, FinishReason, ModelResultProjector,
    ModelTurn, PublicMessage, ToolCall, ToolDispatcher,
)
from labbioagentos.runtime.native_tools import (
    NativeRuntimeCapabilityHost, PrepareHandoffArguments, build_labbio_tool_registry,
)
from native_kernel.test_agent_loop import _binding, _identity, _limits
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401
from test_runtime_evidence_handoff import tools_with_store


def handoff_events(sink, run_id):
    return [event for event in sink.read(run_id)
            if event.payload.get("capability") == "prepare_handoff"]


def selection_error(response):
    return {key: value for key, value in response["error"].items() if key != "correlation_id"}


def invalid_indices(response):
    message = response["error"]["safe_message"]
    assert "invalid_indices=" in message, "The rejection does not identify invalid entries."
    return json.loads(message.split("invalid_indices=", 1)[1].split(".", 1)[0])


def test_handoff_schema_explains_namespace_and_delivery(tmp_path, artifact_query_boundary):
    tools, _ = tools_with_store(tmp_path, artifact_query_boundary)
    host = NativeRuntimeCapabilityHost(tools.binding, tools.services)
    registry = build_labbio_tool_registry(host)
    description = registry.definition("prepare_handoff").spec.description
    schema = PrepareHandoffArguments.model_json_schema()
    field = schema["properties"]["observation_ids"]
    for text in (description, field["description"]):
        assert "capability_invocation_id" in text
        assert "tool_invocation_id" in text
        assert "distinct" in text
        assert "current" in text and "invocation" in text
        assert "evidence_read" in text
    assert (field["minItems"], field["maxItems"]) == (1, 64)


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_kind", ["duplicate", "outer_tool_id", "artifact_id", "unknown"])
async def test_provider_receives_selection_reason_then_supplies_own_valid_selection(
    tmp_path, artifact_query_boundary, wrong_kind,
):
    sink, _, _, _ = artifact_query_boundary
    tools, ref = tools_with_store(tmp_path, artifact_query_boundary)
    host = NativeRuntimeCapabilityHost(tools.binding, tools.services)
    expected_code = ("HANDOFF_DUPLICATE_OBSERVATION" if wrong_kind == "duplicate"
                     else "HANDOFF_OBSERVATION_NOT_DELIVERED")

    class Provider:
        binding = _binding()

        async def generate(self, request):
            if request.turn_index == 1:
                name, args = "artifact_query", {
                    "artifact_id": str(ref.artifact_id), "view_type": "SUMMARY"}
            else:
                outer = next(json.loads(message.content) for message in reversed(request.messages)
                             if message.role == "TOOL")
                previous = outer["content"]
                if request.turn_index == 2:
                    self.source = previous
                    identity = previous["capability_invocation_id"]
                    self.wrong_ids = {
                        "duplicate": [identity, identity],
                        "outer_tool_id": [outer["tool_invocation_id"]],
                        "artifact_id": [str(ref.artifact_id)],
                        "unknown": [str(uuid4())],
                    }[wrong_kind]
                    name, args = "prepare_handoff", {
                        "observation_ids": self.wrong_ids, "summary": "SUMMARY_CANARY",
                        "limitations": ["LIMITATION_CANARY"]}
                elif request.turn_index == 3:
                    self.rejection = previous
                    self.prepared_after_rejection = host.prepared_handoff
                    self.stop_after_rejection = host.stop_reason
                    name, args = "prepare_handoff", {
                        "observation_ids": [self.source["capability_invocation_id"]],
                        "summary": "Agent-selected assessment.", "limitations": ["Agent limit."]}
                else:
                    self.accepted = previous
                    return ModelTurn(content="Done.", finish_reason=FinishReason.STOP)
            return ModelTurn(tool_calls=(ToolCall(
                call_id=f"call-{request.turn_index}", name=name, arguments_json=json.dumps(args)),),
                finish_reason=FinishReason.TOOL_CALLS)

    provider = Provider()
    identity = _identity()
    host.begin_model_invocation(identity)
    budget = BudgetLedger(_limits())
    loop = AgentLoop(provider=provider,
        invocation=AgentInvocation(identity=identity, agent_id="test", mode=AgentMode.CAPABILITY,
                                   tool_names=("artifact_query", "prepare_handoff")),
        budget_ledger=budget,
        dispatcher=ToolDispatcher(build_labbio_tool_registry(host),
            ModelResultProjector(max_bytes=60_000), budget_ledger=budget,
            batch_observer=host.mark_feedback_delivered))
    await loop.run(messages=(PublicMessage(role="USER", content="Synthetic selection test."),),
                   deadline_monotonic=monotonic() + 60)
    assert provider.rejection["success"] is False
    assert provider.rejection["error"]["error_code"] == expected_code
    message = provider.rejection["error"]["safe_message"]
    assert "observation_ids" in message and "capability_invocation_id" in message
    assert "SUMMARY_CANARY" not in message and "LIMITATION_CANARY" not in message
    assert all(identity not in message for identity in provider.wrong_ids)
    assert provider.prepared_after_rejection is None and provider.stop_after_rejection is None
    assert provider.accepted["data"]["status"] == "READY"
    selected = host.prepared_handoff
    assert selected.summary == "Agent-selected assessment."
    assert selected.limitations == ("Agent limit.",)
    assert [str(item.observation_id) for item in selected.observations] == [
        provider.source["capability_invocation_id"]]
    events = handoff_events(sink, tools.binding.run_id)
    assert len(events) == 4
    for event in events[:2]:
        assert event.payload["handoff_observation_ids"] == provider.wrong_ids
        assert event.payload["handoff_observation_count"] == len(provider.wrong_ids)
    assert events[1].event_type is TraceEventType.CAPABILITY_FAILED
    trace = json.dumps([event.model_dump(mode="json") for event in events])
    assert "SUMMARY_CANARY" not in trace and "LIMITATION_CANARY" not in trace


@pytest.mark.asyncio
async def test_unseen_and_foreign_selections_are_indistinguishable_and_clear_old_handoff(
    tmp_path, artifact_query_boundary,
):
    tools, ref = tools_with_store(tmp_path, artifact_query_boundary)
    response = await tools.artifact_query(str(ref.artifact_id), "SUMMARY")
    identity = response["capability_invocation_id"]
    unseen = await tools.prepare_handoff([identity], "Not delivered.")
    assert invalid_indices(unseen) == [0]
    tools.mark_feedback_delivered((response,))
    assert (await tools.prepare_handoff([identity], "Previously valid."))["data"]["status"] == "READY"
    duplicate = await tools.prepare_handoff([identity, identity], "Invalid replacement.")
    assert duplicate["error"]["error_code"] == "HANDOFF_DUPLICATE_OBSERVATION"
    assert tools.prepared_handoff is None
    other = LabBioRuntimeToolSet(replace(tools.binding, run_id=uuid4()), tools.services)
    foreign = await other.artifact_query(str(ref.artifact_id), "SUMMARY")
    for unknown_id in (str(uuid4()), foreign["capability_invocation_id"]):
        unknown = await tools.prepare_handoff([unknown_id], "No access.")
        assert selection_error(unknown) == selection_error(unseen)
        assert unknown["error"]["error_code"] == "HANDOFF_OBSERVATION_NOT_DELIVERED"
    tools.begin_model_invocation(uuid4())
    old = await tools.prepare_handoff([identity], "Read in an older invocation.")
    assert selection_error(old) == selection_error(unseen)
    first = await tools.evidence_read(identity, limit=30)
    tools.mark_feedback_delivered((first,))
    partial = await tools.prepare_handoff([identity], "Only a prefix was read.")
    assert selection_error(partial) == selection_error(unseen)
    rest = await tools.evidence_read(identity, offset=30)
    tools.mark_feedback_delivered((rest,))
    assert (await tools.prepare_handoff([identity], "Actually reread."))["data"]["status"] == "READY"


@pytest.mark.asyncio
@pytest.mark.parametrize("selection", [
    [], ["PRIVATE_INVALID_ID"], [str(uuid4()), "PRIVATE_INVALID_ID"], [str(uuid4())] * 65,
])
async def test_invalid_selection_does_not_leak_unvalidated_values_into_audit(
    tmp_path, artifact_query_boundary, selection,
):
    sink, _, _, _ = artifact_query_boundary
    tools, _ = tools_with_store(tmp_path, artifact_query_boundary)
    result = await tools.prepare_handoff(selection, "SUMMARY_CANARY", ["LIMITATION_CANARY"])
    assert not result["success"]
    events = handoff_events(sink, tools.binding.run_id)
    assert len(events) == 2
    for event in events:
        values = event.payload.get("handoff_observation_ids", [])
        assert len(values) <= 64
        assert all(str(UUID(value)) == value for value in values)
    public = json.dumps(result)
    audit = json.dumps([event.model_dump(mode="json") for event in events])
    for canary in ("PRIVATE_INVALID_ID", "SUMMARY_CANARY", "LIMITATION_CANARY"):
        assert canary not in public and canary not in audit


@pytest.mark.asyncio
async def test_nine_item_selection_locates_bad_item_before_agent_selects_new_scope(
    tmp_path, artifact_query_boundary,
):
    tools, ref = tools_with_store(tmp_path, artifact_query_boundary)
    host = NativeRuntimeCapabilityHost(tools.binding, tools.services)

    class Provider:
        binding = _binding()

        async def generate(self, request):
            if request.turn_index == 1:
                return ModelTurn(tool_calls=tuple(ToolCall(
                    call_id=f"read-{index}", name="artifact_query", arguments_json=json.dumps({
                        "artifact_id": str(ref.artifact_id), "view_type": "SUMMARY"}))
                    for index in range(8)), finish_reason=FinishReason.TOOL_CALLS)
            responses = [json.loads(message.content)["content"] for message in request.messages
                         if message.role == "TOOL"]
            if request.turn_index == 2:
                self.source_ids = [item["capability_invocation_id"] for item in responses]
                self.selection = [*self.source_ids[:7], str(uuid4()), self.source_ids[7]]
                args = {"observation_ids": self.selection, "summary": "First Agent scope."}
            elif request.turn_index == 3:
                self.rejection = responses[-1]
                self.prepared_after_rejection = host.prepared_handoff
                if "observation_ids[7]" not in self.rejection["error"]["safe_message"]:
                    return ModelTurn(content="The rejected entry is unspecified.",
                                     finish_reason=FinishReason.STOP)
                # The simulated Agent chooses a new scope, not all remaining IDs.
                self.new_selection = [self.source_ids[0], self.source_ids[-1]]
                args = {"observation_ids": self.new_selection,
                        "summary": "Agent's narrower scope.", "limitations": ["Agent's limit."]}
            else:
                self.accepted = responses[-1]
                return ModelTurn(content="Done.", finish_reason=FinishReason.STOP)
            return ModelTurn(tool_calls=(ToolCall(
                call_id=f"handoff-{request.turn_index}", name="prepare_handoff",
                arguments_json=json.dumps(args)),), finish_reason=FinishReason.TOOL_CALLS)

    provider = Provider()
    identity = _identity()
    host.begin_model_invocation(identity)
    budget = BudgetLedger(_limits(tool_calls=12).model_copy(update={"evidence_entries": 16}))
    loop = AgentLoop(provider=provider,
        invocation=AgentInvocation(identity=identity, agent_id="test", mode=AgentMode.CAPABILITY,
                                   tool_names=("artifact_query", "prepare_handoff")),
        budget_ledger=budget,
        dispatcher=ToolDispatcher(build_labbio_tool_registry(host),
            ModelResultProjector(max_bytes=60_000), budget_ledger=budget,
            batch_observer=host.mark_feedback_delivered))
    await loop.run(messages=(PublicMessage(role="USER", content="Synthetic nine-item selection."),),
                   deadline_monotonic=monotonic() + 60)
    assert invalid_indices(provider.rejection) == [7]
    assert "observation_ids[7]" in provider.rejection["error"]["safe_message"]
    assert provider.prepared_after_rejection is None
    assert provider.accepted["data"]["status"] == "READY"
    assert host.prepared_handoff.summary == "Agent's narrower scope."
    assert [str(item.observation_id) for item in host.prepared_handoff.observations] == provider.new_selection
    assert all(value not in json.dumps(provider.rejection["error"]) for value in provider.selection)


@pytest.mark.asyncio
@pytest.mark.parametrize(("count", "bad_positions"), [
    (9, [7]), (64, [0, 63]), (64, [0, 31, 63]), (64, list(range(64))),
])
async def test_handoff_indices_are_complete_bounded_and_do_not_echo_values(
    tmp_path, artifact_query_boundary, count, bad_positions,
):
    tools, ref = tools_with_store(tmp_path, artifact_query_boundary)
    responses = [await tools.artifact_query(str(ref.artifact_id), "SUMMARY") for _ in range(count)]
    tools.mark_feedback_delivered(tuple(responses))
    valid = [item["capability_invocation_id"] for item in responses]
    prior = valid if count == 9 else valid[:1]
    assert (await tools.prepare_handoff(prior, "Legal original scope."))["data"]["status"] == "READY"
    selection = [str(uuid4()) if index in bad_positions else value
                 for index, value in enumerate(valid)]
    result = await tools.prepare_handoff(selection, "SUMMARY_CANARY", ["LIMITATION_CANARY"])
    assert result["error"]["error_code"] == "HANDOFF_OBSERVATION_NOT_DELIVERED"
    assert invalid_indices(result) == bad_positions
    assert f"observation_ids[{bad_positions[0]}]" in result["error"]["safe_message"]
    assert len(result["error"]["safe_message"]) <= 1000
    assert tools.prepared_handoff is None and tools.stop_reason is None
    assert all(value not in json.dumps(result["error"]) for value in selection)
    assert "SUMMARY_CANARY" not in json.dumps(result["error"])
    assert "LIMITATION_CANARY" not in json.dumps(result["error"])
    assert (await tools.prepare_handoff(prior, "Agent restored the legal scope."))["data"]["status"] == "READY"


@pytest.mark.asyncio
@pytest.mark.parametrize("duplicates", [[1], list(range(1, 64))])
async def test_duplicate_indices_identify_later_occurrences_without_replacement(
    tmp_path, artifact_query_boundary, duplicates,
):
    tools, ref = tools_with_store(tmp_path, artifact_query_boundary)
    response = await tools.artifact_query(str(ref.artifact_id), "SUMMARY")
    tools.mark_feedback_delivered((response,))
    identity = response["capability_invocation_id"]
    assert (await tools.prepare_handoff([identity], "Legal."))["data"]["status"] == "READY"
    result = await tools.prepare_handoff([identity] * (len(duplicates) + 1), "Duplicate selection.")
    assert result["error"]["error_code"] == "HANDOFF_DUPLICATE_OBSERVATION"
    assert invalid_indices(result) == duplicates
    assert len(result["error"]["safe_message"]) <= 1000
    assert identity not in json.dumps(result["error"]) and tools.prepared_handoff is None
