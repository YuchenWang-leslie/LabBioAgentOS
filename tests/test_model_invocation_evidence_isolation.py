"""Fresh model invocations must actually reread before reusing observations."""

from dataclasses import replace
import json
from time import monotonic
from uuid import UUID, uuid4

import pytest

from labbioagentos.kernel import (
    AgentProfile, AgentRegistry, AgentSpec, BudgetLedger, CollaborationRuntime,
    FakeProvider, FinishReason, ModelResultProjector, ModelTurn, PublicMessage,
    TeamSpec, ToolCall, ToolRegistry,
)
from labbioagentos.runtime.native_tools import NativeRuntimeCapabilityHost, build_labbio_tool_registry
from labbioagentos.runtime.observations import ObservationStore
from native_kernel.test_collaboration import _binding, _identity, _limits
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401
from test_report_scoped_evidence import report_tools
from test_runtime_evidence_handoff import tools_with_store


@pytest.mark.asyncio
async def test_new_invocation_resets_read_state_but_keeps_audit_and_archive(
    tmp_path, artifact_query_boundary,
):
    tools, ref = tools_with_store(tmp_path, artifact_query_boundary)
    first_id, second_id = uuid4(), uuid4()
    tools.begin_model_invocation(first_id)
    response = await tools.artifact_query(str(ref.artifact_id), "TOP_N", 2)
    tools.mark_feedback_delivered((response,))
    identity = response["capability_invocation_id"]
    await tools.prepare_handoff([identity], "First invocation assessment.")
    first_page = await tools.evidence_read(identity, limit=20)
    tools.mark_feedback_delivered((first_page,))
    await tools.evidence_read(identity, offset=20)
    tools.stop_reason = "REPEATED_NON_RETRYABLE_REQUEST"
    before = tools.evidence_items()
    tools.begin_model_invocation(second_id)
    assert not tools._delivered_observations
    assert not tools._observation_read_ranges
    assert not tools._pending_observation_pages
    assert tools.prepared_handoff is None
    assert tools.stop_reason is None and tools._feedback_boundary is None
    assert tools.evidence_items() == before
    assert tools.services.observation_store.load(tools.binding, UUID(identity)).public_response == response
    tools.mark_feedback_delivered((response, first_page))
    assert not tools._delivered_observations  # A late old callback cannot restore credit.
    fresh = await tools.artifact_query(str(ref.artifact_id), "SUMMARY")
    tools.mark_feedback_delivered((fresh,))
    tools.begin_model_invocation(second_id)  # Repeated start notification is idempotent.
    assert UUID(fresh["capability_invocation_id"]) in tools._delivered_observations
    assert response["artifact_read_receipt"]["source_invocation_id"] == str(first_id)
    assert fresh["artifact_read_receipt"]["source_invocation_id"] == str(second_id)


@pytest.mark.asyncio
async def test_report_service_requires_host_bound_delivery_identity(artifact_query_boundary):
    tools, ref, _ = report_tools(artifact_query_boundary, real_service=True)
    model_invocation_id = uuid4()
    tools.begin_model_invocation(model_invocation_id)
    response = await tools.artifact_query(str(ref.artifact_id), "SUMMARY")
    receipt = tools.evidence_items()[-1].artifact_read_receipt
    arguments = dict(title="Synthetic", report_text="Synthetic",
        evidence_artifact_ids=(ref.artifact_id,), principal=tools.binding.principal,
        workspace=tools.binding.workspace, run_id=tools.binding.run_id,
        stage_id=tools.binding.stage_id, invocation_id=tools.binding.invocation_id,
        evidence_read_receipts=(receipt,))
    for delivery_id in (None, uuid4()):
        with pytest.raises(ValueError, match="invocation"):
            tools.services.report_submission.submit(
                **arguments, receipt_delivery_invocation_id=delivery_id)
    tools.mark_feedback_delivered((response,))
    report = await tools.report_submit("Synthetic", "A cited summary.",
        evidence_references=[{"artifact_id": str(ref.artifact_id),
                              "observation_ids": [response["capability_invocation_id"]]}])
    assert report["success"]
    saved = tools.services.artifact_store.load_for_view(report["data"]["report_artifact_id"])
    assert saved.ref.producer_invocation_id == tools.binding.invocation_id
    assert saved.representation.summary["model_invocation_id"] == str(model_invocation_id)


def call(name, arguments):
    return ModelTurn(tool_calls=(ToolCall(call_id=name, name=name,
        arguments_json=json.dumps(arguments)),), finish_reason=FinishReason.TOOL_CALLS)


@pytest.mark.asyncio
async def test_two_delegations_require_redelivery_and_preserve_workflow_report_identity(
    tmp_path, artifact_query_boundary,
):
    tools, ref, _ = report_tools(artifact_query_boundary, real_service=True)
    binding = replace(tools.binding, capability_allowlist=(
        *tools.binding.capability_allowlist, "evidence_read", "prepare_handoff"))
    services = replace(tools.services, observation_store=ObservationStore(tmp_path / "observations"))
    host = NativeRuntimeCapabilityHost(binding, services)

    class Specialist:
        binding = _binding()

        def __init__(self):
            self.invocations = []
            self.replies = {}
            self.source = None
            self.second_started_with_prepared = None

        async def generate(self, request):
            if request.invocation_id not in self.invocations:
                self.invocations.append(request.invocation_id)
            number = len(self.invocations)
            previous = next((json.loads(message.content)["content"]
                for message in reversed(request.messages) if message.role == "TOOL"), None)
            if number == 1:
                if request.turn_index == 1:
                    return call("artifact_aggregate", {"artifact_id": str(ref.artifact_id), "operation": "COUNT"})
                if request.turn_index == 2:
                    self.source = previous
                    return call("prepare_handoff", {
                        "observation_ids": [previous["capability_invocation_id"]], "summary": "First assignment."})
                return ModelTurn(content="First assignment completed.", finish_reason=FinishReason.STOP)
            identity = self.source["capability_invocation_id"]
            references = [{"artifact_id": str(ref.artifact_id), "observation_ids": [identity]}]
            if request.turn_index == 1:
                self.second_started_with_prepared = host.prepared_handoff
                return call("report_check", {"evidence_references": references})
            self.replies[request.turn_index - 1] = previous
            if request.turn_index == 2:
                return call("prepare_handoff", {"observation_ids": [identity], "summary": "Not read here."})
            if request.turn_index == 3:
                return call("evidence_read", {"observation_id": identity})
            if request.turn_index == 4:
                return call("prepare_handoff", {"observation_ids": [identity], "summary": "Actually reread here."})
            if request.turn_index == 5:
                return call("report_submit", {"title": "Synthetic report",
                    "report_text": "Only the reread mechanical count is cited.",
                    "evidence_references": references})
            return ModelTurn(content="Report registered.", finish_reason=FinishReason.STOP)

    specialist = Specialist()
    leader = AgentSpec(agent_id="leader", role="LEADER")
    writer = AgentSpec(agent_id="writer", role="SPECIALIST", tool_names=binding.capability_allowlist)
    team = TeamSpec(leader=leader, specialists=(writer,))
    leader_provider = FakeProvider(binding=_binding(), turns=(
        call("delegate", {"target_agent_id": "writer", "instruction": "First assignment."}),
        call("delegate", {"target_agent_id": "writer", "instruction": "Independent second assignment."}),
        ModelTurn(content="Done.", finish_reason=FinishReason.STOP),
    ))
    registry = AgentRegistry(team=team, profiles=tuple(
        AgentProfile(spec=spec, description="Synthetic fixture.", instructions="Use declared tools.")
        for spec in (leader, writer)), providers={"leader": leader_provider, "writer": specialist})
    runtime = CollaborationRuntime(
        registry=registry, tool_registries={"leader": ToolRegistry(), "writer": build_labbio_tool_registry(host)},
        tool_batch_observers={"writer": host.mark_feedback_delivered},
        tool_invocation_starts={"writer": host.begin_model_invocation},
        projector=ModelResultProjector(max_bytes=60_000), budget_ledger=BudgetLedger(_limits()),
    )
    result = await runtime.run_leader(identity=_identity(),
        messages=(PublicMessage(role="USER", content="Two independent fixture assignments."),),
        deadline_monotonic=monotonic() + 60)
    assert [task.state.value for task in result.tasks] == ["SUCCEEDED", "SUCCEEDED"]
    assert len(specialist.invocations) == 2
    assert specialist.second_started_with_prepared is None
    assert specialist.replies[1]["data"]["items"][0]["status"] == "REPORT_OBSERVATION_NOT_DELIVERED"
    assert not specialist.replies[2]["success"]
    assert specialist.replies[3]["data"]["complete_observation_delivered"]
    assert specialist.replies[4]["data"]["status"] == "READY"
    assert specialist.replies[5]["success"]
    report = services.artifact_store.load_for_view(specialist.replies[5]["data"]["report_artifact_id"])
    citation = report.representation.summary["evidence_read_receipts"][0]
    assert citation["source_invocation_id"] == str(specialist.invocations[0])
    assert citation["delivered_invocation_id"] == str(specialist.invocations[1])
    assert report.ref.producer_invocation_id == binding.invocation_id
    assert report.representation.summary["model_invocation_id"] == str(specialist.invocations[1])
    assert len(host.evidence_items()) == 7  # Both assignments remain in the audit.
