"""Cold-review counterexamples for observational transport diagnostics."""

import asyncio
from time import monotonic
from uuid import uuid4

import pytest

from labbioagentos.kernel import (
    CollaborationRuntime, FinishReason, KernelEvent, KernelEventKind, ModelTurn,
    PublicMessage,
)
from labbioagentos.kernel.http_stream import _RequestProgress, observe_http_progress
from native_kernel.test_collaboration import _identity, _runtime


@pytest.mark.asyncio
async def test_diagnostic_observer_error_cannot_replace_success():
    fixture, providers, budget = _runtime(
        leader_turns=(ModelTurn(content="ok", finish_reason=FinishReason.STOP),),
        child_turns={"writer": ()},
    )

    def unavailable(_event):
        raise OSError("fixture diagnostic observer unavailable")

    runtime = CollaborationRuntime(
        registry=fixture.registry, tool_registries=fixture.tool_registries,
        projector=fixture.projector, budget_ledger=budget,
        event_observer=unavailable,
    )
    result = await runtime.run_leader(
        identity=_identity(),
        messages=(PublicMessage(role="USER", content="offline fixture"),),
        deadline_monotonic=monotonic() + 30,
    )
    assert result.leader_result.output == "ok"
    assert len(providers["leader"].requests) == 1
    assert budget.usage.provider_turns == 1
    assert budget.usage.tokens is None


@pytest.mark.parametrize("error_type", [asyncio.CancelledError, BaseException])
def test_observer_control_signal_propagates_after_event_is_retained(error_type):
    fixture, providers, budget = _runtime(leader_turns=(), child_turns={"writer": ()})

    def control_signal(_event):
        raise error_type("fixture control signal")

    runtime = CollaborationRuntime(
        registry=fixture.registry, tool_registries=fixture.tool_registries,
        projector=fixture.projector, budget_ledger=budget,
        event_observer=control_signal,
    )
    original = KernelEvent(
        kernel_run_id=uuid4(), invocation_id=uuid4(), sequence=99,
        kind=KernelEventKind.MODEL_REQUEST_PREPARED,
    )
    with pytest.raises(error_type):
        runtime._capture_event(original)
    assert len(runtime.events) == 1
    assert runtime.events[0].sequence == 1
    assert runtime.events[0].model_dump(exclude={"sequence"}) == original.model_dump(
        exclude={"sequence"},
    )
    assert len(providers["leader"].requests) == 0
    assert budget.usage.provider_turns == 0


@pytest.mark.asyncio
async def test_proxy_tls_cancellation_reports_observed_tls_phase():
    events = []
    progress = _RequestProgress(monotonic)
    progress.reset(1)
    with observe_http_progress(lambda *event: events.append(event)):
        # httpcore's HTTPS tunnel receives CONNECT headers before the remote
        # TLS upgrade; no sensitive trace information is needed for the phase.
        await progress.trace("http11.receive_response_headers.complete", {})
        await progress.trace("proxy.start_tls.started", {})
        progress.emit("ATTEMPT_CANCELLED")
    assert events[-1][0] == "ATTEMPT_CANCELLED"
    assert events[-1][1] == "TLS"
    assert events[-1][2]["response_bytes"] == 0
    assert events[-1][2]["first_body_elapsed_ms"] is None
