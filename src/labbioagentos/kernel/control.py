"""Deterministic budget and interrupt controls for the Native Kernel."""

from __future__ import annotations

from .contracts import (
    BudgetDelta,
    BudgetLimits,
    BudgetUsage,
    InterruptReason,
    InterruptSignal,
)
from .errors import BudgetExceededError, KernelInterruptedError


class BudgetLedger:
    """Reserve root-run budget atomically across all bounded dimensions."""

    _ADDITIVE_DIMENSIONS = (
        "provider_turns",
        "wall_time_ms",
        "tool_calls",
        "evidence_entries",
        "evidence_bytes",
        "delegations",
        "finalization_attempts",
    )

    def __init__(self, limits: BudgetLimits):
        self._limits = limits
        self._usage = BudgetUsage()

    @property
    def limits(self) -> BudgetLimits:
        return self._limits

    @property
    def usage(self) -> BudgetUsage:
        return self._usage

    def reserve(self, delta: BudgetDelta) -> BudgetUsage:
        """Validate every dimension before committing any usage."""

        candidate: dict[str, int | None] = {}
        for dimension in self._ADDITIVE_DIMENSIONS:
            consumed = getattr(self._usage, dimension)
            requested = getattr(delta, dimension)
            candidate[dimension] = consumed + requested

        if self._usage.tokens is None or delta.tokens is None:
            candidate["tokens"] = None
        else:
            candidate["tokens"] = self._usage.tokens + delta.tokens
        candidate["delegation_depth"] = max(
            self._usage.delegation_depth,
            delta.delegation_depth,
        )

        for dimension, value in candidate.items():
            if value is None:
                continue
            limit = getattr(self._limits, dimension)
            if value > limit:
                consumed = getattr(self._usage, dimension)
                requested = getattr(delta, dimension)
                raise BudgetExceededError(
                    dimension=dimension,
                    limit=limit,
                    consumed=0 if consumed is None else consumed,
                    requested=0 if requested is None else requested,
                )

        self._usage = BudgetUsage(**candidate)
        return self._usage


class InterruptToken:
    """First-writer-wins interrupt state for one root kernel execution."""

    def __init__(self):
        self._signal: InterruptSignal | None = None

    @property
    def signal(self) -> InterruptSignal | None:
        return self._signal

    @property
    def requested(self) -> bool:
        return self._signal is not None

    def request(
        self,
        reason: InterruptReason,
        *,
        reason_code: str,
    ) -> InterruptSignal:
        if self._signal is None:
            self._signal = InterruptSignal(reason=reason, reason_code=reason_code)
        return self._signal

    def raise_if_requested(self) -> None:
        if self._signal is not None:
            raise KernelInterruptedError(self._signal)
