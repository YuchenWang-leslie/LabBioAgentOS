"""Cancellation retains a distinct durable unknown-usage observation."""

import asyncio

import pytest

from labbioagentos import InMemoryTraceSink, RunTraceRecorder, WorkflowStage
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
