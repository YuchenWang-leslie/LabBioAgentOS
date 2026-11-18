"""R0: public request components and provider wire bytes use explicit units."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import JsonValue, TypeAdapter

from labbioagentos.kernel import (
    AnthropicMessagesDialect,
    FakeProvider,
    GeminiGenerateContentDialect,
    GeminiInteractionsDialect,
    ProviderBinding,
    PublicMessage,
    ToolCall,
    ToolSpec,
    create_provider,
)
from labbioagentos.kernel.contracts import ModelRequest


CASES = [
    ("OPENAI_COMPATIBLE_CHAT", None),
    ("OPENAI_RESPONSES", None),
    ("ANTHROPIC_MESSAGES", AnthropicMessagesDialect(thinking_off="DISABLED")),
    ("GEMINI_GENERATE_CONTENT", GeminiGenerateContentDialect(
        thinking_level_enabled="high", thinking_level_disabled="low",
    )),
    ("GEMINI_INTERACTIONS", GeminiInteractionsDialect(
        thinking_level_enabled="high", thinking_level_disabled="low",
    )),
]


def _request(transport="OPENAI_COMPATIBLE_CHAT", dialect=None, **overrides):
    values = {
        "provider": "fixture", "model": "fixture-model",
        "endpoint": "https://provider.invalid/v1", "transport": transport,
        "thinking_mode": "DISABLED",
    }
    if dialect is not None:
        values["dialect"] = dialect
    binding = ProviderBinding(**values)
    data = {
        "binding": binding,
        "messages": (
            PublicMessage(role="SYSTEM", content="当前任务说明。"),
            PublicMessage(role="USER", content='Inspect "café" 🧬\nrows.'),
        ),
        "tools": (ToolSpec(
            name="inspect", description="Read a bounded result.",
            parameters={
                "type": "object", "properties": {"threshold": {
                    "type": "number", "minimum": 1e-8,
                }}, "additionalProperties": False,
            },
        ),),
        "response_schema": {
            "type": "object", "properties": {"value": {
                "type": "number", "minimum": 1e-8,
            }}, "additionalProperties": False,
        },
        "max_output_tokens": 512, "deadline_monotonic": 10_000_000_000.0,
        "kernel_run_id": uuid4(), "invocation_id": uuid4(), "turn_index": 1,
    }
    data.update(overrides)
    return ModelRequest(**data)


def _field_bytes(request, name):
    # Independent value serialization, including Unicode and float spelling.
    field_type = {
        "messages": tuple[PublicMessage, ...],
        "tools": tuple[ToolSpec, ...],
        "response_schema": dict[str, JsonValue],
    }[name]
    return len(TypeAdapter(field_type).dump_json(getattr(request, name)))


def test_public_components_sum_to_the_exact_serialized_request():
    from labbioagentos.kernel.request_metrics import measure_request_size

    request = _request()
    original = request.model_dump_json()
    provider = FakeProvider(binding=request.binding, turns=())

    metrics = measure_request_size(request, provider)

    assert metrics.public_request_bytes == len(original.encode("utf-8"))
    assert metrics.public_messages_bytes == _field_bytes(request, "messages")
    assert metrics.public_tool_declarations_bytes == _field_bytes(request, "tools")
    assert metrics.public_output_schema_bytes == _field_bytes(request, "response_schema")
    assert metrics.public_request_bytes == sum((
        metrics.public_messages_bytes, metrics.public_tool_declarations_bytes,
        metrics.public_output_schema_bytes, metrics.public_envelope_bytes,
    ))
    assert metrics.wire_request_bytes is None
    assert "token" not in metrics.model_dump_json()
    assert request.model_dump_json() == original
    assert provider.requests == ()


def test_absent_declarations_and_schema_have_no_component_bytes():
    from labbioagentos.kernel.request_metrics import measure_request_size

    request = _request(tools=(), response_schema=None)

    metrics = measure_request_size(request)

    assert metrics.public_tool_declarations_bytes == 0
    assert metrics.public_output_schema_bytes == 0
    assert metrics.public_envelope_bytes > 0
    assert metrics.public_request_bytes == (
        metrics.public_messages_bytes + metrics.public_envelope_bytes
    )
    assert metrics.wire_request_bytes is None


@pytest.mark.parametrize("transport,dialect", CASES, ids=[case[0] for case in CASES])
@pytest.mark.parametrize("json_object", [False, True])
def test_each_format_reports_its_real_wire_total_without_relabelling_public_parts(
    transport, dialect, json_object,
):
    from labbioagentos.kernel.request_metrics import measure_request_size

    request = _request(transport, dialect)
    request = request.model_copy(update={"binding": request.binding.model_copy(
        update={"finalization_json_object": json_object},
    )})
    provider = create_provider(request.binding, api_key="PRIVATE_KEY_CANARY")
    expected = len(json.dumps(
        provider._payload(request), ensure_ascii=False, allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8"))

    metrics = measure_request_size(request, provider)

    assert metrics.wire_request_bytes == expected == provider.request_size_bytes(request)
    assert metrics.public_messages_bytes == _field_bytes(request, "messages")
    assert metrics.public_output_schema_bytes == _field_bytes(request, "response_schema")
    assert "PRIVATE_KEY_CANARY" not in metrics.model_dump_json()
    assert "Inspect" not in metrics.model_dump_json()


def test_private_continuation_increases_only_wire_bytes_and_is_not_exported():
    from labbioagentos.kernel.request_metrics import measure_request_size

    request = _request(response_schema=None)
    assistant = PublicMessage(role="ASSISTANT", tool_calls=(ToolCall(
        call_id="call-1", name="inspect", arguments_json="{}",
    ),))
    request = request.model_copy(update={"messages": (
        *request.messages, assistant,
        PublicMessage(role="TOOL", tool_call_id="call-1", content='{"rows": 1}'),
    )})
    provider = create_provider(request.binding, api_key="PRIVATE_KEY_CANARY")
    before = measure_request_size(request, provider)
    provider._private_reasoning.put(
        request.kernel_run_id, request.invocation_id, assistant,
        "PRIVATE_CONTINUITY_CANARY" * 1_000,
    )

    after = measure_request_size(request, provider)

    assert after.wire_request_bytes > before.wire_request_bytes
    assert after.public_request_bytes == before.public_request_bytes
    assert after.public_messages_bytes == before.public_messages_bytes
    assert "PRIVATE_CONTINUITY_CANARY" not in after.model_dump_json()
    assert "PRIVATE_KEY_CANARY" not in after.model_dump_json()


def test_size_contract_rejects_inconsistent_partitions_and_estimated_tokens():
    from pydantic import ValidationError

    from labbioagentos.kernel.contracts import RequestSizeBreakdown

    values = {
        "public_request_bytes": 12, "public_messages_bytes": 4,
        "public_tool_declarations_bytes": 2, "public_output_schema_bytes": 1,
        "public_envelope_bytes": 5,
    }
    assert RequestSizeBreakdown(**values).wire_request_bytes is None
    with pytest.raises(ValidationError):
        RequestSizeBreakdown(**{**values, "public_request_bytes": 11})
    with pytest.raises(ValidationError):
        RequestSizeBreakdown(**values, wire_request_bytes=-1)
    with pytest.raises(ValidationError):
        RequestSizeBreakdown(**values, estimated_input_tokens=3)
