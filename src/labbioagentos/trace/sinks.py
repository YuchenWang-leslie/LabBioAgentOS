"""Append-only local trace sinks."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from threading import Lock
from uuid import UUID

from pydantic import ValidationError

from .models import TraceEvent


class TraceSinkError(RuntimeError):
    """A trace sink could not read or append events."""


class TraceSequenceError(TraceSinkError):
    """An event would violate append-only per-run sequence ordering."""


class TraceSink(ABC):
    """Minimal synchronous append/read contract for observational tracing."""

    @abstractmethod
    def append(self, event: TraceEvent) -> None:
        """Append exactly one event or raise explicitly."""

    @abstractmethod
    def read(self, run_id: UUID | None = None) -> tuple[TraceEvent, ...]:
        """Read events in append order, optionally filtered to one run."""


class InMemoryTraceSink(TraceSink):
    """Primary deterministic unit-test sink."""

    def __init__(self):
        self._events: list[TraceEvent] = []
        self._lock = Lock()

    def append(self, event: TraceEvent) -> None:
        with self._lock:
            expected = sum(item.run_id == event.run_id for item in self._events)
            if event.sequence != expected:
                raise TraceSequenceError(
                    f"Expected sequence {expected} for run {event.run_id}; "
                    f"got {event.sequence}"
                )
            self._events.append(event)

    def read(self, run_id: UUID | None = None) -> tuple[TraceEvent, ...]:
        with self._lock:
            if run_id is None:
                return tuple(self._events)
            return tuple(event for event in self._events if event.run_id == run_id)


class JsonlTraceSink(TraceSink):
    """Append one validated TraceEvent JSON object per local file line."""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and not self.path.is_file():
            raise TraceSinkError(f"JSONL trace path is not a file: {self.path}")
        self._lock = Lock()

    def append(self, event: TraceEvent) -> None:
        with self._lock:
            existing = self._read_unlocked(event.run_id)
            expected = len(existing)
            if event.sequence != expected:
                raise TraceSequenceError(
                    f"Expected sequence {expected} for run {event.run_id}; "
                    f"got {event.sequence}"
                )
            try:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(event.model_dump_json())
                    handle.write("\n")
            except OSError as exc:
                raise TraceSinkError(
                    f"Could not append trace event to {self.path}: {exc}"
                ) from exc

    def read(self, run_id: UUID | None = None) -> tuple[TraceEvent, ...]:
        with self._lock:
            return self._read_unlocked(run_id)

    def _read_unlocked(
        self, run_id: UUID | None = None
    ) -> tuple[TraceEvent, ...]:
        if not self.path.exists():
            return ()
        events: list[TraceEvent] = []
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        raise TraceSinkError(
                            f"Blank JSONL trace line at {self.path}:{line_number}"
                        )
                    try:
                        event = TraceEvent.model_validate_json(line)
                    except ValidationError as exc:
                        raise TraceSinkError(
                            f"Invalid trace event at {self.path}:{line_number}: {exc}"
                        ) from exc
                    if run_id is None or event.run_id == run_id:
                        events.append(event)
        except OSError as exc:
            raise TraceSinkError(
                f"Could not read trace events from {self.path}: {exc}"
            ) from exc
        return tuple(events)
