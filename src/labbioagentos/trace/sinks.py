"""Append-only local trace sinks."""

from __future__ import annotations

import fcntl
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from os import SEEK_END
from pathlib import Path
from threading import Lock
from typing import BinaryIO
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

    def append(self, event: TraceEvent) -> None:
        self.append_next(event.run_id, lambda _: event)

    def append_next(self, run_id: UUID, build_event: Callable[[int], TraceEvent]) -> TraceEvent:
        """Allocate and append under one file lock shared by all local processes."""
        try:
            with self.path.open("a+b") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                handle.seek(0)
                expected = sum(1 for _ in self._events(handle, run_id))
                event = build_event(expected)
                if event.run_id != run_id or event.sequence != expected:
                    raise TraceSequenceError(
                        f"Expected sequence {expected} for run {run_id}; "
                        f"got {event.sequence} for run {event.run_id}"
                    )
                line = event.model_dump_json().encode("utf-8") + b"\n"
                if handle.tell():
                    # JSON Lines permits the final record to omit its terminating newline.
                    handle.seek(-1, SEEK_END)
                    if handle.read(1) != b"\n":
                        line = b"\n" + line
                handle.write(line)
                handle.flush()
                return event
        except OSError as exc:
            raise TraceSinkError(f"Could not append trace event to {self.path}: {exc}") from exc

    def read(self, run_id: UUID | None = None) -> tuple[TraceEvent, ...]:
        try:
            with self.path.open("rb") as handle:
                fcntl.flock(handle, fcntl.LOCK_SH)
                return tuple(self._events(handle, run_id))
        except FileNotFoundError:
            return ()
        except OSError as exc:
            raise TraceSinkError(f"Could not read trace events from {self.path}: {exc}") from exc

    def _events(self, handle: BinaryIO, run_id: UUID | None) -> Iterator[TraceEvent]:
        next_sequences: dict[UUID, int] = {}
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                raise TraceSinkError(f"Blank JSONL trace line at {self.path}:{line_number}")
            try:
                event = TraceEvent.model_validate_json(line)
            except ValidationError as exc:
                raise TraceSinkError(f"Invalid trace event at {self.path}:{line_number}: {exc}") from exc
            expected = next_sequences.get(event.run_id, 0)
            if event.sequence != expected:
                raise TraceSequenceError(
                    f"Existing trace sequence for run {event.run_id} is not contiguous "
                    f"at {self.path}:{line_number}"
                )
            next_sequences[event.run_id] = expected + 1
            if run_id is None or event.run_id == run_id:
                yield event
