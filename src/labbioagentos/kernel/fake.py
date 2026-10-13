"""Deterministic provider implementation for contract and loop tests."""

from __future__ import annotations

from collections.abc import Iterable

from .contracts import ModelRequest, ModelTurn, ProviderBinding
from .errors import FakeProviderExhaustedError, ProviderBindingMismatchError


class FakeProvider:
    """Return a frozen script in order without transport or fallback behavior."""

    def __init__(self, *, binding: ProviderBinding, turns: Iterable[ModelTurn]):
        self._binding = binding
        self._turns = tuple(turns)
        self._index = 0
        self._requests: list[ModelRequest] = []

    @property
    def binding(self) -> ProviderBinding:
        return self._binding

    @property
    def requests(self) -> tuple[ModelRequest, ...]:
        return tuple(self._requests)

    @property
    def remaining_turns(self) -> int:
        return len(self._turns) - self._index

    async def generate(self, request: ModelRequest) -> ModelTurn:
        if request.binding != self._binding:
            raise ProviderBindingMismatchError()
        if self._index >= len(self._turns):
            raise FakeProviderExhaustedError()
        self._requests.append(request)
        turn = self._turns[self._index]
        self._index += 1
        return turn
