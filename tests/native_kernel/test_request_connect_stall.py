"""Offline real HTTPX/httpcore TCP and TLS deadline counterexamples."""

import asyncio
from hashlib import sha256

import httpcore
from httpcore._backends.mock import AsyncMockBackend, AsyncMockStream
import httpx
import pytest

from labbioagentos.kernel import ProviderTransportError
from labbioagentos.kernel.http_stream import observe_http_progress
from test_http_stream import _post, _transport, forbid_network  # noqa: F401


def wire(monkeypatch, transport, phase, outcomes):
    state = {"timeouts": [], "bodies": [], "waiting": asyncio.Event()}

    class Backend(AsyncMockBackend):
        async def connect_tcp(self, **kwargs):
            timeout = kwargs["timeout"]
            state["timeouts"].append(timeout)
            outcome = outcomes.pop(0) if len(outcomes) > 1 else outcomes[0]
            if phase == "tcp" and outcome == "timeout":
                transport._clock.now += timeout
                raise httpcore.ConnectTimeout()

            class Stream(AsyncMockStream):
                async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
                    if phase in {"tls", "proxy_tls"} and outcome == "timeout":
                        transport._clock.now += timeout
                        raise httpcore.ConnectTimeout()
                    if phase in {"tls", "proxy_tls"} and outcome == "cancel":
                        state["waiting"].set()
                        await asyncio.Event().wait()
                    return self

                async def write(self, buffer, timeout=None):
                    if buffer.startswith(b"{"):
                        state["bodies"].append(sha256(buffer).hexdigest())
                    await super().write(buffer, timeout)

            replies = [b'HTTP/1.1 200 OK\r\nContent-Length: 11\r\nConnection: close\r\n\r\n{"ok":true}']
            if phase == "proxy_tls":
                replies.insert(0, b'HTTP/1.1 200 Connection Established\r\n\r\n')
            return Stream(replies)

    protocol = httpx.AsyncHTTPTransport()
    protocol._pool = (httpcore.AsyncHTTPProxy(proxy_url="http://proxy.invalid", network_backend=Backend([]))
                      if phase == "proxy_tls" else httpcore.AsyncConnectionPool(network_backend=Backend([])))
    client = httpx.AsyncClient
    monkeypatch.setattr("labbioagentos.kernel.http_stream.httpx.AsyncClient",
                        lambda **kwargs: client(transport=protocol, **kwargs))
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["tcp", "tls", "proxy_tls"])
async def test_connect_timeout_recovers_before_model_deadline(monkeypatch, phase):
    transport, sleeps = _transport(max_retries=2)
    state = wire(monkeypatch, transport, phase, ["timeout", "ok"])
    events = []
    with observe_http_progress(lambda *event: events.append(event)):
        result = await _post(transport)
    assert result.body == {"ok": True}
    assert state["timeouts"] == [60.0, 60.0]
    assert len(state["bodies"]) == 1
    assert sleeps.waits == [0.5]
    assert [r.category for r in result.retries] == ["NETWORK_CONNECT_TIMEOUT"]
    failure = next(e for e in events if e[0] == "ATTEMPT_FAILED")
    assert failure[1] == {"tcp": "CONNECT_TCP", "tls": "TLS", "proxy_tls": "TLS"}[phase]
    assert failure[2]["response_bytes"] == 0


@pytest.mark.asyncio
async def test_short_connect_deadline_stays_short_without_retry(monkeypatch):
    transport, sleeps = _transport(max_retries=2)
    state = wire(monkeypatch, transport, "tls", ["timeout", "ok"])
    with pytest.raises(ProviderTransportError) as caught:
        await _post(transport, deadline=1_020.0)
    assert caught.value.validation_type == "NETWORK_CONNECT_TIMEOUT"
    assert state["timeouts"] == [20.0]
    assert state["bodies"] == [] and sleeps.waits == []


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["tls", "proxy_tls"])
async def test_cancel_during_tls_is_not_retried(monkeypatch, phase):
    transport, sleeps = _transport(max_retries=2)
    state = wire(monkeypatch, transport, phase, ["cancel", "ok"])
    task = asyncio.create_task(_post(transport))
    await asyncio.wait_for(state["waiting"].wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert state["bodies"] == [] and sleeps.waits == []


@pytest.mark.asyncio
@pytest.mark.parametrize("max_retries", [0, 2])
async def test_failed_handshakes_preserve_retry_limit(monkeypatch, max_retries):
    transport, sleeps = _transport(max_retries=max_retries)
    state = wire(monkeypatch, transport, "proxy_tls", ["timeout"])
    with pytest.raises(ProviderTransportError) as caught:
        await _post(transport)
    assert caught.value.validation_type == "NETWORK_CONNECT_TIMEOUT"
    assert state["timeouts"] == [60.0] * (max_retries + 1)
    assert len(caught.value.transport_retries) == max_retries
    assert len(sleeps.waits) == max_retries
    assert state["bodies"] == []
