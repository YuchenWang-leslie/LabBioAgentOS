"""Transport observations are durable before a Native phase returns."""

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import pytest

from labbioagentos import JsonlTraceSink, RunTraceRecorder, WorkflowStage
from labbioagentos.kernel import CollaborationRuntime, KernelEvent, KernelEventKind
from labbioagentos.kernel import http_stream
from labbioagentos.runtime.assembly import RuntimeStageAssemblySpec
from labbioagentos.runtime.native_stage import NativeKernelStageInvoker, NativeRuntimeFactory
from labbioagentos.trace import TraceEventType
from native_kernel.test_collaboration import _runtime
from test_native_stage_application import (
    WorkflowProvider, _capability_catalog, _stage_input, _toolset,
)


@pytest.mark.asyncio
async def test_delayed_transport_cleanup_keeps_its_original_turn_index():
    from time import monotonic
    from labbioagentos.kernel import FinishReason, ModelTurn, PublicMessage, ToolCall
    from native_kernel.test_agent_loop import _tool_loop

    class Provider:
        binding = WorkflowProvider.binding

        async def generate(self, request):
            if request.turn_index == 1:
                self.old_observer = http_stream._HTTP_PROGRESS.get()
                return ModelTurn(tool_calls=(ToolCall(call_id="first", name="record_value",
                    arguments_json='{"value":"fixture"}'),), finish_reason=FinishReason.TOOL_CALLS)
            # A previous SSE generator may close after the next turn has begun.
            self.old_observer("HTTP_PHASE_FAILED", "COMPLETED", {"attempt_index": 1})
            return ModelTurn(content="Done.", finish_reason=FinishReason.STOP)

    loop, _, _ = _tool_loop(())
    loop.provider = Provider()
    await loop.run(messages=(PublicMessage(role="USER", content="Diagnostic identity fixture."),),
                   deadline_monotonic=monotonic() + 60)
    events = [event for event in loop.events
              if event.kind is KernelEventKind.MODEL_TRANSPORT_PROGRESS]
    assert len(events) == 1
    assert events[0].numeric_facts["turn_index"] == 1


def invoker_for(tmp_path, stage_input, provider, recorder, *, capability_phase):
    fixture = _toolset(tmp_path, stage_input, recorder)
    return NativeKernelStageInvoker(
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


@pytest.mark.asyncio
@pytest.mark.parametrize('capability_phase', [False, True])
@pytest.mark.parametrize('total_tokens', [13, None])
async def test_usage_is_durable_before_next_response_without_duplicate_final_projection(
    tmp_path, capability_phase, total_tokens,
):
    from labbioagentos.kernel import FinishReason, ModelTurn, TokenUsage, ToolCall
    path = tmp_path/'usage.jsonl'
    recorder = RunTraceRecorder(JsonlTraceSink(path))
    class Provider:
        binding = WorkflowProvider.binding
        def __init__(self): self.waiting = asyncio.Event()
        async def generate(self, request):
            if request.turn_index == 1:
                return ModelTurn(content=None if capability_phase else 'Invalid synthetic JSON.',
                    tool_calls=(ToolCall(call_id='list',name='artifact_list',arguments_json='{}'),) if capability_phase else (),
                    finish_reason=FinishReason.TOOL_CALLS if capability_phase else FinishReason.STOP,
                    usage=TokenUsage(total_tokens=total_tokens))
            self.waiting.set()
            await asyncio.Event().wait()
    provider = Provider()
    stage = _stage_input()
    if not capability_phase:
        stage = stage.model_copy(update={'allowed_capabilities':()})
    invoker = invoker_for(tmp_path, stage, provider, recorder, capability_phase=capability_phase)
    task = asyncio.create_task(invoker.invoke(stage))
    try:
        await asyncio.wait_for(provider.waiting.wait(), 1)
        assert not task.done()
        events = RunTraceRecorder(JsonlTraceSink(path)).events(stage.run_id)
        usage = [e for e in events if e.event_type is TraceEventType.PROVIDER_TURN_OBSERVED]
        assert len(usage) == 1
        assert usage[0].payload['numeric_facts']['total_tokens'] == total_tokens
        assert not any(e.event_type.value.endswith('PHASE_COMPLETED') for e in events)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert not invoker._projected_kernel_events
    events = RunTraceRecorder(JsonlTraceSink(path)).events(stage.run_id)
    usage = [e for e in events if e.event_type is TraceEventType.PROVIDER_TURN_OBSERVED]
    assert len(usage) == 1 and usage[0].payload['numeric_facts']['total_tokens'] == total_tokens
    assert len([e for e in events if e.event_type is TraceEventType.PROVIDER_REQUEST_PREPARED]) == 2
    assert len([e for e in events if e.event_type is TraceEventType.PROVIDER_USAGE_UNREPORTED]) == 1


def test_projection_is_idempotent_and_keeps_original_delegation_receipts(tmp_path):
    stage = _stage_input()
    recorder = RunTraceRecorder(JsonlTraceSink(tmp_path/'trace.jsonl'))
    invoker = invoker_for(tmp_path, stage, object(), recorder, capability_phase=True)
    events = tuple(KernelEvent(kernel_run_id=uuid4(), invocation_id=uuid4(), sequence=index,
        kind=kind, status_code='SUCCEEDED') for index,kind in enumerate((
            KernelEventKind.MODEL_TURN_OBSERVED,KernelEventKind.DELEGATION_FINISHED),1))
    first = invoker._project_kernel_events(stage, events)
    second = invoker._project_kernel_events(stage, events)
    assert first == second and len(first)==1
    assert len(recorder.events(stage.run_id)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('reject_after_projection', [False, True])
async def test_completed_usage_is_not_counted_twice_when_result_validation_fails(
    tmp_path, monkeypatch, reject_after_projection,
):
    from labbioagentos import RuntimeIntegrationError
    recorder = RunTraceRecorder(JsonlTraceSink(tmp_path/'usage.jsonl'))
    stage = _stage_input().model_copy(update={'allowed_capabilities':()})
    invoker = invoker_for(tmp_path, stage, WorkflowProvider([]), recorder, capability_phase=False)
    if reject_after_projection:
        def reject(*_args): raise ValueError('Synthetic post-projection rejection.')
        monkeypatch.setattr('labbioagentos.runtime.native_stage.validate_skill_assessment', reject)
        with pytest.raises(RuntimeIntegrationError):
            await invoker.invoke(stage)
    else:
        assert (await invoker.invoke(stage)).stage_id is stage.stage_id
    events = recorder.events(stage.run_id)
    usage = [e for e in events if e.event_type is TraceEventType.PROVIDER_TURN_OBSERVED]
    assert len(usage)==1 and usage[0].payload['numeric_facts']['total_tokens']==2
    assert not invoker._projected_kernel_events


def test_failed_live_projection_is_retried_by_mandatory_final_projection(tmp_path, monkeypatch):
    recorder = RunTraceRecorder(JsonlTraceSink(tmp_path/'usage.jsonl'))
    stage = _stage_input()
    invoker = invoker_for(tmp_path, stage, object(), recorder, capability_phase=True)
    event = KernelEvent(kernel_run_id=uuid4(),invocation_id=uuid4(),sequence=1,
        kind=KernelEventKind.MODEL_USAGE_ON_FAILURE,numeric_facts={'total_tokens':7})
    emit = recorder.emit
    def unavailable(*_args, **_kwargs): raise OSError('Synthetic trace failure.')
    monkeypatch.setattr(recorder,'emit',unavailable)
    # Loop observers may swallow the failed live write. The final projection
    # remains mandatory and must neither mark the failed write saved nor skip it.
    with pytest.raises(OSError):
        invoker._record_kernel_progress(stage,event,phase='CAPABILITY')
    with pytest.raises(OSError):
        invoker._project_kernel_events(stage,(event,))
    monkeypatch.setattr(recorder,'emit',emit)
    invoker._project_kernel_events(stage,(event,))
    invoker._project_kernel_events(stage,(event,))
    events = recorder.events(stage.run_id)
    assert len(events)==1 and events[0].payload['numeric_facts']['total_tokens']==7


@pytest.mark.asyncio
@pytest.mark.parametrize("capability_phase", [False, True])
async def test_waiting_transport_is_durable_and_concurrent_runs_do_not_mix(
    tmp_path, capability_phase,
):
    path = tmp_path / "trace.jsonl"
    recorder = RunTraceRecorder(JsonlTraceSink(path))
    tasks, providers, stages = [], [], []

    class WaitingProvider:
        binding = WorkflowProvider.binding

        def __init__(self, marker):
            self.marker = marker
            self.started = asyncio.Event()
            self.request = None

        async def generate(self, request):
            self.request = request
            http_stream._emit_http_progress(
                "HTTP_REQUEST_STARTED", "WAIT_RESPONSE_HEADERS",
                {"attempt": 1, "fixture_marker": self.marker},
            )
            self.started.set()
            await asyncio.Event().wait()

    try:
        for marker in (1, 2):
            stage = _stage_input()
            if not capability_phase:
                stage = stage.model_copy(update={"allowed_capabilities": ()})
            provider = WaitingProvider(marker)
            invoker = invoker_for(
                tmp_path, stage, provider, recorder, capability_phase=capability_phase,
            )
            stages.append(stage)
            providers.append(provider)
            tasks.append(asyncio.create_task(invoker.invoke(stage)))
        await asyncio.wait_for(asyncio.gather(
            *(provider.started.wait() for provider in providers),
        ), timeout=1)

        # A new reader must see the observation while neither invocation ended.
        reopened = RunTraceRecorder(JsonlTraceSink(path))
        original_ids = []
        for task, provider, stage in zip(tasks, providers, stages, strict=True):
            assert not task.done()
            events = reopened.events(stage.run_id)
            progress = [event for event in events
                        if event.event_type.value == "PROVIDER_TRANSPORT_PROGRESS"]
            assert len(progress) == 1
            event = progress[0]
            original_ids.append(event.event_id)
            assert event.stage_id is stage.stage_id
            assert event.invocation_id == stage.invocation_id
            assert event.status == "HTTP_REQUEST_STARTED"
            assert event.payload["phase"] == ("CAPABILITY" if capability_phase else "FINALIZE")
            assert event.payload["kernel_invocation_id"] == str(provider.request.invocation_id)
            assert event.payload["kernel_run_id"] == str(provider.request.kernel_run_id)
            assert event.payload["reason_code"] == "WAIT_RESPONSE_HEADERS"
            assert event.payload["numeric_facts"]["fixture_marker"] == provider.marker
            assert event.payload["numeric_facts"]["turn_index"] == 1
            assert not any(event.event_type.value.endswith("PHASE_COMPLETED") for event in events)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert all(task.cancelled() for task in tasks)
    final = RunTraceRecorder(JsonlTraceSink(path)).events()
    final_ids = [event.event_id for event in final
                 if event.event_type.value == "PROVIDER_TRANSPORT_PROGRESS"]
    assert len(final_ids) == len(original_ids) == 2
    assert set(final_ids) == set(original_ids)
    for stage in stages:
        assert len([event for event in final if event.run_id == stage.run_id
                    and event.event_type.value == "PROVIDER_USAGE_UNREPORTED"]) == 1


def test_collaboration_observer_receives_globally_renumbered_original_events():
    fixture, _, _ = _runtime(leader_turns=(), child_turns={"writer": ()})
    observed = []
    runtime = CollaborationRuntime(
        registry=fixture.registry, tool_registries=fixture.tool_registries,
        projector=fixture.projector, budget_ledger=fixture.budget_ledger,
        event_observer=observed.append,
    )
    originals = tuple(KernelEvent(
        kernel_run_id=uuid4(), invocation_id=uuid4(), sequence=99,
        kind=KernelEventKind.MODEL_REQUEST_PREPARED,
    ) for _ in range(2))
    for event in originals:
        runtime._capture_event(event)
    assert tuple(observed) == runtime.events
    assert [event.sequence for event in observed] == [1, 2]
    for original, captured in zip(originals, observed, strict=True):
        assert captured.model_dump(exclude={"sequence"}) == original.model_dump(exclude={"sequence"})


def test_progress_keeps_occurrence_time_and_does_not_change_delegation_references(tmp_path):
    stage = _stage_input()
    recorder = RunTraceRecorder(JsonlTraceSink(tmp_path / "trace.jsonl"))
    invoker = invoker_for(tmp_path, stage, object(), recorder, capability_phase=True)
    timestamp = datetime(2026, 10, 29, 0, 0, tzinfo=timezone.utc)
    progress = KernelEvent(
        kernel_run_id=uuid4(), invocation_id=uuid4(), sequence=3,
        kind=KernelEventKind.MODEL_TRANSPORT_PROGRESS, occurred_at=timestamp,
        status_code="HTTP_REQUEST_STARTED", reason_code="WAIT_RESPONSE_HEADERS",
        numeric_facts={"turn_index": 1, "attempt": 1},
    )
    delegation = tuple(KernelEvent(
        kernel_run_id=progress.kernel_run_id, invocation_id=uuid4(), task_id=uuid4(),
        sequence=number, kind=kind, status_code=status,
    ) for number, kind, status in (
        (2, KernelEventKind.DELEGATION_STARTED, "STARTED"),
        (4, KernelEventKind.DELEGATION_FINISHED, "SUCCEEDED"),
    ))
    invoker._record_transport_progress(stage, progress, phase="CAPABILITY")
    references = invoker._project_kernel_events(stage, (delegation[0], progress, delegation[1]))
    events = recorder.events(stage.run_id)
    assert [event.event_type.value for event in events] == [
        "PROVIDER_TRANSPORT_PROGRESS", "DELEGATION_STARTED", "DELEGATION_COMPLETED",
    ]
    assert events[0].timestamp == progress.occurred_at
    assert events[0].payload["kernel_event_id"] == str(progress.event_id)
    assert references == tuple(event.event_id for event in events[1:])
    assert [event.payload["kernel_task_id"] for event in events[1:]] == [
        str(event.task_id) for event in delegation
    ]


def test_only_new_transport_diagnostics_are_best_effort(tmp_path, monkeypatch):
    stage = _stage_input()
    recorder = RunTraceRecorder(JsonlTraceSink(tmp_path / "trace.jsonl"))
    invoker = invoker_for(tmp_path, stage, object(), recorder, capability_phase=True)

    def unavailable(*_args, **_kwargs):
        raise OSError("fixture sink unavailable")

    monkeypatch.setattr(recorder, "emit", unavailable)
    progress = KernelEvent(
        kernel_run_id=uuid4(), invocation_id=uuid4(), sequence=1,
        kind=KernelEventKind.MODEL_TRANSPORT_PROGRESS,
        status_code="HTTP_REQUEST_STARTED", reason_code="WAIT_RESPONSE_HEADERS",
    )
    invoker._record_transport_progress(stage, progress, phase="CAPABILITY")
    with pytest.raises(OSError, match="fixture sink unavailable"):
        invoker._emit(stage, TraceEventType.CAPABILITY_PHASE_STARTED, "STARTED", {})
