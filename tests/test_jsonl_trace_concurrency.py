"""Shared JSONL writers allocate ordered audit events for each run."""

import fcntl
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import get_context
from threading import Event
from uuid import uuid4

import pytest

from labbioagentos import JsonlTraceSink, RunTraceRecorder, TraceEvent, TraceEventType
from labbioagentos.trace.sinks import TraceSequenceError, TraceSinkError


def _emit(recorder, run_id, caller):
    return recorder.emit(run_id, TraceEventType.AUTHORIZATION_ALLOWED, caller=caller)


def test_gold_reader_can_audit_during_pending_writer(tmp_path):
    path = tmp_path / "trace.jsonl"
    writer = RunTraceRecorder(JsonlTraceSink(path))
    reader = RunTraceRecorder(JsonlTraceSink(path))
    run_id = uuid4()
    first = _emit(writer, run_id, "writer")
    other = _emit(reader, run_id, "reader")
    last = writer.emit(run_id, TraceEventType.SKILL_PROPOSAL_CREATED)
    assert (first.sequence, other.sequence, last.sequence) == (0, 1, 2)
    assert writer.events(run_id) == (first, other, last)


def _process_writer(path, run_id, index, ready):
    recorder = RunTraceRecorder(JsonlTraceSink(path))
    ready.wait(timeout=10)
    for _ in range(9):
        _emit(recorder, run_id, str(index))


def test_concurrent_jsonl_processes_preserve_every_event(tmp_path):
    path, run_id = tmp_path / "trace.jsonl", uuid4()
    ctx = get_context("fork")
    ready = ctx.Barrier(3)
    writers = [ctx.Process(target=_process_writer, args=(path, run_id, i, ready))
               for i in range(3)]
    try:
        for writer in writers:
            writer.start()
        for writer in writers:
            writer.join(timeout=15)
        assert [p.exitcode for p in writers] == [0, 0, 0]
        records = JsonlTraceSink(path).read(run_id)
        assert [r.sequence for r in records] == list(range(27))
        assert sorted(r.caller for r in records) == sorted(["0", "1", "2"] * 9)
    finally:
        for writer in writers:
            if writer.is_alive():
                writer.terminate()
                writer.join(timeout=5)


def test_readers_wait_for_writers_complete_line(tmp_path):
    path, run_id = tmp_path / "trace.jsonl", uuid4()
    sink = JsonlTraceSink(path)
    event = TraceEvent(run_id=run_id, sequence=0, event_type=TraceEventType.AUTHORIZATION_ALLOWED)
    encoded = event.model_dump_json().encode()
    entered = Event()

    def read():
        entered.set()
        return sink.read(run_id)

    with ThreadPoolExecutor(1) as worker, path.open("wb") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(encoded[:10])
        handle.flush()
        future = worker.submit(read)
        try:
            assert entered.wait(timeout=2)
            with pytest.raises(TimeoutError):
                future.result(timeout=0.1)
            handle.write(encoded[10:] + b"\n")
            handle.flush()
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
        assert future.result(timeout=3) == (event,)


def test_explicit_append_rejects_wrong_sequence_and_mixed_runs_are_independent(tmp_path):
    sink = JsonlTraceSink(tmp_path / "trace.jsonl")
    first, second = uuid4(), uuid4()
    recorder = RunTraceRecorder(sink)
    a = _emit(recorder, first, "a")
    b = _emit(recorder, second, "b")
    with pytest.raises(TraceSequenceError):
        sink.append(a)
    assert (a.sequence, b.sequence) == (0, 0)
    next_a = a.model_copy(update={"sequence": 1, "event_id": uuid4()})
    sink.append(next_a)
    assert sink.read() == (a, b, next_a)


@pytest.mark.parametrize("corruption", ["bad_json", "sequence_gap"])
def test_existing_corruption_is_refused_before_any_new_append(tmp_path, corruption):
    path = tmp_path / "trace.jsonl"
    sink = JsonlTraceSink(path)
    run_id = uuid4()
    event = TraceEvent(run_id=run_id, sequence=2, event_type=TraceEventType.AUTHORIZATION_ALLOWED)
    bad = b'{"broken":' if corruption == "bad_json" else event.model_dump_json().encode() + b"\n"
    path.write_bytes(bad)
    with pytest.raises(TraceSinkError):
        RunTraceRecorder(sink).emit(uuid4(), TraceEventType.SKILL_PROPOSAL_CREATED)
    assert path.read_bytes() == bad


def test_valid_last_record_without_newline_remains_separate(tmp_path):
    path, run_id = tmp_path / "trace.jsonl", uuid4()
    sink = JsonlTraceSink(path)
    first = TraceEvent(run_id=run_id, sequence=0, event_type=TraceEventType.AUTHORIZATION_ALLOWED)
    path.write_text(first.model_dump_json())
    second = _emit(RunTraceRecorder(sink), run_id, "reader")
    assert sink.read(run_id) == (first, second)
