"""Fixtures for the backend-neutral kernel contracts."""

from __future__ import annotations

import socket

import pytest

from .backends import NativeBackend
from .contracts import KernelContractBackend


@pytest.fixture
def kernel_backend() -> KernelContractBackend:
    return NativeBackend()


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def denied(*_args, **_kwargs):
        pytest.fail("Kernel contract fixtures must not contact a network")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)
