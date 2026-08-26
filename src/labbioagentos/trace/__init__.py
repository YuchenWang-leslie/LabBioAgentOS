"""Append-only RunTrace infrastructure."""

from .models import (
    InstructionKind,
    InstructionRecord,
    TraceEvent,
    TraceEventType,
    TracePayloadError,
)
from .projection import (
    DelegationProjection,
    InvocationProjection,
    RunTraceProjection,
    TraceProjectionError,
    project_run_trace,
)
from .recorder import RunTraceRecorder
from .sinks import (
    InMemoryTraceSink,
    JsonlTraceSink,
    TraceSequenceError,
    TraceSink,
    TraceSinkError,
)

__all__ = [
    "DelegationProjection",
    "InMemoryTraceSink",
    "InstructionKind",
    "InstructionRecord",
    "InvocationProjection",
    "JsonlTraceSink",
    "RunTraceProjection",
    "RunTraceRecorder",
    "TraceEvent",
    "TraceEventType",
    "TracePayloadError",
    "TraceProjectionError",
    "TraceSequenceError",
    "TraceSink",
    "TraceSinkError",
    "project_run_trace",
]
