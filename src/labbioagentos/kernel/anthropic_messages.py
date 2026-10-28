"""Anthropic Messages API adapter for the Native Kernel."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from time import monotonic
from typing import Any

from pydantic import ValidationError

from .contracts import (
    FinishReason,
    ModelRequest,
    ModelTurn,
    ProviderBinding,
    ProviderRetry,
    PublicMessage,
    TokenUsage,
    ToolCall,
    ToolSpec,
    provider_reference,
)
from .continuation import PrivateContinuationStore
from .errors import (
    KernelDeadlineExceededError,
    ProviderBindingMismatchError,
    ProviderProtocolError,
    ProviderTransportError,
    preserve_failure_usage,
    bounded_provider_name,
    unsupported_type_error,
)
from .http_stream import (
    HttpStreamTransport,
    RetryableStreamError,
    SseEvent,
    response_byte_cap,
    strict_json_loads,
)
from .schema_projection import project_json_schema, schema_instruction

# Keywords outside the subset Anthropic enforces for strict tools and JSON
# outputs (minItems is supported only as 0 or 1). Local validation still
# applies the full schema.
STRICT_SCHEMA_REMOVED_KEYWORDS = frozenset({
    "exclusiveMaximum", "exclusiveMinimum", "maxItems", "maxLength",
    "maxProperties", "maximum", "minItems", "minLength", "minProperties",
    "minimum", "multipleOf", "uniqueItems",
})
# Unknown stop reasons, and the known ones this adapter never requests
# (stop_sequence, pause_turn), become OTHER (DECIDED-C14-2).
_STOP_REASONS = {
    "end_turn": FinishReason.STOP,
    "tool_use": FinishReason.TOOL_CALLS,
    "max_tokens": FinishReason.LENGTH,
    "model_context_window_exceeded": FinishReason.LENGTH,
    "refusal": FinishReason.CONTENT_FILTER,
}
# delta type -> (block type it updates, field it carries)
_DELTAS = {
    "text_delta": ("text", "text"),
    "thinking_delta": ("thinking", "thinking"),
    "signature_delta": ("thinking", "signature"),
    "input_json_delta": ("tool_use", "partial_json"),
}
_PRIVATE_BLOCKS = frozenset({"thinking", "redacted_thinking"})


def _protocol(field: str) -> ProviderProtocolError:
    return ProviderProtocolError(validation_fields=(field,))


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise _protocol(field)
    return value


class AnthropicMessagesProvider:
    """Exact-binding Messages API adapter that replays each assistant turn verbatim."""

    def __init__(
        self,
        *,
        binding: ProviderBinding,
        api_key: str,
        transport: HttpStreamTransport | None = None,
    ):
        if not api_key:
            raise ValueError("api_key is required")
        if binding.transport != "ANTHROPIC_MESSAGES":
            raise ValueError("Unsupported provider transport")
        self._binding = binding
        self._api_key = api_key
        self._transport = transport or HttpStreamTransport()
        # Each assistant turn exactly as received: a thinking signature is bound
        # to the conversation before it, so replay must not rebuild any turn.
        self._turns = PrivateContinuationStore()

    @property
    def binding(self) -> ProviderBinding:
        return self._binding

    async def generate(self, request: ModelRequest) -> ModelTurn:
        if request.binding != self.binding:
            raise ProviderBindingMismatchError()
        if request.deadline_monotonic - monotonic() <= 0:
            raise ProviderTransportError()
        payload = self._payload(request)
        try:
            result = await self._transport.post(
                url=self._url(),
                headers={
                    "x-api-key": self._api_key,
                    "anthropic-version": self.binding.dialect.anthropic_version,
                },
                payload=payload,
                deadline_monotonic=request.deadline_monotonic,
                max_response_bytes=response_byte_cap(request.max_output_tokens),
                decode_stream=_reassemble_message_stream,
            )
        except (
            KernelDeadlineExceededError,
            ProviderTransportError,
            ProviderProtocolError,
        ):
            raise
        except Exception:
            raise ProviderTransportError(validation_type="ADAPTER") from None
        try:
            return self._turn(request, result.body, result.retries)
        except ProviderProtocolError as exc:
            exc.transport_retries = result.retries
            preserve_failure_usage(exc, result.body.get("usage"), self._usage)
            raise

    def clear_invocation(self, kernel_run_id, invocation_id) -> None:
        self._turns.clear(kernel_run_id, invocation_id)

    def discard_tool_batch(self, kernel_run_id, invocation_id, calls, *, retained_messages=()) -> None:
        self._turns.discard_tool_batch(
            kernel_run_id, invocation_id, calls, retained_messages=retained_messages,
        )

    def request_size_bytes(self, request: ModelRequest) -> int:
        """Include provider-only continuation and schemas in request admission."""
        return len(json.dumps(
            self._payload(request), ensure_ascii=False, allow_nan=False, separators=(",", ":"),
        ).encode("utf-8"))

    def _url(self) -> str:
        endpoint = self.binding.endpoint.rstrip("/")
        if endpoint.endswith("/messages"):
            return endpoint
        return endpoint + "/messages"

    def _payload(self, request: ModelRequest) -> dict[str, Any]:
        binding = self.binding
        dialect = binding.dialect
        system, messages = self._messages(request)
        if request.response_schema is not None and binding.finalization_json_object:
            system.append({
                "type": "text", "text": schema_instruction(request.response_schema),
            })
        payload: dict[str, Any] = {
            "model": binding.model,
            "max_tokens": request.max_output_tokens,
            "stream": True,
        }
        if system:
            payload["system"] = system
        payload["messages"] = messages
        if binding.thinking_mode == "ENABLED":
            payload["thinking"] = {"type": "adaptive"}
            effort = dialect.effort_enabled
        else:
            if dialect.thinking_off == "DISABLED":
                payload["thinking"] = {"type": "disabled"}
            elif dialect.thinking_off == "BETWEEN_TOOLS":
                payload["thinking"] = {"type": "between_tools"}
            effort = dialect.effort_disabled
        output_config: dict[str, Any] = {}
        if effort is not None:
            output_config["effort"] = effort
        if request.response_schema is not None and not binding.finalization_json_object:
            output_config["format"] = {
                "type": "json_schema",
                "schema": project_json_schema(
                    request.response_schema,
                    removed_keywords=STRICT_SCHEMA_REMOVED_KEYWORDS,
                ),
            }
        if output_config:
            payload["output_config"] = output_config
        if request.tools:
            payload["tools"] = [
                self._tool(tool, binding.provider_tool_schema_strict)
                for tool in request.tools
            ]
        return payload

    @staticmethod
    def _tool(tool: ToolSpec, strict: bool) -> dict[str, Any]:
        if not strict:
            return {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.parameters,
            }
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": project_json_schema(
                tool.parameters, removed_keywords=STRICT_SCHEMA_REMOVED_KEYWORDS
            ),
            "strict": True,
        }

    def _messages(self, request: ModelRequest):
        system: list[dict[str, Any]] = []
        messages: list[dict[str, Any]] = []
        leading = True
        for message in request.messages:
            if message.role == "SYSTEM":
                if leading:
                    system.append({"type": "text", "text": message.content})
                else:
                    messages.append({"role": "system", "content": message.content})
                continue
            leading = False
            if message.role == "USER":
                messages.append({"role": "user", "content": message.content})
            elif message.role == "ASSISTANT":
                messages.append({
                    "role": "assistant",
                    "content": self._assistant_content(request, message),
                })
            else:
                block: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id,
                    "content": message.content,
                }
                if message.tool_error:
                    block["is_error"] = True
                previous = messages[-1] if messages else None
                if (
                    previous is not None
                    and previous["role"] == "user"
                    and isinstance(previous["content"], list)
                ):
                    # Every result of one assistant turn goes in one user message.
                    previous["content"].append(block)
                else:
                    messages.append({"role": "user", "content": [block]})
        return system, messages

    def _assistant_content(self, request: ModelRequest, message: PublicMessage):
        stored = self._turns.get(request.kernel_run_id, request.invocation_id, message)
        if stored is not None:
            return json.loads(stored)
        # A turn this provider did not produce in this invocation has no
        # private blocks to replay; rebuild its public part.
        content: list[dict[str, Any]] = []
        if message.content is not None:
            content.append({"type": "text", "text": message.content})
        for call in message.tool_calls:
            try:
                arguments = strict_json_loads(call.arguments_json or "")
            except ValueError:
                arguments = None
            if not isinstance(arguments, dict):
                raise _protocol("history.tool_use.input")
            content.append({
                "type": "tool_use", "id": call.call_id, "name": call.name,
                "input": arguments,
            })
        return content

    def _turn(
        self,
        request: ModelRequest,
        message: Mapping[str, Any],
        retries: tuple[ProviderRetry, ...],
    ) -> ModelTurn:
        component = "usage"
        try:
            usage = self._usage(message["usage"])
            blocks = message["content"]
            finish = _STOP_REASONS.get(message["stop_reason"], FinishReason.OTHER)
            private = any(block["type"] in _PRIVATE_BLOCKS for block in blocks)
            component = "model_turn"
            if finish is FinishReason.CONTENT_FILTER:
                # A refusal keeps only its finish reason and declared category.
                details = message["stop_details"]
                category = details.get("category") if isinstance(details, dict) else None
                return ModelTurn(
                    finish_reason=finish,
                    usage=usage,
                    provider_request_id=provider_reference(message["id"]),
                    private_reasoning_present=private,
                    transport_retries=retries,
                    refusal_category=(
                        None if category is None else bounded_provider_name(category)
                    ),
                )
            text = "".join(block["text"] for block in blocks if block["type"] == "text")
            tool_blocks = [block for block in blocks if block["type"] == "tool_use"]
            calls = tuple(
                ToolCall(call_id=block["id"], name=block["name"], arguments_json=raw)
                for block, raw in zip(tool_blocks, message["tool_arguments"], strict=True)
            )
            turn = ModelTurn(
                content=text or None,
                tool_calls=calls,
                finish_reason=finish,
                usage=usage,
                provider_request_id=provider_reference(message["id"]),
                private_reasoning_present=private,
                transport_retries=retries,
            )
        except ValidationError as exc:
            issues = exc.errors(include_url=False, include_input=False, include_context=False)
            first = issues[0] if issues else None
            raise ProviderProtocolError(
                validation_type=first["type"] if first else "invalid",
                validation_fields=(
                    (component, *(str(item) for item in first["loc"]))
                    if first and component == "model_turn" else (component,)
                ),
            ) from None
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderProtocolError(
                validation_type=type(exc).__name__, validation_fields=(component,),
            ) from None
        if turn.content is not None or turn.tool_calls:
            replay = [
                block for block in blocks
                if not (block["type"] == "text" and not block["text"])
            ]
            self._turns.put(
                request.kernel_run_id,
                request.invocation_id,
                PublicMessage(
                    role="ASSISTANT", content=turn.content, tool_calls=turn.tool_calls,
                ),
                json.dumps(replay, ensure_ascii=False, separators=(",", ":")),
            )
        return turn

    @staticmethod
    def _usage(raw) -> TokenUsage:
        if not isinstance(raw, dict):
            raise ValueError("Usage must be an object")
        counts = {}
        for key in (
            "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens",
            "output_tokens",
        ):
            value = raw.get(key)
            if value is not None and type(value) is not int:
                raise ValueError("Token counts must be integers")
            counts[key] = value
        input_tokens = counts["input_tokens"]
        if input_tokens is not None:
            # Anthropic reports cached prompt tokens apart from input_tokens.
            input_tokens += (counts["cache_creation_input_tokens"] or 0) + (
                counts["cache_read_input_tokens"] or 0
            )
        output_tokens = counts["output_tokens"]
        return TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=(
                input_tokens + output_tokens
                if input_tokens is not None and output_tokens is not None
                else None
            ),
        )


def _start_block(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise _protocol("stream.content_block")
    kind = raw.get("type")
    if kind == "text":
        return {"type": "text", "text": _string(raw.get("text", ""), "stream.content_block.text")}
    if kind == "thinking":
        return {
            "type": "thinking",
            "thinking": _string(raw.get("thinking", ""), "stream.content_block.thinking"),
            "signature": _string(raw.get("signature", ""), "stream.content_block.signature"),
        }
    if kind == "redacted_thinking":
        return {
            "type": "redacted_thinking",
            "data": _string(raw.get("data"), "stream.content_block.data"),
        }
    if kind == "tool_use":
        arguments = raw.get("input", {})
        if not isinstance(arguments, dict):
            raise _protocol("stream.content_block.input")
        return {
            "type": "tool_use",
            "id": _string(raw.get("id"), "stream.content_block.id"),
            "name": _string(raw.get("name"), "stream.content_block.name"),
            "input": arguments,
        }
    raise unsupported_type_error("stream.content_block.type", kind)


def _raise_stream_error(error: Any):
    kind = error.get("type") if isinstance(error, dict) else None
    if kind == "overloaded_error":
        raise RetryableStreamError("STREAM_OVERLOADED")
    if kind == "rate_limit_error":
        raise RetryableStreamError("STREAM_RATE_LIMITED")
    raise ProviderTransportError(validation_type="STREAM_ERROR")


async def _reassemble_message_stream(events: AsyncIterator[SseEvent]) -> Mapping[str, Any]:
    message = None
    blocks: list[dict[str, Any]] = []
    arguments: list[str] = []
    partial_json: list[str] = []
    open_index = None
    stop_reason = None
    stop_details = None
    usage: dict[str, Any] = {}
    done = False
    async for event in events:
        data = strict_json_loads(event.data)
        if not isinstance(data, dict) or not isinstance(data.get("type"), str):
            raise _protocol("stream.event")
        kind = data["type"]
        if kind == "ping":
            continue
        if kind == "error":
            _raise_stream_error(data.get("error"))
        if kind == "message_start":
            start = data.get("message")
            if (
                message is not None
                or not isinstance(start, dict)
                or start.get("role") != "assistant"
                or not isinstance(start.get("id"), str)
                or not isinstance(start.get("usage", {}), dict)
            ):
                raise _protocol("stream.message_start")
            message = start
            usage.update(start.get("usage") or {})
        elif kind in {"content_block_start", "content_block_delta", "content_block_stop",
                      "message_delta", "message_stop"} and message is None:
            raise _protocol("stream.message_start")
        elif kind == "content_block_start":
            if open_index is not None or data.get("index") != len(blocks):
                raise _protocol("stream.content_block_start")
            blocks.append(_start_block(data.get("content_block")))
            open_index = len(blocks) - 1
            partial_json = []
        elif kind == "content_block_delta":
            if open_index is None or data.get("index") != open_index:
                raise _protocol("stream.content_block_delta")
            block = blocks[open_index]
            delta = data.get("delta")
            if not isinstance(delta, dict):
                raise _protocol("stream.delta")
            if delta.get("type") not in _DELTAS:
                raise unsupported_type_error("stream.delta.type", delta.get("type"))
            block_type, field = _DELTAS[delta["type"]]
            if block["type"] != block_type:
                raise _protocol("stream.delta")
            fragment = _string(delta.get(field), f"stream.delta.{field}")
            if field == "partial_json":
                partial_json.append(fragment)
            else:
                block[field] += fragment
        elif kind == "content_block_stop":
            if open_index is None or data.get("index") != open_index:
                raise _protocol("stream.content_block_stop")
            block = blocks[open_index]
            if block["type"] == "tool_use":
                raw = "".join(partial_json)
                if raw:
                    value = strict_json_loads(raw)
                    if not isinstance(value, dict):
                        raise _protocol("stream.tool_use.input")
                    block["input"] = value
                else:
                    raw = json.dumps(block["input"], ensure_ascii=False, separators=(",", ":"))
                arguments.append(raw)
            open_index = None
        elif kind == "message_delta":
            delta = data.get("delta")
            if not isinstance(delta, dict):
                raise _protocol("stream.message_delta")
            if delta.get("stop_reason") is not None:
                stop_reason = _string(delta["stop_reason"], "stream.stop_reason")
            if delta.get("stop_details") is not None:
                stop_details = delta["stop_details"]
            if data.get("usage") is not None:
                if not isinstance(data["usage"], dict):
                    raise _protocol("stream.usage")
                usage.update(data["usage"])
        elif kind == "message_stop":
            done = True
            break
        # Any other event type carries no content and is ignored (DECIDED-C14-2).
    if not done:
        raise ProviderTransportError(validation_type="STREAM_INCOMPLETE")
    if open_index is not None or stop_reason is None:
        raise _protocol("stream.incomplete")
    return {
        "id": message["id"],
        "content": blocks,
        "tool_arguments": arguments,
        "stop_reason": stop_reason,
        "stop_details": stop_details,
        "usage": usage,
    }
