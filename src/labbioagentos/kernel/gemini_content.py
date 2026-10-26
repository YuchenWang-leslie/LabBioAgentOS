"""Gemini generateContent adapter for the Native Kernel."""

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
    provider_reference,
)
from .continuation import PrivateContinuationStore
from .errors import (
    KernelDeadlineExceededError,
    ProviderBindingMismatchError,
    ProviderProtocolError,
    ProviderTransportError,
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
from .schema_projection import schema_instruction

# Finish reasons that report a provider filter; the lowercased reason is the
# refusal category (DECIDED-C14-9). Every other finish reason except STOP and
# MAX_TOKENS becomes OTHER (DECIDED-C14-2).
_FILTER_FINISHES = frozenset({
    "BLOCKLIST", "ESCALATION", "IMAGE_PROHIBITED_CONTENT", "IMAGE_RECITATION",
    "IMAGE_SAFETY", "LANGUAGE", "PROHIBITED_CONTENT", "PUP_LIMITED_DISABLED",
    "RECITATION", "SAFETY", "SPII",
})
# Part fields that describe a part rather than carry its data.
_PART_METADATA = frozenset({"thought", "thoughtSignature", "partMetadata"})


def _protocol(field: str) -> ProviderProtocolError:
    return ProviderProtocolError(validation_fields=(field,))


class GeminiGenerateContentProvider:
    """Exact-binding generateContent adapter that replays model parts verbatim."""

    def __init__(
        self,
        *,
        binding: ProviderBinding,
        api_key: str,
        transport: HttpStreamTransport | None = None,
    ):
        if not api_key:
            raise ValueError("api_key is required")
        if binding.transport != "GEMINI_GENERATE_CONTENT":
            raise ValueError("Unsupported provider transport")
        self._binding = binding
        self._api_key = api_key
        self._transport = transport or HttpStreamTransport()
        # Each model turn's parts exactly as streamed: a thought signature must
        # return in the part that carried it, and the docs define no merge.
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
                headers={"x-goog-api-key": self._api_key},
                payload=payload,
                deadline_monotonic=request.deadline_monotonic,
                max_response_bytes=response_byte_cap(request.max_output_tokens),
                decode_stream=_reassemble_content_stream,
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
        model = self.binding.model
        if not model.startswith("models/"):
            model = "models/" + model
        return f"{self.binding.endpoint.rstrip('/')}/{model}:streamGenerateContent?alt=sse"

    def _payload(self, request: ModelRequest) -> dict[str, Any]:
        binding = self.binding
        dialect = binding.dialect
        system, contents = self._contents(request)
        if request.response_schema is not None and binding.finalization_json_object:
            system.append({"text": schema_instruction(request.response_schema)})
        payload: dict[str, Any] = {"contents": contents}
        if system:
            payload["systemInstruction"] = {"parts": system}
        if request.tools:
            payload["tools"] = [{"functionDeclarations": [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parametersJsonSchema": tool.parameters,
                }
                for tool in request.tools
            ]}]
            if binding.provider_tool_schema_strict:
                payload["toolConfig"] = {"functionCallingConfig": {"mode": "VALIDATED"}}
        generation: dict[str, Any] = {
            "maxOutputTokens": request.max_output_tokens,
            "thinkingConfig": {"thinkingLevel": (
                dialect.thinking_level_enabled
                if binding.thinking_mode == "ENABLED"
                else dialect.thinking_level_disabled
            )},
        }
        if request.response_schema is not None:
            generation["responseMimeType"] = "application/json"
            if not binding.finalization_json_object:
                generation["responseJsonSchema"] = request.response_schema
        payload["generationConfig"] = generation
        return payload

    def _contents(self, request: ModelRequest):
        system: list[dict[str, Any]] = []
        contents: list[dict[str, Any]] = []
        call_names: dict[str, str] = {}
        leading = True
        for message in request.messages:
            if message.role == "SYSTEM":
                if not leading:
                    # generateContent has no mid-conversation system role.
                    raise _protocol("history.system")
                system.append({"text": message.content})
                continue
            leading = False
            if message.role == "USER":
                contents.append({"role": "user", "parts": [{"text": message.content}]})
            elif message.role == "ASSISTANT":
                for call in message.tool_calls:
                    call_names[call.call_id] = call.name
                contents.append({
                    "role": "model", "parts": self._model_parts(request, message),
                })
            else:
                name = call_names.get(message.tool_call_id)
                if name is None:
                    raise _protocol("history.function_response.name")
                part = {"functionResponse": {
                    "name": name,
                    "id": message.tool_call_id,
                    "response": (
                        {"error": message.content} if message.tool_error
                        else {"output": message.content}
                    ),
                }}
                previous = contents[-1] if contents else None
                if previous is not None and previous["role"] == "user" and all(
                    "functionResponse" in item for item in previous["parts"]
                ):
                    # All responses of one model turn follow all of its calls.
                    previous["parts"].append(part)
                else:
                    contents.append({"role": "user", "parts": [part]})
        return system, contents

    def _model_parts(self, request: ModelRequest, message: PublicMessage):
        stored = self._turns.get(request.kernel_run_id, request.invocation_id, message)
        if stored is not None:
            return json.loads(stored)
        # A turn this provider did not produce here has no signatures to
        # replay; rebuild its public part. No placeholder signature is sent.
        parts: list[dict[str, Any]] = []
        if message.content is not None:
            parts.append({"text": message.content})
        for call in message.tool_calls:
            try:
                arguments = strict_json_loads(call.arguments_json or "")
            except ValueError:
                arguments = None
            if not isinstance(arguments, dict):
                raise _protocol("history.function_call.args")
            parts.append({"functionCall": {
                "name": call.name, "args": arguments, "id": call.call_id,
            }})
        return parts

    def _turn(
        self,
        request: ModelRequest,
        response: Mapping[str, Any],
        retries: tuple[ProviderRetry, ...],
    ) -> ModelTurn:
        component = "usage"
        try:
            usage = self._usage(response["usage"])
            component = "parts"
            text: list[str] = []
            calls: list[ToolCall] = []
            private = False
            for part in response["parts"]:
                private = self._read_part(part, text, calls) or private
            component = "model_turn"
            blocked = response["block_reason"]
            finish = response["finish_reason"]
            if blocked is not None or finish in _FILTER_FINISHES:
                # A filtered turn keeps only its finish reason and category.
                return ModelTurn(
                    finish_reason=FinishReason.CONTENT_FILTER,
                    usage=usage,
                    provider_request_id=provider_reference(response["id"]),
                    private_reasoning_present=private,
                    transport_retries=retries,
                    refusal_category=bounded_provider_name(
                        (blocked if blocked is not None else finish).lower()
                    ),
                )
            if finish == "STOP":
                # Gemini also reports STOP when the turn ends in function calls.
                normalized = FinishReason.TOOL_CALLS if calls else FinishReason.STOP
            elif finish == "MAX_TOKENS":
                normalized = FinishReason.LENGTH
            else:
                normalized = FinishReason.OTHER
            turn = ModelTurn(
                content="".join(text) or None,
                tool_calls=tuple(calls),
                finish_reason=normalized,
                usage=usage,
                provider_request_id=provider_reference(response["id"]),
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
                part for part in response["parts"]
                if part != {"text": ""}
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
    def _read_part(part: Any, text: list[str], calls: list[ToolCall]) -> bool:
        """Collect one part's public data; return whether it is private."""

        if not isinstance(part, dict):
            raise _protocol("stream.part")
        signature = part.get("thoughtSignature")
        if signature is not None and not isinstance(signature, str):
            raise _protocol("stream.part.thoughtSignature")
        if "functionCall" in part:
            call = part["functionCall"]
            if not isinstance(call, dict):
                raise _protocol("stream.function_call")
            if not isinstance(call.get("id"), str):
                # Gemini 3 and later always send an id; older models are unsupported.
                raise _protocol("stream.function_call.id")
            arguments = call.get("args", {})
            if not isinstance(arguments, dict) or not isinstance(call.get("name"), str):
                raise _protocol("stream.function_call")
            calls.append(ToolCall(
                call_id=call["id"],
                name=call["name"],
                arguments_json=json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
            ))
            return signature is not None
        if "text" in part:
            if not isinstance(part["text"], str):
                raise _protocol("stream.part.text")
            if part.get("thought") is True:
                return True
            text.append(part["text"])
            return signature is not None
        kind = next((key for key in part if key not in _PART_METADATA), None)
        raise unsupported_type_error("stream.part", kind)

    @staticmethod
    def _usage(raw) -> TokenUsage:
        if raw is None:
            return TokenUsage()
        if not isinstance(raw, dict):
            raise ValueError("Usage must be an object")
        counts = {}
        for key in (
            "promptTokenCount", "toolUsePromptTokenCount", "candidatesTokenCount",
            "thoughtsTokenCount", "totalTokenCount",
        ):
            value = raw.get(key)
            if value is not None and type(value) is not int:
                raise ValueError("Token counts must be integers")
            counts[key] = value
        prompt = counts["promptTokenCount"]
        generated = [
            counts[key] for key in ("candidatesTokenCount", "thoughtsTokenCount")
            if counts[key] is not None
        ]
        return TokenUsage(
            input_tokens=(
                prompt + (counts["toolUsePromptTokenCount"] or 0)
                if prompt is not None else None
            ),
            # Thinking tokens are generated and billed as output.
            output_tokens=sum(generated) if generated else None,
            total_tokens=counts["totalTokenCount"],
        )


def _raise_stream_error(error: Any):
    status = error.get("status") if isinstance(error, dict) else None
    if status == "UNAVAILABLE":
        raise RetryableStreamError("STREAM_OVERLOADED")
    if status == "RESOURCE_EXHAUSTED":
        raise RetryableStreamError("STREAM_RATE_LIMITED")
    raise ProviderTransportError(validation_type="STREAM_ERROR")


async def _reassemble_content_stream(events: AsyncIterator[SseEvent]) -> Mapping[str, Any]:
    parts: list[Any] = []
    response_id = None
    finish = None
    block_reason = None
    usage = None
    async for event in events:
        chunk = strict_json_loads(event.data)
        if not isinstance(chunk, dict):
            raise _protocol("stream.chunk")
        if "error" in chunk:
            _raise_stream_error(chunk["error"])
        chunk_id = chunk.get("responseId")
        if chunk_id is not None:
            if not isinstance(chunk_id, str) or (response_id and chunk_id != response_id):
                raise _protocol("stream.responseId")
            response_id = chunk_id
        if chunk.get("usageMetadata") is not None:
            usage = chunk["usageMetadata"]
        feedback = chunk.get("promptFeedback")
        if feedback is not None:
            if not isinstance(feedback, dict):
                raise _protocol("stream.promptFeedback")
            if feedback.get("blockReason") is not None:
                if not isinstance(feedback["blockReason"], str):
                    raise _protocol("stream.promptFeedback.blockReason")
                block_reason = feedback["blockReason"]
        candidates = chunk.get("candidates")
        if candidates is None:
            continue
        if not isinstance(candidates, list) or len(candidates) != 1:
            raise _protocol("stream.candidates")
        candidate = candidates[0]
        if not isinstance(candidate, dict) or candidate.get("index", 0) != 0:
            raise _protocol("stream.candidates")
        content = candidate.get("content")
        if content is not None:
            if not isinstance(content, dict) or content.get("role", "model") != "model":
                raise _protocol("stream.content")
            chunk_parts = content.get("parts") or []
            if not isinstance(chunk_parts, list):
                raise _protocol("stream.content.parts")
            parts.extend(chunk_parts)
        if candidate.get("finishReason") is not None:
            if not isinstance(candidate["finishReason"], str):
                raise _protocol("stream.finishReason")
            finish = candidate["finishReason"]
    # There is no end marker: a finish reason, or a blocked prompt, ends the turn.
    if finish is None and block_reason is None:
        raise ProviderTransportError(validation_type="STREAM_INCOMPLETE")
    return {
        "id": response_id,
        "parts": parts,
        "finish_reason": finish,
        "block_reason": block_reason,
        "usage": usage,
    }
