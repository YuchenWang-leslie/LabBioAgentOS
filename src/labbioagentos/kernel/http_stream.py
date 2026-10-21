"""Shared provider HTTP transport for every Native Kernel wire format."""

from __future__ import annotations

import asyncio
import codecs
import json
import math
import random
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from time import monotonic, time
from typing import Any

import httpx

from .errors import ProviderProtocolError, ProviderTransportError

# DECIDED-C14-8: a stream may frame every generated token as its own event, so
# the cap is a fixed allowance plus a per-output-token allowance.
RESPONSE_BYTES_BASE = 8 * 1024 * 1024
RESPONSE_BYTES_PER_OUTPUT_TOKEN = 1024
# DECIDED-C14-3: retry stays off until retry attempts are recorded with the turn.
DEFAULT_MAX_RETRIES = 0
MAX_RETRIES_LIMIT = 10
STREAM_RETRY_CATEGORIES = frozenset({"STREAM_OVERLOADED", "STREAM_RATE_LIMITED"})

_LINE_BREAK = re.compile(r"\r\n|\r|\n")


def response_byte_cap(max_output_tokens: int) -> int:
    """Return the decoded-byte cap for one response to one model request."""

    if type(max_output_tokens) is not int or max_output_tokens < 1:
        raise ValueError("max_output_tokens must be a positive integer")
    return RESPONSE_BYTES_BASE + RESPONSE_BYTES_PER_OUTPUT_TOKEN * max_output_tokens


# The cap for the largest ModelRequest.max_output_tokens.
MAX_RESPONSE_BYTES = response_byte_cap(131_072)


def _reject_duplicate_keys(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("Duplicate JSON key")
        value[key] = item
    return value


def _reject_json_constant(_value):
    raise ValueError("Non-finite JSON constant")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("Non-finite JSON number")
    return value


def strict_json_loads(text: str) -> Any:
    """Parse provider JSON, rejecting duplicate keys and non-finite numbers."""

    return json.loads(
        text,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_json_constant,
        parse_float=_finite_float,
    )


@dataclass(frozen=True)
class SseEvent:
    """One dispatched server-sent event; ``event`` is None when unnamed."""

    event: str | None
    data: str


class RetryableStreamError(Exception):
    """Raised by a stream decoder for a provider overload or rate-limit event."""

    def __init__(self, category: str):
        if category not in STREAM_RETRY_CATEGORIES:
            raise ValueError("Unsupported stream retry category")
        super().__init__(category)
        self.category = category


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded retry of transient failures; every other failure is final."""

    max_retries: int = DEFAULT_MAX_RETRIES
    initial_backoff_seconds: float = 0.5
    max_backoff_seconds: float = 8.0

    def __post_init__(self):
        if (
            type(self.max_retries) is not int
            or not 0 <= self.max_retries <= MAX_RETRIES_LIMIT
        ):
            raise ValueError("max_retries must be an integer from 0 to 10")
        if not (
            math.isfinite(self.max_backoff_seconds)
            and 0 < self.initial_backoff_seconds <= self.max_backoff_seconds
        ):
            raise ValueError("Retry backoff bounds are invalid")


@dataclass(frozen=True)
class RetryAttempt:
    """One discarded attempt: its bounded failure category and the wait after it."""

    category: str
    wait_ms: int


@dataclass(frozen=True)
class HttpStreamResult:
    """The accepted response body and the attempts discarded before it."""

    body: Mapping[str, Any]
    retries: tuple[RetryAttempt, ...] = ()


StreamDecoder = Callable[[AsyncIterator[SseEvent]], Awaitable[Mapping[str, Any]]]


class HttpStreamTransport:
    """POST with no redirects, a decoded-byte cap, strict JSON and bounded retry."""

    def __init__(
        self,
        *,
        trust_env: bool = True,
        retry: RetryPolicy = RetryPolicy(),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = monotonic,
        wall_clock: Callable[[], float] = time,
        random_fraction: Callable[[], float] = random.random,
    ):
        self.trust_env = trust_env
        self.retry = retry
        self._sleep = sleep
        self._clock = clock
        self._wall_clock = wall_clock
        self._random_fraction = random_fraction

    async def post(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, Any],
        deadline_monotonic: float,
        max_response_bytes: int,
        decode_stream: StreamDecoder | None = None,
    ) -> HttpStreamResult:
        if type(max_response_bytes) is not int or max_response_bytes < 1:
            raise ValueError("max_response_bytes must be a positive integer")
        retries: list[RetryAttempt] = []
        try:
            # Serialized once, so every attempt sends identical bytes.
            body = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
            request_headers = httpx.Headers(headers)
            request_headers.setdefault("Content-Type", "application/json")
            async with httpx.AsyncClient(
                follow_redirects=False, trust_env=self.trust_env
            ) as client:
                while True:
                    try:
                        value = await self._attempt(
                            client, url, request_headers, body,
                            deadline_monotonic, max_response_bytes, decode_stream,
                        )
                    except (ProviderProtocolError, ProviderTransportError):
                        raise
                    except Exception as exc:
                        category, retryable, retry_after = _classify(
                            exc, self._wall_clock()
                        )
                        if not retryable or len(retries) >= self.retry.max_retries:
                            raise ProviderTransportError(validation_type=category) from None
                        wait = (
                            retry_after if retry_after is not None
                            else self._backoff(len(retries))
                        )
                        if self._clock() + wait >= deadline_monotonic:
                            raise ProviderTransportError(validation_type=category) from None
                        retries.append(RetryAttempt(
                            category=category, wait_ms=round(wait * 1000),
                        ))
                        await self._sleep(wait)
                    else:
                        return HttpStreamResult(body=value, retries=tuple(retries))
        except (ProviderProtocolError, ProviderTransportError):
            raise
        except Exception as exc:
            raise ProviderTransportError(
                validation_type=_classify(exc, 0.0)[0]
            ) from None

    async def _attempt(
        self, client, url, headers, body, deadline_monotonic, max_response_bytes,
        decode_stream,
    ) -> Mapping[str, Any]:
        remaining = deadline_monotonic - self._clock()
        if remaining <= 0:
            raise ProviderTransportError(validation_type="TIMEOUT")
        async with client.stream(
            "POST", url, content=body, headers=headers,
            timeout=httpx.Timeout(remaining),
        ) as response:
            response.raise_for_status()
            budget = _ByteBudget(max_response_bytes)
            if decode_stream is None:
                return _json_object(b"".join([
                    budget.spend(chunk) async for chunk in response.aiter_bytes()
                ]))
            events = _sse_events(response, budget)
            try:
                return await decode_stream(events)
            finally:
                await events.aclose()

    def _backoff(self, retry_index: int) -> float:
        delay = min(
            self.retry.initial_backoff_seconds * 2 ** retry_index,
            self.retry.max_backoff_seconds,
        )
        return delay * (1 - 0.25 * self._random_fraction())


class _ByteBudget:
    def __init__(self, limit: int):
        self.remaining = limit

    def spend(self, chunk: bytes) -> bytes:
        self.remaining -= len(chunk)
        if self.remaining < 0:
            raise ProviderTransportError(validation_type="RESPONSE_TOO_LARGE")
        return chunk


def _json_object(body: bytes) -> dict[str, Any]:
    value = strict_json_loads(body.decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Provider response must be an object")
    return value


class _SseFramer:
    """Incremental SSE framing over strict UTF-8 with LF, CR and CRLF line ends."""

    def __init__(self):
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._partial: list[str] = []
        self._event: str | None = None
        self._data: list[str] = []

    def feed(self, chunk: bytes, *, final: bool = False) -> list[SseEvent]:
        text = self._decoder.decode(chunk, final)
        if not final and "\n" not in text and "\r" not in text:
            if text:
                self._partial.append(text)
            return []
        text = "".join(self._partial) + text
        self._partial.clear()
        lines = _LINE_BREAK.split(text)
        tail = lines.pop()
        if not final and text.endswith("\r"):
            # The CR may be the first half of a CRLF split across chunks.
            tail = lines.pop() + "\r"
        if tail:
            if final:
                lines.append(tail)
            else:
                self._partial.append(tail)
        events = [event for line in lines if (event := self._line(line)) is not None]
        if final and self._data:
            raise ProviderProtocolError(validation_fields=("stream.incomplete",))
        return events

    def _line(self, line: str) -> SseEvent | None:
        if not line:
            event = (
                SseEvent(event=self._event, data="\n".join(self._data))
                if self._data else None
            )
            self._event = None
            self._data = []
            return event
        if line.startswith("data:"):
            self._data.append(line[5:].lstrip(" "))
        elif line.startswith("event:"):
            self._event = line[6:].lstrip(" ")
        return None


async def _sse_events(
    response: httpx.Response, budget: _ByteBudget
) -> AsyncIterator[SseEvent]:
    framer = _SseFramer()
    async for chunk in response.aiter_bytes():
        for event in framer.feed(budget.spend(chunk)):
            yield event
    for event in framer.feed(b"", final=True):
        yield event


def _classify(exc: Exception, wall_now: float) -> tuple[str, bool, float | None]:
    """Map a failed attempt to (bounded category, retryable, requested wait)."""

    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status == 429 or 500 <= status <= 599:
            return f"HTTP_{status}", True, _retry_after(exc.response.headers, wall_now)
        return f"HTTP_{status}", False, None
    if isinstance(exc, RetryableStreamError):
        return exc.category, True, None
    if isinstance(exc, httpx.TimeoutException):
        return "TIMEOUT", False, None
    if isinstance(exc, httpx.RemoteProtocolError):
        return "NETWORK_REMOTE_PROTOCOL", True, None
    if isinstance(exc, httpx.ReadError):
        return "NETWORK_READ", True, None
    if isinstance(exc, httpx.NetworkError):
        return "NETWORK", True, None
    if isinstance(exc, httpx.TransportError):
        return "NETWORK", False, None
    if isinstance(exc, ValueError):
        return "INVALID_JSON", False, None
    return "OTHER", False, None


def _retry_after(headers: httpx.Headers, wall_now: float) -> float | None:
    """Return the provider-requested wait in seconds, or None if absent or invalid."""

    seconds = _non_negative_seconds(headers.get("retry-after-ms"))
    if seconds is not None:
        return seconds / 1000
    value = headers.get("retry-after")
    if value is None:
        return None
    seconds = _non_negative_seconds(value)
    if seconds is not None:
        return seconds
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return max(0.0, moment.timestamp() - wall_now)


def _non_negative_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None
