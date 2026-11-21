"""Model-visible continuity through real host persistence and kernel compaction."""

import json
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from labbioagentos.governance import AuthorizationDenied
from labbioagentos.kernel import (
    AgentInvocation, AgentLoop, AgentMode, BudgetLedger, BudgetLimits,
    FinishReason, KernelIdentity, ModelRequest, ModelResultProjector, ModelTurn, ProviderBinding,
    PublicMessage, ToolCall, ToolDispatcher,
)
from labbioagentos.runtime.native_tools import (
    NativeRuntimeCapabilityHost, build_labbio_tool_registry,
)
from labbioagentos.runtime.observations import ObservationStore
from labbioagentos.kernel.request_metrics import measure_request_size
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401
from test_runtime_working_notes import note_tools


NOTE_TEXT = "AGENT_CONTEXT_CANARY: retain this unresolved assessment. " + "Agent-owned working state. " * 230


class ContextCheckingProvider:
    """A scripted test Agent whose actions depend on actual received tool bodies."""

    binding = ProviderBinding(
        provider="fixture", model="context-checking-fixture",
        endpoint="https://provider.invalid/v1", transport="OPENAI_COMPATIBLE_CHAT",
        thinking_mode="DISABLED",
    )

    def __init__(self, respond):
        self.respond = respond
        self.requests = []

    async def generate(self, request):
        self.requests.append(request)
        return self.respond(request, len(self.requests))


def call(name, arguments, number):
    return ModelTurn(tool_calls=(ToolCall(
        call_id=f"{name}-{number}", name=name,
        arguments_json=json.dumps(arguments),
    ),), finish_reason=FinishReason.TOOL_CALLS)


def latest_result(request):
    feedback = next(message for message in reversed(request.messages) if message.role == "TOOL")
    return json.loads(feedback.content)["content"]


def new_loop(binding, services, provider, *, max_request_bytes=28_000):
    host = NativeRuntimeCapabilityHost(binding, services)
    identity = KernelIdentity(
        thread_id=uuid4(), workflow_run_id=binding.run_id,
        kernel_run_id=uuid4(), invocation_id=binding.invocation_id,
        chain=(binding.invocation_id,),
    )
    host.begin_model_invocation(identity)
    budget = BudgetLedger(BudgetLimits(
        provider_turns=12, tokens=100_000, wall_time_ms=60_000,
        tool_calls=12, evidence_entries=12, evidence_bytes=120_000,
        delegations=0, delegation_depth=0, finalization_attempts=1,
    ))
    loop = AgentLoop(
        provider=provider, invocation=AgentInvocation(
            identity=identity, agent_id=binding.actor_profile_key,
            mode=AgentMode.CAPABILITY, tool_names=binding.capability_allowlist,
        ), budget_ledger=budget, max_request_bytes=max_request_bytes,
        dispatcher=ToolDispatcher(
            build_labbio_tool_registry(host), ModelResultProjector(max_bytes=16_000),
            budget_ledger=budget, batch_observer=host.mark_feedback_delivered,
        ),
    )
    return loop, host


def _note_window_capacity(loop):
    request = ModelRequest(binding=loop.provider.binding,
        messages=(PublicMessage(role="USER", content="Measure fixture tool declarations."),),
        tools=loop._tool_specs(), max_output_tokens=8192,
        deadline_monotonic=10_000_000_000.0,
        kernel_run_id=loop.invocation.identity.kernel_run_id,
        invocation_id=loop.invocation.identity.invocation_id, turn_index=1)
    schema_bytes = measure_request_size(request, loop.provider).public_tool_declarations_bytes
    # Reserve a latest note request/response pair plus one original-note body
    # for framing/references. Older multi-note history must still be compacted.
    return schema_bytes + 3 * len(NOTE_TEXT.encode("utf-8"))


@pytest.mark.asyncio
@pytest.mark.parametrize("source_revoked", [False, True])
async def test_compacted_note_is_exactly_reread_and_continued_without_inherited_credit(
    tmp_path, artifact_query_boundary, monkeypatch, source_revoked,
):
    tools, artifact = note_tools(tmp_path, artifact_query_boundary)
    seen = {}

    def author(request, turn):
        if turn == 1:
            return call("artifact_query", {
                "artifact_id": str(artifact.artifact_id), "view_type": "TOP_N", "limit": 2,
            }, turn)
        result = latest_result(request)
        assert result["success"], result
        if turn == 2:
            seen["source_id"] = result["capability_invocation_id"]
            return call("working_note_write", {
                "text": NOTE_TEXT, "source_observation_ids": [seen["source_id"]],
            }, turn)
        if turn == 3:
            seen["note_id"] = result["capability_invocation_id"]
        if turn in (3, 4, 5):
            return call("working_note_write", {"text": f"Independent note {turn}. " * 320}, turn)
        return ModelTurn(content="Working state persisted.", finish_reason=FinishReason.STOP)

    provider = ContextCheckingProvider(author)
    loop, host = new_loop(tools.binding, tools.services, provider)
    loop.max_request_bytes = _note_window_capacity(loop)
    unbounded_sizes = {}
    bound_request = loop._bound_request

    def record_pressure(request):
        sizes = measure_request_size(request, loop.provider)
        unbounded_sizes[request.turn_index] = max(sizes.public_request_bytes, sizes.wire_request_bytes or 0)
        return bound_request(request)

    monkeypatch.setattr(loop, "_bound_request", record_pressure)
    first = await loop.run(
        messages=(PublicMessage(role="USER", content="Persist your own working assessment."),),
        deadline_monotonic=10_000_000_000.0,
    )
    assert first.termination == "MODEL_COMPLETED"
    assert any(event.numeric_facts.get("compacted_batches", 0) > 0 for event in first.events)
    markers = [json.loads(message.content) for message in provider.requests[-1].messages
               if message.role == "USER" and "COMPLETED_TOOL_BATCH" in message.content]
    replaced_ids = {entry["observation_id"] for marker in markers for entry in marker["observations"]}
    assert {seen["source_id"], seen["note_id"]}.issubset(replaced_ids)
    assert all(NOTE_TEXT not in (message.content or "") for message in provider.requests[-1].messages)
    assert all(len(request.model_dump_json().encode()) <= loop.max_request_bytes for request in provider.requests)
    prepared = {event.numeric_facts["turn_index"]: event.numeric_facts
        for event in first.events if event.kind.value == "MODEL_REQUEST_PREPARED"}
    for turn in (4, 5, 6):
        assert unbounded_sizes[turn] > loop.max_request_bytes
        assert prepared[turn]["request_bytes"] <= loop.max_request_bytes
        assert prepared[turn]["compacted_batches"] > 0
    store_root = tools.services.observation_store.root
    stored_note = ObservationStore(store_root).load(tools.binding, UUID(seen["note_id"]))
    assert stored_note.public_response["data"]["text"] == NOTE_TEXT
    original_source = ObservationStore(store_root).load(tools.binding, UUID(seen["source_id"]))
    assert original_source.public_response["data"]["records"] == [{"row": 0}, {"row": 1}]

    references = [{"artifact_id": str(artifact.artifact_id), "observation_ids": [seen["source_id"]]}]

    def continue_from_exact_id(request, turn):
        if turn == 1:
            assert request.messages[0].content == f"Continue from this exact note: {seen['note_id']}"
            assert NOTE_TEXT not in request.messages[0].content
            return call("working_note_read", {"note_id": seen["note_id"]}, turn)
        result = latest_result(request)
        if source_revoked:
            assert not result["success"]
            assert "AGENT_CONTEXT_CANARY" not in json.dumps(result)
            return ModelTurn(content="The note cannot be released under current authority.",
                             finish_reason=FinishReason.STOP)
        assert result["success"], result
        if turn == 2:
            assert result["information_authority"] == "MODEL_CONTEXT"
            assert result["data"]["text"] == NOTE_TEXT
            assert result["data"]["source_read_credit"] is False
            return call("working_note_write", {
                "text": result["data"]["text"][:53] + " Continued by the current Agent; source not reread.",
                "source_observation_ids": [result["capability_invocation_id"]],
                "previous_note_id": seen["note_id"],
            }, turn)
        if turn == 3:
            assert result["data"]["revision"] == 2
            assert result["data"]["previous_note_id"] == seen["note_id"]
            assert result["data"]["text"].endswith("source not reread.")
            return call("report_check", {"evidence_references": references, "delivery_artifact_ids": []}, turn)
        if turn == 4:
            assert not result["data"]["ready"], "Reading a note granted source-evidence credit"
            return call("evidence_read", {"observation_id": seen["source_id"]}, turn)
        if turn == 5:
            assert result["data"]["complete_observation_delivered"]
            assert json.loads(result["data"]["text"]) == original_source.public_response
            return call("report_check", {"evidence_references": references, "delivery_artifact_ids": []}, turn)
        assert result["data"]["ready"], "Explicitly delivered original evidence was not credited"
        return ModelTurn(content="Continued from exact working state and separately reread evidence.",
                         finish_reason=FinishReason.STOP)

    fresh_binding = replace(tools.binding, invocation_id=uuid4())
    fresh_services = replace(tools.services, observation_store=ObservationStore(store_root))
    fresh_provider = ContextCheckingProvider(continue_from_exact_id)
    fresh_loop, fresh_host = new_loop(fresh_binding, fresh_services, fresh_provider)
    fresh_loop.max_request_bytes = _note_window_capacity(fresh_loop)
    assert fresh_loop.max_request_bytes == loop.max_request_bytes
    assert fresh_host is not host
    assert fresh_host._host.services.observation_store is not host._host.services.observation_store
    if source_revoked:
        def deny_current_source(_item):
            raise AuthorizationDenied("Synthetic source authority was revoked.")
        monkeypatch.setattr(fresh_host._host, "_validate_report_observation", deny_current_source)
    resumed = await fresh_loop.run(
        messages=(PublicMessage(role="USER", content=f"Continue from this exact note: {seen['note_id']}"),),
        deadline_monotonic=10_000_000_000.0,
    )
    assert resumed.termination == "MODEL_COMPLETED"
    assert len(fresh_provider.requests) == (2 if source_revoked else 6)
    assert all(len(request.model_dump_json().encode()) <= fresh_loop.max_request_bytes
        for request in fresh_provider.requests)
    if source_revoked:
        assert UUID(seen["source_id"]) not in fresh_host._host._delivered_observations
        assert all("AGENT_CONTEXT_CANARY" not in (message.content or "")
                   for request in fresh_provider.requests for message in request.messages)
    assert ObservationStore(store_root).load(fresh_binding, UUID(seen["note_id"])) == stored_note
