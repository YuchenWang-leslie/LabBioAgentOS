"""Native tool registration, strict transport, and foreground dispatch."""

from __future__ import annotations

import inspect
import json
import math
from collections.abc import Awaitable, Callable, Iterable
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, JsonValue, TypeAdapter, ValidationError

from .contracts import (
    BudgetDelta,
    FinishReason,
    ToolCall,
    ToolError,
    ToolResult,
    ToolSpec,
)
from .control import BudgetLedger
from .errors import (
    ToolBatchRejectedError,
    ToolExecutionFailure,
    ToolResultProjectionError,
)


ToolHandler = Callable[[BaseModel], Awaitable[Any] | Any]
ResultValidator = Callable[[JsonValue], None]
_JSON_VALUE_ADAPTER = TypeAdapter(JsonValue)


class _DuplicateKeyError(ValueError):
    pass


@dataclass(frozen=True)
class ToolDefinition:
    """Host registration kept separate from provider-visible metadata."""

    spec: ToolSpec
    arguments_model: type[BaseModel]
    handler: ToolHandler


@dataclass(frozen=True)
class ParsedToolCall:
    """Strictly validated arguments and their sole canonical replay form."""

    request: ToolCall
    definition: ToolDefinition
    arguments: BaseModel
    canonical_arguments: str


@dataclass(frozen=True)
class ProjectedModelResult:
    """Complete JSON transport value and its exact serialized representation."""

    value: JsonValue
    serialized: str
    byte_count: int


@dataclass(frozen=True)
class DispatchOutcome:
    """One foreground dispatch result in original batch order."""

    result: ToolResult
    canonical_arguments: str


@dataclass(frozen=True)
class ToolExecutionContext:
    """Host-owned identity of the currently executing foreground tool."""

    tool_invocation_id: UUID
    call_id: str
    tool_name: str


_TOOL_EXECUTION_CONTEXT: ContextVar[ToolExecutionContext | None] = ContextVar(
    "native_tool_execution_context",
    default=None,
)


def current_tool_execution_context() -> ToolExecutionContext:
    context = _TOOL_EXECUTION_CONTEXT.get()
    if context is None:
        raise RuntimeError("No foreground tool is currently executing")
    return context


def inline_schema_references(schema: dict[str, Any]) -> dict[str, JsonValue]:
    definitions = schema.get("$defs", {})

    def resolve(value: Any, stack: tuple[str, ...] = ()) -> Any:
        if isinstance(value, list):
            return [resolve(item, stack) for item in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            reference = value["$ref"]
            prefix = "#/$defs/"
            if not isinstance(reference, str) or not reference.startswith(prefix):
                raise ValueError("Only local JSON Schema references are supported")
            name = reference[len(prefix):]
            if name in stack:
                raise ValueError("Recursive tool argument schemas are not supported")
            target = definitions.get(name)
            if not isinstance(target, dict):
                raise ValueError("Tool argument schema reference is unresolved")
            merged = deepcopy(target)
            merged.update({key: item for key, item in value.items() if key != "$ref"})
            return resolve(merged, (*stack, name))
        return {
            key: resolve(item, stack)
            for key, item in value.items()
            if key != "$defs"
        }

    resolved = resolve(schema)
    if not isinstance(resolved, dict):
        raise ValueError("Tool argument schema must be an object")
    return resolved


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError(key)
        result[key] = value
    return result


def _require_finite(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Non-finite JSON number")
    if isinstance(value, dict):
        for child in value.values():
            _require_finite(child)
    elif isinstance(value, list):
        for child in value:
            _require_finite(child)


def _reject_json_constant(_value: str):
    raise ValueError("Non-finite JSON constant")


def parse_strict_json_object(raw_json: str | None) -> tuple[dict[str, Any], str]:
    """Parse one strict object and return its stable canonical JSON text."""

    try:
        if raw_json is None:
            raise ValueError("Missing argument document")
        raw = json.loads(
            raw_json,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
        if not isinstance(raw, dict):
            raise ValueError("Top-level JSON value must be an object")
        _require_finite(raw)
        canonical = json.dumps(
            raw,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (json.JSONDecodeError, UnicodeError, ValueError):
        raise ValueError("Value must be one strict JSON object") from None
    return raw, canonical


def model_json_schema(model: type[BaseModel]) -> dict[str, JsonValue]:
    """Generate the closed inline provider schema for one Pydantic model."""

    return inline_schema_references(model.model_json_schema(mode="validation"))


class ToolRegistry:
    """Ordered host-owned tool definitions with generated strict schemas."""

    def __init__(self):
        self._definitions: dict[str, ToolDefinition] = {}

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._definitions)

    @property
    def specs(self) -> tuple[ToolSpec, ...]:
        return tuple(item.spec for item in self._definitions.values())

    def register(
        self,
        *,
        name: str,
        description: str,
        arguments_model: type[BaseModel],
        handler: ToolHandler,
    ) -> ToolDefinition:
        if name in self._definitions:
            raise ValueError(f"Tool {name!r} is already registered")
        if not isinstance(arguments_model, type) or not issubclass(
            arguments_model, BaseModel
        ):
            raise TypeError("arguments_model must be a Pydantic model class")
        if not callable(handler):
            raise TypeError("handler must be callable")
        parameters = model_json_schema(arguments_model)
        definition = ToolDefinition(
            spec=ToolSpec(
                name=name,
                description=description,
                parameters=parameters,
            ),
            arguments_model=arguments_model,
            handler=handler,
        )
        self._definitions[name] = definition
        return definition

    def definition(self, name: str) -> ToolDefinition:
        try:
            return self._definitions[name]
        except KeyError as exc:
            raise ToolBatchRejectedError(
                "UNKNOWN_TOOL",
                "The requested tool is not registered",
                tool_name=name,
            ) from exc

    def validate_batch(
        self,
        calls: Iterable[ToolCall],
        *,
        finish_reason: FinishReason,
        max_calls: int,
    ) -> tuple[ParsedToolCall, ...]:
        batch = tuple(calls)
        if finish_reason is FinishReason.LENGTH:
            raise ToolBatchRejectedError(
                "RESPONSE_TRUNCATED",
                "A truncated provider response cannot execute tools",
            )
        if finish_reason is FinishReason.CONTENT_FILTER:
            raise ToolBatchRejectedError(
                "RESPONSE_FILTERED",
                "A filtered provider response cannot execute tools",
            )
        if len(batch) > max_calls:
            raise ToolBatchRejectedError(
                "TOOL_BATCH_LIMIT_EXCEEDED",
                "The provider tool batch exceeds its configured limit",
            )
        call_ids = tuple(call.call_id for call in batch)
        if len(call_ids) != len(set(call_ids)):
            raise ToolBatchRejectedError(
                "DUPLICATE_TOOL_CALL_ID",
                "Provider tool call IDs must be unique within a turn",
            )
        return tuple(self._parse(call) for call in batch)

    def _parse(self, call: ToolCall) -> ParsedToolCall:
        definition = self.definition(call.name)
        try:
            raw, canonical = parse_strict_json_object(call.arguments_json)
        except ValueError:
            raise ToolBatchRejectedError(
                "INVALID_JSON_OBJECT",
                "Tool arguments must be one strict JSON object",
                call_id=call.call_id,
                tool_name=call.name,
            ) from None
        if "_background" in raw:
            raise ToolBatchRejectedError(
                "BACKGROUND_EXECUTION_DISABLED",
                "Native Kernel tools execute only in the foreground",
                call_id=call.call_id,
                tool_name=call.name,
            )
        try:
            arguments = definition.arguments_model.model_validate_json(
                canonical,
                strict=True,
            )
        except ValidationError as exc:
            issues = exc.errors(include_url=False, include_input=False, include_context=False)[:16]
            fields = tuple(
                ".".join(str(part) for part in issue["loc"][:8]) or "request"
                for issue in issues
            )
            raise ToolBatchRejectedError(
                "INVALID_TOOL_ARGUMENTS",
                ("The reported field requires a JSON object matching the registered schema; "
                 "omit it if optional instead of sending prose"
                 if issues and issues[0]["type"] == "model_type"
                 else "Tool arguments do not match the registered schema"),
                call_id=call.call_id,
                tool_name=call.name,
                validation_type=issues[0]["type"] if issues else None,
                validation_fields=fields,
            ) from None
        return ParsedToolCall(
            request=call,
            definition=definition,
            arguments=arguments,
            canonical_arguments=canonical,
        )


class ModelResultProjector:
    """The sole complete, non-truncating JSON projection for tool results."""

    def __init__(
        self,
        *,
        max_bytes: int,
        validate_value: ResultValidator | None = None,
    ):
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self.max_bytes = max_bytes
        self.validate_value = validate_value

    def project(self, value: Any) -> ProjectedModelResult:
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json", by_alias=True)
        elif isinstance(value, tuple):
            value = list(value)
        try:
            validated = _JSON_VALUE_ADAPTER.validate_python(value, strict=True)
            if self.validate_value is not None:
                self.validate_value(validated)
            serialized = json.dumps(
                validated,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError, ValidationError):
            raise ToolResultProjectionError(
                "INVALID_MODEL_RESULT",
                "Tool result is not a strict JSON value",
            ) from None
        byte_count = len(serialized.encode("utf-8"))
        if byte_count > self.max_bytes:
            raise ToolResultProjectionError(
                "MODEL_RESULT_TOO_LARGE",
                "Tool result exceeds the configured transport limit",
            )
        return ProjectedModelResult(
            value=json.loads(serialized),
            serialized=serialized,
            byte_count=byte_count,
        )


class ToolDispatcher:
    """Validate a complete batch, reserve budget, then execute in order."""

    def __init__(
        self,
        registry: ToolRegistry,
        projector: ModelResultProjector,
        *,
        budget_ledger: BudgetLedger | None = None,
        max_calls_per_batch: int = 64,
        batch_observer: Callable[[], None] | None = None,
        stop_requested: Callable[[], str | None] | None = None,
    ):
        if max_calls_per_batch < 1 or max_calls_per_batch > 64:
            raise ValueError("max_calls_per_batch must be between 1 and 64")
        self.registry = registry
        self.projector = projector
        self.budget_ledger = budget_ledger
        self.max_calls_per_batch = max_calls_per_batch
        self.batch_observer = batch_observer
        self.stop_requested = stop_requested

    @property
    def stop_reason(self) -> str | None:
        return self.stop_requested() if self.stop_requested is not None else None

    async def dispatch_batch(
        self,
        calls: Iterable[ToolCall],
        *,
        finish_reason: FinishReason,
    ) -> tuple[DispatchOutcome, ...]:
        parsed = self.registry.validate_batch(
            calls,
            finish_reason=finish_reason,
            max_calls=self.max_calls_per_batch,
        )
        return await self.dispatch_parsed(parsed, reserve_budget=True)

    async def dispatch_parsed(
        self,
        parsed: Iterable[ParsedToolCall],
        *,
        reserve_budget: bool,
    ) -> tuple[DispatchOutcome, ...]:
        parsed = tuple(parsed)
        if reserve_budget and self.budget_ledger is not None:
            self.budget_ledger.reserve(BudgetDelta(tool_calls=len(parsed)))

        outcomes: list[DispatchOutcome] = []
        for item in parsed:
            tool_invocation_id = uuid4()
            context_token = _TOOL_EXECUTION_CONTEXT.set(
                ToolExecutionContext(
                    tool_invocation_id=tool_invocation_id,
                    call_id=item.request.call_id,
                    tool_name=item.request.name,
                )
            )
            try:
                value = item.definition.handler(item.arguments)
                if inspect.isawaitable(value):
                    value = await value
                projected = self.projector.project(value)
                result = ToolResult(
                    tool_invocation_id=tool_invocation_id,
                    call_id=item.request.call_id,
                    name=item.request.name,
                    status="SUCCEEDED",
                    content=projected.value,
                )
            except ToolExecutionFailure as exc:
                result = self._failed_result(
                    item,
                    tool_invocation_id,
                    code=exc.code,
                    message=exc.safe_message,
                    retryable=exc.retryable,
                )
            except ToolResultProjectionError as exc:
                result = self._failed_result(
                    item,
                    tool_invocation_id,
                    code=exc.code,
                    message=(
                        "Tool result exceeds the configured transport limit."
                        if exc.code == "MODEL_RESULT_TOO_LARGE"
                        else "Tool result cannot be transported as strict JSON."
                    ),
                )
            except Exception:
                result = self._failed_result(
                    item,
                    tool_invocation_id,
                    code="INTERNAL_TOOL_ERROR",
                    message="The tool could not complete.",
                )
            finally:
                _TOOL_EXECUTION_CONTEXT.reset(context_token)
            outcomes.append(
                DispatchOutcome(
                    result=result,
                    canonical_arguments=item.canonical_arguments,
                )
            )
        if self.batch_observer is not None:
            self.batch_observer()
        return tuple(outcomes)

    @staticmethod
    def _failed_result(
        item: ParsedToolCall,
        tool_invocation_id,
        *,
        code: str,
        message: str,
        retryable: bool = False,
    ) -> ToolResult:
        return ToolResult(
            tool_invocation_id=tool_invocation_id,
            call_id=item.request.call_id,
            name=item.request.name,
            status="FAILED",
            error=ToolError(code=code, message=message, retryable=retryable),
        )
