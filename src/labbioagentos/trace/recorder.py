"""Small event emitter that assigns deterministic per-run trace sequences."""

from __future__ import annotations

from datetime import datetime
from threading import Lock
from typing import Any
from uuid import UUID

from pydantic import JsonValue

from labbioagentos.contracts import WorkflowStage

from .models import InstructionRecord, TraceEvent, TraceEventType
from .sinks import TraceSequenceError, TraceSink


class RunTraceRecorder:
    """Emit immutable events to a sink; it is not a mutable RunTrace model."""

    def __init__(self, sink: TraceSink):
        if not isinstance(sink, TraceSink):
            raise TypeError("sink must implement TraceSink")
        self.sink = sink
        self._next_sequence: dict[UUID, int] = {}
        self._lock = Lock()

    def emit(
        self,
        run_id: UUID,
        event_type: TraceEventType,
        *,
        stage_id: WorkflowStage | None = None,
        invocation_id: UUID | None = None,
        parent_invocation_id: UUID | None = None,
        agent_name: str | None = None,
        caller: str | None = None,
        target: str | None = None,
        execution_context_id: str | None = None,
        parent_tool_call_id: str | None = None,
        chain_path: tuple[str, ...] = (),
        status: str | None = None,
        payload: dict[str, JsonValue] | None = None,
        timestamp: datetime | None = None,
    ) -> TraceEvent:
        """Append one event; validation or sink failures are never swallowed."""

        with self._lock:
            sequence = self._sequence_for(run_id)
            event_kwargs: dict[str, Any] = {
                "run_id": run_id,
                "sequence": sequence,
                "event_type": event_type,
                "stage_id": stage_id,
                "invocation_id": invocation_id,
                "parent_invocation_id": parent_invocation_id,
                "agent_name": agent_name,
                "caller": caller,
                "target": target,
                "execution_context_id": execution_context_id,
                "parent_tool_call_id": parent_tool_call_id,
                "chain_path": chain_path,
                "status": status,
                "payload": payload or {},
            }
            if timestamp is not None:
                event_kwargs["timestamp"] = timestamp
            event = TraceEvent(**event_kwargs)
            self.sink.append(event)
            self._next_sequence[run_id] = sequence + 1
            return event

    def record_instruction(self, record: InstructionRecord) -> TraceEvent:
        """Append a caller-declared sanitized instruction record."""

        return self.emit(
            record.run_id,
            TraceEventType.INSTRUCTION_RECORDED,
            stage_id=record.stage_id,
            invocation_id=record.invocation_id,
            status="RECORDED",
            payload={"instruction": record.model_dump(mode="json")},
        )

    def events(self, run_id: UUID | None = None) -> tuple[TraceEvent, ...]:
        return self.sink.read(run_id)

    def _sequence_for(self, run_id: UUID) -> int:
        if run_id not in self._next_sequence:
            existing = self.sink.read(run_id)
            sequences = [event.sequence for event in existing]
            expected = list(range(len(existing)))
            if sequences != expected:
                raise TraceSequenceError(
                    f"Existing trace sequence for run {run_id} is not contiguous"
                )
            self._next_sequence[run_id] = len(existing)
        return self._next_sequence[run_id]
