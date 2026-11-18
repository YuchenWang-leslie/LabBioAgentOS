"""Batch causality is explicit without disabling independent tool batches."""

import json
from dataclasses import replace
from time import monotonic
from uuid import UUID, uuid4

import pytest

from labbioagentos import local_config
from labbioagentos.kernel import (
    AgentProfile, AgentRegistry, AgentSpec, BudgetLedger, CollaborationRuntime,
    FinishReason, ModelResultProjector, ModelTurn, PublicMessage, TeamSpec,
    ToolCall, ToolRegistry,
)
from native_kernel.test_collaboration import _identity, _limits
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401
from test_local_configuration import settings_file  # noqa: F401
from test_runtime_working_notes import note_tools
from test_working_note_continuation import ContextCheckingProvider, new_loop


@pytest.mark.asyncio
async def test_assembled_capability_system_explains_batch_feedback_causality(settings_file):
    settings = local_config.load_settings(settings_file)
    app = local_config.build_application(
        settings, settings.result_root / "batch-contract", load_provider=False,
    )
    checked = []

    provider = ContextCheckingProvider(lambda _request, _turn: ModelTurn(
        content="No action requested.", finish_reason=FinishReason.STOP,
    ))
    app.runtime_factory.provider_builder = lambda _model: provider
    try:
        for assembly in app.configuration.stage_assemblies:
            if not assembly.capability_phase_enabled:
                continue
            profile, prompt, _model, _schema, resolved_provider = app.runtime_factory.resolve(
                assembly.root_profile_key, assembly.capability_prompt_values,
            )
            spec = AgentSpec(agent_id=profile.profile_key, role="LEADER")
            runtime = CollaborationRuntime(
                registry=AgentRegistry(
                    team=TeamSpec(leader=spec),
                    profiles=(AgentProfile(
                        spec=spec, description=profile.role_description,
                        instructions=prompt.sanitized_text,
                    ),),
                    providers={profile.profile_key: resolved_provider},
                ),
                tool_registry=ToolRegistry(),
                projector=ModelResultProjector(max_bytes=16_000),
                budget_ledger=BudgetLedger(_limits()),
            )
            await runtime.run_leader(
                identity=_identity(),
                messages=(PublicMessage(role="USER", content="Inspect the interface contract."),),
                deadline_monotonic=monotonic() + 60,
            )
            system = provider.requests[-1].messages[0]
            assert system.role == "SYSTEM"
            assert system.content == prompt.sanitized_text
            for contract in (
                "Tool calls emitted in one response are fixed before their results exist for you",
                "ordered host execution does not expose earlier results to later calls in that batch",
                "Feedback reaches you in the next model turn",
                "Batch independent calls freely",
                "wait for its actual feedback and capability_invocation_id before emitting it",
                "do not predict same-batch output",
                "A source-free note of pending intent remains allowed when independent of those outputs",
            ):
                assert contract in system.content, contract
            checked.append(system.content)
        assert len(checked) == sum(
            assembly.capability_phase_enabled for assembly in app.configuration.stage_assemblies
        )
    finally:
        app.run_state_store.close()


def batch(*calls):
    return ModelTurn(tool_calls=tuple(
        ToolCall(call_id=identity, name=name, arguments_json=json.dumps(arguments))
        for identity, name, arguments in calls
    ), finish_reason=FinishReason.TOOL_CALLS)


def feedback(request):
    return {message.tool_call_id: json.loads(message.content)["content"]
            for message in request.messages if message.role == "TOOL"}


@pytest.mark.asyncio
async def test_same_batch_read_cannot_supply_note_or_handoff_until_next_model_turn(
    tmp_path, artifact_query_boundary,
):
    tools, artifact = note_tools(tmp_path, artifact_query_boundary)
    original = await tools.artifact_query(str(artifact.artifact_id), "TOP_N", 2)
    source_id = original["capability_invocation_id"]
    binding = replace(tools.binding, invocation_id=uuid4())
    seen = {}

    def respond(request, turn):
        if turn == 1:
            return batch(
                ("read", "evidence_read", {"observation_id": source_id}),
                ("early-note", "working_note_write", {
                    "text": "An assessment depending on the pending read.",
                    "source_observation_ids": [source_id],
                }),
                ("early-handoff", "prepare_handoff", {
                    "observation_ids": [source_id], "summary": "Pending read assessment.",
                }),
            )
        results = feedback(request)
        if turn == 2:
            page = results["read"]
            assert page["success"] and page["data"]["complete_observation_delivered"]
            actual = json.loads(page["data"]["text"])
            assert actual == original
            assert UUID(source_id) not in host._host._delivered_observations
            assert not results["early-note"]["success"]
            assert not results["early-handoff"]["success"]
            assert host.prepared_handoff is None
            assert not any(item.capability_name == "working_note_write"
                           and item.status == "COMPLETED" for item in host.evidence_items())
            seen["rejections"] = (results["early-note"], results["early-handoff"])
            return batch(
                ("observed-note", "working_note_write", {
                    "text": "An assessment after receiving the original observation.",
                    "source_observation_ids": [actual["capability_invocation_id"]],
                }),
                ("observed-handoff", "prepare_handoff", {
                    "observation_ids": [actual["capability_invocation_id"]],
                    "summary": "Assessment of the actual observation.",
                }),
            )
        assert results["observed-note"]["success"]
        assert results["observed-note"]["information_authority"] == "MODEL_CONTEXT"
        assert results["observed-note"]["data"]["source_read_credit"] is False
        assert results["observed-handoff"]["data"]["status"] == "READY"
        seen["note"] = results["observed-note"]
        return ModelTurn(content="Observed state handed off.", finish_reason=FinishReason.STOP)

    provider = ContextCheckingProvider(respond)
    loop, host = new_loop(binding, tools.services, provider, max_request_bytes=80_000)
    result = await loop.run(
        messages=(PublicMessage(role="USER", content="Exercise batch delivery boundaries."),),
        deadline_monotonic=monotonic() + 60,
    )
    assert result.termination == "MODEL_COMPLETED"
    assert len(provider.requests) == 3
    assert seen["note"]["data"]["source_observation_ids"] == [source_id]
    assert host.prepared_handoff.observations[0].public_response == original
    assert [item.capability_name for item in host.evidence_items()].count("evidence_read") == 1


@pytest.mark.asyncio
async def test_independent_query_and_source_free_pending_note_remain_one_legal_batch(
    tmp_path, artifact_query_boundary,
):
    tools, artifact = note_tools(tmp_path, artifact_query_boundary)
    note = "Pending intent: inspect the query after its feedback arrives."

    def respond(request, turn):
        if turn == 1:
            return batch(
                ("query", "artifact_query", {
                    "artifact_id": str(artifact.artifact_id), "view_type": "TOP_N", "limit": 2,
                }),
                ("pending-note", "working_note_write", {"text": note}),
            )
        results = feedback(request)
        assert results["query"]["success"]
        saved = results["pending-note"]
        assert saved["success"] and saved["data"]["text"] == note
        assert saved["data"]["source_observation_ids"] == []
        assert saved["information_authority"] == "MODEL_CONTEXT"
        assert saved["data"]["source_read_credit"] is False
        return ModelTurn(content="Independent calls completed.", finish_reason=FinishReason.STOP)

    provider = ContextCheckingProvider(respond)
    loop, host = new_loop(tools.binding, tools.services, provider, max_request_bytes=80_000)
    result = await loop.run(
        messages=(PublicMessage(role="USER", content="Exercise independent batching."),),
        deadline_monotonic=monotonic() + 60,
    )
    assert result.termination == "MODEL_COMPLETED"
    assert len(provider.requests) == 2
    assert [item.capability_name for item in host.evidence_items()] == [
        "artifact_query", "working_note_write",
    ]
