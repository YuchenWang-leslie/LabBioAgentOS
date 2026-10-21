"""Single explicit OpenAI-compatible Chat transport for the Native Kernel."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Mapping
from time import monotonic
from typing import Any, Protocol

# Not used here directly: tests reach the shared client class through
# ``labbioagentos.kernel.openai_chat.httpx.AsyncClient``.
import httpx  # noqa: F401
from pydantic import ValidationError

from .contracts import (
    FinishReason,
    ModelRequest,
    ModelTurn,
    OpenAIChatDialect,
    ProviderBinding,
    ProviderRetry,
    PublicMessage,
    TokenUsage,
    ToolCall,
)
from .continuation import PrivateContinuationStore
from .errors import (
    ProviderBindingMismatchError,
    ProviderProtocolError,
    ProviderTransportError,
)
from .http_stream import (
    DEFAULT_MAX_RETRIES,
    MAX_RESPONSE_BYTES,
    HttpStreamResult,
    HttpStreamTransport,
    RetryPolicy,
    SseEvent,
    response_byte_cap,
    strict_json_loads,
)


class ChatCompletionTransport(Protocol):
    async def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> HttpStreamResult: ...


class HttpxChatCompletionTransport:
    """Chat Completions over the shared provider transport, with no fallback."""

    def __init__(self, *, trust_env: bool = True, max_retries: int = DEFAULT_MAX_RETRIES):
        self.trust_env = trust_env
        self._http = HttpStreamTransport(
            trust_env=trust_env, retry=RetryPolicy(max_retries=max_retries)
        )

    async def post_json(
        self, *, url, headers, payload, timeout_seconds,
        max_response_bytes=MAX_RESPONSE_BYTES,
    ) -> HttpStreamResult:
        return await self._http.post(
            url=url,
            headers=headers,
            payload=payload,
            deadline_monotonic=monotonic() + timeout_seconds,
            max_response_bytes=max_response_bytes,
            decode_stream=(
                _reassemble_chat_stream if payload.get("stream") is True else None
            ),
        )


async def _reassemble_chat_stream(events: AsyncIterator[SseEvent]) -> Mapping[str, Any]:
    request_id = None
    role = "assistant"
    content = []
    reasoning = []
    tool_calls: dict[int, dict[str, Any]] = {}
    finish_reason = None
    usage = None
    done = False
    async for event in events:
        if event.data == "[DONE]":
            done = True
            break
        chunk = strict_json_loads(event.data)
        if not isinstance(chunk, dict):
            raise ValueError("Provider stream chunk must be an object")
        chunk_id = chunk.get("id")
        if chunk_id is not None:
            if not isinstance(chunk_id, str) or (request_id and chunk_id != request_id):
                raise ProviderProtocolError(validation_fields=("stream.id",))
            request_id = chunk_id
        if chunk.get("usage") is not None:
            usage = chunk["usage"]
        choices = chunk.get("choices")
        if choices == []:
            continue
        if not isinstance(choices, list) or len(choices) != 1:
            raise ProviderProtocolError(validation_fields=("stream.choices",))
        choice = choices[0]
        if not isinstance(choice, dict) or choice.get("index") != 0:
            raise ProviderProtocolError(validation_fields=("stream.choices",))
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            raise ProviderProtocolError(validation_fields=("stream.delta",))
        if delta.get("role") is not None:
            role = delta["role"]
        for key, target in (("content", content), ("reasoning_content", reasoning)):
            fragment = delta.get(key)
            if fragment is not None:
                if not isinstance(fragment, str):
                    raise ProviderProtocolError(validation_fields=(f"stream.delta.{key}",))
                target.append(fragment)
        for item in delta.get("tool_calls") or ():
            if not isinstance(item, dict) or type(item.get("index")) is not int or item["index"] < 0:
                raise ProviderProtocolError(validation_fields=("stream.delta.tool_calls",))
            call = tool_calls.setdefault(item["index"], {
                "id": "", "type": "function", "function": {"name": "", "arguments": ""},
            })
            if item.get("type") is not None and item["type"] != call["type"]:
                raise ProviderProtocolError(validation_fields=("stream.delta.tool_calls",))
            if item.get("id") is not None:
                if not isinstance(item["id"], str):
                    raise ProviderProtocolError(validation_fields=("stream.delta.tool_calls",))
                call["id"] += item["id"]
            function = item.get("function") or {}
            if not isinstance(function, dict):
                raise ProviderProtocolError(validation_fields=("stream.delta.tool_calls",))
            for key in ("name", "arguments"):
                fragment = function.get(key)
                if fragment is not None:
                    if not isinstance(fragment, str):
                        raise ProviderProtocolError(validation_fields=("stream.delta.tool_calls",))
                    call["function"][key] += fragment
        if choice.get("finish_reason") is not None:
            finish_reason = choice["finish_reason"]
    if not done:
        raise ProviderTransportError(validation_type="STREAM_INCOMPLETE")
    if finish_reason is None:
        raise ProviderProtocolError(validation_fields=("stream.finish_reason",))
    if sorted(tool_calls) != list(range(len(tool_calls))):
        raise ProviderProtocolError(validation_fields=("stream.delta.tool_calls",))
    return {
        "id": request_id,
        "choices": [{"finish_reason": finish_reason, "message": {
            "role": role,
            "content": "".join(content) if content else None,
            "reasoning_content": "".join(reasoning) if reasoning else None,
            "tool_calls": [tool_calls[index] for index in sorted(tool_calls)],
        }}],
        "usage": usage,
    }


class OpenAICompatibleChatProvider:
    """Exact-binding Chat Completions adapter with run-local private continuity."""

    def __init__(
        self,
        *,
        binding: ProviderBinding,
        api_key: str,
        transport: ChatCompletionTransport | None = None,
    ):
        if not api_key:
            raise ValueError("api_key is required")
        if binding.transport != "OPENAI_COMPATIBLE_CHAT":
            raise ValueError("Unsupported provider transport")
        if binding.dialect != OpenAIChatDialect():
            raise ValueError("Unsupported provider dialect")
        self._binding = binding
        self._api_key = api_key
        self._transport = transport or HttpxChatCompletionTransport()
        self._private_reasoning = PrivateContinuationStore()

    @property
    def binding(self) -> ProviderBinding:
        return self._binding

    async def generate(self, request: ModelRequest) -> ModelTurn:
        if request.binding != self.binding:
            raise ProviderBindingMismatchError()
        timeout = request.deadline_monotonic - monotonic()
        if timeout <= 0:
            raise ProviderTransportError()
        payload = self._payload(request)
        try:
            result = await self._transport.post_json(
                url=self._url(),
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                payload=payload,
                timeout_seconds=timeout,
                max_response_bytes=response_byte_cap(request.max_output_tokens),
            )
        except (ProviderTransportError, ProviderProtocolError):
            raise
        except Exception:
            raise ProviderTransportError(validation_type="ADAPTER") from None
        try:
            return self._parse_response(request, result.body, result.retries)
        except ProviderProtocolError as exc:
            exc.transport_retries = result.retries
            raise

    def clear_invocation(self, kernel_run_id, invocation_id) -> None:
        self._private_reasoning.clear(kernel_run_id, invocation_id)

    def _url(self) -> str:
        endpoint = self.binding.endpoint.rstrip("/")
        if endpoint.endswith("/chat/completions"):
            return endpoint
        return endpoint + "/chat/completions"

    def _payload(self, request: ModelRequest) -> dict[str, Any]:
        messages = [self._message(request, item) for item in request.messages]
        payload: dict[str, Any] = {
            "model": self.binding.model,
            "messages": messages,
            "max_tokens": request.max_output_tokens,
            "n": 1,
            "stream": True,
            "thinking": {
                "type": self.binding.thinking_mode.lower(),
            },
        }
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameters,
                        "strict": self.binding.provider_tool_schema_strict,
                    },
                }
                for tool in request.tools
            ]
        if request.response_schema is not None:
            if self.binding.finalization_json_object:
                payload["response_format"] = {"type": "json_object"}
                schema_text = json.dumps(
                    request.response_schema,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                schema_instruction = (
                    "Return exactly one JSON object that matches this JSON Schema. "
                    "Do not add fields or prose outside the object. JSON Schema: "
                    + schema_text
                )
                for message in messages:
                    if message["role"] == "system":
                        message["content"] = (
                            message.get("content", "") + "\n\n" + schema_instruction
                        )
                        break
                else:
                    messages.insert(0, {
                        "role": "system",
                        "content": schema_instruction,
                    })
            else:
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "structured_output",
                        "strict": True,
                        "schema": request.response_schema,
                    },
                }
        return payload

    def _message(self, request: ModelRequest, message: PublicMessage) -> dict[str, Any]:
        wire: dict[str, Any] = {"role": message.role.lower()}
        if message.content is not None:
            wire["content"] = message.content
        if message.tool_call_id is not None:
            wire["tool_call_id"] = message.tool_call_id
        if message.tool_calls:
            wire["tool_calls"] = [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": call.arguments_json,
                    },
                }
                for call in message.tool_calls
            ]
            reasoning = self._private_reasoning.get(
                request.kernel_run_id, request.invocation_id, message
            )
            if reasoning is not None:
                wire["reasoning_content"] = reasoning
        return wire

    def _parse_response(
        self,
        request,
        response: Mapping[str, Any],
        retries: tuple[ProviderRetry, ...] = (),
    ) -> ModelTurn:
        component = "choices"
        try:
            choices = response["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError("Missing provider choice")
            choice = choices[0]
            if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
                raise ValueError("Missing provider message")
            message = choice["message"]
            component = "message.role"
            if message.get("role", "assistant") != "assistant":
                raise ValueError("Unsupported provider message role")
            component = "message.content"
            content = message.get("content")
            if content is not None and not isinstance(content, str):
                raise ValueError("Unsupported provider content")
            if content == "" and message.get("tool_calls"):
                content = None
            component = "message.tool_calls"
            calls = self._tool_calls(message.get("tool_calls", ()))
            component = "message.reasoning_content"
            private_reasoning = message.get("reasoning_content")
            if private_reasoning is not None and (
                not isinstance(private_reasoning, str)
                or len(private_reasoning) > 64_000
            ):
                raise ValueError("Unsupported private reasoning field")
            finish = self._finish_reason(choice.get("finish_reason"))
            component = "usage"
            usage = self._usage(response.get("usage"))
            component = "id"
            request_id = response.get("id")
            if request_id is not None and not isinstance(request_id, str):
                raise ValueError("Unsupported provider request identity")
            component = "model_turn"
            turn = ModelTurn(
                content=content,
                tool_calls=calls,
                finish_reason=finish,
                usage=usage,
                provider_request_id=request_id,
                private_reasoning_present=private_reasoning is not None,
                transport_retries=retries,
            )
            if private_reasoning is not None and calls:
                self._private_reasoning.put(
                    request.kernel_run_id,
                    request.invocation_id,
                    PublicMessage(role="ASSISTANT", content=content, tool_calls=calls),
                    private_reasoning,
                )
            return turn
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            issues = exc.errors(include_url=False, include_input=False, include_context=False) if isinstance(exc, ValidationError) else ()
            raise ProviderProtocolError(
                validation_type=issues[0]["type"] if issues else type(exc).__name__,
                validation_fields=(component, *(str(item) for item in issues[0]["loc"]))
                if issues and component == "model_turn" else (component,),
            ) from None

    @staticmethod
    def _tool_calls(raw) -> tuple[ToolCall, ...]:
        if raw is None:
            return ()
        if not isinstance(raw, (list, tuple)):
            raise ValueError("Tool calls must be a list")
        calls = []
        for item in raw:
            if not isinstance(item, dict) or item.get("type", "function") != "function":
                raise ValueError("Unsupported tool call")
            function = item.get("function")
            if not isinstance(function, dict):
                raise ValueError("Missing tool function")
            calls.append(
                ToolCall(
                    call_id=item.get("id"),
                    name=function.get("name"),
                    arguments_json=function.get("arguments"),
                )
            )
        return tuple(calls)

    @staticmethod
    def _finish_reason(value) -> FinishReason:
        return {
            "stop": FinishReason.STOP,
            "tool_calls": FinishReason.TOOL_CALLS,
            "length": FinishReason.LENGTH,
            "content_filter": FinishReason.CONTENT_FILTER,
        }.get(value, FinishReason.OTHER)

    @staticmethod
    def _usage(raw) -> TokenUsage:
        if raw is None:
            return TokenUsage()
        if not isinstance(raw, dict):
            raise ValueError("Usage must be an object")
        return TokenUsage(
            input_tokens=raw.get("prompt_tokens"),
            output_tokens=raw.get("completion_tokens"),
            total_tokens=raw.get("total_tokens"),
        )
