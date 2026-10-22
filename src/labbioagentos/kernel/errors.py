"""Typed failures shared by Native Kernel contracts and test doubles."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .contracts import InterruptSignal, ProviderRetry


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
    def __init__(self, *, transport_retries: tuple[ProviderRetry, ...] = ()):
        super().__init__(
            "KERNEL_DEADLINE_EXCEEDED",
            "The kernel invocation deadline was reached",
        )
        # Provider attempts discarded before the deadline, for the loop to record.
        self.transport_retries = transport_retries


class IncompleteProviderResponseError(KernelContractError):
    def __init__(self, *, finish_reason: str):
        super().__init__(
            "INCOMPLETE_PROVIDER_RESPONSE",
            "The provider response was incomplete and cannot be accepted",
        )
        self.finish_reason = finish_reason


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
        transport_retries: tuple[ProviderRetry, ...] = (),
    ):
        super().__init__(
            "PROVIDER_PROTOCOL_ERROR",
            "The provider returned an unsupported response shape",
        )
        self.validation_type = validation_type
        self.validation_fields = validation_fields
        # Attempts discarded before this failure, for the loop to record.
        self.transport_retries = transport_retries


# A provider type name is recorded only when it has this plain identifier form.
_PROVIDER_TYPE_NAME = re.compile(r"[a-z][a-z0-9_.]{0,63}")


def unsupported_type_error(path: str, value: object) -> ProviderProtocolError:
    """Name an unknown content-bearing provider type without echoing other text."""

    name = (
        value
        if isinstance(value, str) and _PROVIDER_TYPE_NAME.fullmatch(value)
        else "unrecognized"
    )
    return ProviderProtocolError(
        validation_type="unsupported_type", validation_fields=(path, name)
    )


class ProviderTransportError(KernelContractError):
    def __init__(
        self,
        *,
        validation_type: str | None = None,
        transport_retries: tuple[ProviderRetry, ...] = (),
    ):
        super().__init__(
            "PROVIDER_TRANSPORT_ERROR",
            "The provider request could not complete",
        )
        self.validation_type = validation_type
        # Attempts discarded before this failure, for the loop to record.
        self.transport_retries = transport_retries


class AgentHistoryLimitError(KernelContractError):
    def __init__(self):
        super().__init__(
            "AGENT_HISTORY_LIMIT_EXCEEDED",
            "The invocation public history exceeds the configured transport bound",
        )
