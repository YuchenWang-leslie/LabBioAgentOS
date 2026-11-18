"""Capacity snapshots expose actual constraints without selecting evidence."""

import json
from dataclasses import replace
from uuid import uuid4

import pytest

from labbioagentos.runtime.contracts import RuntimeReference
from labbioagentos.runtime.contracts import HandoffObservation
from labbioagentos.runtime.observations import canonical_bytes
from labbioagentos import WorkflowStage
from labbioagentos.kernel import FinishReason, ModelTurn, TokenUsage, ToolCall
from labbioagentos.runtime.assembly import RuntimeStageAssemblySpec
from labbioagentos.runtime.native_stage import NativeKernelStageInvoker, NativeRuntimeFactory
from test_finalization_completion_delivery import finalization_case
from test_native_stage_application import WorkflowProvider, _capability_catalog, _stage_input, _toolset
from test_runtime_evidence_handoff import tools_with_store
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401


@pytest.mark.asyncio
async def test_capacity_identifies_control_overflow_even_when_selected_body_fits(tmp_path):
    stage, evidence, invoker, calls = await finalization_case(
        tmp_path, "Agent completion", prepared=True,
    )
    stage = stage.model_copy(update={"model_context_references": tuple(
        RuntimeReference(reference_id=f"engineering:{index}", kind="OTHER", label="x" * 1000)
        for index in range(64)
    )})
    original = evidence.model_dump_json()
    status = invoker._handoff_capacity_status(stage, evidence)
    control = status["finalization_control"]
    assert control["serialized_bytes"] > control["max_bytes"]
    assert control["over_by_bytes"] == control["serialized_bytes"] - control["max_bytes"]
    assert control["component_bytes"]["stage_input"] > 60_000
    assert status["prepared_handoff"]["status"] == "READY"
    assert status["prepared_handoff"]["over_by_bytes"] == 0
    assert not status["within_message_limits"]
    assert "Agent completion" not in json.dumps(status)
    assert evidence.model_dump_json() == original
    assert not calls


@pytest.mark.asyncio
async def test_capacity_reports_exact_unicode_bytes_and_provenance_width(tmp_path):
    stage, evidence, invoker, calls = await finalization_case(
        tmp_path, "完成情况：已检查", prepared=True,
    )
    references = 5
    extra = 39 * references - 1
    evidence = evidence.model_copy(update={"prepared_handoff":
        evidence.prepared_handoff.model_copy(update={"summary": "选中观察；其他范围未检查"})})
    status = invoker._handoff_capacity_status(stage, evidence, additional_provenance_bytes=extra)
    _, payload, _, content = invoker._finalization_envelope(
        stage, evidence, additional_provenance_bytes=extra,
    )
    control = status["finalization_control"]
    assert control["serialized_bytes"] == len(content.encode("utf-8")) + extra
    assert control["framing_bytes"] + sum(control["component_bytes"].values()) == control["serialized_bytes"]
    assert control["component_bytes"]["capability_evidence"] == len(json.dumps(
        payload["capability_evidence"], ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")) + extra
    assert status["prepared_handoff"]["serialized_bytes"] == len(
        evidence.prepared_handoff.model_dump_json(exclude_none=True).encode("utf-8"))
    assert status["completion_content_bytes"] == len(evidence.explicit_completion.encode("utf-8"))
    assert status["within_message_limits"]
    assert not calls


@pytest.mark.asyncio
async def test_handoff_status_query_never_selects_or_clears_evidence(tmp_path, artifact_query_boundary):
    tools, ref = tools_with_store(tmp_path, artifact_query_boundary)
    tools.binding = replace(tools.binding, capability_allowlist=(
        *tools.binding.capability_allowlist, "handoff_status"))
    initial = await tools.handoff_status()
    assert initial["success"]
    assert initial["data"]["prepared_handoff"]["status"] == "NOT_PREPARED"
    assert initial["data"]["finalization_control"] is None
    response = await tools.artifact_query(str(ref.artifact_id), "TOP_N", 2)
    tools.mark_feedback_delivered((response,))
    await tools.prepare_handoff([response["capability_invocation_id"]], "Agent chosen scope")
    original = tools.prepared_handoff.model_dump_json()
    status = await tools.handoff_status()
    assert status["success"]
    assert status["data"]["prepared_handoff"]["observation_ids"] == [response["capability_invocation_id"]]
    assert tools.prepared_handoff.model_dump_json() == original
    assert tools.evidence_items()[-1].capability_name == "handoff_status"


@pytest.mark.asyncio
async def test_handoff_status_does_not_bypass_tool_permission(tmp_path, artifact_query_boundary):
    tools, _ = tools_with_store(tmp_path, artifact_query_boundary)
    response = await tools.handoff_status()
    assert not response["success"]
    assert response["error"]["error_code"] == "AUTHORIZATION_DENIED"
    assert tools.prepared_handoff is None


@pytest.mark.asyncio
async def test_preselection_sizes_cover_only_actually_delivered_originals(tmp_path, artifact_query_boundary):
    tools, ref = tools_with_store(tmp_path, artifact_query_boundary)
    tools.binding = replace(tools.binding, capability_allowlist=(*tools.binding.capability_allowlist, 'handoff_status'))
    response = await tools.artifact_query(str(ref.artifact_id), 'TOP_N', 2)
    before = await tools.handoff_status()
    assert before['data']['available_observations'] == []
    tools.mark_feedback_delivered((response,))
    after = await tools.handoff_status()
    rows = after['data']['available_observations']
    assert len(rows) == 1
    row = rows[0]
    item = tools._delivered_observations[next(iter(tools._delivered_observations))]
    original = HandoffObservation(observation_id=item.capability_invocation_id,
        capability_name=item.capability_name, information_authority=item.information_authority,
        public_response=response, artifact_read_receipt=item.artifact_read_receipt)
    assert row['observation_id'] == response['capability_invocation_id']
    assert row['handoff_item_bytes'] == len(canonical_bytes(original.model_dump(mode='json', exclude_none=True)))
    assert 'safe_result' not in row and 'public_response' not in row
    assert after['data']['catalog_source_read_credit'] is False
    assert tools.prepared_handoff is None and len(tools._delivered_observations) == 1


@pytest.mark.asyncio
async def test_preselection_catalog_does_not_disclose_revoked_source_sizes(tmp_path, artifact_query_boundary, monkeypatch):
    tools, ref = tools_with_store(tmp_path, artifact_query_boundary)
    tools.binding = replace(tools.binding, capability_allowlist=(*tools.binding.capability_allowlist, 'handoff_status'))
    response = await tools.artifact_query(str(ref.artifact_id), 'TOP_N', 2)
    tools.mark_feedback_delivered((response,))
    policy = tools.services.artifact_exposure.policy
    original = policy.decide
    monkeypatch.setattr(type(policy), 'decide', lambda self, *args: original(*args).model_copy(update={'allowed': False}))
    status = await tools.handoff_status()
    assert status['success']
    assert status['data']['available_observations'] == [{
        'observation_id': response['capability_invocation_id'], 'status': 'UNAVAILABLE', 'handoff_item_bytes': None}]


@pytest.mark.asyncio
async def test_model_can_query_capacity_then_maintain_its_own_selection(tmp_path):
    stage = _stage_input().model_copy(update={
        "allowed_capabilities": ("artifact_list", "handoff_status", "prepare_handoff"),
    })
    tools = _toolset(tmp_path, stage)

    class CapabilityProvider:
        binding = WorkflowProvider.binding
        calls = 0
        selected = None

        async def generate(self, request):
            self.calls += 1
            if self.calls == 1:
                name, arguments = "artifact_list", {}
            elif self.calls == 2:
                response = json.loads(request.messages[-1].content)["content"]
                self.selected = response["capability_invocation_id"]
                name, arguments = "handoff_status", {}
            elif self.calls == 3:
                status = json.loads(request.messages[-1].content)["content"]["data"]
                assert status["snapshot"] == "BEFORE_THIS_STATUS_RESPONSE"
                assert status["prepared_handoff"]["status"] == "NOT_PREPARED"
                assert any(row['observation_id'] == self.selected and row['handoff_item_bytes'] > 0
                           for row in status['available_observations'])
                assert status['catalog_source_read_credit'] is False
                assert status["finalization_control"]["serialized_bytes"] > 0
                assert status["request_admission"] == "SNAPSHOT_NOT_RESERVATION"
                assert status["complete_request"]["snapshot"]["public_output_schema_bytes"] > 0
                name, arguments = "prepare_handoff", {
                    "observation_ids": [self.selected], "summary": "Agent selected this listing.",
                }
            elif self.calls == 4:
                name, arguments = "handoff_status", {}
            else:
                status = json.loads(request.messages[-1].content)["content"]["data"]
                assert status["prepared_handoff"]["observation_ids"] == [self.selected]
                assert status["prepared_handoff"]["status"] == "READY"
                assert status["within_message_limits"]
                assert status["completion_content_bytes"] == 0
                return ModelTurn(content="Agent completed the bounded task.",
                                 finish_reason=FinishReason.STOP, usage=TokenUsage(total_tokens=1))
            return ModelTurn(tool_calls=(ToolCall(
                call_id=f"step-{self.calls}", name=name, arguments_json=json.dumps(arguments),
            ),), finish_reason=FinishReason.TOOL_CALLS, usage=TokenUsage(total_tokens=1))

    capability = CapabilityProvider()
    final_calls = []
    catalog = _capability_catalog()
    catalog.capabilities["coordinator-capabilities"] = catalog.capabilities[
        "coordinator-capabilities"].model_copy(update={"capability_allowlist": stage.allowed_capabilities})
    invoker = NativeKernelStageInvoker(
        assembly=RuntimeStageAssemblySpec(
            stage_id=WorkflowStage.INTAKE, root_profile_key="coordinator",
            prompt_template_key="runtime-generic", capability_allowlist=stage.allowed_capabilities,
            required_capabilities=("artifact_list",), max_capability_turns=6,
        ),
        factory=NativeRuntimeFactory(catalog, lambda _model:
            capability if not capability.calls else WorkflowProvider(final_calls)),
        principal=tools.binding.principal, workspace=tools.binding.workspace, services=tools.services,
    )
    result = await invoker.invoke(stage)
    assert result.stage_id is WorkflowStage.INTAKE
    assert capability.calls == 5
    assert len(final_calls) == 1
    assert final_calls[0].tools == ()
    delivered = json.loads(final_calls[0].messages[-2].content)
    assert delivered["summary"] == "Agent selected this listing."
    assert [item["observation_id"] for item in delivered["observations"]] == [capability.selected]
