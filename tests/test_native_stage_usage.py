"""Cancellation retains a distinct durable unknown-usage observation."""

import asyncio

import pytest

from labbioagentos import InMemoryTraceSink, RunTraceRecorder, WorkflowStage
from labbioagentos.kernel import ProviderProtocolError, TokenUsage
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
