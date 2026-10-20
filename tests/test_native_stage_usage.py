"""Cancellation retains a distinct durable unknown-usage observation."""

import asyncio

import pytest

from labbioagentos import InMemoryTraceSink, RunTraceRecorder, TraceEventType, WorkflowStage
from labbioagentos.kernel import FinishReason, ModelTurn, ProviderProtocolError, TokenUsage, ToolCall
from labbioagentos.runtime.errors import RuntimeIntegrationError
from labbioagentos.runtime.native_stage import NativeKernelStageInvoker, NativeRuntimeFactory
from labbioagentos.runtime.assembly import RuntimeStageAssemblySpec
from test_native_stage_application import WorkflowProvider, _capability_catalog, _stage_input, _toolset


@pytest.mark.asyncio
@pytest.mark.parametrize("capability_phase", [False, True])
async def test_cancelled_provider_usage_is_projected_before_cancellation_propagates(
    tmp_path, capability_phase,
):
    class CancelledProvider:
        binding = WorkflowProvider.binding

        async def generate(self, _request):
            raise asyncio.CancelledError()

    stage_input = _stage_input()
    if not capability_phase:
        stage_input = stage_input.model_copy(update={"allowed_capabilities": ()})
    recorder = RunTraceRecorder(InMemoryTraceSink())
    fixture = _toolset(tmp_path, stage_input, recorder)
    invoker = NativeKernelStageInvoker(
        assembly=RuntimeStageAssemblySpec(
            stage_id=WorkflowStage.INTAKE, root_profile_key="coordinator",
            prompt_template_key="runtime-generic",
            capability_allowlist=("artifact_list",) if capability_phase else (),
            capability_phase_enabled=capability_phase,
        ),
        factory=NativeRuntimeFactory(_capability_catalog(), lambda _model: CancelledProvider()),
        principal=fixture.binding.principal, workspace=fixture.binding.workspace,
        services=fixture.services, trace_recorder=recorder,
    )
    with pytest.raises(asyncio.CancelledError):
        await invoker.invoke(stage_input)
    events = recorder.events(stage_input.run_id)
    unknown = [event for event in events if event.event_type.value == "PROVIDER_USAGE_UNREPORTED"]
    assert len(unknown) == 1
    assert unknown[0].payload["numeric_facts"]["total_tokens"] is None
    assert not any(event.event_type.value in {
        "PROVIDER_TURN_OBSERVED", "FINALIZATION_PHASE_COMPLETED",
    } for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("capability_phase", [False, True])
@pytest.mark.parametrize("snapshot_only", [False, True])
async def test_rejected_response_usage_is_durably_projected_without_a_model_turn(
    tmp_path, capability_phase, snapshot_only,
):
    class RejectedProvider:
        binding = WorkflowProvider.binding

        async def generate(self, _request):
            raise ProviderProtocolError(
                validation_fields=("message.content",),
                **{("usage_snapshot" if snapshot_only else "usage"):
                   TokenUsage(input_tokens=9, output_tokens=12, total_tokens=21)},
            )

    stage_input = _stage_input()
    if not capability_phase:
        stage_input = stage_input.model_copy(update={"allowed_capabilities": ()})
    recorder = RunTraceRecorder(InMemoryTraceSink())
    fixture = _toolset(tmp_path, stage_input, recorder)
    invoker = NativeKernelStageInvoker(
        assembly=RuntimeStageAssemblySpec(
            stage_id=WorkflowStage.INTAKE, root_profile_key="coordinator",
            prompt_template_key="runtime-generic",
            capability_allowlist=("artifact_list",) if capability_phase else (),
            capability_phase_enabled=capability_phase,
        ),
        factory=NativeRuntimeFactory(_capability_catalog(), lambda _model: RejectedProvider()),
        principal=fixture.binding.principal, workspace=fixture.binding.workspace,
        services=fixture.services, trace_recorder=recorder,
    )
    with pytest.raises(RuntimeIntegrationError):
        await invoker.invoke(stage_input)
    events = recorder.events(stage_input.run_id)
    kind = "PROVIDER_USAGE_UNREPORTED" if snapshot_only else "PROVIDER_USAGE_ON_FAILURE"
    accounted = [event for event in events if event.event_type.value == kind]
    assert len(accounted) == 1
    assert accounted[0].payload["numeric_facts"]["total_tokens"] == (None if snapshot_only else 21)
    if snapshot_only:
        assert accounted[0].payload["numeric_facts"]["last_validated_total_tokens"] == 21
        assert accounted[0].status == "PARTIAL_RESPONSE_USAGE"
    assert accounted[0].payload["numeric_facts"]["turn_index"] == 1
    assert accounted[0].payload["kernel_invocation_id"]
    assert not any(event.event_type.value in {
        "PROVIDER_TURN_OBSERVED", "FINALIZATION_PHASE_COMPLETED",
    } for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot_only", [False, True])
async def test_protocol_failure_after_capability_evidence_projects_safe_validation(
    tmp_path, snapshot_only,
):
    class ToolThenInvalidProvider:
        binding = WorkflowProvider.binding
        calls = 0

        async def generate(self, _request):
            self.calls += 1
            if self.calls == 1:
                return ModelTurn(
                    tool_calls=(ToolCall(
                        call_id="artifact-list-before-failure", name="artifact_list",
                        arguments_json="{}",
                    ),),
                    finish_reason=FinishReason.TOOL_CALLS,
                    usage=TokenUsage(total_tokens=4),
                )
            raise ProviderProtocolError(
                validation_type="value_error",
                validation_fields=("message.content",),
                **{("usage_snapshot" if snapshot_only else "usage"):
                   TokenUsage(total_tokens=7)},
            )

    stage_input = _stage_input()
    recorder = RunTraceRecorder(InMemoryTraceSink())
    fixture = _toolset(tmp_path, stage_input, recorder)
    capability_provider = ToolThenInvalidProvider()
    providers = []

    def builder(_model):
        provider = capability_provider if not providers else WorkflowProvider([])
        providers.append(provider)
        return provider

    invoker = NativeKernelStageInvoker(
        assembly=RuntimeStageAssemblySpec(
            stage_id=WorkflowStage.INTAKE, root_profile_key="coordinator",
            prompt_template_key="runtime-generic",
            capability_allowlist=("artifact_list",),
        ),
        factory=NativeRuntimeFactory(_capability_catalog(), builder),
        principal=fixture.binding.principal, workspace=fixture.binding.workspace,
        services=fixture.services, trace_recorder=recorder,
    )
    result = await invoker.invoke(stage_input)

    assert result.stage_id is WorkflowStage.INTAKE
    assert capability_provider.calls == 2
    events = recorder.events(stage_input.run_id)
    usage_type = (TraceEventType.PROVIDER_USAGE_UNREPORTED if snapshot_only
                  else TraceEventType.PROVIDER_USAGE_ON_FAILURE)
    usage_events = [event for event in events if event.event_type is usage_type]
    assert len(usage_events) == 1
    assert usage_events[0].payload["validation_type"] == "value_error"
    assert usage_events[0].payload["validation_fields"] == ["message.content"]
    assert usage_events[0].payload["numeric_facts"]["total_tokens"] == (
        None if snapshot_only else 7
    )
    completed = next(event for event in events
                     if event.event_type is TraceEventType.CAPABILITY_PHASE_COMPLETED)
    assert completed.payload["capability_count"] == 1
    assert completed.payload["termination_reason"] == "PROVIDER_PROTOCOL_STOP"
