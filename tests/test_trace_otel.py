"""Optional offline telemetry uses real SDK spans and a strict data allowlist."""

from __future__ import annotations

import builtins
import importlib
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from labbioagentos import WorkflowStage
from labbioagentos.trace import TraceEvent, TraceEventType, TraceProjectionError


@pytest.fixture
def telemetry():
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource({}), shutdown_on_exit=False)
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield provider.get_tracer("labbio.test"), exporter
    provider.shutdown()


def _events():
    run, parent, child = uuid4(), uuid4(), uuid4()
    start = datetime(2026, 10, 28, 0, 0, tzinfo=timezone.utc)
    specs = [
        (TraceEventType.RUN_STARTED, None, None),
        (TraceEventType.AGENT_STARTED, parent, None),
        (TraceEventType.AGENT_STARTED, child, parent),
        (TraceEventType.PROVIDER_TURN_OBSERVED, child, parent),
        (TraceEventType.AGENT_COMPLETED, child, parent),
        (TraceEventType.AGENT_COMPLETED, parent, None),
        (TraceEventType.RUN_COMPLETED, None, None),
    ]
    return tuple(TraceEvent(
        run_id=run, sequence=index, timestamp=start + timedelta(seconds=index),
        event_type=kind, invocation_id=invocation, parent_invocation_id=ancestor,
        stage_id=WorkflowStage.EXECUTE,
    ) for index, (kind, invocation, ancestor) in enumerate(specs))


def test_importing_adapter_does_not_import_any_otel_dependency(monkeypatch):
    actual_import = builtins.__import__

    def without_otel(name, *args, **kwargs):
        if name == "opentelemetry" or name.startswith("opentelemetry."):
            raise AssertionError("Optional OTel dependency imported eagerly")
        return actual_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_otel)
    module = importlib.import_module("labbioagentos.trace.otel")
    importlib.reload(module)
    assert callable(module.export_run_trace)


def test_offline_spans_preserve_recorded_parentage_events_and_times(telemetry):
    from opentelemetry.trace import StatusCode

    from labbioagentos.trace.otel import export_run_trace

    tracer, exporter = telemetry
    events = _events()
    assert export_run_trace(events, tracer=tracer) == 3

    spans = exporter.get_finished_spans()
    root = next(span for span in spans if span.name == "labbio.run")
    invocations = {
        span.attributes["labbio.invocation_id"]: span
        for span in spans if span.name == "labbio.invocation"
    }
    parent = invocations[str(events[1].invocation_id)]
    child = invocations[str(events[2].invocation_id)]
    assert root.parent is None
    assert parent.parent.span_id == root.context.span_id
    assert child.parent.span_id == parent.context.span_id
    assert len({span.context.trace_id for span in spans}) == 1
    assert child.start_time == root.start_time + 2_000_000_000
    assert child.end_time == root.start_time + 4_000_000_000
    assert root.end_time - root.start_time == 6_000_000_000
    assert root.status.status_code is StatusCode.OK
    assert [item.name for item in child.events] == [
        "AGENT_STARTED", "PROVIDER_TURN_OBSERVED", "AGENT_COMPLETED",
    ]
    assert child.events[1].timestamp == root.start_time + 3_000_000_000
    assert root.attributes["labbio.timing_source"] == "recorded_trace_timestamps"


def test_allowlist_excludes_content_paths_names_and_unknown_metrics(telemetry):
    from labbioagentos.trace.otel import export_run_trace

    tracer, exporter = telemetry
    events = list(_events())
    reference = uuid4()
    events[3] = events[3].model_copy(update={
        "agent_name": "PATIENT_NAME_CANARY", "caller": "PRIVATE_CALLER_CANARY",
        "target": "PRIVATE_TARGET_CANARY", "execution_context_id": "secret/data.tsv",
        "status": "PATIENT_STATUS_CANARY",
        "payload": {
            "notes": "MODEL_CONTENT_CANARY", "relative_path": "secret/data.tsv",
            "reason_code": "PROVIDER_PROTOCOL_ERROR",
            "error_code": "PATIENT_ERROR_CANARY",
            "validation_fields": ["SENSITIVE_VALUE_CANARY"],
            "numeric_facts": {
                "elapsed_ms": 23, "input_tokens": None, "output_tokens": 0,
                "public_request_bytes": 101, "wire_request_bytes": None,
                "PATIENT_METRIC_CANARY": 57,
            },
            "references": [str(reference), "secret/data.tsv", "PRIVATE_REFERENCE_CANARY"],
        },
    })

    export_run_trace(events, tracer=tracer)

    spans = exporter.get_finished_spans()
    encoded = json.dumps([span.to_json() for span in spans])
    for forbidden in (
        "CANARY", "secret/data.tsv", "validation_fields", "PATIENT_METRIC",
    ):
        assert forbidden not in encoded
    observed = next(item for span in spans for item in span.events
                    if item.name == "PROVIDER_TURN_OBSERVED")
    assert observed.attributes["labbio.reason_code"] == "PROVIDER_PROTOCOL_ERROR"
    assert observed.attributes["labbio.metric.elapsed_ms"] == 23
    assert observed.attributes["labbio.metric.output_tokens"] == 0
    assert observed.attributes["labbio.metric.input_tokens.known"] is False
    assert "labbio.metric.input_tokens" not in observed.attributes
    assert observed.attributes["labbio.metric.wire_request_bytes.known"] is False
    assert observed.attributes["labbio.references"] == (str(reference),)


@pytest.mark.parametrize("invalid", ["mixed-run", "gap", "duplicate"])
def test_invalid_trace_is_rejected_before_emitting_any_span(telemetry, invalid):
    from labbioagentos.trace.otel import export_run_trace

    tracer, exporter = telemetry
    events = list(_events())
    if invalid == "mixed-run":
        events[-1] = events[-1].model_copy(update={"run_id": uuid4()})
    elif invalid == "gap":
        events.pop(2)
    else:
        events.append(events[-1])

    with pytest.raises(TraceProjectionError):
        export_run_trace(events, tracer=tracer)
    assert exporter.get_finished_spans() == ()


def test_run_selection_excludes_other_run_and_never_mutates_source(telemetry):
    from labbioagentos.trace.otel import export_run_trace

    tracer, exporter = telemetry
    selected, other = _events(), _events()
    events = (*selected, *other)
    before = tuple(event.model_dump_json() for event in events)

    export_run_trace(events, tracer=tracer, run_id=selected[0].run_id)

    assert tuple(event.model_dump_json() for event in events) == before
    assert {span.attributes["labbio.run_id"] for span in exporter.get_finished_spans()} == {
        str(selected[0].run_id),
    }


def test_export_does_not_join_ambient_context_or_replace_global_provider(telemetry):
    from opentelemetry import trace

    from labbioagentos.trace.otel import export_run_trace

    tracer, exporter = telemetry
    before = trace.get_tracer_provider()
    with tracer.start_as_current_span("unrelated"):
        export_run_trace(_events(), tracer=tracer)
    exported = next(span for span in exporter.get_finished_spans() if span.name == "labbio.run")
    assert exported.parent is None
    assert trace.get_tracer_provider() is before


def test_terminal_status_uses_explicit_run_fact_not_recoverable_error(telemetry):
    from opentelemetry.trace import StatusCode

    from labbioagentos.trace.otel import export_run_trace

    tracer, exporter = telemetry
    events = list(_events())
    events[3] = events[3].model_copy(update={"event_type": TraceEventType.CAPABILITY_FAILED})
    export_run_trace(events, tracer=tracer)
    root = next(span for span in exporter.get_finished_spans() if span.name == "labbio.run")
    assert root.status.status_code is StatusCode.OK

    exporter.clear()
    events[-1] = events[-1].model_copy(update={"event_type": TraceEventType.RUN_FAILED})
    export_run_trace(events, tracer=tracer)
    root = next(span for span in exporter.get_finished_spans() if span.name == "labbio.run")
    assert root.status.status_code is StatusCode.ERROR
