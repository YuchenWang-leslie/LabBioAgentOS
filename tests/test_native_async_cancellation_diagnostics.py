"""An awaited task cancellation is observed, not declared a workflow outcome."""

import asyncio

import pytest

from labbioagentos import JsonlTraceSink, RunTraceRecorder, WorkflowStage
from labbioagentos.runtime.assembly import RuntimeStageAssemblySpec
from labbioagentos.runtime.native_stage import NativeKernelStageInvoker, NativeRuntimeFactory
from test_native_stage_application import WorkflowProvider, _capability_catalog, _stage_input, _toolset


@pytest.mark.asyncio
@pytest.mark.parametrize("capability_phase", [False, True])
async def test_async_cancellation_reason_survives_native_projection_and_reopen(
    tmp_path, capability_phase,
):
    started = asyncio.Event()

    class WaitingProvider:
        binding = WorkflowProvider.binding
        cancellation = None

        async def generate(self, _request):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as exc:
                self.cancellation = exc
                raise

    provider = WaitingProvider()
    stage_input = _stage_input()
    if not capability_phase:
        stage_input = stage_input.model_copy(update={"allowed_capabilities": ()})
    path = tmp_path / "trace.jsonl"
    recorder = RunTraceRecorder(JsonlTraceSink(path))
    fixture = _toolset(tmp_path, stage_input, recorder)
    invoker = NativeKernelStageInvoker(
        assembly=RuntimeStageAssemblySpec(
            stage_id=WorkflowStage.INTAKE, root_profile_key="coordinator",
            prompt_template_key="runtime-generic",
            capability_allowlist=("artifact_list",) if capability_phase else (),
            capability_phase_enabled=capability_phase,
        ),
        factory=NativeRuntimeFactory(_capability_catalog(), lambda _model: provider),
        principal=fixture.binding.principal, workspace=fixture.binding.workspace,
        services=fixture.services, trace_recorder=recorder,
    )
    task = asyncio.create_task(invoker.invoke(stage_input))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    assert isinstance(provider.cancellation, asyncio.CancelledError)

    events = RunTraceRecorder(JsonlTraceSink(path)).events(stage_input.run_id)
    unknown = [event for event in events if event.event_type.value == "PROVIDER_USAGE_UNREPORTED"]
    assert len(unknown) == 1
    event = unknown[0]
    assert event.status == "UNKNOWN_USAGE"
    assert event.stage_id is stage_input.stage_id
    assert event.invocation_id == stage_input.invocation_id
    assert event.payload["reason_code"] == "ASYNC_TASK_CANCELLED"
    assert all(event.payload["numeric_facts"][name] is None for name in (
        "input_tokens", "output_tokens", "total_tokens",
    ))
    assert not any(event.event_type.value in {
        "PROVIDER_TURN_OBSERVED", "CAPABILITY_PHASE_COMPLETED",
        "FINALIZATION_PHASE_COMPLETED", "RUN_FAILED", "RUN_CANCELLED",
    } for event in events)
