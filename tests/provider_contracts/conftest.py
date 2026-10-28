"""Fixtures for the provider-format contract suite."""

from __future__ import annotations

import json
import socket

import httpx
import pytest

from .wire import WIRES

REAL_ASYNC_CLIENT = httpx.AsyncClient


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def denied(*_args, **_kwargs):
        pytest.fail("Provider contracts must not contact a network")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


class ScriptedServer:
    """Provider endpoint that answers each request with the next scripted outcome.

    An outcome is an SSE body, an ``httpx.Response``, or a callable that takes
    the decoded request body and returns either.
    """

    def __init__(self):
        self.outcomes: list = []
        self.requests: list[httpx.Request] = []

    def script(self, *outcomes) -> None:
        self.outcomes.extend(outcomes)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if callable(outcome):
            outcome = outcome(json.loads(request.content))
        if isinstance(outcome, httpx.Response):
            return outcome
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            stream=httpx.ByteStream(outcome.encode("utf-8")),
        )

    @property
    def bodies(self) -> list[dict]:
        return [json.loads(request.content) for request in self.requests]


@pytest.fixture
def server(monkeypatch) -> ScriptedServer:
    server = ScriptedServer()
    monkeypatch.setattr(
        "labbioagentos.kernel.http_stream.httpx.AsyncClient",
        lambda **kwargs: REAL_ASYNC_CLIENT(
            transport=httpx.MockTransport(server.handler), **kwargs,
        ),
    )
    return server


@pytest.fixture(params=WIRES, ids=lambda wire: wire.name)
def wire(request):
    return request.param
