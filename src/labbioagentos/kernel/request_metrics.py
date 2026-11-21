"""Content-free byte accounting for the public request and provider projection."""

from __future__ import annotations

from .contracts import ModelProvider, ModelRequest, RequestSizeBreakdown


def measure_request_size(
    request: ModelRequest, provider: ModelProvider | None = None,
) -> RequestSizeBreakdown:
    """Measure serialized bytes without sending a request or estimating tokens.

    The public components use the same serializer as ModelRequest. The provider
    total reuses its admission measurement, including provider-only continuity.
    An adapter with no wire measurement leaves that total unknown.
    """

    def field_bytes(name: str) -> int:
        encoded = request.model_dump_json(include={name}).encode("utf-8")
        return len(encoded) - len(f'{{"{name}":}}'.encode("utf-8"))

    public_bytes = len(request.model_dump_json().encode("utf-8"))
    messages_bytes = field_bytes("messages")
    tools_bytes = field_bytes("tools") if request.tools else 0
    schema_bytes = (
        field_bytes("response_schema") if request.response_schema is not None else 0
    )
    measure_wire = getattr(provider, "request_size_bytes", None)
    return RequestSizeBreakdown(
        public_request_bytes=public_bytes,
        public_messages_bytes=messages_bytes,
        public_tool_declarations_bytes=tools_bytes,
        public_output_schema_bytes=schema_bytes,
        public_envelope_bytes=public_bytes - messages_bytes - tools_bytes - schema_bytes,
        wire_request_bytes=measure_wire(request) if callable(measure_wire) else None,
    )
