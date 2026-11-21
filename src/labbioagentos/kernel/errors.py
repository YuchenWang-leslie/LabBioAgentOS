"""Typed failures shared by Native Kernel contracts and test doubles."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .contracts import FinishReason, ToolCallCapacityIssue

if TYPE_CHECKING:
    from .contracts import InterruptSignal, ProviderRetry, TokenUsage


class KernelContractError(RuntimeError):
    """Base class for failures at a Native Kernel contract boundary."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ProviderBindingMismatchError(KernelContractError):
    """A request does not match the provider instance's frozen binding."""

    def __init__(self):
        super().__init__(
            "PROVIDER_BINDING_MISMATCH",
            "The request provider binding does not match this provider instance",
        )


class FakeProviderExhaustedError(KernelContractError):
    """The deterministic provider has no scripted turn left."""

    def __init__(self):
        super().__init__(
            "FAKE_PROVIDER_EXHAUSTED",
            "The deterministic provider has no scripted turn left",
        )


class BudgetExceededError(KernelContractError):
    """A reservation would exceed one frozen budget dimension."""

    def __init__(
        self,
        *,
        dimension: str,
        limit: int,
        consumed: int,
        requested: int,
    ):
        super().__init__(
            "BUDGET_EXCEEDED",
            f"Budget {dimension!r} would exceed its limit",
        )
        self.dimension = dimension
        self.limit = limit
        self.consumed = consumed
        self.requested = requested


class KernelInterruptedError(KernelContractError):
    """Execution stopped because an explicit interrupt signal was set."""

    def __init__(self, signal: InterruptSignal):
        super().__init__("KERNEL_INTERRUPTED", "Kernel execution was interrupted")
        self.signal = signal


class ToolBatchRejectedError(KernelContractError):
    """A complete provider tool batch was rejected before execution."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        call_id: str | None = None,
        tool_name: str | None = None,
        validation_type: str | None = None,
        validation_fields: tuple[str, ...] = (),
    ):
        super().__init__(code, message)
        self.call_id = call_id
        self.tool_name = tool_name
        self.validation_type = validation_type
        self.validation_fields = validation_fields


class CloseoutRequiredError(ToolBatchRejectedError):
    """A work admission cannot consume the reserved completion capacity."""

    def __init__(self):
        super().__init__('CLOSEOUT_REQUIRED',
            'This batch exceeds the remaining work-tool capacity; the shared total includes '
            'reserved completion-control calls. No requested work was executed. Inspect '
            'kernel_budget and choose a fitting work batch or exposed closeout tools. At zero '
            'work capacity, choose your own handoff from delivered evidence, including limits '
            'and a legal next action, or state insufficiency. The host chose no evidence or decision.')


class ToolResultProjectionError(KernelContractError):
    """A handler result cannot be transported completely as bounded JSON."""


class ToolExecutionFailure(KernelContractError):
    """A handler-declared bounded failure safe for model feedback."""

    def __init__(
        self,
        *,
        code: str,
        safe_message: str,
        retryable: bool = False,
    ):
        super().__init__(code, safe_message)
        self.safe_message = safe_message
        self.retryable = retryable


class NoObservableProgressError(KernelContractError):
    """The provider produced no public progress within the configured bound."""

    def __init__(self, *, elapsed_ms: int, turn_count: int):
        super().__init__(
            "NO_OBSERVABLE_PROGRESS",
            "The model produced no observable progress within the configured limit",
        )
        self.elapsed_ms = elapsed_ms
        self.turn_count = turn_count


class KernelDeadlineExceededError(KernelContractError):
    def __init__(self, *, usage: TokenUsage | None = None,
                 usage_snapshot: TokenUsage | None = None,
                 transport_retries: tuple[ProviderRetry, ...] = ()):
        super().__init__(
            "KERNEL_DEADLINE_EXCEEDED",
            "The kernel invocation deadline was reached",
        )
        self.usage = usage
        self.usage_snapshot = usage_snapshot
        # Provider attempts discarded before the deadline, for the loop to record.
        self.transport_retries = transport_retries


class IncompleteProviderResponseError(KernelContractError):
    def __init__(self, *, finish_reason: str):
        super().__init__(
            "INCOMPLETE_PROVIDER_RESPONSE",
            "The provider response was incomplete and cannot be accepted",
        )
        self.finish_reason = finish_reason
        # Read by stage runtimes that project failures into the run trace.
        self.validation_type = finish_reason


class FinalizationProtocolError(KernelContractError):
    def __init__(self):
        super().__init__(
            "FINALIZATION_PROTOCOL_ERROR",
            "Finalization returned a tool call or another unsupported response shape",
        )


class FinalizationRejectedError(KernelContractError):
    def __init__(
        self,
        *,
        validation_type: str,
        validation_fields: tuple[str, ...] = (),
    ):
        super().__init__(
            "STRUCTURED_RESPONSE_REJECTED",
            "The final response did not match the required structured contract",
        )
        self.validation_type = validation_type
        self.validation_fields = validation_fields


class ProviderProtocolError(KernelContractError):
    def __init__(
        self,
        *,
        validation_type: str | None = None,
        validation_fields: tuple[str, ...] = (),
        usage: TokenUsage | None = None,
        usage_snapshot: TokenUsage | None = None,
        transport_retries: tuple[ProviderRetry, ...] = (),
        provider_finish_reason: FinishReason | None = None,
        tool_call_capacity: ToolCallCapacityIssue | None = None,
    ):
        if tool_call_capacity is not None and not isinstance(tool_call_capacity, ToolCallCapacityIssue):
            raise TypeError("tool_call_capacity must contain typed safe capacity facts")
        if provider_finish_reason is not None and not isinstance(
            provider_finish_reason, FinishReason
        ):
            raise TypeError("provider_finish_reason must be a FinishReason or None")
        super().__init__(
            "PROVIDER_TOOL_CAPACITY_REJECTED" if tool_call_capacity is not None else "PROVIDER_PROTOCOL_ERROR",
            "The provider returned an unsupported response shape",
        )
        self.validation_type = validation_type
        self.validation_fields = validation_fields
        self.usage = usage
        self.usage_snapshot = usage_snapshot
        self.provider_finish_reason = provider_finish_reason
        self.tool_call_capacity = tool_call_capacity
        # Attempts discarded before this failure, for the loop to record.
        self.transport_retries = transport_retries


class ToolCallCapacityError(ValueError):
    """Internal parser signal; the full response must still pass its other checks."""

    def __init__(self, issue: ToolCallCapacityIssue):
        self.issue = issue
        super().__init__("A tool-call string exceeds its native channel capacity")


# A provider type name is recorded only when it has this plain identifier form.
_PROVIDER_TYPE_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.]{0,63}")


def bounded_provider_name(value: object) -> str:
    """Return a provider-declared name if it is a plain identifier, else a marker."""

    if isinstance(value, str) and _PROVIDER_TYPE_NAME.fullmatch(value):
        return value
    return "unrecognized"


def unsupported_type_error(path: str, value: object) -> ProviderProtocolError:
    """Name an unknown content-bearing provider type without echoing other text."""

    return ProviderProtocolError(
        validation_type="unsupported_type",
        validation_fields=(path, bounded_provider_name(value)),
    )


def preserve_failure_usage(error, raw, parser) -> None:
    """Retain independently valid billing from a complete rejected response."""
    try:
        usage = parser(raw)
    except (TypeError, ValueError):
        return
    if any(value is not None for value in (
        usage.input_tokens, usage.output_tokens, usage.total_tokens,
    )):
        error.usage = usage


class ProviderTransportError(KernelContractError):
    def __init__(
        self,
        *,
        validation_type: str | None = None,
        usage: TokenUsage | None = None,
        usage_snapshot: TokenUsage | None = None,
        transport_retries: tuple[ProviderRetry, ...] = (),
    ):
        super().__init__(
            "PROVIDER_TRANSPORT_ERROR",
            "The provider request could not complete",
        )
        self.validation_type = validation_type
        self.usage = usage
        self.usage_snapshot = usage_snapshot
        # Attempts discarded before this failure, for the loop to record.
        self.transport_retries = transport_retries


class AgentHistoryLimitError(KernelContractError):
    def __init__(self):
        super().__init__(
            "AGENT_HISTORY_LIMIT_EXCEEDED",
            "The invocation public history exceeds the configured transport bound",
        )


class ContextCapacityError(KernelContractError):
    def __init__(self, *, request_bytes: int, max_request_bytes: int):
        super().__init__(
            "CONTEXT_CAPACITY_EXCEEDED",
            "The complete model request exceeds its configured byte capacity",
        )
        self.request_bytes = request_bytes
        self.max_request_bytes = max_request_bytes
