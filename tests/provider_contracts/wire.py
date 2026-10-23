"""One neutral provider script in the wire form of each C14 API format.

Each wire variant turns a neutral ``Turn`` into the exact server-sent event
stream its format documents, builds the real adapter over the real shared
transport, and reads the parts of a request body the contracts compare.
Differences between formats are declared here as attributes, never skipped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from labbioagentos.kernel import (
    AnthropicMessagesDialect,
    AnthropicMessagesProvider,
    GeminiGenerateContentDialect,
    GeminiGenerateContentProvider,
    GeminiInteractionsDialect,
    GeminiInteractionsProvider,
    HttpxChatCompletionTransport,
    OpenAIChatDialect,
    OpenAICompatibleChatProvider,
    OpenAIResponsesDialect,
    OpenAIResponsesProvider,
    ProviderBinding,
)
from labbioagentos.kernel.http_stream import HttpStreamTransport
from labbioagentos.kernel.schema_projection import schema_instruction

KEY = "PRIVATE_KEY_CANARY"
# Normalized usage every variant's known usage block must produce.
KNOWN_USAGE = (11, 7, 18)


@dataclass(frozen=True)
class Call:
    call_id: str
    arguments: dict[str, Any]
    name: str = "record_value"

    @property
    def raw(self) -> str:
        return json.dumps(self.arguments, separators=(",", ":"))


@dataclass(frozen=True)
class Turn:
    """One provider turn: public output, an opaque private value, finish, usage."""

    text: str | None = None
    calls: tuple[Call, ...] = ()
    # Opaque private continuation the provider returns: reasoning text, an
    # encrypted item or a signature, depending on the format.
    reasoning: str | None = None
    # STOP, TOOL_CALLS, LENGTH, CONTENT_FILTER or OTHER; None follows the calls.
    finish: str | None = None
    usage: bool = True

    @property
    def resolved_finish(self) -> str:
        return self.finish or ("TOOL_CALLS" if self.calls else "STOP")


async def _no_wait(_seconds):
    return None


def _stream_transport() -> HttpStreamTransport:
    return HttpStreamTransport(sleep=_no_wait, random_fraction=lambda: 0.0)


def _data(value) -> str:
    return "data: " + json.dumps(value) + "\n\n"


def _named(kind: str, value) -> str:
    return f"event: {kind}\ndata: " + json.dumps(value) + "\n\n"


class Wire:
    name: str
    transport: str
    history_key: str
    endpoint = "https://contract.invalid/v1"
    # Whether the format returns a private continuation to replay.
    carries_reasoning = True
    # Category of the format's retryable in-stream error, or None if it has none.
    stream_retry: str | None = None
    # Category a CONTENT_FILTER turn declares in this format.
    filter_category: str | None = None
    # Whether a provider-filtered turn keeps its output (a declared difference).
    filter_keeps_output = False

    def dialect(self):
        return None

    def binding(self, *, thinking=True, strict=False, json_object=False) -> ProviderBinding:
        values: dict[str, Any] = {
            "provider": "contract",
            "model": "contract-model",
            "endpoint": self.endpoint,
            "transport": self.transport,
            "thinking_mode": "ENABLED" if thinking else "DISABLED",
            "provider_tool_schema_strict": strict,
            "finalization_json_object": json_object,
        }
        dialect = self.dialect()
        if dialect is not None:
            values["dialect"] = dialect
        return ProviderBinding(**values)

    def history(self, body) -> list:
        return body[self.history_key]

    def fixed(self, body) -> dict:
        return {key: value for key, value in body.items() if key != self.history_key}

    def __repr__(self) -> str:
        return self.name


class ChatVendorWire(Wire):
    name = "chat-vendor"
    transport = "OPENAI_COMPATIBLE_CHAT"
    history_key = "messages"
    # Only a refusal drops the output; a content_filter finish keeps it.
    filter_keeps_output = True
    _finish = {"STOP": "stop", "TOOL_CALLS": "tool_calls", "LENGTH": "length",
               "CONTENT_FILTER": "content_filter", "OTHER": "future_reason"}
    _usage = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}

    def provider(self, binding):
        transport = HttpxChatCompletionTransport()
        # The Chat transport wraps the shared one; swap in one that does not wait.
        transport._http = _stream_transport()
        return OpenAICompatibleChatProvider(binding=binding, api_key=KEY, transport=transport)

    @staticmethod
    def _chunk(number, delta, *, finish=None, **extra):
        return {"id": f"chatcmpl-{number}", "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}], **extra}

    def _parts(self, turn, number):
        chunks = [self._chunk(number, {"role": "assistant"})]
        if turn.reasoning is not None and self.carries_reasoning:
            chunks.append(self._chunk(number, {"reasoning_content": turn.reasoning}))
        if turn.text is not None:
            chunks.append(self._chunk(number, {"content": turn.text}))
        for index, call in enumerate(turn.calls):
            chunks.append(self._chunk(number, {"tool_calls": [{
                "index": index, "id": call.call_id, "type": "function",
                "function": {"name": call.name, "arguments": call.raw},
            }]}))
        return chunks

    def _closing(self, turn, number):
        return [self._chunk(number, {}, finish=self._finish[turn.resolved_finish],
                            usage=self._usage if turn.usage else None)]

    def encode(self, turn, number=1) -> str:
        chunks = self._parts(turn, number) + self._closing(turn, number)
        return "".join(map(_data, chunks)) + "data: [DONE]\n\n"

    def truncated(self, turn) -> str:
        return "".join(map(_data, self._parts(turn, 1)))

    def retryable_error(self):
        return None

    def fatal_error(self) -> str:
        return (_data(self._chunk(1, {"role": "assistant"}))
                + _data({"error": {"message": "PRIVATE_CANARY", "type": "server_error"}}))

    def unknown_content(self):
        body = (_data(self._chunk(1, {"tool_calls": [{
            "index": 0, "id": "call-x", "type": "custom", "function": {"name": "x"},
        }]}, finish="tool_calls")) + "data: [DONE]\n\n")
        return body, ("stream.delta.tool_calls.type", "custom")

    def last_user_text(self, body) -> str:
        return [item for item in body["messages"] if item["role"] == "user"][-1]["content"]

    def result_call_ids(self, body) -> list[str]:
        return [item["tool_call_id"] for item in body["messages"] if item["role"] == "tool"]

    def schema_location(self, body, schema) -> str | None:
        response_format = body.get("response_format", {})
        if response_format.get("type") == "json_schema":
            return "native" if response_format["json_schema"]["schema"] == schema else None
        system = body["messages"][0]
        if (response_format.get("type") == "json_object" and system["role"] == "system"
                and schema_instruction(schema) in system["content"]):
            return "instruction"
        return None


class ChatOfficialWire(ChatVendorWire):
    name = "chat-official"
    # Official Chat Completions returns no reasoning to replay.
    carries_reasoning = False

    def dialect(self):
        return OpenAIChatDialect(
            thinking_parameter="REASONING_EFFORT", reasoning_effort_enabled="high",
            reasoning_effort_disabled="none", output_limit_parameter="max_completion_tokens",
            stream_include_usage=True,
        )

    def _closing(self, turn, number):
        chunks = [self._chunk(number, {}, finish=self._finish[turn.resolved_finish])]
        if turn.usage:
            # The usage chunk follows the finish and has no choices.
            chunks.append({"id": f"chatcmpl-{number}", "object": "chat.completion.chunk",
                           "choices": [], "usage": self._usage})
        return chunks


class ResponsesWire(Wire):
    name = "responses"
    transport = "OPENAI_RESPONSES"
    history_key = "input"
    stream_retry = "STREAM_RATE_LIMITED"
    _status = {"STOP": ("completed", None), "TOOL_CALLS": ("completed", None),
               "LENGTH": ("incomplete", "max_output_tokens"),
               "CONTENT_FILTER": ("incomplete", "content_filter"),
               "OTHER": ("incomplete", "future_reason")}
    _usage = {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18}

    def dialect(self):
        return OpenAIResponsesDialect(reasoning_effort_enabled="high",
                                      reasoning_effort_disabled="low")

    def provider(self, binding):
        return OpenAIResponsesProvider(binding=binding, api_key=KEY,
                                       transport=_stream_transport())

    def _created(self, number):
        return _named("response.created", {"type": "response.created", "response": {
            "id": f"resp_{number}", "object": "response", "status": "in_progress",
            "output": [], "usage": None,
        }})

    def encode(self, turn, number=1) -> str:
        output: list[dict[str, Any]] = []
        if turn.reasoning is not None:
            output.append({"id": f"rs_{number}", "type": "reasoning", "summary": [],
                           "encrypted_content": turn.reasoning})
        if turn.text is not None:
            output.append({"id": f"msg_{number}", "type": "message", "status": "completed",
                           "role": "assistant", "content": [
                               {"type": "output_text", "text": turn.text, "annotations": []},
                           ]})
        for call in turn.calls:
            output.append({"id": f"fc_{number}_{call.call_id}", "type": "function_call",
                           "status": "completed", "call_id": call.call_id, "name": call.name,
                           "arguments": call.raw})
        status, reason = self._status[turn.resolved_finish]
        response = {"id": f"resp_{number}", "object": "response", "status": status,
                    "output": output,
                    "incomplete_details": {"reason": reason} if reason else None,
                    "usage": self._usage if turn.usage else None}
        return self._created(number) + _named(
            f"response.{status}", {"type": f"response.{status}", "response": response},
        )

    def truncated(self, turn) -> str:
        return self._created(1) + _named("response.output_text.delta", {
            "type": "response.output_text.delta", "item_id": "msg_1", "delta": "partial",
        })

    def retryable_error(self) -> str:
        return _named("error", {"type": "error", "code": "rate_limit_exceeded",
                                "message": "PRIVATE_CANARY", "param": None})

    def fatal_error(self) -> str:
        return _named("error", {"type": "error", "code": "server_error",
                                "message": "PRIVATE_CANARY", "param": None})

    def unknown_content(self):
        response = {"id": "resp_1", "object": "response", "status": "completed",
                    "output": [{"id": "ws_1", "type": "web_search_call", "status": "completed"}]}
        body = self._created(1) + _named("response.completed",
                                         {"type": "response.completed", "response": response})
        return body, ("response.output.type", "web_search_call")

    def last_user_text(self, body) -> str:
        return [item for item in body["input"] if item.get("role") == "user"][-1]["content"]

    def result_call_ids(self, body) -> list[str]:
        return [item["call_id"] for item in body["input"]
                if item.get("type") == "function_call_output"]

    def schema_location(self, body, schema) -> str | None:
        output_format = body.get("text", {}).get("format", {})
        if output_format.get("type") == "json_schema":
            return "native" if output_format["schema"] == schema else None
        if (output_format.get("type") == "json_object"
                and schema_instruction(schema) in body.get("instructions", "")):
            return "instruction"
        return None


class AnthropicWire(Wire):
    name = "anthropic"
    transport = "ANTHROPIC_MESSAGES"
    history_key = "messages"
    stream_retry = "STREAM_OVERLOADED"
    filter_category = "bio"
    _stop = {"STOP": "end_turn", "TOOL_CALLS": "tool_use", "LENGTH": "max_tokens",
             "CONTENT_FILTER": "refusal", "OTHER": "future_reason"}

    def dialect(self):
        return AnthropicMessagesDialect(thinking_off="DISABLED")

    def provider(self, binding):
        return AnthropicMessagesProvider(binding=binding, api_key=KEY,
                                         transport=_stream_transport())

    @staticmethod
    def _event(kind, **data):
        return _named(kind, {"type": kind, **data})

    def _start(self, turn, number):
        message = {"id": f"msg_{number}", "type": "message", "role": "assistant", "content": [],
                   "model": "contract-model", "stop_reason": None}
        if turn.usage:
            # Cached prompt tokens are reported apart from input_tokens.
            message["usage"] = {"input_tokens": 8, "cache_read_input_tokens": 3,
                                "output_tokens": 1}
        return self._event("message_start", message=message)

    def _blocks(self, turn):
        event, index, body = self._event, 0, ""
        if turn.reasoning is not None:
            body += (event("content_block_start", index=index, content_block={
                         "type": "thinking", "thinking": "", "signature": ""})
                     + event("content_block_delta", index=index,
                             delta={"type": "signature_delta", "signature": turn.reasoning})
                     + event("content_block_stop", index=index))
            index += 1
        if turn.text is not None:
            body += (event("content_block_start", index=index,
                           content_block={"type": "text", "text": ""})
                     + event("content_block_delta", index=index,
                             delta={"type": "text_delta", "text": turn.text})
                     + event("content_block_stop", index=index))
            index += 1
        for call in turn.calls:
            body += (event("content_block_start", index=index, content_block={
                         "type": "tool_use", "id": call.call_id, "name": call.name, "input": {}})
                     + event("content_block_delta", index=index,
                             delta={"type": "input_json_delta", "partial_json": call.raw})
                     + event("content_block_stop", index=index))
            index += 1
        return body

    def encode(self, turn, number=1) -> str:
        finish = turn.resolved_finish
        delta = {"delta": {
            "stop_reason": self._stop[finish], "stop_sequence": None,
            "stop_details": ({"type": "refusal", "category": "bio"}
                             if finish == "CONTENT_FILTER" else None),
        }}
        if turn.usage:
            delta["usage"] = {"output_tokens": 7}
        return (self._start(turn, number) + self._blocks(turn)
                + self._event("message_delta", **delta) + self._event("message_stop"))

    def truncated(self, turn) -> str:
        return self._start(turn, 1) + self._blocks(turn)

    def retryable_error(self) -> str:
        return self._event("error", error={"type": "overloaded_error",
                                           "message": "PRIVATE_CANARY"})

    def fatal_error(self) -> str:
        return self._event("error", error={"type": "api_error", "message": "PRIVATE_CANARY"})

    def unknown_content(self):
        body = (self._start(Turn(), 1) + self._event(
            "content_block_start", index=0,
            content_block={"type": "server_tool_use", "id": "srvtoolu_1", "name": "x"},
        ))
        return body, ("stream.content_block.type", "server_tool_use")

    def last_user_text(self, body) -> str:
        return [item for item in body["messages"]
                if item["role"] == "user" and isinstance(item["content"], str)][-1]["content"]

    def result_call_ids(self, body) -> list[str]:
        return [block["tool_use_id"] for item in body["messages"]
                if item["role"] == "user" and isinstance(item["content"], list)
                for block in item["content"] if block["type"] == "tool_result"]

    def schema_location(self, body, schema) -> str | None:
        output_format = body.get("output_config", {}).get("format")
        if output_format is not None:
            return "native" if output_format["type"] == "json_schema" else None
        system = "".join(block["text"] for block in body.get("system", ()))
        return "instruction" if schema_instruction(schema) in system else None


class GeminiWire(Wire):
    name = "gemini"
    transport = "GEMINI_GENERATE_CONTENT"
    history_key = "contents"
    stream_retry = "STREAM_OVERLOADED"
    filter_category = "safety"
    _finish = {"STOP": "STOP", "TOOL_CALLS": "STOP", "LENGTH": "MAX_TOKENS",
               "CONTENT_FILTER": "SAFETY", "OTHER": "FUTURE_REASON"}
    _usage = {"promptTokenCount": 9, "toolUsePromptTokenCount": 2, "candidatesTokenCount": 4,
              "thoughtsTokenCount": 3, "totalTokenCount": 18}

    def dialect(self):
        return GeminiGenerateContentDialect(thinking_level_enabled="high",
                                            thinking_level_disabled="low")

    def provider(self, binding):
        return GeminiGenerateContentProvider(binding=binding, api_key=KEY,
                                             transport=_stream_transport())

    @staticmethod
    def _chunk(number, parts, **candidate):
        return {"candidates": [{"content": {"role": "model", "parts": parts}, "index": 0,
                                **candidate}],
                "responseId": f"resp-{number}"}

    def _parts(self, turn):
        parts: list[dict[str, Any]] = []
        if turn.text is not None:
            parts.append({"text": turn.text})
        for call in turn.calls:
            parts.append({"functionCall": {"name": call.name, "args": call.arguments,
                                           "id": call.call_id}})
        if turn.reasoning is not None:
            if parts:
                parts[0]["thoughtSignature"] = turn.reasoning
            else:
                parts.append({"text": "", "thoughtSignature": turn.reasoning})
        return parts

    def encode(self, turn, number=1) -> str:
        closing = self._chunk(number, [], finishReason=self._finish[turn.resolved_finish])
        if turn.usage:
            closing["usageMetadata"] = self._usage
        return _data(self._chunk(number, self._parts(turn))) + _data(closing)

    def truncated(self, turn) -> str:
        return _data(self._chunk(1, self._parts(turn)))

    def retryable_error(self) -> str:
        return _data({"error": {"code": 503, "message": "PRIVATE_CANARY",
                                "status": "UNAVAILABLE"}})

    def fatal_error(self) -> str:
        return _data({"error": {"code": 400, "message": "PRIVATE_CANARY",
                                "status": "INVALID_ARGUMENT"}})

    def unknown_content(self):
        body = _data(self._chunk(1, [{"executableCode": {"language": "PYTHON", "code": "x"}}],
                                 finishReason="STOP"))
        return body, ("stream.part", "executableCode")

    def last_user_text(self, body) -> str:
        return [item for item in body["contents"]
                if item["role"] == "user" and "text" in item["parts"][0]][-1]["parts"][0]["text"]

    def result_call_ids(self, body) -> list[str]:
        return [part["functionResponse"]["id"] for item in body["contents"]
                for part in item["parts"] if "functionResponse" in part]

    def schema_location(self, body, schema) -> str | None:
        generation = body.get("generationConfig", {})
        if "responseJsonSchema" in generation:
            return "native" if generation["responseJsonSchema"] == schema else None
        system = "".join(part["text"] for part in body.get("systemInstruction", {})
                         .get("parts", ()))
        if (generation.get("responseMimeType") == "application/json"
                and schema_instruction(schema) in system):
            return "instruction"
        return None


class InteractionsWire(Wire):
    name = "interactions"
    transport = "GEMINI_INTERACTIONS"
    history_key = "input"
    endpoint = "https://contract.invalid/v1beta"
    stream_retry = "STREAM_OVERLOADED"
    filter_category = "safety"
    _status = {"STOP": "completed", "TOOL_CALLS": "requires_action", "LENGTH": "incomplete",
               "OTHER": "cancelled"}
    _usage = {"total_input_tokens": 9, "total_tool_use_tokens": 2, "total_output_tokens": 4,
              "total_thought_tokens": 3, "total_tokens": 18}

    def dialect(self):
        return GeminiInteractionsDialect(thinking_level_enabled="high",
                                         thinking_level_disabled="low")

    def provider(self, binding):
        return GeminiInteractionsProvider(binding=binding, api_key=KEY,
                                          transport=_stream_transport())

    @staticmethod
    def _event(kind, **fields):
        return _named(kind, {"event_type": kind, **fields})

    def _steps(self, turn, number):
        event = self._event
        body = event("interaction.created", interaction={"id": f"v1_int{number}",
                                                          "status": "in_progress"})
        index = 0
        if turn.reasoning is not None:
            body += (event("step.start", index=index, step={"type": "thought", "signature": ""})
                     + event("step.delta", index=index, delta={
                         "type": "thought_signature", "signature": turn.reasoning})
                     + event("step.stop", index=index))
            index += 1
        if turn.text is not None:
            body += (event("step.start", index=index, step={"type": "model_output"})
                     + event("step.delta", index=index, delta={"type": "text", "text": turn.text})
                     + event("step.stop", index=index))
            index += 1
        for call in turn.calls:
            body += (event("step.start", index=index, step={
                         "type": "function_call", "id": call.call_id, "name": call.name,
                         "arguments": {}})
                     + event("step.delta", index=index,
                             delta={"type": "arguments_delta", "arguments": call.raw})
                     + event("step.stop", index=index))
            index += 1
        return body

    def encode(self, turn, number=1) -> str:
        body = self._steps(turn, number)
        finish = turn.resolved_finish
        if finish == "CONTENT_FILTER":
            # A safety filter ends the stream with an error event.
            return body + self._event("error", error={"code": "safety",
                                                      "message": "PRIVATE_CANARY"})
        interaction = {"id": f"v1_int{number}", "status": self._status[finish]}
        if turn.usage:
            interaction["usage"] = self._usage
        return (body + self._event("interaction.completed", interaction=interaction)
                + "event: done\ndata: [DONE]\n\n")

    def truncated(self, turn) -> str:
        return self._steps(turn, 1)

    def retryable_error(self) -> str:
        return self._event("error", error={"code": "unavailable", "message": "PRIVATE_CANARY"})

    def fatal_error(self) -> str:
        return self._event("error", error={"code": "gateway_timeout",
                                           "message": "PRIVATE_CANARY"})

    def unknown_content(self):
        body = (self._event("interaction.created", interaction={"id": "v1_int1"})
                + self._event("step.start", index=0,
                              step={"type": "google_search_call", "id": "gs_1"}))
        return body, ("stream.step.type", "google_search_call")

    def last_user_text(self, body) -> str:
        return [item for item in body["input"]
                if item["type"] == "user_input"][-1]["content"][0]["text"]

    def result_call_ids(self, body) -> list[str]:
        return [item["call_id"] for item in body["input"] if item["type"] == "function_result"]

    def schema_location(self, body, schema) -> str | None:
        response_format = body.get("response_format", {})
        if "schema" in response_format:
            return "native" if response_format["schema"] == schema else None
        if (response_format.get("mime_type") == "application/json"
                and schema_instruction(schema) in body.get("system_instruction", "")):
            return "instruction"
        return None


WIRES = (
    ChatVendorWire(),
    ChatOfficialWire(),
    ResponsesWire(),
    AnthropicWire(),
    GeminiWire(),
    InteractionsWire(),
)
