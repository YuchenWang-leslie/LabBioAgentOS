"""Shared provider HTTP transport for every Native Kernel wire format."""

from __future__ import annotations

import asyncio
import codecs
import json
import math
import random
import re
import zlib
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from contextlib import aclosing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import timezone
from email.utils import parsedate_to_datetime
from time import monotonic, time
from typing import Any

import httpx

from .contracts import ProviderRetry
from .errors import (
    KernelDeadlineExceededError,
    ProviderProtocolError,
    ProviderTransportError,
)

# DECIDED-C14-8: a stream may frame every generated token as its own event, so
# the cap is a fixed allowance plus a per-output-token allowance.
RESPONSE_BYTES_BASE = 8 * 1024 * 1024
RESPONSE_BYTES_PER_OUTPUT_TOKEN = 1024
# DECIDED-C14-3: at most three requests, as in the official SDKs.
DEFAULT_MAX_RETRIES = 2
MAX_RETRIES_LIMIT = 10
STREAM_RETRY_CATEGORIES = frozenset({"STREAM_OVERLOADED", "STREAM_RATE_LIMITED"})
# An attempt ends this long before the request deadline, so the deadline outcome
# and its retry records reach the loop before the loop's own deadline
# enforcement cancels the call.
DEADLINE_MARGIN_SECONDS = 0.5
# Upload inactivity is not model-generation time. A stalled write must leave
# room for the existing bounded retry instead of consuming the stage deadline.
REQUEST_CONNECT_TIMEOUT_SECONDS = 60.0
REQUEST_WRITE_TIMEOUT_SECONDS = 60.0

_LINE_BREAK = re.compile(r"\r\n|\r|\n")
# DECIDED-C14-8: only gzip is requested, and the transport inflates it itself so
# the byte cap also bounds decompression. "x-gzip" is gzip's registered alias.
_GZIP_ENCODINGS = frozenset({"gzip", "x-gzip"})
_GZIP_WBITS = zlib.MAX_WBITS | 16


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
class HttpStreamResult:
    """The accepted response body and the attempts discarded before it."""

    body: Mapping[str, Any]
    retries: tuple[ProviderRetry, ...] = ()


StreamDecoder = Callable[[AsyncIterator[SseEvent]], Awaitable[Mapping[str, Any]]]


_HTTP_PROGRESS = ContextVar("native_http_progress", default=None)
_TRACE_PHASES = {
    "connection.connect_tcp": "CONNECT_TCP",
    "connection.start_tls": "TLS",
    "proxy.start_tls": "TLS",
    **{f"{protocol}.{operation}": phase
       for protocol in ("http11", "http2")
       for operation, phase in (
           ("send_request_headers", "SEND_REQUEST_HEADERS"),
           ("send_request_body", "SEND_REQUEST_BODY"),
           ("receive_response_headers", "WAIT_RESPONSE_HEADERS"),
           ("receive_response_body", "READ_RESPONSE_BODY"),
       )},
}


@contextmanager
def observe_http_progress(observer):
    """Bind a safe, request-local observer without modifying provider contracts."""
    token = _HTTP_PROGRESS.set(observer)
    try:
        yield
    finally:
        _HTTP_PROGRESS.reset(token)


def _emit_http_progress(status, reason, facts):
    observer = _HTTP_PROGRESS.get()
    if observer is not None:
        try:
            observer(status, reason, facts)
        except Exception:
            # New diagnostics must not turn an otherwise valid call into a retry.
            pass


class _RequestProgress:
    """Only enums and numeric activity; never httpcore's sensitive trace info."""

    def __init__(self, clock):
        self.clock = clock
        self.reset(0)

    def reset(self, attempt):
        self.attempt = attempt
        self.started = self.clock()
        self.last_activity = self.started
        self.last_body_event = self.started
        self.first_body = self.last_body = None
        self.response_bytes = self.response_chunks = 0
        self.response_status = None
        self.phase = "SERIALIZING_REQUEST"

    def emit(self, status, phase=None, *, activity=False):
        now = self.clock()
        if phase is not None:
            self.phase = phase
        if activity:
            self.last_activity = now
        ms = lambda value: max(0, round((value - self.started) * 1000))
        _emit_http_progress(status, self.phase, {
            "attempt_index": self.attempt, "elapsed_ms": ms(now),
            "response_status": self.response_status,
            "response_bytes": self.response_bytes, "response_chunks": self.response_chunks,
            "first_body_elapsed_ms": ms(self.first_body) if self.first_body is not None else None,
            "last_body_elapsed_ms": ms(self.last_body) if self.last_body is not None else None,
            "last_activity_elapsed_ms": ms(self.last_activity),
            "idle_ms": max(0, round((now - self.last_activity) * 1000)),
        })

    async def trace(self, name, _info):
        base, _, outcome = name.rpartition(".")
        phase = _TRACE_PHASES.get(base)
        if phase is None:
            return
        if outcome == "started":
            self.emit("HTTP_PHASE_STARTED", phase)
        elif outcome == "complete":
            self.emit("HTTP_PHASE_COMPLETED", phase, activity=True)
        elif outcome == "failed":
            if (self.phase == "COMPLETED" and phase == "READ_RESPONSE_BODY"
                    and isinstance(_info.get("exception"), GeneratorExit)):
                # A valid terminal SSE event can leave body iterators suspended.
                # Their later normal closure is not another attempt outcome.
                self.emit("HTTP_STREAM_CLOSED")
            else:
                # Preserve genuine failures and the I/O phase of cancellation.
                self.emit("HTTP_PHASE_FAILED")

    def body(self, size):
        now = self.clock()
        first = self.first_body is None
        if first:
            self.first_body = now
        self.last_body = self.last_activity = now
        self.response_bytes += size
        self.response_chunks += 1
        if first or now - self.last_body_event >= 5:
            self.last_body_event = now
            self.emit("FIRST_RESPONSE_BODY_BYTES" if first else "BODY_PROGRESS",
                      "READ_RESPONSE_BODY")


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
        retries: list[ProviderRetry] = []
        progress = _RequestProgress(self._clock)
        progress.emit("HTTP_REQUEST_STARTED")
        try:
            # Serialized once, so every attempt sends identical bytes.
            body = json.dumps(
                payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
            request_headers = httpx.Headers(headers)
            request_headers.setdefault("Content-Type", "application/json")
            request_headers["Accept-Encoding"] = "gzip"
            progress.emit("REQUEST_SERIALIZED", "CLIENT_SETUP", activity=True)
            async with httpx.AsyncClient(
                follow_redirects=False, trust_env=self.trust_env
            ) as client:
                while True:
                    # A delayed trace callback must retain its original attempt.
                    progress = _RequestProgress(self._clock)
                    progress.reset(len(retries) + 1)
                    progress.emit("ATTEMPT_STARTED", "CONNECTING")
                    try:
                        value = await self._attempt(
                            client, url, request_headers, body,
                            deadline_monotonic, max_response_bytes, decode_stream, progress,
                        )
                    except asyncio.CancelledError:
                        progress.emit("ATTEMPT_CANCELLED")
                        raise
                    except (
                        KernelDeadlineExceededError,
                        ProviderProtocolError,
                        ProviderTransportError,
                    ) as exc:
                        progress.emit("ATTEMPT_FAILED")
                        exc.transport_retries = tuple(retries)
                        raise
                    except Exception as exc:
                        progress.emit("ATTEMPT_FAILED")
                        category, retryable, retry_after = _classify(
                            exc, self._wall_clock()
                        )
                        wait = (
                            retry_after if retry_after is not None
                            else self._backoff(len(retries))
                        )
                        if (
                            not retryable
                            or len(retries) >= self.retry.max_retries
                            or self._clock() + wait
                            >= deadline_monotonic - DEADLINE_MARGIN_SECONDS
                        ):
                            raise ProviderTransportError(
                                validation_type=category,
                                transport_retries=tuple(retries),
                                usage_snapshot=getattr(exc, "usage_snapshot", None),
                            ) from None
                        retries.append(ProviderRetry(
                            category=category, wait_ms=round(wait * 1000),
                            usage_snapshot=getattr(exc, "usage_snapshot", None),
                        ))
                        progress.emit("HTTP_RETRY_WAIT", "RETRY_WAIT")
                        await self._sleep(wait)
                        if self._clock() >= deadline_monotonic - DEADLINE_MARGIN_SECONDS:
                            # The wait ended too late for another attempt; the
                            # outcome stays the failure that was being retried.
                            raise ProviderTransportError(
                                validation_type=category,
                                transport_retries=tuple(retries),
                                usage_snapshot=getattr(exc, "usage_snapshot", None),
                            ) from None
                    else:
                        progress.emit("ATTEMPT_COMPLETED", "COMPLETED", activity=True)
                        return HttpStreamResult(body=value, retries=tuple(retries))
        except asyncio.CancelledError:
            if progress.phase in {"SERIALIZING_REQUEST", "CLIENT_SETUP", "RETRY_WAIT"}:
                progress.emit("HTTP_REQUEST_CANCELLED")
            raise
        except (
            KernelDeadlineExceededError,
            ProviderProtocolError,
            ProviderTransportError,
        ):
            raise
        except Exception as exc:
            raise ProviderTransportError(
                validation_type=_classify(exc, 0.0)[0]
            ) from None

    async def _attempt(
        self, client, url, headers, body, deadline_monotonic, max_response_bytes,
        decode_stream, progress,
    ) -> Mapping[str, Any]:
        remaining = deadline_monotonic - self._clock()
        if remaining <= DEADLINE_MARGIN_SECONDS:
            raise KernelDeadlineExceededError()
        try:
            # Keep the total/model-read deadline, but bound connection setup and
            # uploads independently so neither can consume the entire stage.
            async with asyncio.timeout(remaining - DEADLINE_MARGIN_SECONDS) as scope:
                async with client.stream(
                    "POST", url, content=body, headers=headers,
                    timeout=httpx.Timeout(
                        remaining, connect=min(remaining, REQUEST_CONNECT_TIMEOUT_SECONDS),
                        write=min(remaining, REQUEST_WRITE_TIMEOUT_SECONDS),
                    ),
                    extensions={"trace": progress.trace},
                ) as response:
                    progress.response_status = response.status_code
                    progress.emit("RESPONSE_HEADERS_RECEIVED", "READ_RESPONSE_BODY", activity=True)
                    response.raise_for_status()
                    chunks = _decoded_chunks(response, _ByteBudget(max_response_bytes), progress)
                    if decode_stream is None:
                        async with aclosing(chunks):
                            return _json_object(b"".join([
                                chunk async for chunk in chunks
                            ]))
                    events = _sse_events(chunks)
                    try:
                        return await decode_stream(events)
                    finally:
                        await events.aclose()
        except TimeoutError as exc:
            if scope.expired():
                raise KernelDeadlineExceededError(
                    usage_snapshot=getattr(exc.__cause__, "usage_snapshot", None),
                ) from None
            raise

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


class _BoundedGzip:
    """gzip inflation whose output never exceeds the response byte budget."""

    def __init__(self, budget: _ByteBudget):
        self._budget = budget
        self._inflater = zlib.decompressobj(wbits=_GZIP_WBITS)
        self._started = False

    def feed(self, data: bytes) -> Iterator[bytes]:
        try:
            while data:
                self._started = True
                if self._inflater.eof:
                    # RFC 1952 allows several members in one body.
                    self._inflater = zlib.decompressobj(wbits=_GZIP_WBITS)
                piece = self._inflater.decompress(data, self._budget.remaining + 1)
                if piece:
                    yield self._budget.spend(piece)
                data = (
                    self._inflater.unused_data if self._inflater.eof
                    else self._inflater.unconsumed_tail
                )
        except zlib.error:
            raise ProviderTransportError(validation_type="INVALID_ENCODING") from None

    def finish(self) -> None:
        if self._started and not self._inflater.eof:
            raise ProviderTransportError(validation_type="INVALID_ENCODING")


async def _decoded_chunks(
    response: httpx.Response, budget: _ByteBudget, progress=None,
) -> AsyncIterator[bytes]:
    """Yield the decoded body, spending every decoded byte from ``budget``."""

    encoding = response.headers.get("content-encoding", "").strip().lower()
    if encoding in {"", "identity"}:
        async for chunk in _raw_chunks(response, progress):
            yield budget.spend(chunk)
        return
    if encoding not in _GZIP_ENCODINGS:
        raise ProviderTransportError(validation_type="UNSUPPORTED_ENCODING")
    inflater = _BoundedGzip(budget)
    async for chunk in _raw_chunks(response, progress):
        for piece in inflater.feed(chunk):
            yield piece
    inflater.finish()


async def _raw_chunks(response, progress):
    async for chunk in response.aiter_raw():
        if progress is not None and chunk:
            progress.body(len(chunk))
        yield chunk
    if progress is not None:
        progress.emit("BODY_COMPLETE", "DECODE_RESPONSE", activity=True)


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
            raise ProviderTransportError(validation_type="STREAM_INCOMPLETE")
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


async def _sse_events(chunks: AsyncIterator[bytes]) -> AsyncIterator[SseEvent]:
    framer = _SseFramer()
    async with aclosing(chunks):
        async for chunk in chunks:
            for event in framer.feed(chunk):
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
    if isinstance(exc, httpx.ConnectTimeout):
        return "NETWORK_CONNECT_TIMEOUT", True, None
    if isinstance(exc, httpx.WriteTimeout):
        return "NETWORK_WRITE_TIMEOUT", True, None
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
