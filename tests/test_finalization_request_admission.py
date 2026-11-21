"""First finalizer-request admission is visible before capability tools close."""

from dataclasses import replace
import json
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ConfigDict

from labbioagentos import PromptProfile, WorkflowStage
from labbioagentos.kernel import FinishReason, ModelTurn, TokenUsage, ToolCall
from labbioagentos.kernel.request_metrics import measure_request_size
from labbioagentos.kernel.tooling import model_json_schema
from labbioagentos.runtime.assembly import RuntimeStageAssemblySpec
from labbioagentos.runtime.native_stage import NativeKernelStageInvoker, NativeRuntimeFactory
from labbioagentos.runtime.tooling import LabBioRuntimeToolSet
from test_finalization_completion_delivery import finalization_case
from test_native_stage_application import WorkflowProvider, _capability_catalog, _stage_input, _toolset


@pytest.mark.asyncio
async def test_full_request_feedback_keeps_tools_available_for_agent_selected_narrower_handoff(
    tmp_path, monkeypatch,
):
    stage = _stage_input().model_copy(update={"allowed_capabilities": (
        "artifact_list", "prepare_handoff", "handoff_status")})
    fixture = _toolset(tmp_path, stage)

    async def engineering_records(tools, offset=0, limit=20):
        return await tools._call("artifact_list", lambda: {"engineering_segments":
            ["x" * 7000 for _ in range(6)] if limit != 1 else ["narrow inspected scope"]})

    monkeypatch.setattr(LabBioRuntimeToolSet, "artifact_list", engineering_records)
    catalog = _capability_catalog()
    catalog.prompts["runtime-generic"] = PromptProfile(template_id="runtime-generic",
        version="engineering", template_text="{protocol}", max_value_length=8000)
    catalog.capabilities["coordinator-capabilities"] = catalog.capabilities[
        "coordinator-capabilities"].model_copy(update={"capability_allowlist": stage.allowed_capabilities})
    original_schema = catalog.schemas["runtime-stage-result"]

    def large_response_format(*args):
        base = original_schema.response_format(*args)
        class EngineeringSchema(base):
            model_config = ConfigDict(json_schema_extra={"description": "结构" * 2000})
        return EngineeringSchema

    catalog.schemas["runtime-stage-result"] = SimpleNamespace(response_format=large_response_format)

    class CapabilityProvider:
        binding = WorkflowProvider.binding
        calls = 0
        selected = None
        feedback = None

        async def generate(self, request):
            self.calls += 1
            if self.calls in (1, 5):
                name, arguments = "artifact_list", {"limit": 1} if self.calls == 5 else {}
            elif self.calls in (2, 6):
                observed = json.loads(request.messages[-1].content)["content"]
                self.selected = observed["capability_invocation_id"]
                name, arguments = "prepare_handoff", {"observation_ids": [self.selected],
                    "summary": "Agent-selected scope, no whole-table claim."}
            elif self.calls == 3:
                name, arguments = "handoff_status", {}
            elif self.calls == 4:
                status = json.loads(request.messages[-1].content)["content"]["data"]
                assert status["within_message_limits"]
                return ModelTurn(content="续" * 6000, finish_reason=FinishReason.STOP,
                                 usage=TokenUsage(total_tokens=1))
            elif self.calls == 7:
                return ModelTurn(content="Agent completed the narrower inspected scope.",
                    finish_reason=FinishReason.STOP, usage=TokenUsage(total_tokens=1))
            else:
                raise AssertionError("Unexpected provider call")
            if self.calls == 5:
                self.feedback = json.loads(request.messages[-1].content)
                assert self.feedback["reason"] == "FINALIZATION_REQUEST_TOO_LARGE"
                capacity = self.feedback["capacity"]
                assert capacity["within_message_limits"]
                full = capacity["complete_request"]
                assert full["public_request_bytes_upper_bound"] > 80_000
                assert full["snapshot"]["public_output_schema_bytes"] > 12_000
                assert full["over_by_bytes"] > 0
            return ModelTurn(tool_calls=(ToolCall(call_id=f"step-{self.calls}", name=name,
                arguments_json=json.dumps(arguments)),), finish_reason=FinishReason.TOOL_CALLS,
                usage=TokenUsage(total_tokens=1))

    capability = CapabilityProvider()
    final_calls = []
    built = []

    def build(_model):
        provider = capability if not built else WorkflowProvider(final_calls)
        built.append(provider)
        return provider

    invoker = NativeKernelStageInvoker(assembly=RuntimeStageAssemblySpec(
        stage_id=WorkflowStage.INTAKE, root_profile_key="coordinator",
        prompt_template_key="runtime-generic", capability_allowlist=stage.allowed_capabilities,
        capability_prompt_values={"protocol": "Capability engineering fixture."},
        finalization_prompt_values={"protocol": "证" * 4000},
        preserve_capability_completion=True, max_capability_turns=8, max_request_bytes=80_000),
        factory=NativeRuntimeFactory(catalog, build), principal=fixture.binding.principal,
        workspace=fixture.binding.workspace, services=fixture.services)
    result = await invoker.invoke(stage)
    assert result.stage_id is WorkflowStage.INTAKE
    assert capability.feedback is not None and capability.calls == 7
    assert len(built) == 2 and len(final_calls) == 1
    final = final_calls[0]
    assert final.tools == ()
    assert final.messages[0].content == "证" * 4000
    assert final.response_schema["description"] == "结构" * 2000
    assert measure_request_size(final).public_request_bytes <= 80_000
    selected = json.loads(final.messages[-2].content)
    assert [item["observation_id"] for item in selected["observations"]] == [capability.selected]
    assert selected["observations"][0]["public_response"]["data"] == {
        "engineering_segments": ["narrow inspected scope"]}


@pytest.mark.asyncio
async def test_private_capacity_without_resolved_provider_does_not_guess_full_request(tmp_path):
    stage, evidence, invoker, calls = await finalization_case(tmp_path, "Synthetic.", prepared=True)
    status = invoker._handoff_capacity_status(stage, evidence)
    assert status["complete_request"]["status"] == "UNAVAILABLE"
    assert status["within_request_limits"] is None
    assert not calls


@pytest.mark.asyncio
async def test_illegal_control_message_still_returns_capacity_feedback_without_constructing_invalid_request(tmp_path):
    from labbioagentos.runtime.contracts import RuntimeReference
    stage, evidence, invoker, calls = await finalization_case(tmp_path, "Synthetic.", prepared=True)
    stage = stage.model_copy(update={"model_context_references": tuple(
        RuntimeReference(reference_id=f"engineering:{index}", kind="OTHER", label="x" * 1000)
        for index in range(64))})
    resolved = invoker.factory.resolve(invoker.assembly.root_profile_key,
                                      invoker.assembly.finalization_prompt_values)
    status = invoker._handoff_capacity_status(stage, evidence, resolved_finalization=resolved)
    assert not status["within_message_limits"]
    assert status["finalization_control"]["over_by_bytes"] > 0
    assert status["complete_request"]["status"] == "UNAVAILABLE"
    assert status["complete_request"]["reason"] == "MESSAGE_LIMIT_EXCEEDED"
    assert not calls


@pytest.mark.asyncio
async def test_complete_request_threshold_uses_labeled_upper_bound_and_unknown_wire_stays_null(tmp_path):
    stage, evidence, invoker, calls = await finalization_case(tmp_path, "完成情况", prepared=True)
    resolved = invoker.factory.resolve(invoker.assembly.root_profile_key,
                                      invoker.assembly.finalization_prompt_values)
    status = invoker._handoff_capacity_status(stage, evidence, resolved_finalization=resolved)
    full = status["complete_request"]
    sizes = full["snapshot"]
    assert sizes["wire_request_bytes"] is None and full["wire_request_bytes_upper_bound"] is None
    assert sum(sizes[key] for key in ("public_messages_bytes", "public_tool_declarations_bytes",
        "public_output_schema_bytes", "public_envelope_bytes")) == sizes["public_request_bytes"]
    assert full["deadline_width_max_bytes"] == 24
    assert full["public_request_bytes_upper_bound"] == sizes["public_request_bytes"] + (
        24 - full["deadline_width_bytes"])
    invoker.assembly = replace(invoker.assembly, max_request_bytes=full["public_request_bytes_upper_bound"])
    assert invoker._handoff_capacity_status(stage, evidence,
        resolved_finalization=resolved)["within_request_limits"]
    invoker.assembly = replace(invoker.assembly, max_request_bytes=full["public_request_bytes_upper_bound"] - 1)
    rejected = invoker._handoff_capacity_status(stage, evidence, resolved_finalization=resolved)
    assert rejected["within_request_limits"] is False
    assert rejected["complete_request"]["over_by_bytes"] == 1
    assert not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("deadline", [1.0, 0.00012345678901234567, 1.2345678901234567e-308,
    sys.float_info.max, sys.float_info.min, 12345678.123456789])
async def test_positive_finite_deadline_json_width_is_within_explicit_reservation(tmp_path, deadline):
    from labbioagentos.kernel import ModelRequest, PublicMessage
    request = ModelRequest(binding=WorkflowProvider.binding,
        messages=(PublicMessage(role="SYSTEM", content="Synthetic."),), max_output_tokens=8192,
        deadline_monotonic=deadline, kernel_run_id=uuid4(), invocation_id=uuid4(), turn_index=1)
    encoded = request.model_dump_json(include={"deadline_monotonic"}).encode()
    width = len(encoded) - len(b'{"deadline_monotonic":}')
    assert 0 < width <= 24


@pytest.mark.asyncio
@pytest.mark.parametrize("references", [0, 2, 6])
@pytest.mark.parametrize("provider_type,transport", [
    ("OpenAICompatibleChatProvider", "OPENAI_COMPATIBLE_CHAT"),
    ("OpenAIResponsesProvider", "OPENAI_RESPONSES"),
    ("AnthropicMessagesProvider", "ANTHROPIC_MESSAGES"),
    ("GeminiGenerateContentProvider", "GEMINI_GENERATE_CONTENT"),
    ("GeminiInteractionsProvider", "GEMINI_INTERACTIONS"),
])
async def test_builtin_wire_and_public_provenance_account_for_nested_json_escaping(
    tmp_path, references, provider_type, transport,
):
    import labbioagentos.kernel as kernel
    stage, evidence, invoker, calls = await finalization_case(tmp_path, "完成：证据范围有限", prepared=True)
    dialect = {"kind": transport}
    if transport == "ANTHROPIC_MESSAGES":
        dialect["thinking_off"] = "DISABLED"
    elif transport in {"GEMINI_GENERATE_CONTENT", "GEMINI_INTERACTIONS"}:
        dialect.update(thinking_level_enabled="high", thinking_level_disabled="low")
    binding = kernel.ProviderBinding(provider="fixture", model="fixture-model",
        endpoint="https://provider.invalid/v1", transport=transport, dialect=dialect,
        thinking_mode="DISABLED")
    provider = getattr(kernel, provider_type)(binding=binding,
        api_key="synthetic-never-sent", transport=object())
    resolved = (*invoker.factory.resolve(invoker.assembly.root_profile_key,
        invoker.assembly.finalization_prompt_values)[:4], provider)
    raw_extra = 39 * references - 1 if references else 0
    status = invoker._handoff_capacity_status(stage, evidence,
        resolved_finalization=resolved, additional_provenance_bytes=raw_extra)
    full = status["complete_request"]
    assert full["additional_provenance_public_bytes"] == raw_extra + 2 * references
    actual_evidence = evidence.model_copy(update={"delegation_trace_event_ids":
        tuple(uuid4() for _ in range(references))})
    effective, payload, messages, content = invoker._finalization_envelope(stage, actual_evidence)
    response_format, messages = invoker._finalization_model_input(
        effective, payload, messages, content, resolved)
    request = kernel.ModelRequest(binding=provider.binding, messages=messages,
        response_schema=model_json_schema(response_format), max_output_tokens=resolved[2].max_output_tokens or 8192,
        deadline_monotonic=sys.float_info.max, kernel_run_id=uuid4(), invocation_id=uuid4(), turn_index=1)
    actual = measure_request_size(request, provider)
    assert actual.public_request_bytes <= full["public_request_bytes_upper_bound"]
    assert actual.public_messages_bytes == full["snapshot"]["public_messages_bytes"] + raw_extra + 2 * references
    assert actual.wire_request_bytes == full["wire_request_bytes_upper_bound"]
    assert full["wire_bound_status"] == "JSON_TEXT_FIRST_REQUEST"
    assert not calls


@pytest.mark.asyncio
async def test_opaque_wire_snapshot_is_not_forgotten_when_future_provenance_is_unknown(tmp_path):
    stage, evidence, invoker, calls = await finalization_case(tmp_path, "Synthetic.", prepared=True)
    class OpaqueWireProvider(WorkflowProvider):
        def request_size_bytes(self, request):
            return 90_000
    provider = OpaqueWireProvider(calls)
    resolved = (*invoker.factory.resolve(invoker.assembly.root_profile_key,
        invoker.assembly.finalization_prompt_values)[:4], provider)
    invoker.assembly = replace(invoker.assembly, max_request_bytes=80_000)
    status = invoker._handoff_capacity_status(stage, evidence,
        resolved_finalization=resolved, additional_provenance_bytes=38)
    full = status["complete_request"]
    assert full["snapshot"]["wire_request_bytes"] == 90_000
    assert full["public_request_bytes_upper_bound"] < 80_000
    assert full["wire_request_bytes_upper_bound"] is None
    assert full["wire_provenance_delta_bytes"] is None
    assert full["wire_bound_status"] == "SNAPSHOT_ONLY"
    assert full["checked_request_bytes"] == 90_000
    assert not status["within_request_limits"] and full["over_by_bytes"] == 10_000
    assert not calls


@pytest.mark.asyncio
async def test_overridden_builtin_serializer_is_not_misclassified_as_proven_wire_mapping(tmp_path):
    from labbioagentos.kernel import OpenAICompatibleChatProvider
    stage, evidence, invoker, _calls = await finalization_case(tmp_path, "Synthetic.", prepared=True)
    class Override(OpenAICompatibleChatProvider):
        def request_size_bytes(self, request):
            return 12345
    provider = Override(binding=WorkflowProvider.binding, api_key="synthetic-never-sent", transport=object())
    resolved = (*invoker.factory.resolve(invoker.assembly.root_profile_key,
        invoker.assembly.finalization_prompt_values)[:4], provider)
    full = invoker._handoff_capacity_status(stage, evidence,
        resolved_finalization=resolved)["complete_request"]
    assert full["snapshot"]["wire_request_bytes"] == 12345
    assert full["wire_request_bytes_upper_bound"] is None
    assert full["wire_bound_status"] == "SNAPSHOT_ONLY"


@pytest.mark.asyncio
@pytest.mark.parametrize("stage_id,capabilities", [
    (WorkflowStage.PLAN, ("skill_search",)),
    (WorkflowStage.EXECUTE, ("execution_submit",)),
    (WorkflowStage.REPORT, ("report_submit",)),
])
async def test_snapshot_and_final_send_share_current_dynamic_schema(tmp_path, stage_id, capabilities):
    stage, evidence, invoker, calls = await finalization_case(tmp_path, "Synthetic.", prepared=True)
    stage = stage.model_copy(update={"stage_id": stage_id, "allowed_capabilities": capabilities})
    evidence = evidence.model_copy(update={"stage_id": stage_id})
    invoker.assembly = replace(invoker.assembly, stage_id=stage_id, capability_allowlist=capabilities)

    class StageProvider(WorkflowProvider):
        async def generate(self, request):
            turn = await super().generate(request)
            content = json.loads(turn.content)
            if stage_id is WorkflowStage.PLAN:
                content["body"]["skill_assessment"] = {"status": "NOT_ASSESSED",
                    "reason": "No fixture search completed.", "search_capability_invocation_ids": [],
                    "proposal_id": None}
            elif stage_id is WorkflowStage.REPORT:
                content = {"summary": "Synthetic compact handoff.", "report_artifact_id": None,
                           "next_action": content["next_action"]}
            return turn.model_copy(update={"content": json.dumps(content)})

    invoker.factory.provider_builder = lambda _model: StageProvider(calls)
    resolved = invoker.factory.resolve(invoker.assembly.root_profile_key,
                                      invoker.assembly.finalization_prompt_values)
    before = invoker._handoff_capacity_status(stage, evidence, resolved_finalization=resolved)
    result = await invoker._finalize(stage, capability_evidence=evidence, resolved_finalization=resolved)
    assert result.stage_id is stage_id
    assert len(calls) == 1 and calls[0].tools == ()
    actual = measure_request_size(calls[0])
    assert actual.public_output_schema_bytes == before["complete_request"]["snapshot"]["public_output_schema_bytes"]
    assert actual.public_messages_bytes == before["complete_request"]["snapshot"]["public_messages_bytes"]
    assert actual.public_request_bytes <= before["complete_request"]["public_request_bytes_upper_bound"]


@pytest.mark.asyncio
async def test_finalizer_resolution_cache_is_local_to_each_invoke(tmp_path):
    stage = _stage_input().model_copy(update={"allowed_capabilities": ("artifact_list",)})
    fixture = _toolset(tmp_path, stage)
    built = []
    final_calls = []

    class CapabilityProvider:
        binding = WorkflowProvider.binding
        calls = 0
        async def generate(self, request):
            self.calls += 1
            if self.calls == 1:
                return ModelTurn(tool_calls=(ToolCall(call_id="read", name="artifact_list",
                    arguments_json="{}"),), finish_reason=FinishReason.TOOL_CALLS,
                    usage=TokenUsage(total_tokens=1))
            return ModelTurn(content="Synthetic completion.", finish_reason=FinishReason.STOP,
                             usage=TokenUsage(total_tokens=1))

    def build(_model):
        provider = CapabilityProvider() if len(built) % 2 == 0 else WorkflowProvider(final_calls)
        built.append(provider)
        return provider

    invoker = NativeKernelStageInvoker(assembly=RuntimeStageAssemblySpec(
        stage_id=WorkflowStage.INTAKE, root_profile_key="coordinator",
        prompt_template_key="runtime-generic", capability_allowlist=stage.allowed_capabilities),
        factory=NativeRuntimeFactory(_capability_catalog(), build), principal=fixture.binding.principal,
        workspace=fixture.binding.workspace, services=fixture.services)
    await invoker.invoke(stage)
    await invoker.invoke(stage.model_copy(update={"run_id": uuid4(), "invocation_id": uuid4()}))
    assert len(built) == 4 and built[1] is not built[3]
    assert len(final_calls) == 2 and all(request.tools == () for request in final_calls)
    assert built[0].calls == built[2].calls == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("fatal", ["EXECUTION_SUBMISSION_PERSISTENCE_FAILED", "ENVIRONMENT_BUILD_UNCONFIRMED"])
async def test_unconfirmed_child_effect_blocks_capacity_queries_before_final_provider_resolution(
    tmp_path, monkeypatch, fatal,
):
    from labbioagentos import RuntimeIntegrationError
    from labbioagentos.runtime import native_stage
    from labbioagentos.runtime.assembly import RuntimeAgentCapabilitySpec
    stage = _stage_input(WorkflowStage.EXECUTE).model_copy(update={
        "allowed_capabilities": ("execution_submit", "handoff_status")})
    fixture = _toolset(tmp_path, _stage_input())
    hosts, built = [], []
    original_host = native_stage.NativeRuntimeCapabilityHost

    def capture_host(*args):
        host = original_host(*args)
        hosts.append(host)
        return host

    async def child_unconfirmed(_runtime, **_kwargs):
        hosts[1]._host.stop_reason = fatal
        response = await hosts[0]._host.handoff_status()
        assert response["success"]
        assert response["data"]["request_admission"] == "BLOCKED_UNCONFIRMED_SIDE_EFFECT"
        assert response["data"]["complete_request"] == {"status": "UNAVAILABLE", "reason": fatal}
        return SimpleNamespace(events=())

    def build(_model):
        assert len(built) < 2, "No final provider may mask the unconfirmed effect"
        provider = SimpleNamespace(binding=WorkflowProvider.binding)
        built.append(provider)
        return provider

    monkeypatch.setattr(native_stage, "NativeRuntimeCapabilityHost", capture_host)
    monkeypatch.setattr(native_stage.CollaborationRuntime, "run_leader", child_unconfirmed)
    catalog = _capability_catalog()
    catalog.capabilities["coordinator-capabilities"] = catalog.capabilities[
        "coordinator-capabilities"].model_copy(update={"capability_allowlist": stage.allowed_capabilities})
    catalog.agents["worker"] = catalog.agents["coordinator"].model_copy(update={
        "profile_key": "worker", "agent_name": "Worker", "capability_profile_key": "worker-capabilities"})
    catalog.capabilities["worker-capabilities"] = catalog.capabilities[
        "coordinator-capabilities"].model_copy(update={
            "profile_key": "worker-capabilities", "capability_allowlist": ("execution_submit",)})
    invoker = NativeKernelStageInvoker(assembly=RuntimeStageAssemblySpec(
        stage_id=WorkflowStage.EXECUTE, root_profile_key="coordinator",
        prompt_template_key="runtime-generic", capability_allowlist=stage.allowed_capabilities,
        capability_peer_specs=(RuntimeAgentCapabilitySpec(
            profile_key="worker", capability_allowlist=("execution_submit",)),)),
        factory=NativeRuntimeFactory(catalog, build), principal=fixture.binding.principal,
        workspace=fixture.binding.workspace, services=fixture.services)
    with pytest.raises(RuntimeIntegrationError) as caught:
        await invoker.invoke(stage)
    assert caught.value.error_code == fatal
    assert len(built) == 2 and hosts[1].stop_reason == fatal
