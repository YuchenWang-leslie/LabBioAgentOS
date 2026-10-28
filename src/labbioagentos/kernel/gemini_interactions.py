"""Gemini Interactions API adapter for the Native Kernel."""

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
from .schema_projection import schema_instruction

# Error codes that report a safety filter; the code is the refusal category
# (DECIDED-C14-9).
_FILTER_CODES = frozenset({"safety", "jailbreak", "prohibited_content", "blocklist"})
# Step types this adapter reassembles, and the delta types each accepts.
_STEP_DELTAS = {
    "model_output": frozenset({"text"}),
    "thought": frozenset({"thought_signature", "thought_summary"}),
    "function_call": frozenset({"arguments_delta"}),
}
_KNOWN_DELTAS = frozenset().union(*_STEP_DELTAS.values())


def _protocol(field: str) -> ProviderProtocolError:
    return ProviderProtocolError(validation_fields=(field,))


class GeminiInteractionsProvider:
    """Exact-binding stateless Interactions adapter that replays model steps verbatim."""

    def __init__(
        self,
        *,
        binding: ProviderBinding,
        api_key: str,
        transport: HttpStreamTransport | None = None,
    ):
        if not api_key:
            raise ValueError("api_key is required")
        if binding.transport != "GEMINI_INTERACTIONS":
            raise ValueError("Unsupported provider transport")
        self._binding = binding
        self._api_key = api_key
        self._transport = transport or HttpStreamTransport()
        # Each model turn's steps as reassembled from the stream: nothing is
        # stored server side, and thought signatures must return unchanged.
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
                decode_stream=_reassemble_interaction_stream,
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
        if endpoint.endswith("/interactions"):
            return endpoint
        return endpoint + "/interactions"

    def _payload(self, request: ModelRequest) -> dict[str, Any]:
        binding = self.binding
        dialect = binding.dialect
        system, steps = self._input(request)
        if request.response_schema is not None and binding.finalization_json_object:
            system.append(schema_instruction(request.response_schema))
        payload: dict[str, Any] = {"model": binding.model}
        if system:
            payload["system_instruction"] = "\n\n".join(system)
        payload["input"] = steps
        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                }
                for tool in request.tools
            ]
        generation: dict[str, Any] = {
            "max_output_tokens": request.max_output_tokens,
            "thinking_level": (
                dialect.thinking_level_enabled
                if binding.thinking_mode == "ENABLED"
                else dialect.thinking_level_disabled
            ),
        }
        if request.tools and binding.provider_tool_schema_strict:
            generation["tool_choice"] = "validated"
        payload["generation_config"] = generation
        if request.response_schema is not None:
            response_format: dict[str, Any] = {"type": "text", "mime_type": "application/json"}
            if not binding.finalization_json_object:
                response_format["schema"] = request.response_schema
            payload["response_format"] = response_format
        payload["stream"] = True
        # Stateless: the service keeps nothing, so every request carries the
        # whole history and none names a previous interaction.
        payload["store"] = False
        return payload

    def _input(self, request: ModelRequest):
        system: list[str] = []
        steps: list[dict[str, Any]] = []
        call_names: dict[str, str] = {}
        leading = True
        for message in request.messages:
            if message.role == "SYSTEM":
                if not leading:
                    # The Interactions API has no mid-conversation system step.
                    raise _protocol("history.system")
                system.append(message.content)
                continue
            leading = False
            if message.role == "USER":
                steps.append({
                    "type": "user_input",
                    "content": [{"type": "text", "text": message.content}],
                })
            elif message.role == "ASSISTANT":
                for call in message.tool_calls:
                    call_names[call.call_id] = call.name
                steps.extend(self._model_steps(request, message))
            else:
                step: dict[str, Any] = {"type": "function_result", "call_id": message.tool_call_id}
                name = call_names.get(message.tool_call_id)
                if name is not None:
                    step["name"] = name
                step["result"] = [{"type": "text", "text": message.content}]
                if message.tool_error:
                    step["is_error"] = True
                steps.append(step)
        return system, steps

    def _model_steps(self, request: ModelRequest, message: PublicMessage):
        stored = self._turns.get(request.kernel_run_id, request.invocation_id, message)
        if stored is not None:
            return json.loads(stored)
        # A turn this provider did not produce in this invocation has no
        # thought steps to replay; rebuild its public part.
        steps: list[dict[str, Any]] = []
        if message.content is not None:
            steps.append({
                "type": "model_output",
                "content": [{"type": "text", "text": message.content}],
            })
        for call in message.tool_calls:
            try:
                arguments = strict_json_loads(call.arguments_json or "")
            except ValueError:
                arguments = None
            if not isinstance(arguments, dict):
                raise _protocol("history.function_call.arguments")
            steps.append({
                "type": "function_call", "id": call.call_id, "name": call.name,
                "arguments": arguments,
            })
        return steps

    def _turn(
        self,
        request: ModelRequest,
        interaction: Mapping[str, Any],
        retries: tuple[ProviderRetry, ...],
    ) -> ModelTurn:
        component = "usage"
        try:
            usage = self._usage(interaction["usage"])
            component = "steps"
            steps = interaction["steps"]
            private = any(step["type"] == "thought" for step in steps)
            if interaction["filter"] is not None:
                component = "model_turn"
                # A filtered turn keeps only its finish reason and category;
                # its steps may be unfinished and are not read.
                return ModelTurn(
                    finish_reason=FinishReason.CONTENT_FILTER,
                    usage=usage,
                    provider_request_id=provider_reference(interaction["id"]),
                    private_reasoning_present=private,
                    transport_retries=retries,
                    refusal_category=bounded_provider_name(interaction["filter"]),
                )
            raw_arguments = iter(interaction["tool_arguments"])
            text: list[str] = []
            calls: list[ToolCall] = []
            for step in steps:
                if step["type"] == "model_output":
                    text.extend(item["text"] for item in step.get("content", ()))
                elif step["type"] == "function_call":
                    calls.append(ToolCall(
                        call_id=step["id"], name=step["name"], arguments_json=next(raw_arguments),
                    ))
            component = "model_turn"
            status = interaction["status"]
            if status == "requires_action":
                if not calls:
                    raise _protocol("stream.status")
                finish = FinishReason.TOOL_CALLS
            elif status == "completed":
                finish = FinishReason.TOOL_CALLS if calls else FinishReason.STOP
            elif status == "incomplete":
                # The API states no reason; its documented case is the output limit.
                finish = FinishReason.LENGTH
            else:
                finish = FinishReason.OTHER
            turn = ModelTurn(
                content="".join(text) or None,
                tool_calls=tuple(calls),
                finish_reason=finish,
                usage=usage,
                provider_request_id=provider_reference(interaction["id"]),
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
        except (KeyError, TypeError, ValueError, StopIteration) as exc:
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
                json.dumps(steps, ensure_ascii=False, separators=(",", ":")),
            )
        return turn

    @staticmethod
    def _usage(raw) -> TokenUsage:
        if raw is None:
            return TokenUsage()
        if not isinstance(raw, dict):
            raise ValueError("Usage must be an object")
        counts = {}
        for key in (
            "total_input_tokens", "total_tool_use_tokens", "total_output_tokens",
            "total_thought_tokens", "total_tokens",
        ):
            value = raw.get(key)
            if value is not None and type(value) is not int:
                raise ValueError("Token counts must be integers")
            counts[key] = value
        prompt = counts["total_input_tokens"]
        generated = [
            counts[key] for key in ("total_output_tokens", "total_thought_tokens")
            if counts[key] is not None
        ]
        return TokenUsage(
            input_tokens=(
                prompt + (counts["total_tool_use_tokens"] or 0)
                if prompt is not None else None
            ),
            # Thought tokens are generated and billed as output.
            output_tokens=sum(generated) if generated else None,
            total_tokens=counts["total_tokens"],
        )


def _text_content(item: Any, field: str) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise _protocol(field)
    if item.get("type") != "text":
        raise unsupported_type_error(field + ".type", item.get("type"))
    if not isinstance(item.get("text"), str):
        raise _protocol(field + ".text")
    return dict(item)


def _start_step(raw: Any) -> dict[str, Any]:
    """Return the started step as received, checked for the fields it needs."""

    if not isinstance(raw, dict):
        raise _protocol("stream.step")
    kind = raw.get("type")
    step = dict(raw)
    if kind == "model_output":
        content = raw.get("content", [])
        if not isinstance(content, list):
            raise _protocol("stream.step.content")
        if "content" in raw:
            step["content"] = [_text_content(item, "stream.step.content") for item in content]
    elif kind == "thought":
        if not isinstance(raw.get("signature", ""), str):
            raise _protocol("stream.step.signature")
        summary = raw.get("summary", [])
        if not isinstance(summary, list):
            raise _protocol("stream.step.summary")
        if "summary" in raw:
            step["summary"] = [_text_content(item, "stream.step.summary") for item in summary]
    elif kind == "function_call":
        if not isinstance(raw.get("id"), str):
            raise _protocol("stream.function_call.id")
        if not isinstance(raw.get("name"), str):
            raise _protocol("stream.function_call.name")
        if not isinstance(raw.get("arguments", {}), dict):
            raise _protocol("stream.function_call.arguments")
        step["arguments"] = dict(raw.get("arguments", {}))
    else:
        raise unsupported_type_error("stream.step.type", kind)
    return step


def _append_text(items: list[dict[str, Any]], text: str) -> None:
    if items and items[-1].get("type") == "text":
        items[-1]["text"] += text
    else:
        items.append({"type": "text", "text": text})


def _apply_delta(step: dict[str, Any], delta: Any, fragments: list[str]) -> None:
    if not isinstance(delta, dict):
        raise _protocol("stream.delta")
    kind = delta.get("type")
    if kind not in _KNOWN_DELTAS:
        raise unsupported_type_error("stream.delta.type", kind)
    if kind not in _STEP_DELTAS[step["type"]]:
        raise _protocol("stream.delta")
    if kind == "text":
        if not isinstance(delta.get("text"), str):
            raise _protocol("stream.delta.text")
        _append_text(step.setdefault("content", []), delta["text"])
    elif kind == "thought_signature":
        if not isinstance(delta.get("signature"), str):
            raise _protocol("stream.delta.signature")
        step["signature"] = step.get("signature", "") + delta["signature"]
    elif kind == "thought_summary":
        item = _text_content(delta.get("content"), "stream.delta.content")
        _append_text(step.setdefault("summary", []), item["text"])
    else:
        if not isinstance(delta.get("arguments"), str):
            raise _protocol("stream.delta.arguments")
        fragments.append(delta["arguments"])


def _failure(error: Any) -> str:
    """Return a filter code from an error object, or raise its stream error."""

    code = error.get("code") if isinstance(error, dict) else None
    if code in _FILTER_CODES:
        return code
    if code == "unavailable":
        raise RetryableStreamError("STREAM_OVERLOADED")
    if code == "resource_exhausted":
        raise RetryableStreamError("STREAM_RATE_LIMITED")
    raise ProviderTransportError(validation_type="STREAM_ERROR")


def _index(data: Mapping[str, Any]) -> int | None:
    index = data.get("index")
    return index if type(index) is int else None


async def _reassemble_interaction_stream(events: AsyncIterator[SseEvent]) -> Mapping[str, Any]:
    # Without storage (store: false) the service assigns no interaction id.
    interaction_id = None
    created = False
    steps: list[dict[str, Any]] = []
    # index -> argument fragments of each started step not yet stopped
    open_steps: dict[int, list[str]] = {}
    arguments: dict[int, str] = {}

    def result(status: str, usage: Any = None, filter_code: str | None = None):
        return {
            "id": interaction_id,
            "status": status,
            "steps": steps,
            "tool_arguments": [arguments[index] for index in sorted(arguments)],
            "usage": usage,
            "filter": filter_code,
        }

    async for event in events:
        if event.event == "done" or event.data == "[DONE]":
            break
        data = strict_json_loads(event.data)
        if not isinstance(data, dict) or not isinstance(data.get("event_type"), str):
            raise _protocol("stream.event")
        kind = data["event_type"]
        if kind == "error":
            return result("failed", filter_code=_failure(data.get("error")))
        if kind == "interaction.created":
            interaction = data.get("interaction")
            created_id = interaction.get("id") if isinstance(interaction, dict) else None
            if (
                created
                or not isinstance(interaction, dict)
                or (created_id is not None and not isinstance(created_id, str))
            ):
                raise _protocol("stream.interaction.created")
            created = True
            interaction_id = created_id
        elif kind == "step.start":
            index = _index(data)
            if index != len(steps):
                raise _protocol("stream.step.start")
            steps.append(_start_step(data.get("step")))
            open_steps[index] = []
        elif kind == "step.delta":
            index = _index(data)
            if index not in open_steps:
                raise _protocol("stream.step.delta")
            _apply_delta(steps[index], data.get("delta"), open_steps[index])
        elif kind == "step.stop":
            index = _index(data)
            if index not in open_steps:
                raise _protocol("stream.step.stop")
            fragments = open_steps.pop(index)
            step = steps[index]
            if step["type"] == "function_call":
                raw = "".join(fragments) if fragments else json.dumps(
                    step["arguments"], ensure_ascii=False, separators=(",", ":"),
                )
                value = strict_json_loads(raw)
                if not isinstance(value, dict):
                    raise _protocol("stream.function_call.arguments")
                step["arguments"] = value
                arguments[index] = raw
        elif kind == "interaction.completed":
            interaction = data.get("interaction")
            if not isinstance(interaction, dict) or not isinstance(interaction.get("status"), str):
                raise _protocol("stream.interaction.completed")
            completed_id = interaction.get("id")
            if completed_id is not None:
                if not isinstance(completed_id, str) or (
                    interaction_id is not None and completed_id != interaction_id
                ):
                    raise _protocol("stream.interaction.id")
                interaction_id = completed_id
            if open_steps:
                raise _protocol("stream.incomplete")
            if interaction["status"] == "failed":
                errors = interaction.get("errors")
                first = errors[0] if isinstance(errors, list) and errors else None
                return result(
                    "failed", usage=interaction.get("usage"), filter_code=_failure(first),
                )
            return result(interaction["status"], usage=interaction.get("usage"))
        # Any other event type carries no content and is ignored (DECIDED-C14-2).
    raise ProviderTransportError(validation_type="STREAM_INCOMPLETE")
