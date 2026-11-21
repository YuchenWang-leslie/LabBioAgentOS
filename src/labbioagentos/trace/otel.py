"""Explicit, content-free offline projection of RunTrace into caller-owned OTel."""

from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from typing import TYPE_CHECKING, Sequence
from uuid import UUID

from .models import TraceEvent, TraceEventType
from .projection import TraceProjectionError, project_run_trace

if TYPE_CHECKING:
    from opentelemetry.trace import Tracer


_METRICS = frozenset({
    "turn_index", "tool_call_count", "elapsed_ms", "input_tokens", "output_tokens",
    "total_tokens", "last_validated_input_tokens", "last_validated_output_tokens",
    "last_validated_total_tokens", "attempt", "wait_ms", "public_request_bytes",
    "request_bytes", "wire_request_bytes", "max_request_bytes", "message_count",
    "compacted_batches", "public_messages_bytes", "public_tool_declarations_bytes",
    "public_output_schema_bytes", "public_envelope_bytes", "handoff_bytes",
    "max_handoff_bytes", "handoff_selection_count", "evidence_bytes", "evidence_entries",
})
_UUID_FIELDS = frozenset({
    "correlation_id", "kernel_event_id", "kernel_invocation_id", "kernel_task_id",
    "capability_invocation_id", "execution_id", "artifact_id", "observation_id",
    "report_artifact_id", "tool_invocation_id", "parent_tool_invocation_id",
})
_STATUSES = frozenset({
    "CREATED", "RUNNING", "COMPLETED", "SUCCEEDED", "FAILED", "CANCELLED",
    "INTERRUPTED", "READY", "REJECTED", "STOP", "TOOL_CALLS", "LENGTH",
    "CONTENT_FILTER", "OTHER", "OBSERVED", "UNKNOWN_USAGE", "PARTIAL_RESPONSE_USAGE",
    "REJECTED_RESPONSE_USAGE", "RETRIED", "DENIED", "ALLOWED", "RECORDED",
})
_ERROR_CODES = frozenset({
    "PROVIDER_BINDING_MISMATCH", "BUDGET_EXCEEDED", "KERNEL_INTERRUPTED",
    "NO_OBSERVABLE_PROGRESS", "KERNEL_DEADLINE_EXCEEDED", "INCOMPLETE_PROVIDER_RESPONSE",
    "FINALIZATION_PROTOCOL_ERROR", "STRUCTURED_RESPONSE_REJECTED", "PROVIDER_PROTOCOL_ERROR",
    "PROVIDER_TRANSPORT_ERROR", "AGENT_HISTORY_LIMIT_EXCEEDED", "CONTEXT_CAPACITY_EXCEEDED",
    "NONFINAL_USAGE_SNAPSHOT", "INVALID_TOOL_ARGUMENTS", "UNKNOWN_TOOL", "TOOL_NOT_ALLOWED",
    "TOOL_EXECUTION_FAILED", "CHILD_INVOCATION_FAILED", "HANDOFF_REQUIRED", "HANDOFF_TOO_LARGE",
    "NETWORK_CONNECT", "NETWORK_READ", "NETWORK_WRITE", "NETWORK_REMOTE_PROTOCOL",
    "HTTP_408", "HTTP_429", "HTTP_500", "HTTP_502", "HTTP_503", "HTTP_504",
    "unsupported_type",
})
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _nanoseconds(value: datetime) -> int:
    delta = value - _EPOCH
    return ((delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds) * 1_000


def _uuid(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(UUID(value))
    except ValueError:
        return None


def _event_attributes(event: TraceEvent) -> dict:
    attributes = {
        "labbio.event_id": str(event.event_id), "labbio.sequence": event.sequence,
    }
    for name in ("invocation_id", "parent_invocation_id"):
        if (value := getattr(event, name)) is not None:
            attributes[f"labbio.{name}"] = str(value)
    if event.stage_id is not None:
        attributes["labbio.stage_id"] = event.stage_id.value
    if event.status in _STATUSES:
        attributes["labbio.status"] = event.status
    for name in _UUID_FIELDS:
        if (value := _uuid(event.payload.get(name))) is not None:
            attributes[f"labbio.{name}"] = value
    for name in ("reason_code", "error_code", "cause_code", "validation_type"):
        value = event.payload.get(name)
        if isinstance(value, str) and value in _ERROR_CODES:
            attributes[f"labbio.{name}"] = value
    metrics = event.payload.get("numeric_facts")
    if isinstance(metrics, dict):
        for name in _METRICS & metrics.keys():
            value = metrics[name]
            if value is None:
                attributes[f"labbio.metric.{name}.known"] = False
            elif type(value) in (int, float) and isfinite(value):
                attributes[f"labbio.metric.{name}"] = value
    references = event.payload.get("references")
    if isinstance(references, list):
        safe = tuple(value for item in references[:128] if (value := _uuid(item)))
        if safe:
            attributes["labbio.references"] = safe
    return attributes


def export_run_trace(
    events: Sequence[TraceEvent], *, tracer: Tracer, run_id: UUID | None = None,
) -> int:
    """Project one ordered run into a supplied tracer and return its span count.

    No global provider, exporter, network destination, or audit sink is installed.
    Callers configure and flush their own SDK. Times are recorded trace timestamps,
    not reconstructed tool start times; numeric elapsed measurements stay separate.
    Unknown usage stays unknown. Exported spans never establish workflow success.
    SDK sampling and span/event limits still apply; RunTrace remains the audit.
    """

    selected = tuple(event for event in events if run_id is None or event.run_id == run_id)
    project_run_trace(selected)
    grouped: dict[UUID, list[TraceEvent]] = {}
    for event in selected:
        if event.invocation_id is not None:
            grouped.setdefault(event.invocation_id, []).append(event)
    parents = {}
    for identity, group in grouped.items():
        ancestors = {event.parent_invocation_id for event in group
                     if event.parent_invocation_id is not None}
        if len(ancestors) > 1:
            raise TraceProjectionError("Invocation has conflicting recorded parents")
        parents[identity] = next(iter(ancestors), None)
    ordered, visiting = [], set()

    def visit(identity):
        if identity in visiting:
            raise TraceProjectionError("Invocation parentage contains a cycle")
        if identity in ordered:
            return
        visiting.add(identity)
        if parents[identity] in grouped:
            visit(parents[identity])
        visiting.remove(identity)
        ordered.append(identity)

    for identity in grouped:
        visit(identity)

    # The optional dependency is loaded only for an explicit export invocation.
    from opentelemetry.context import Context
    from opentelemetry.trace import StatusCode, set_span_in_context

    root = tracer.start_span(
        "labbio.run", context=Context(),
        start_time=min(_nanoseconds(event.timestamp) for event in selected),
        attributes={
            "labbio.run_id": str(selected[0].run_id),
            "labbio.timing_source": "recorded_trace_timestamps",
        },
    )
    spans = {}
    try:
        for identity in ordered:
            group = grouped[identity]
            attributes = {"labbio.run_id": str(selected[0].run_id),
                          "labbio.invocation_id": str(identity)}
            if parents[identity] is not None:
                attributes["labbio.parent_invocation_id"] = str(parents[identity])
            spans[identity] = tracer.start_span(
                "labbio.invocation",
                context=set_span_in_context(spans.get(parents[identity], root), Context()),
                start_time=min(_nanoseconds(event.timestamp) for event in group),
                attributes=attributes,
            )
        for event in selected:
            spans.get(event.invocation_id, root).add_event(
                event.event_type.value, attributes=_event_attributes(event),
                timestamp=_nanoseconds(event.timestamp),
            )
        terminal = [event.event_type for event in selected if event.event_type in {
            TraceEventType.RUN_COMPLETED, TraceEventType.RUN_FAILED, TraceEventType.RUN_CANCELLED,
        }]
        if terminal and terminal[-1] is TraceEventType.RUN_COMPLETED:
            root.set_status(StatusCode.OK)
        elif terminal and terminal[-1] is TraceEventType.RUN_FAILED:
            root.set_status(StatusCode.ERROR)
    finally:
        for identity, span in reversed(tuple(spans.items())):
            span.end(end_time=max(_nanoseconds(event.timestamp) for event in grouped[identity]))
        root.end(end_time=max(_nanoseconds(event.timestamp) for event in selected))
    return len(spans) + 1
