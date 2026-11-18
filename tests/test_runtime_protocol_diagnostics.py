"""Rejected Chat responses retain only safe facts through the Native boundary."""

import json

import pytest

from labbioagentos import InMemoryTraceSink, RunTraceRecorder, RuntimeIntegrationError, RuntimeStageResult
from labbioagentos.kernel import OpenAICompatibleChatProvider
from labbioagentos.runtime.assembly import RuntimeStageAssemblySpec
from labbioagentos.runtime.native_stage import NativeKernelStageInvoker, NativeRuntimeFactory
from native_kernel.test_openai_chat_provider import RecordingTransport
from test_native_stage_application import WorkflowProvider, _body, _capability_catalog, _stage_input, _toolset


@pytest.mark.asyncio
@pytest.mark.parametrize("capability,expected_output", [(True, 16384), (False, 8192)])
@pytest.mark.parametrize("finish,expected", [("stop", "STOP"),
                                           ("PRIVATE_REASON_CANARY", "OTHER"), (None, None)])
async def test_rejected_empty_response_does_not_lose_safe_finish_or_billable_usage(
    tmp_path, capability, expected_output, finish, expected,
):
    stage = _stage_input().model_copy(update={"allowed_capabilities": ()})
    fixture = _toolset(tmp_path, stage)
    catalog = _capability_catalog()
    catalog.models["runtime-default"] = catalog.models["runtime-default"].model_copy(
        update={"max_output_tokens": 16384})
    sink = InMemoryTraceSink()
    transport = RecordingTransport(({
        "choices": [{"finish_reason": finish, "message": {
            "content": "", "reasoning_content": "PRIVATE_THOUGHT_CANARY"}}],
        "usage": {"prompt_tokens": 9, "completion_tokens": 12, "total_tokens": 21},
    },))
    invoker = NativeKernelStageInvoker(
        assembly=RuntimeStageAssemblySpec(stage_id=stage.stage_id,
            root_profile_key="coordinator", prompt_template_key="runtime-generic",
            capability_allowlist=(), capability_phase_enabled=capability),
        factory=NativeRuntimeFactory(catalog, lambda _model: OpenAICompatibleChatProvider(
            binding=WorkflowProvider.binding, api_key="fixture-never-sent", transport=transport)),
        principal=fixture.binding.principal, workspace=fixture.binding.workspace,
        services=fixture.services, trace_recorder=RunTraceRecorder(sink),
    )
    with pytest.raises(RuntimeIntegrationError) as caught:
        await invoker.invoke(stage)
    assert caught.value.error_code == ("NATIVE_CAPABILITY_FAILED" if capability else "NATIVE_INVOCATION_FAILED")
    events = sink.read(stage.run_id)
    usage = [item for item in events if item.payload.get("status") == "REJECTED_RESPONSE_USAGE"
             or item.status == "REJECTED_RESPONSE_USAGE"]
    assert len(usage) == 1
    assert usage[0].payload["numeric_facts"]["total_tokens"] == 21
    assert usage[0].payload["provider_finish_reason"] == expected
    assert usage[0].payload["validation_fields"] == ["model_turn", "content"]
    requests = [item for item in events if item.event_type.value == "PROVIDER_REQUEST_PREPARED"]
    assert len(requests) == 1
    assert requests[0].payload["numeric_facts"]["max_output_tokens"] == expected_output
    assert len(transport.requests) == 1
    assert "PRIVATE_REASON_CANARY" not in "".join(item.model_dump_json() for item in events)
    assert "PRIVATE_THOUGHT_CANARY" not in "".join(item.model_dump_json() for item in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", [True, False])
async def test_valid_empty_limit_response_is_charged_and_safely_regenerated(tmp_path, capability):
    stage = _stage_input().model_copy(update={"allowed_capabilities": ()})
    fixture = _toolset(tmp_path, stage)
    sink = InMemoryTraceSink()
    final = RuntimeStageResult(stage_id=stage.stage_id, summary="Synthetic current result.",
        body=_body(stage.stage_id), next_action={"action": "transition", "target_stage": "UNDERSTAND"})

    def response(finish, content):
        return {"choices": [{"finish_reason": finish, "message": {
            "content": content, "reasoning_content": "PRIVATE_THOUGHT_CANARY"}}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 12, "total_tokens": 21}}

    values = [response("length", "")]
    if capability:
        values.append(response("stop", "A concise capability handoff."))
    values.append(response("stop", final.model_dump_json(exclude={"result_id"})))
    transport = RecordingTransport(tuple(values))
    invoker = NativeKernelStageInvoker(assembly=RuntimeStageAssemblySpec(
        stage_id=stage.stage_id, root_profile_key="coordinator", prompt_template_key="runtime-generic",
        capability_allowlist=(), capability_phase_enabled=capability),
        factory=NativeRuntimeFactory(_capability_catalog(), lambda _model: OpenAICompatibleChatProvider(
            binding=WorkflowProvider.binding, api_key="fixture-never-sent", transport=transport)),
        principal=fixture.binding.principal, workspace=fixture.binding.workspace,
        services=fixture.services, trace_recorder=RunTraceRecorder(sink))
    result = await invoker.invoke(stage)
    assert result.next_action.target_stage.value == "UNDERSTAND"
    events = sink.read(stage.run_id)
    usage = [e for e in events if e.event_type.value == "PROVIDER_TURN_OBSERVED"]
    assert len(usage) == len(values) == len(transport.requests)
    assert sum(e.payload["numeric_facts"]["total_tokens"] for e in usage) == 21 * len(values)
    assert usage[0].status == "LENGTH"
    feedback = [e for e in events if e.payload.get("reason_code") == "NO_PUBLIC_OUTPUT_AT_LIMIT"]
    assert len(feedback) == int(capability)
    assert "PRIVATE_THOUGHT_CANARY" not in json.dumps([e.model_dump(mode="json") for e in events])
