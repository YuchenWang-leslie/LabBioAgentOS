"""A successful SSE terminal event is distinct from delayed generator cleanup."""

import asyncio
import json
import os
from pathlib import Path
import socket
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

import httpcore
from httpcore._backends.mock import AsyncMockBackend, AsyncMockStream
import httpx
import pytest

import labbioagentos
from labbioagentos import InMemoryTraceSink, RunTraceRecorder, WorkflowStage
from labbioagentos.kernel import KernelEvent, KernelEventKind, ProviderTransportError
from labbioagentos.kernel import http_stream
from labbioagentos.kernel.anthropic_messages import _reassemble_message_stream
from labbioagentos.kernel.openai_chat import _reassemble_chat_stream
from labbioagentos.runtime.native_stage import NativeKernelStageInvoker


@pytest.fixture(autouse=True)
def offline_with_explicit_import_root(monkeypatch):
    expected = os.environ.get("LABBIO_TEST_EXPECTED_PACKAGE_ROOT")
    if expected:
        assert Path(labbioagentos.__file__).resolve().parent == Path(expected).resolve()
    def forbidden(*_args, **_kwargs):
        pytest.fail("Terminal-cleanup fixtures must not contact a network/provider")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


def _real_httpcore_case(monkeypatch, *, complete_body=False, read_failure=False):
    # The real Chat stream decoder stops at [DONE] before HTTP body EOF. All
    # content here is a harmless local fixture; only counters/types are retained.
    chunk = {"id": "fixture", "choices": [{"index": 0,
        "delta": {"role": "assistant", "content": "fixture-body-canary"},
        "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
    body = ("data: " + json.dumps(chunk) + "\n\n"
            + ("" if read_failure else "data: [DONE]\n\n")).encode()
    buffers = [b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
               b"Content-Type: text/event-stream\r\n\r\n",
               f"{len(body):x}\r\n".encode() + body + b"\r\n"]
    if not read_failure:
        buffers.append(b"0\r\n\r\n")

    class Stream(AsyncMockStream):
        async def read(self, max_bytes, timeout=None):
            if not self._buffer and read_failure:
                raise httpcore.ReadError("private-read-error-canary")
            return await super().read(max_bytes, timeout)

    class Backend(AsyncMockBackend):
        async def connect_tcp(self, **_kwargs):
            return Stream(list(buffers))

    wire = httpx.AsyncHTTPTransport()
    wire._pool = httpcore.AsyncConnectionPool(network_backend=Backend([]))
    real_client = httpx.AsyncClient
    monkeypatch.setattr(http_stream.httpx, "AsyncClient",
        lambda **kwargs: real_client(transport=wire, **kwargs))
    state = {"events": [], "trace": []}
    original_trace = http_stream._RequestProgress.trace

    async def traced(progress, name, info):
        state["trace"].append({"name": name, "phase_before": progress.phase,
            "exception_type": type(info["exception"]).__name__ if "exception" in info else None})
        await original_trace(progress, name, info)
    monkeypatch.setattr(http_stream._RequestProgress, "trace", traced)

    async def consume_to_eof(events):
        count = 0
        async for _event in events:
            count += 1
        return {"event_count": count}

    async def run():
        with http_stream.observe_http_progress(
                lambda status, reason, facts: state["events"].append((status, reason, facts))):
            result = await http_stream.HttpStreamTransport(trust_env=False,
                retry=http_stream.RetryPolicy(max_retries=0)).post(
                    url="https://provider.invalid/v1/chat/completions", headers={},
                    payload={"stream": True}, deadline_monotonic=monotonic() + 30,
                    max_response_bytes=10_000,
                    decode_stream=consume_to_eof if complete_body else _reassemble_chat_stream)
            # Use an isolated asyncio.run loop: deterministic delayed cleanup
            # must not close unrelated pytest/production generators.
            await asyncio.get_running_loop().shutdown_asyncgens()
            return result
    return run, state


def test_real_terminal_done_then_delayed_generator_close_is_not_http_failure(monkeypatch):
    run, state = _real_httpcore_case(monkeypatch)
    result = asyncio.run(run())
    assert result.body["choices"][0]["finish_reason"] == "stop"
    assert result.retries == ()
    assert any(item == {"name": "http11.receive_response_body.failed",
        "phase_before": "COMPLETED", "exception_type": "GeneratorExit"}
        for item in state["trace"]), "The fixture must reproduce real httpcore delayed teardown"
    completed = [item for item in state["events"] if item[0] == "ATTEMPT_COMPLETED"]
    assert len(completed) == 1
    assert not any(item[0] in {"HTTP_PHASE_FAILED", "ATTEMPT_FAILED", "ATTEMPT_CANCELLED"}
        for item in state["events"])
    closed = [item for item in state["events"] if item[0] == "HTTP_STREAM_CLOSED"]
    assert len(closed) == 1 and closed[0][1] == "COMPLETED"
    assert state["events"].index(completed[0]) < state["events"].index(closed[0])
    assert closed[0][2]["response_status"] == 200
    assert closed[0][2]["response_bytes"] == completed[0][2]["response_bytes"] > 0
    assert "fixture-body-canary" not in json.dumps(state)


def test_real_full_body_eof_keeps_original_success_events(monkeypatch):
    run, state = _real_httpcore_case(monkeypatch, complete_body=True)
    result = asyncio.run(run())
    assert result.body == {"event_count": 2}
    assert result.retries == ()
    assert len([item for item in state["events"] if item[0] == "BODY_COMPLETE"]) == 1
    assert len([item for item in state["events"] if item[0] == "ATTEMPT_COMPLETED"]) == 1
    assert not any(item[0] in {"HTTP_PHASE_FAILED", "HTTP_STREAM_CLOSED"} for item in state["events"])


def test_real_body_read_failure_is_still_failed_and_propagates(monkeypatch):
    run, state = _real_httpcore_case(monkeypatch, read_failure=True)
    with pytest.raises(ProviderTransportError) as raised:
        asyncio.run(run())
    assert raised.value.validation_type == "NETWORK_READ"
    assert any(item[0] == "HTTP_PHASE_FAILED" and item[1] == "READ_RESPONSE_BODY"
        for item in state["events"])
    assert any(item[0] == "ATTEMPT_FAILED" for item in state["events"])
    assert not any(item[0] in {"ATTEMPT_COMPLETED", "HTTP_STREAM_CLOSED"} for item in state["events"])
    assert "private-read-error-canary" not in json.dumps(state)


def test_late_first_attempt_cleanup_cannot_borrow_successful_retry_identity(monkeypatch):
    def sse(values):
        return "".join("data: " + json.dumps(value) + "\n\n" for value in values).encode()
    bodies = [
        sse([{"type": "error", "error": {"type": "overloaded_error"}}]),
        sse([{"type": "message_start", "message": {"id": "fixture", "role": "assistant",
                   "usage": {"input_tokens": 1}}},
             {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                   "usage": {"output_tokens": 1}}, {"type": "message_stop"}]),
    ]
    class Backend(AsyncMockBackend):
        count = 0
        async def connect_tcp(self, **_kwargs):
            body = bodies[self.count]
            self.count += 1
            return AsyncMockStream([b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n"
                b"Content-Type: text/event-stream\r\n\r\n",
                f"{len(body):x}\r\n".encode() + body + b"\r\n", b"0\r\n\r\n"])
    backend = Backend([])
    wire = httpx.AsyncHTTPTransport()
    wire._pool = httpcore.AsyncConnectionPool(network_backend=backend)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(http_stream.httpx, "AsyncClient",
        lambda **kwargs: real_client(transport=wire, **kwargs))
    events, traces, first_body_generators = [], [], []
    original_trace = http_stream._RequestProgress.trace
    async def traced(progress, name, info):
        if name == "http11.receive_response_body.started" and progress.attempt == 1:
            first_body_generators.extend(generator for generator in asyncio.get_running_loop()._asyncgens
                if generator.ag_code.co_qualname == "HTTP11ConnectionByteStream.__aiter__")
        traces.append({"name": name, "attempt": progress.attempt, "phase": progress.phase,
            "exception_type": type(info["exception"]).__name__ if "exception" in info else None})
        await original_trace(progress, name, info)
    monkeypatch.setattr(http_stream._RequestProgress, "trace", traced)
    async def no_wait(_seconds):
        await asyncio.sleep(0)
    async def run():
        with http_stream.observe_http_progress(lambda *item: events.append(item)):
            result = await http_stream.HttpStreamTransport(trust_env=False,
                retry=http_stream.RetryPolicy(max_retries=1), sleep=no_wait).post(
                    url="https://provider.invalid/v1/messages", headers={}, payload={"stream": True},
                    deadline_monotonic=monotonic() + 30, max_response_bytes=10_000,
                    decode_stream=_reassemble_message_stream)
            assert result.body["stop_reason"] == "end_turn"
            assert [item.category for item in result.retries] == ["STREAM_OVERLOADED"]
            assert backend.count == 2 and len(first_body_generators) == 1
            event_start, trace_start = len(events), len(traces)
            for generator in first_body_generators:
                await generator.aclose()
            delayed_events, delayed_trace = events[event_start:], traces[trace_start:]
            # Finish remaining generators in this isolated event loop as well.
            await asyncio.get_running_loop().shutdown_asyncgens()
            return delayed_events, delayed_trace
    delayed_events, delayed_trace = asyncio.run(run())
    assert len(delayed_trace) == 1
    assert delayed_trace[0]["name"] == "http11.receive_response_body.failed"
    assert delayed_trace[0]["exception_type"] == "GeneratorExit"
    assert delayed_trace[0]["attempt"] == 1, "Old cleanup must retain its own attempt binding"
    delayed, = delayed_events
    assert delayed[0:2] == ("HTTP_PHASE_FAILED", "RETRY_WAIT")
    assert delayed[2]["attempt_index"] == 1
    assert delayed[2]["response_bytes"] == len(bodies[0]) != len(bodies[1])
    completed = [item for item in events if item[0] == "ATTEMPT_COMPLETED"]
    assert len(completed) == 1 and completed[0][2]["attempt_index"] == 2
    assert completed[0][2]["response_bytes"] == len(bodies[1])
    assert any(item[0] == "ATTEMPT_FAILED" and item[2]["attempt_index"] == 1 for item in events)
    assert any(item[0] == "HTTP_STREAM_CLOSED" and item[2]["attempt_index"] == 2 for item in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase,name,error", [
    ("READ_RESPONSE_BODY", "http11.receive_response_body.failed", GeneratorExit()),
    ("COMPLETED", "http11.receive_response_body.failed", ValueError("private-error-canary")),
    ("COMPLETED", "http11.receive_response_body.failed", asyncio.CancelledError()),
    ("COMPLETED", "http11.receive_response_body.failed", httpx.ReadTimeout("private-error-canary")),
    ("COMPLETED", "http11.send_request_body.failed", GeneratorExit()),
])
async def test_completion_cleanup_exception_is_narrow_and_keeps_other_failures(phase, name, error):
    progress = http_stream._RequestProgress(lambda: 1.0)
    progress.reset(1)
    progress.phase = phase
    events = []
    with http_stream.observe_http_progress(lambda *item: events.append(item)):
        await progress.trace(name, {"exception": error})
    assert len(events) == 1 and events[0][0:2] == ("HTTP_PHASE_FAILED", phase)
    assert "private-error-canary" not in json.dumps(events)


@pytest.mark.asyncio
async def test_http2_terminal_generator_close_has_the_same_bounded_semantics():
    progress = http_stream._RequestProgress(lambda: 1.0)
    progress.reset(1)
    progress.emit("ATTEMPT_COMPLETED", "COMPLETED")
    events = []
    with http_stream.observe_http_progress(lambda *item: events.append(item)):
        await progress.trace("http2.receive_response_body.failed", {"exception": GeneratorExit()})
    assert len(events) == 1 and events[0][0:2] == ("HTTP_STREAM_CLOSED", "COMPLETED")


def test_native_projection_keeps_closure_status_and_original_turn_identity():
    # Execute the actual Native mapper with fixed host bindings. No provider,
    # tool or model response is synthesized by the projection itself.
    sink = InMemoryTraceSink()
    invoker = object.__new__(NativeKernelStageInvoker)
    invoker.trace_recorder = RunTraceRecorder(sink)
    invoker.assembly = SimpleNamespace(root_profile_key="fixture")
    invoker.factory = SimpleNamespace(catalog=SimpleNamespace(agents={
        "fixture": SimpleNamespace(agent_name="FixtureAgent")}))
    stage = SimpleNamespace(run_id=uuid4(), invocation_id=uuid4(), stage_id=WorkflowStage.EXECUTE)
    event = KernelEvent(kernel_run_id=uuid4(), invocation_id=uuid4(), sequence=7,
        kind=KernelEventKind.MODEL_TRANSPORT_PROGRESS, status_code="HTTP_STREAM_CLOSED",
        reason_code="COMPLETED", numeric_facts={"attempt_index": 1, "turn_index": 3,
            "response_status": 200, "response_bytes": 214})
    invoker._record_transport_progress(stage, event, phase="CAPABILITY")
    recorded, = sink.read(stage.run_id)
    assert recorded.event_type.value == "PROVIDER_TRANSPORT_PROGRESS"
    assert recorded.status == "HTTP_STREAM_CLOSED"
    assert recorded.payload["reason_code"] == "COMPLETED"
    assert recorded.payload["numeric_facts"] == event.numeric_facts
    assert recorded.payload["kernel_invocation_id"] == str(event.invocation_id)
    assert recorded.invocation_id == stage.invocation_id
