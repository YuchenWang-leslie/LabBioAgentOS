"""Memory-only private provider continuation for replay within one invocation."""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

from .contracts import PublicMessage
from .errors import ProviderProtocolError
from .tooling import parse_strict_json_object

# Total private content one invocation may hold, in UTF-8 bytes.
MAX_CONTINUATION_BYTES = 32 * 1024 * 1024


def message_fingerprint(message: PublicMessage) -> str | None:
    """Digest of a public message with canonical tool arguments, or None if unkeyable."""

    calls = []
    for call in message.tool_calls:
        try:
            _arguments, canonical = parse_strict_json_object(call.arguments_json)
        except ValueError:
            return None
        calls.append([call.call_id, call.name, canonical])
    document = json.dumps(
        {"role": message.role, "content": message.content, "tool_calls": calls},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(document.encode("utf-8")).hexdigest()


class PrivateContinuationStore:
    """Opaque per-message provider state; bounded per invocation, never exported."""

    def __init__(self, *, max_bytes: int = MAX_CONTINUATION_BYTES):
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        self._max_bytes = max_bytes
        self._entries: dict[tuple[UUID, UUID, str], str] = {}
        self._sizes: dict[tuple[UUID, UUID], int] = {}

    def __repr__(self) -> str:
        return f"PrivateContinuationStore(entries={len(self._entries)})"

    def put(
        self,
        kernel_run_id: UUID,
        invocation_id: UUID,
        message: PublicMessage,
        value: str,
    ) -> None:
        """Keep ``value`` for an assistant message; unkeyable messages keep nothing."""

        fingerprint = message_fingerprint(message)
        if fingerprint is None:
            return
        key = (kernel_run_id, invocation_id, fingerprint)
        scope = (kernel_run_id, invocation_id)
        previous = self._entries.get(key)
        size = (
            self._sizes.get(scope, 0)
            - (len(previous.encode("utf-8")) if previous is not None else 0)
            + len(value.encode("utf-8"))
        )
        if size > self._max_bytes:
            raise ProviderProtocolError(
                validation_type="PRIVATE_CONTINUATION_LIMIT",
                validation_fields=("private_continuation",),
            )
        self._entries[key] = value
        self._sizes[scope] = size

    def get(
        self, kernel_run_id: UUID, invocation_id: UUID, message: PublicMessage
    ) -> str | None:
        fingerprint = message_fingerprint(message)
        if fingerprint is None:
            return None
        return self._entries.get((kernel_run_id, invocation_id, fingerprint))

    def clear(self, kernel_run_id: UUID, invocation_id: UUID) -> None:
        for key in tuple(self._entries):
            if key[:2] == (kernel_run_id, invocation_id):
                del self._entries[key]
        self._sizes.pop((kernel_run_id, invocation_id), None)
