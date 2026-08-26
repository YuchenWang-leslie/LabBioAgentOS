"""Structural delegation policy without agent selection or task routing."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Sequence

from labbioagentos.contracts import (
    AgentDescriptor,
    DelegationDecision,
    StageContext,
)


class DelegationPolicy(ABC):
    """Decide whether a runtime-selected caller may invoke a target."""

    @abstractmethod
    def can_call(
        self,
        caller: AgentDescriptor,
        target: AgentDescriptor,
        stage_context: StageContext,
    ) -> DelegationDecision:
        """Return a structural allow/deny decision for the proposed edge."""

    def list_allowed_agents(
        self,
        caller: AgentDescriptor,
        candidates: Sequence[AgentDescriptor],
        stage_context: StageContext,
    ) -> tuple[AgentDescriptor, ...]:
        """Filter candidates in their existing order without selecting or ranking."""

        return tuple(
            target
            for target in candidates
            if self.can_call(caller, target, stage_context).allowed
        )


class InMemoryDelegationPolicy(DelegationPolicy):
    """Small Phase 3 fixture policy expressed as explicit caller-target edges."""

    def __init__(self, allowed_calls: Mapping[str, Iterable[str]]):
        self._allowed_calls = {
            caller: frozenset(targets)
            for caller, targets in allowed_calls.items()
        }

    def can_call(
        self,
        caller: AgentDescriptor,
        target: AgentDescriptor,
        stage_context: StageContext,
    ) -> DelegationDecision:
        """Allow only an explicitly configured edge; stage context is not routed on."""

        del stage_context
        allowed = target.name in self._allowed_calls.get(caller.name, frozenset())
        if allowed:
            reason = "Delegation edge is explicitly allowed by policy."
        else:
            reason = "Delegation edge is not allowed by policy."
        return DelegationDecision(
            allowed=allowed,
            caller=caller.name,
            target=target.name,
            reason=reason,
        )
