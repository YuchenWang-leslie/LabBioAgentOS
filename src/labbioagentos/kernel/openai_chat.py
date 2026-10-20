"""Single explicit OpenAI-compatible Chat transport for the Native Kernel."""

from __future__ import annotations

import json
from collections.abc import Mapping
from time import monotonic
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from .contracts import (
    FinishReason,
    ModelRequest,
    ModelTurn,
    ProviderBinding,
    PublicMessage,
    TokenUsage,
    ToolCall,
)
from .errors import (
    ProviderBindingMismatchError,
    ProviderProtocolError,
    ProviderTransportError,
)
from .tooling import parse_strict_json_object


def _reject_duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


def _reject_json_constant(_value):
    raise ValueError("Non-finite JSON constant")


class ChatCompletionTransport(Protocol):
    async def post_json(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        timeout_seconds: float,
    ) -> Mapping[str, Any]: ...


class HttpxChatCompletionTransport:
    """One HTTP transport with explicit environment policy and no fallback."""

    def __init__(self, *, trust_env: bool = True):
        self.trust_env = trust_env

    async def post_json(self, *, url, headers, payload, timeout_seconds):
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout_seconds),
                follow_redirects=False,
                trust_env=self.trust_env,
            ) as client:
                async with client.stream(
                    "POST", url, headers=dict(headers), json=dict(payload)
                ) as response:
                    response.raise_for_status()
                    if payload.get("stream") is True:
                        return await self._stream_response(response)
                    body = await response.aread()
            value = json.loads(
                body.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
            if not isinstance(value, dict):
                raise ValueError("Provider response must be an object")
            return value
        except httpx.HTTPStatusError as exc:
            raise ProviderTransportError(
                validation_type=f"HTTP_{exc.response.status_code}"
            ) from None
        except httpx.TimeoutException:
            raise ProviderTransportError(validation_type="TIMEOUT") from None
        except httpx.RemoteProtocolError:
            raise ProviderTransportError(validation_type="NETWORK_REMOTE_PROTOCOL") from None
        except httpx.ReadError:
            raise ProviderTransportError(validation_type="NETWORK_READ") from None
        except httpx.TransportError:
            raise ProviderTransportError(validation_type="NETWORK") from None
        except (UnicodeDecodeError, ValueError):
            raise ProviderTransportError(validation_type="INVALID_JSON") from None
        except ProviderProtocolError:
            raise
        except Exception:
            raise ProviderTransportError(validation_type="OTHER") from None

    @staticmethod
    async def _stream_response(response: httpx.Response) -> Mapping[str, Any]:
        request_id = None
        role = "assistant"
        content = []
        reasoning = []
        tool_calls: dict[int, dict[str, Any]] = {}
        finish_reason = None
        usage = None
        data_lines = []
        done = False
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" "))
                continue
            if line or not data_lines:
                continue
            data = "\n".join(data_lines)
            data_lines.clear()
            if data == "[DONE]":
                done = True
                break
            chunk = json.loads(
                data, object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
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
        if not done or finish_reason is None or data_lines:
            raise ProviderProtocolError(validation_fields=("stream.incomplete",))
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
        self._binding = binding
        self._api_key = api_key
        self._transport = transport or HttpxChatCompletionTransport()
        self._private_reasoning: dict[tuple, str] = {}

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
            response = await self._transport.post_json(
                url=self._url(),
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                payload=payload,
                timeout_seconds=timeout,
            )
        except ProviderTransportError:
            raise
        except Exception:
            raise ProviderTransportError(validation_type="ADAPTER") from None
        return self._parse_response(request, response)

    def clear_invocation(self, kernel_run_id, invocation_id) -> None:
        prefix = (kernel_run_id, invocation_id)
        for key in tuple(self._private_reasoning):
            if key[:2] == prefix:
                del self._private_reasoning[key]

    def discard_tool_batch(
        self, kernel_run_id, invocation_id, calls, *, retained_messages=()
    ) -> None:
        key = self._reasoning_key(kernel_run_id, invocation_id, calls)
        retained = any(
            self._reasoning_key(kernel_run_id, invocation_id, message.tool_calls) == key
            for message in retained_messages if message.tool_calls
        )
        if key is not None and not retained:
            self._private_reasoning.pop(key, None)

    def request_size_bytes(self, request: ModelRequest) -> int:
        """Count the actual wire payload without exposing private continuity."""
        return len(json.dumps(
            self._payload(request), ensure_ascii=False, allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8"))

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
            key = self._reasoning_key(
                request.kernel_run_id,
                request.invocation_id,
                message.tool_calls,
            )
            reasoning = self._private_reasoning.get(key) if key is not None else None
            if reasoning is not None:
                wire["reasoning_content"] = reasoning
        return wire

    def _parse_response(self, request, response: Mapping[str, Any]) -> ModelTurn:
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
            )
            if private_reasoning is not None and calls:
                key = self._reasoning_key(
                    request.kernel_run_id,
                    request.invocation_id,
                    calls,
                )
                if key is not None:
                    self._private_reasoning[key] = private_reasoning
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

    @staticmethod
    def _reasoning_key(kernel_run_id, invocation_id, calls):
        normalized = []
        for call in calls:
            try:
                _arguments, canonical = parse_strict_json_object(
                    call.arguments_json
                )
            except ValueError:
                return None
            normalized.append((call.call_id, call.name, canonical))
        return (
            kernel_run_id,
            invocation_id,
            tuple(normalized),
        )
