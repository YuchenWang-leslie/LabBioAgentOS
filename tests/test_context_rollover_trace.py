"""Context boundaries must survive the kernel-to-persistent trace projection."""

from uuid import uuid4
from types import SimpleNamespace

from labbioagentos.kernel.contracts import KernelEvent, KernelEventKind
from labbioagentos.runtime.native_stage import NativeKernelStageInvoker
from labbioagentos.trace.models import TraceEventType


def test_context_rollover_is_projected_without_content_or_identity_loss():
    old_context, new_context = uuid4(), uuid4()
    event = KernelEvent(
        kernel_run_id=uuid4(), invocation_id=uuid4(), sequence=3,
        kind=KernelEventKind.CONTEXT_ROLLED_OVER,
        status_code="ROLLED_OVER", reason_code="CONTEXT_CAPACITY_EXCEEDED",
        numeric_facts={"previous_wire_request_bytes": 87_291,
                       "wire_request_bytes": 51_000, "turn_index": 2},
        references=(str(old_context), str(new_context)),
    )
    records = []
    invoker = object.__new__(NativeKernelStageInvoker)
    invoker._projected_kernel_events = {}
    invoker._emit = lambda stage, kind, status, payload: records.append((kind, status, payload))

    invoker._project_kernel_events(SimpleNamespace(invocation_id=uuid4()), (event,))

    assert len(records) == 1
    kind, status, payload = records[0]
    assert kind == TraceEventType.PROVIDER_CONTEXT_ROLLED_OVER
    assert status == "ROLLED_OVER"
    assert payload["kernel_invocation_id"] == str(event.invocation_id)
    assert payload["references"] == [str(old_context), str(new_context)]
    assert payload["numeric_facts"] == event.numeric_facts
