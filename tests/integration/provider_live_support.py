"""Instrumentation shared by the live provider checks; it records, never alters."""

from __future__ import annotations

import copy
import json

from labbioagentos.kernel import continuation, http_stream

# Block types whose every string is private continuation.
_PRIVATE_TYPES = frozenset({"thinking", "redacted_thinking", "reasoning", "thought"})
# Shorter strings could match unrelated text in a scan.
_MIN_SCAN_LENGTH = 24


class Observed:
    """The real adapter, keeping each normalized turn for the record."""

    def __init__(self, provider):
        self.provider = provider
        self.turns = []

    @property
    def binding(self):
        return self.provider.binding

    async def generate(self, request):
        turn = await self.provider.generate(request)
        self.turns.append(turn)
        return turn

    def clear_invocation(self, kernel_run_id, invocation_id):
        self.provider.clear_invocation(kernel_run_id, invocation_id)


class Wire:
    """Request payloads and decoded response sizes of one check, in memory only."""

    def __init__(self, monkeypatch):
        self.payloads: list[dict] = []
        self.budgets: list = []
        wire = self
        real_post = http_stream.HttpStreamTransport.post

        async def post(transport, **kwargs):
            wire.payloads.append(copy.deepcopy(kwargs["payload"]))
            return await real_post(transport, **kwargs)

        class Budget(http_stream._ByteBudget):
            def __init__(self, limit):
                super().__init__(limit)
                self.limit = limit
                wire.budgets.append(self)

        monkeypatch.setattr(http_stream.HttpStreamTransport, "post", post)
        monkeypatch.setattr(http_stream, "_ByteBudget", Budget)

    @property
    def response_bytes(self) -> list[int]:
        return [budget.limit - budget.remaining for budget in self.budgets]


def _continuation_sent(api_format: str, payload: dict) -> bool:
    """Whether a request carried private continuation back to the provider."""
    if api_format == "openai":
        return any("reasoning_content" in item for item in payload.get("messages", ()))
    if api_format == "openai-responses":
        return any(item.get("type") == "reasoning" for item in payload.get("input", ()))
    if api_format == "anthropic":
        return any(
            block.get("type") in {"thinking", "redacted_thinking"}
            for item in payload.get("messages", ()) if item.get("role") == "assistant"
            and isinstance(item.get("content"), list) for block in item["content"]
        )
    if api_format == "gemini":
        return any("thoughtSignature" in part for item in payload.get("contents", ())
                   if item.get("role") == "model" for part in item.get("parts", ()))
    return any(step.get("type") == "thought" for step in payload.get("input", ()))


def _turn_record(turn) -> dict:
    return {
        "finish_reason": turn.finish_reason.value,
        "refusal_category": turn.refusal_category,
        "content_present": turn.content is not None,
        "tool_calls": len(turn.tool_calls),
        "private_reasoning_present": turn.private_reasoning_present,
        "usage": [turn.usage.input_tokens, turn.usage.output_tokens, turn.usage.total_tokens],
        "retries": [[item.category, item.wait_ms] for item in turn.transport_retries],
    }


def _error_record(exc: Exception) -> dict:
    return {
        "error": type(exc).__name__,
        "code": getattr(exc, "code", None),
        "validation_type": getattr(exc, "validation_type", None),
        "validation_fields": list(getattr(exc, "validation_fields", ()) or ()),
        "retries": [[item.category, item.wait_ms]
                    for item in getattr(exc, "transport_retries", ()) or ()],
    }


def _private_strings(value, found: set, inside: bool = False) -> None:
    if isinstance(value, dict):
        private = inside or value.get("type") in _PRIVATE_TYPES or value.get("thought") is True
        for key, item in value.items():
            if key == "thoughtSignature" and isinstance(item, str):
                found.add(item)
            elif key not in {"type", "id", "status"}:
                _private_strings(item, found, private)
    elif isinstance(value, list):
        for item in value:
            _private_strings(item, found, inside)
    elif isinstance(value, str) and inside:
        found.add(value)


class PrivateValues:
    """Private continuation the adapters stored, kept in memory for a leak scan only."""

    def __init__(self, monkeypatch):
        self.values: set[str] = set()
        values = self.values
        real_put = continuation.PrivateContinuationStore.put

        def put(store, kernel_run_id, invocation_id, message, value):
            try:
                parsed = json.loads(value)
            except ValueError:
                parsed = None
            if isinstance(parsed, list):
                _private_strings(parsed, values)
            else:
                # Chat keeps the reasoning text itself.
                values.add(value)
            return real_put(store, kernel_run_id, invocation_id, message, value)

        monkeypatch.setattr(continuation.PrivateContinuationStore, "put", put)

    def found_in(self, data: bytes) -> int:
        """How many private values occur in data, plainly or JSON-escaped."""
        return sum(
            1 for value in self.values if len(value) >= _MIN_SCAN_LENGTH and (
                value.encode("utf-8") in data
                or json.dumps(value)[1:-1].encode("utf-8") in data
            )
        )
