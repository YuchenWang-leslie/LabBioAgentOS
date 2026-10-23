"""OpenAI Responses API adapter for the Native Kernel."""

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
)
from .continuation import PrivateContinuationStore
from .errors import (
    KernelDeadlineExceededError,
    ProviderBindingMismatchError,
    ProviderProtocolError,
    ProviderTransportError,
    unsupported_type_error,
)
from .http_stream import (
    HttpStreamTransport,
    RetryableStreamError,
    SseEvent,
    response_byte_cap,
    strict_json_loads,
)
from .schema_projection import schema_instruction

# Terminal events and the response status each must carry. Only the full
# response object of a terminal event builds a turn.
_TERMINAL_EVENTS = {
    "response.completed": "completed",
    "response.incomplete": "incomplete",
    "response.failed": "failed",
}
# Unknown incomplete reasons become OTHER (DECIDED-C14-2).
_INCOMPLETE_REASONS = {
    "max_output_tokens": FinishReason.LENGTH,
    "content_filter": FinishReason.CONTENT_FILTER,
}


def _protocol(field: str) -> ProviderProtocolError:
    return ProviderProtocolError(validation_fields=(field,))


class OpenAIResponsesProvider:
    """Exact-binding stateless Responses API adapter that replays output items verbatim."""

    def __init__(
        self,
        *,
        binding: ProviderBinding,
        api_key: str,
        transport: HttpStreamTransport | None = None,
    ):
        if not api_key:
            raise ValueError("api_key is required")
        if binding.transport != "OPENAI_RESPONSES":
            raise ValueError("Unsupported provider transport")
        self._binding = binding
        self._api_key = api_key
        self._transport = transport or HttpStreamTransport()
        # Each assistant turn's output items exactly as received: nothing is
        # stored server side, so encrypted reasoning returns only through them.
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
                headers={"Authorization": f"Bearer {self._api_key}"},
                payload=payload,
                deadline_monotonic=request.deadline_monotonic,
                max_response_bytes=response_byte_cap(request.max_output_tokens),
                decode_stream=_reassemble_response_stream,
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
            raise

    def clear_invocation(self, kernel_run_id, invocation_id) -> None:
        self._turns.clear(kernel_run_id, invocation_id)

    def _url(self) -> str:
        endpoint = self.binding.endpoint.rstrip("/")
        if endpoint.endswith("/responses"):
            return endpoint
        return endpoint + "/responses"

    def _payload(self, request: ModelRequest) -> dict[str, Any]:
        binding = self.binding
        dialect = binding.dialect
        instructions, items = self._input(request)
        if request.response_schema is not None and binding.finalization_json_object:
            instruction = schema_instruction(request.response_schema)
            instructions = (
                instruction if instructions is None else instructions + "\n\n" + instruction
            )
        payload: dict[str, Any] = {"model": binding.model}
        if instructions is not None:
            payload["instructions"] = instructions
        payload["input"] = items
        payload["max_output_tokens"] = request.max_output_tokens
        payload["stream"] = True
        # Stateless: the service keeps nothing, so every request carries the
        # whole history and none names a previous response.
        payload["store"] = False
        effort = (
            dialect.reasoning_effort_enabled
            if binding.thinking_mode == "ENABLED"
            else dialect.reasoning_effort_disabled
        )
        if effort is not None:
            payload["reasoning"] = {"effort": effort}
            # Without stored state a reasoning item can return only with its
            # encrypted content.
            payload["include"] = ["reasoning.encrypted_content"]
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                    # Responses makes function tools strict unless told otherwise.
                    "strict": binding.provider_tool_schema_strict,
                }
                for tool in request.tools
            ]
        if request.response_schema is not None:
            if binding.finalization_json_object:
                output_format: dict[str, Any] = {"type": "json_object"}
            else:
                output_format = {
                    "type": "json_schema",
                    "name": "structured_output",
                    "schema": request.response_schema,
                    "strict": True,
                }
            payload["text"] = {"format": output_format}
        return payload

    def _input(self, request: ModelRequest):
        instructions = None
        items: list[dict[str, Any]] = []
        for index, message in enumerate(request.messages):
            if message.role == "SYSTEM":
                if index == 0:
                    instructions = message.content
                else:
                    items.append({"role": "system", "content": message.content})
            elif message.role == "USER":
                items.append({"role": "user", "content": message.content})
            elif message.role == "ASSISTANT":
                items.extend(self._assistant_items(request, message))
            else:
                # Responses has no error flag; the result text states a failure.
                items.append({
                    "type": "function_call_output",
                    "call_id": message.tool_call_id,
                    "output": message.content,
                })
        return instructions, items

    def _assistant_items(self, request: ModelRequest, message: PublicMessage):
        stored = self._turns.get(request.kernel_run_id, request.invocation_id, message)
        if stored is not None:
            return json.loads(stored)
        # A turn this provider did not produce in this invocation has no
        # reasoning to replay; rebuild its public part without item ids.
        items: list[dict[str, Any]] = []
        if message.content is not None:
            items.append({"role": "assistant", "content": message.content})
        for call in message.tool_calls:
            if call.arguments_json is None:
                raise _protocol("history.function_call.arguments")
            items.append({
                "type": "function_call",
                "call_id": call.call_id,
                "name": call.name,
                "arguments": call.arguments_json,
            })
        return items

    def _turn(
        self,
        request: ModelRequest,
        response: Mapping[str, Any],
        retries: tuple[ProviderRetry, ...],
    ) -> ModelTurn:
        component = "usage"
        try:
            usage = self._usage(response.get("usage"))
            component = "output"
            items = response["output"]
            if not isinstance(items, list):
                raise TypeError("Output must be a list")
            text: list[str] = []
            calls: list[ToolCall] = []
            kinds = {self._read_item(item, text, calls) for item in items}
            component = "model_turn"
            if response["status"] == "completed":
                finish = FinishReason.TOOL_CALLS if calls else FinishReason.STOP
            else:
                details = response.get("incomplete_details")
                reason = details.get("reason") if isinstance(details, dict) else None
                finish = _INCOMPLETE_REASONS.get(reason, FinishReason.OTHER)
            if "refusal" in kinds or finish is FinishReason.CONTENT_FILTER:
                # A refused or filtered turn keeps only its finish reason; the
                # Responses API declares no refusal category.
                return ModelTurn(
                    finish_reason=FinishReason.CONTENT_FILTER,
                    usage=usage,
                    provider_request_id=response["id"],
                    private_reasoning_present="reasoning" in kinds,
                    transport_retries=retries,
                )
            turn = ModelTurn(
                content="".join(text) or None,
                tool_calls=tuple(calls),
                finish_reason=finish,
                usage=usage,
                provider_request_id=response["id"],
                private_reasoning_present="reasoning" in kinds,
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
            self._turns.put(
                request.kernel_run_id,
                request.invocation_id,
                PublicMessage(
                    role="ASSISTANT", content=turn.content, tool_calls=turn.tool_calls,
                ),
                json.dumps(items, ensure_ascii=False, separators=(",", ":")),
            )
        return turn

    @staticmethod
    def _read_item(item: Any, text: list[str], calls: list[ToolCall]) -> str:
        """Collect one output item's public data and return its kind."""

        if not isinstance(item, dict):
            raise _protocol("response.output")
        kind = item.get("type")
        if kind == "reasoning":
            return kind
        if kind == "function_call":
            for field in ("call_id", "name", "arguments"):
                if not isinstance(item.get(field), str):
                    raise _protocol(f"response.function_call.{field}")
            calls.append(ToolCall(
                call_id=item["call_id"], name=item["name"], arguments_json=item["arguments"],
            ))
            return kind
        if kind == "message":
            parts = item.get("content")
            if item.get("role") != "assistant" or not isinstance(parts, list):
                raise _protocol("response.message")
            refused = False
            for part in parts:
                if not isinstance(part, dict):
                    raise _protocol("response.message.content")
                if part.get("type") == "output_text":
                    if not isinstance(part.get("text"), str):
                        raise _protocol("response.message.content.text")
                    text.append(part["text"])
                elif part.get("type") == "refusal":
                    refused = True
                else:
                    raise unsupported_type_error(
                        "response.message.content.type", part.get("type"),
                    )
            return "refusal" if refused else kind
        raise unsupported_type_error("response.output.type", kind)

    @staticmethod
    def _usage(raw) -> TokenUsage:
        if raw is None:
            return TokenUsage()
        if not isinstance(raw, dict):
            raise ValueError("Usage must be an object")
        counts = {}
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            value = raw.get(key)
            if value is not None and type(value) is not int:
                raise ValueError("Token counts must be integers")
            counts[key] = value
        # Input tokens include cached tokens; output tokens include reasoning.
        return TokenUsage(
            input_tokens=counts["input_tokens"],
            output_tokens=counts["output_tokens"],
            total_tokens=counts["total_tokens"],
        )


def _raise_stream_error(code: Any):
    if code == "rate_limit_exceeded":
        raise RetryableStreamError("STREAM_RATE_LIMITED")
    # server_error included: only overload and rate limits retry (DECIDED-C14-3).
    raise ProviderTransportError(validation_type="STREAM_ERROR")


async def _reassemble_response_stream(events: AsyncIterator[SseEvent]) -> Mapping[str, Any]:
    async for event in events:
        data = strict_json_loads(event.data)
        if not isinstance(data, dict) or not isinstance(data.get("type"), str):
            raise _protocol("stream.event")
        kind = data["type"]
        if kind == "error":
            _raise_stream_error(data.get("code"))
        status = _TERMINAL_EVENTS.get(kind)
        if status is None:
            # Deltas repeat what the terminal response carries, and other
            # events carry no content (DECIDED-C14-2).
            continue
        response = data.get("response")
        if not isinstance(response, dict) or response.get("status") != status:
            raise _protocol("stream.response")
        if status == "failed":
            error = response.get("error")
            _raise_stream_error(error.get("code") if isinstance(error, dict) else None)
        return response
    raise ProviderTransportError(validation_type="STREAM_INCOMPLETE")
