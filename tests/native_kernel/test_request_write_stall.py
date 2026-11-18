"""An upload stall must not consume the model's entire inference deadline."""

import asyncio
from hashlib import sha256

import httpcore
from httpcore._backends.mock import AsyncMockBackend, AsyncMockStream
import httpx
import pytest

from labbioagentos.kernel import ProviderTransportError
from labbioagentos.kernel.http_stream import observe_http_progress
from test_http_stream import (
    _post, _transport,
    forbid_network,  # noqa: F401 - real protocol machinery, no network sockets
)


class Wire(AsyncMockBackend):
    """Advance a virtual clock by the timeout actually supplied to httpcore."""

    def __init__(self, clock, outcomes):
        super().__init__([])
        self.clock, self.outcomes = clock, list(outcomes)
        self.bodies, self.timeouts, self.connects = [], [], []
        self.writing = asyncio.Event()

    async def connect_tcp(self, **kwargs):
        self.connects.append(kwargs["timeout"])
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        wire = self

        class Stream(AsyncMockStream):
            async def write(self, buffer, timeout=None):
                if buffer.startswith(b"{"):
                    wire.bodies.append(sha256(buffer).hexdigest())
                    if outcome == "cancel":
                        wire.writing.set()
                        await asyncio.Event().wait()
                    if outcome == "write_timeout":
                        wire.clock.now += timeout
                        raise httpcore.WriteTimeout()
                await super().write(buffer, timeout)

            async def read(self, max_bytes, timeout=None):
                if outcome == "read_timeout":
                    wire.clock.now += timeout
                    raise httpcore.ReadTimeout()
                if outcome == "slow_read":
                    wire.clock.now += 90
                return await super().read(max_bytes, timeout)

        return Stream([
            b"HTTP/1.1 200 OK\r\nContent-Length: 11\r\nConnection: close\r\n\r\n"
            b'{"ok":true}',
        ])


def _wire(monkeypatch, transport, outcomes):
    wire = Wire(transport._clock, outcomes)
    protocol = httpx.AsyncHTTPTransport()
    protocol._pool = httpcore.AsyncConnectionPool(network_backend=wire)
    real_client = httpx.AsyncClient

    async def record(request):
        wire.timeouts.append(dict(request.extensions["timeout"]))

    monkeypatch.setattr(
        "labbioagentos.kernel.http_stream.httpx.AsyncClient",
        lambda **kwargs: real_client(
            transport=protocol, event_hooks={"request": [record]}, **kwargs,
        ),
    )
    return wire


@pytest.mark.asyncio
async def test_body_stall_recovers_inside_original_model_deadline(monkeypatch):
    transport, sleeps = _transport(max_retries=2)
    wire = _wire(monkeypatch, transport, ["write_timeout", "ok"])
    events = []
    with observe_http_progress(lambda *event: events.append(event)):
        result = await _post(transport)

    assert result.body == {"ok": True}
    assert [retry.category for retry in result.retries] == ["NETWORK_WRITE_TIMEOUT"]
    assert result.retries[0].usage_snapshot is None
    assert sleeps.waits == [0.5]
    assert len(wire.bodies) == 2 and len(set(wire.bodies)) == 1
    assert wire.timeouts == [
        {"connect": 60.0, "read": 600.0, "write": 60.0, "pool": 600.0},
        {"connect": 60.0, "read": 539.5, "write": 60.0, "pool": 539.5},
    ]
    failed = [event for event in events if event[0] == "ATTEMPT_FAILED"]
    assert len(failed) == 1 and failed[0][1] == "SEND_REQUEST_BODY"
    assert failed[0][2]["response_status"] is None
    assert failed[0][2]["response_bytes"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("max_retries", [0, 2])
async def test_upload_timeout_preserves_retry_count_limit(monkeypatch, max_retries):
    transport, sleeps = _transport(max_retries=max_retries)
    wire = _wire(monkeypatch, transport, ["write_timeout"])
    with pytest.raises(ProviderTransportError) as caught:
        await _post(transport)

    assert caught.value.validation_type == "NETWORK_WRITE_TIMEOUT"
    assert len(wire.bodies) == max_retries + 1
    assert len(caught.value.transport_retries) == max_retries
    assert len(sleeps.waits) == max_retries
    assert all(retry.usage_snapshot is None for retry in caught.value.transport_retries)


@pytest.mark.asyncio
async def test_short_deadline_clamps_upload_timeout_and_prevents_retry(monkeypatch):
    transport, sleeps = _transport(max_retries=2)
    wire = _wire(monkeypatch, transport, ["write_timeout", "ok"])
    with pytest.raises(ProviderTransportError) as caught:
        await _post(transport, deadline=1_020.0)

    assert caught.value.validation_type == "NETWORK_WRITE_TIMEOUT"
    assert wire.timeouts == [{"connect": 20.0, "read": 20.0, "write": 20.0, "pool": 20.0}]
    assert len(wire.bodies) == 1
    assert sleeps.waits == []


@pytest.mark.asyncio
async def test_inference_read_can_exceed_upload_timeout(monkeypatch):
    transport, sleeps = _transport(max_retries=2)
    wire = _wire(monkeypatch, transport, ["slow_read"])
    result = await _post(transport)

    assert result.body == {"ok": True}
    assert wire.clock.now == 1_090.0
    assert wire.timeouts[0]["read"] == 600.0
    assert wire.connects[0] == 60.0
    assert wire.timeouts[0]["write"] == 60.0
    assert result.retries == () and sleeps.waits == []


@pytest.mark.asyncio
async def test_read_timeout_remains_final(monkeypatch):
    transport, sleeps = _transport(max_retries=2)
    wire = _wire(monkeypatch, transport, ["read_timeout", "ok"])
    with pytest.raises(ProviderTransportError) as caught:
        await _post(transport)

    assert caught.value.validation_type == "TIMEOUT"
    assert len(wire.bodies) == 1
    assert caught.value.transport_retries == () and sleeps.waits == []


@pytest.mark.asyncio
async def test_cancel_during_upload_is_not_retried(monkeypatch):
    transport, sleeps = _transport(max_retries=2)
    wire = _wire(monkeypatch, transport, ["cancel", "ok"])
    events = []
    with observe_http_progress(lambda *event: events.append(event)):
        task = asyncio.create_task(_post(transport))
        await asyncio.wait_for(wire.writing.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert len(wire.bodies) == 1 and sleeps.waits == []
    assert [(event[0], event[1]) for event in events if event[0] == "ATTEMPT_CANCELLED"] == [
        ("ATTEMPT_CANCELLED", "SEND_REQUEST_BODY"),
    ]
