"""Completion admission and finalization measure the same public envelope."""

import json
from uuid import uuid4

import pytest

from labbioagentos import CapabilityEvidenceBundle, CapabilityEvidenceItem, WorkflowStage
from labbioagentos.kernel import FinishReason, ModelTurn, TokenUsage, ToolCall
from labbioagentos.runtime import native_stage
from labbioagentos.runtime.assembly import RuntimeStageAssemblySpec
from labbioagentos.runtime.contracts import (
    CapabilityEvidenceStatus, RuntimeGateDecisionView, RuntimeWorkflowControlView,
)
from labbioagentos.runtime.handoff import finalization_evidence
from labbioagentos.runtime.tooling import LabBioRuntimeToolSet
from test_native_stage_application import WorkflowProvider, _capability_catalog, _stage_input, _toolset
from test_finalization_completion_delivery import finalization_case


@pytest.mark.asyncio
@pytest.mark.parametrize("target,needs_handoff", [(59_500, False), (59_990, True)])
async def test_completion_gets_handoff_feedback_before_finalizer_capacity_failure(
    tmp_path, monkeypatch, target, needs_handoff,
):
    stage = _stage_input().model_copy(update={
        "allowed_capabilities": ("artifact_list", "prepare_handoff"),
    })
    fixture = _toolset(tmp_path, stage)
    hosts = []
    original_host = native_stage.NativeRuntimeCapabilityHost

    def capture_host(*args):
        host = original_host(*args)
        hosts.append(host)
        return host

    async def large_engineering_observation(tools, offset=0, limit=20):
        size = 56_000
        for _ in range(3):
            data = {"engineering_segments": [
                chr(65 + index) * min(7000, size - index * 7000)
                for index in range((size + 6999) // 7000)
            ]}
            item = CapabilityEvidenceItem(
                actor_profile_key=tools.binding.actor_profile_key,
                actor_agent_name=tools.binding.actor_agent_name,
                capability_name="artifact_list",
                information_authority="AUTHORITATIVE_EVIDENCE",
                status=CapabilityEvidenceStatus.COMPLETED, safe_result=data,
            )
            provisional = CapabilityEvidenceBundle(
                run_id=stage.run_id, stage_id=stage.stage_id,
                invocation_id=stage.invocation_id, items=(item,),
            )
            encoded = json.dumps({
                "stage_input": stage.model_dump(mode="json"),
                "capability_evidence": finalization_evidence(provisional),
            }, ensure_ascii=False, separators=(",", ":")).encode()
            size += target - len(encoded)
        assert len(encoded) == target
        return await tools._call("artifact_list", lambda: data)

    monkeypatch.setattr(native_stage, "NativeRuntimeCapabilityHost", capture_host)
    monkeypatch.setattr(LabBioRuntimeToolSet, "artifact_list", large_engineering_observation)

    class CapabilityProvider:
        binding = WorkflowProvider.binding
        calls = 0

        async def generate(self, request):
            self.calls += 1
            if self.calls == 1:
                return ModelTurn(tool_calls=(ToolCall(
                    call_id="read", name="artifact_list", arguments_json="{}",
                ),), finish_reason=FinishReason.TOOL_CALLS, usage=TokenUsage(total_tokens=1))
            if self.calls == 3:
                feedback = json.loads(request.messages[-1].content)
                assert feedback["reason"] == "HANDOFF_REQUIRED"
                assert feedback["serialized_bytes"] > 60_000
                identity = str(hosts[0].evidence_items()[0].capability_invocation_id)
                return ModelTurn(tool_calls=(ToolCall(
                    call_id="selected", name="prepare_handoff",
                    arguments_json=json.dumps({
                        "observation_ids": [identity], "summary": "Synthetic observations selected.",
                    }),
                ),), finish_reason=FinishReason.TOOL_CALLS, usage=TokenUsage(total_tokens=1))
            return ModelTurn(content="x", finish_reason=FinishReason.STOP,
                             usage=TokenUsage(total_tokens=1))

    capability = CapabilityProvider()
    final_calls = []
    builders = []

    def build(_model):
        provider = capability if not builders else WorkflowProvider(final_calls)
        builders.append(provider)
        return provider

    catalog = _capability_catalog()
    catalog.capabilities["coordinator-capabilities"] = catalog.capabilities[
        "coordinator-capabilities"].model_copy(update={
            "capability_allowlist": stage.allowed_capabilities,
        })
    invoker = native_stage.NativeKernelStageInvoker(
        assembly=RuntimeStageAssemblySpec(
            stage_id=WorkflowStage.INTAKE, root_profile_key="coordinator",
            prompt_template_key="runtime-generic",
            capability_allowlist=stage.allowed_capabilities,
            required_capabilities=("artifact_list",),
            preserve_capability_completion=True, max_capability_turns=4,
        ),
        factory=native_stage.NativeRuntimeFactory(catalog, build),
        principal=fixture.binding.principal, workspace=fixture.binding.workspace,
        services=fixture.services,
    )
    envelopes = []
    assemble = invoker._finalization_envelope

    def capture_envelope(*args, **kwargs):
        value = assemble(*args, **kwargs)
        envelopes.append(value)
        return value

    monkeypatch.setattr(invoker, "_finalization_envelope", capture_envelope)
    result = await invoker.invoke(stage)
    assert result.stage_id is WorkflowStage.INTAKE
    assert capability.calls == (4 if needs_handoff else 2)
    assert len(final_calls) == 1
    assert bool(hosts[0].prepared_handoff) is needs_handoff
    if not needs_handoff:
        # Different real bundle UUIDs have equal width; every other field,
        # including provider turn counts and termination, is measured equally.
        assert len(envelopes[0][-1].encode()) == len(envelopes[-1][-1].encode())
        assert envelopes[0][1]["capability_evidence"]["provider_turn_count"] == 2
        assert envelopes[0][1]["capability_evidence"]["termination_reason"] == "MODEL_RETURNED"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage_id,capabilities,control_key", [
    (WorkflowStage.INTAKE, ("artifact_list",), None),
    (WorkflowStage.PLAN, ("skill_search",), "skill_retrieval_control"),
    (WorkflowStage.EXECUTE, ("execution_submit",), "execution_result_control"),
    (WorkflowStage.REPORT, ("report_submit",), "report_result_control"),
])
@pytest.mark.parametrize("prepared", [False, True])
@pytest.mark.parametrize("references", [0, 2, 6])
async def test_envelope_includes_effective_control_and_exact_delegation_width(
    tmp_path, stage_id, capabilities, control_key, prepared, references,
):
    stage, evidence, invoker, provider_calls = await finalization_case(
        tmp_path, "Exact preceding assessment.", prepared=prepared,
    )
    stage = stage.model_copy(update={
        "stage_id": stage_id,
        "allowed_capabilities": capabilities,
        "workflow_control": RuntimeWorkflowControlView(
            current_stage=stage_id, request_user_input_available=True,
            retry_available=False, finish_available=False,
        ),
        "gate_decisions": (RuntimeGateDecisionView(
            gate_id="resolved-gate", source_stage=stage_id, approved=True,
            domain_reference_id="synthetic:approval",
        ),),
    })
    evidence = evidence.model_copy(update={
        "stage_id": stage_id, "provider_turn_count": 12,
        "provider_turn_limit": 24, "termination_reason": "MODEL_RETURNED",
    })
    reference_bytes = 39 * references - 1 if references else 0
    effective, preview, messages, content = invoker._finalization_envelope(
        stage, evidence, additional_provenance_bytes=reference_bytes,
    )
    real_ids = tuple(uuid4() for _ in range(references))
    _actual, actual, actual_messages, actual_content = invoker._finalization_envelope(
        stage, evidence.model_copy(update={"delegation_trace_event_ids": real_ids}),
    )
    assert effective.workflow_control.request_user_input_available is False
    assert preview["stage_input"]["workflow_control"]["request_user_input_available"] is False
    assert preview["capability_evidence"]["provider_turn_count"] == 12
    assert preview["capability_evidence"]["provider_turn_limit"] == 24
    assert preview["capability_evidence"]["delegation_trace_event_ids"] == []
    assert actual["capability_evidence"]["delegation_trace_event_ids"] == list(map(str, real_ids))
    assert len(content.encode()) + reference_bytes == len(actual_content.encode())
    assert messages == actual_messages
    assert preview["explicit_completion_control"]["character_count"] == len(evidence.explicit_completion)
    assert bool(preview.get("prepared_handoff_control")) is prepared
    if control_key is not None:
        assert preview[control_key] == actual[control_key]
        assert preview[control_key]["authority"] == "CONTROL_STATE"
    assert not provider_calls
