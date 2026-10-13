"""Neutral DTOs and the test-harness protocol for kernel characterization."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolCallSpec:
    """One provider-emitted tool request using its unparsed argument text."""

    call_id: str
    name: str = "record_value"
    arguments: str | None = "{}"


@dataclass(frozen=True)
class TurnSpec:
    """One synthetic provider turn."""

    content: str | None = None
    reasoning: str | None = None
    tool_calls: tuple[ToolCallSpec, ...] = ()
    finish_reason: str | None = None
    completion_tokens: Any = None
    total_tokens: Any = None
    private_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ErrorView:
    """Bounded failure projection; provider text is deliberately excluded."""

    kind: str
    code: str | None = None
    elapsed_ms: int | None = None
    turn_count: int | None = None
    validation_type: str | None = None


@dataclass(frozen=True)
class AgentRunCapture:
    provider_histories: tuple[tuple[dict[str, Any], ...], ...]
    observations: tuple[dict[str, Any], ...]
    tool_feedback: tuple[dict[str, Any], ...]
    public_steps: tuple[dict[str, Any], ...]
    public_chunks: tuple[dict[str, Any], ...]
    public_events: tuple[dict[str, Any], ...]
    result_content: Any
    effects: tuple[Any, ...]
    pre_hook_count: int
    post_hook_count: int
    error: ErrorView | None = None


@dataclass(frozen=True)
class StructuredCapture:
    observations: tuple[dict[str, Any], ...]
    public_steps: tuple[dict[str, Any], ...]
    public_chunks: tuple[dict[str, Any], ...]
    public_events: tuple[dict[str, Any], ...]
    callback_order: tuple[str, ...]
    provider_calls: int
    result_content: Any
    error: ErrorView | None = None


@dataclass(frozen=True)
class TransportCapture:
    requests: tuple[dict[str, Any], ...]
    source_parameters: dict[str, Any]
    schemas_before: tuple[dict[str, Any], ...] = ()
    schemas_after: tuple[dict[str, Any], ...] = ()
    error: ErrorView | None = None


@dataclass(frozen=True)
class ForegroundCapture:
    schemas: tuple[dict[str, Any], ...]
    feedback: tuple[dict[str, Any], ...]
    effects: tuple[Any, ...]
    pre_hook_count: int
    post_hook_count: int
    background_task_count: int


@dataclass(frozen=True)
class PrivateReasoningCapture:
    requests: tuple[dict[str, Any], ...]
    cache_snapshots: tuple[tuple[dict[str, Any], ...], ...]
    public_steps: tuple[dict[str, Any], ...]
    public_chunks: tuple[dict[str, Any], ...]
    public_events: tuple[dict[str, Any], ...]
    result_documents: tuple[dict[str, Any], ...]
    effects: tuple[Any, ...]


class KernelContractBackend(Protocol):
    """Behavioral seam shared by the legacy and replacement runtimes."""

    async def run_agent(
        self,
        turns: tuple[TurnSpec, ...],
        *,
        strict_arguments: bool = True,
        no_progress_seconds: int | None = None,
        clock_values: tuple[float, ...] = (),
        tool_result: Any = None,
    ) -> AgentRunCapture: ...

    async def tool_schema(self, tool_name: str) -> dict[str, Any]: ...

    async def canonical_replay(
        self, turns: tuple[TurnSpec, ...]
    ) -> AgentRunCapture: ...

    async def structured_response(
        self, content: str, *, finish_reason: str
    ) -> StructuredCapture: ...

    async def thinking_transport(
        self, *, enabled: bool, calls: int = 2
    ) -> TransportCapture: ...

    async def strict_schema_transport(
        self, *, supported: bool = True
    ) -> TransportCapture: ...

    async def foreground_dispatch(
        self, scenario: str
    ) -> ForegroundCapture: ...

    async def private_reasoning_roundtrip(self) -> PrivateReasoningCapture: ...

    def private_reasoning_policy(self, *, strict_arguments: bool) -> ErrorView | None: ...
