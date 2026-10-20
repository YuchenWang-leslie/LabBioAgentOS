"""P3 acceptance for the sole OpenAI-compatible Chat provider adapter."""

from __future__ import annotations

from copy import deepcopy
import json
from uuid import uuid4

import pytest
import httpx

from labbioagentos.kernel import (
    FinishReason,
    HttpxChatCompletionTransport,
    ModelRequest,
    OpenAICompatibleChatProvider,
    ProviderBinding,
    ProviderProtocolError,
    ProviderTransportError,
    PublicMessage,
    ToolCall,
    ToolSpec,
)


class RecordingTransport:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    async def post_json(self, *, url, headers, payload, timeout_seconds):
        self.requests.append(
            {
                "url": url,
                "headers": deepcopy(headers),
                "payload": deepcopy(payload),
                "timeout_seconds": timeout_seconds,
            }
        )
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)


@pytest.mark.asyncio
async def test_http_transport_provider_only_proxy_policy(monkeypatch):
    real_client = httpx.AsyncClient
    options = []

    def client(**kwargs):
        options.append(kwargs["trust_env"])
        return real_client(transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"ok": True})
        ), **kwargs)

    monkeypatch.setattr("labbioagentos.kernel.openai_chat.httpx.AsyncClient", client)
    for trust_env in (False, True):
        response = await HttpxChatCompletionTransport(trust_env=trust_env).post_json(
            url="https://provider.invalid/v1/chat/completions",
            headers={}, payload={"model": "fixture"}, timeout_seconds=1,
        )
        assert response == {"ok": True}
    assert options == [False, True]


@pytest.mark.asyncio
@pytest.mark.parametrize("response_or_error,expected_type", [
    (httpx.Response(503, json={"private": "PRIVATE_CANARY"}), "HTTP_503"),
    (httpx.ReadTimeout("PRIVATE_CANARY"), "TIMEOUT"),
    (httpx.RemoteProtocolError("PRIVATE_CANARY"), "NETWORK_REMOTE_PROTOCOL"),
    (httpx.ReadError("PRIVATE_CANARY"), "NETWORK_READ"),
])
async def test_http_transport_reports_only_safe_failure_class(
    monkeypatch, response_or_error, expected_type,
):
    real_client = httpx.AsyncClient

    async def handler(_request):
        if isinstance(response_or_error, Exception):
            raise response_or_error
        return response_or_error

    monkeypatch.setattr(
        "labbioagentos.kernel.openai_chat.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )

    with pytest.raises(ProviderTransportError) as caught:
        await HttpxChatCompletionTransport().post_json(
            url="https://provider.invalid/v1/chat/completions",
            headers={}, payload={"model": "fixture"}, timeout_seconds=1,
        )

    assert caught.value.validation_type == expected_type
    assert "PRIVATE_CANARY" not in str(caught.value)


@pytest.mark.asyncio
async def test_http_transport_reassembles_streamed_tool_call_and_usage(monkeypatch):
    events = (
        '{"id":"request-1","choices":[{"index":0,"delta":{"role":"assistant","reasoning_content":"PRIVATE_"},"finish_reason":null}]}',
        '{"id":"request-1","choices":[{"index":0,"delta":{"reasoning_content":"REASONING","tool_calls":[{"index":0,"id":"call-1","type":"function","function":{"name":"inspect","arguments":"{\\"view\\":"}}]},"finish_reason":null}]}',
        '{"id":"request-1","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"SUMMARY\\"}"}}]},"finish_reason":"tool_calls"}]}',
        '{"id":"request-1","choices":[],"usage":{"prompt_tokens":9,"completion_tokens":12,"total_tokens":21}}',
    )
    body = "".join(f"data: {event}\n\n" for event in events) + "data: [DONE]\n\n"
    real_client = httpx.AsyncClient

    def handler(request):
        assert request.read() == b'{"model":"fixture","stream":true}'
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    monkeypatch.setattr(
        "labbioagentos.kernel.openai_chat.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    response = await HttpxChatCompletionTransport().post_json(
        url="https://provider.invalid/v1/chat/completions",
        headers={}, payload={"model": "fixture", "stream": True}, timeout_seconds=1,
    )

    assert response == {
        "id": "request-1",
        "choices": [{"finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None,
            "reasoning_content": "PRIVATE_REASONING",
            "tool_calls": [{"id": "call-1", "type": "function", "function": {
                "name": "inspect", "arguments": '{"view":"SUMMARY"}',
            }}],
        }}],
        "usage": {"prompt_tokens": 9, "completion_tokens": 12, "total_tokens": 21},
    }


@pytest.mark.asyncio
async def test_http_transport_rejects_truncated_stream(monkeypatch):
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "labbioagentos.kernel.openai_chat.httpx.AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(lambda _request: httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            text='data: {"choices":[{"index":0,"delta":{"content":"partial"},"finish_reason":null}]}\n\n',
        )), **kwargs),
    )

    with pytest.raises(ProviderProtocolError):
        await HttpxChatCompletionTransport().post_json(
            url="https://provider.invalid/v1/chat/completions",
            headers={}, payload={"model": "fixture", "stream": True}, timeout_seconds=1,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_usage", [False, True])
async def test_protocol_error_identifies_component_without_response_values(invalid_usage):
    response = {"choices": [{"finish_reason": "stop", "message": {
        "content": "complete" if invalid_usage else {"secret": "PRIVATE_CANARY"},
    }}]}
    if invalid_usage:
        response["usage"] = {"prompt_tokens": 9, "total_tokens": 0}
    binding = _binding()
    provider = OpenAICompatibleChatProvider(
        binding=binding, api_key="secret-token",
        transport=RecordingTransport((response,)),
    )
    with pytest.raises(ProviderProtocolError) as caught:
        await provider.generate(_request(binding, (PublicMessage(role="USER", content="Run."),)))
    assert caught.value.validation_fields == (("usage",) if invalid_usage else ("message.content",))
    assert caught.value.validation_type == ("value_error" if invalid_usage else "ValueError")
    assert "PRIVATE_CANARY" not in str(caught.value)


@pytest.mark.asyncio
async def test_protocol_error_identifies_overlong_public_content_field():
    response = {"choices": [{"finish_reason": "stop", "message": {
        "content": "PRIVATE_CANARY" + "x" * 64_000,
    }}]}
    binding = _binding()
    provider = OpenAICompatibleChatProvider(
        binding=binding, api_key="secret-token",
        transport=RecordingTransport((response,)),
    )

    with pytest.raises(ProviderProtocolError) as caught:
        await provider.generate(_request(binding, (PublicMessage(role="USER", content="Run."),)))

    assert caught.value.validation_fields == ("model_turn", "content")
    assert "PRIVATE_CANARY" not in str(caught.value)


def _binding(
    *,
    thinking="ENABLED",
    provider_tool_schema_strict=True,
    finalization_json_object=False,
):
    return ProviderBinding(
        provider="compatible",
        model="model-a",
        endpoint="https://provider.invalid/v1",
        transport="OPENAI_COMPATIBLE_CHAT",
        thinking_mode=thinking,
        provider_tool_schema_strict=provider_tool_schema_strict,
        finalization_json_object=finalization_json_object,
    )


def _request(binding, messages, *, turn=1, tools=(), schema=None):
    return ModelRequest(
        binding=binding,
        messages=messages,
        tools=tools,
        response_schema=schema,
        max_output_tokens=512,
        deadline_monotonic=10_000_000_000.0,
        kernel_run_id=uuid4(),
        invocation_id=uuid4(),
        turn_index=turn,
    )


@pytest.mark.asyncio
async def test_chat_adapter_emits_strict_schema_and_preserves_unknown_usage():
    response = {
        "id": "request-1",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "PRIVATE_REASONING_SENTINEL",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "inspect",
                                "arguments": '{"view":"SUMMARY"}',
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 9},
    }
    transport = RecordingTransport((response,))
    binding = _binding()
    provider = OpenAICompatibleChatProvider(
        binding=binding,
        api_key="secret-token",
        transport=transport,
    )
    spec = ToolSpec(
        name="inspect",
        description="Inspect a bounded object.",
        parameters={
            "type": "object",
            "properties": {"view": {"type": "string"}},
            "additionalProperties": False,
        },
    )
    schema = {
        "type": "object",
        "properties": {"result": {"type": "string"}},
        "required": ["result"],
        "additionalProperties": False,
    }
    request = _request(
        binding,
        (PublicMessage(role="USER", content="Inspect."),),
        tools=(spec,),
        schema=schema,
    )

    turn = await provider.generate(request)

    assert turn.finish_reason is FinishReason.TOOL_CALLS
    assert turn.private_reasoning_present is True
    assert turn.usage.input_tokens == 9
    assert turn.usage.output_tokens is None
    assert turn.usage.total_tokens is None
    sent = transport.requests[0]
    assert sent["url"] == "https://provider.invalid/v1/chat/completions"
    assert sent["headers"]["Authorization"] == "Bearer secret-token"
    assert sent["payload"]["thinking"] == {"type": "enabled"}
    assert sent["payload"]["tools"][0]["function"]["strict"] is True
    assert sent["payload"]["response_format"]["json_schema"]["strict"] is True
    assert "PRIVATE_REASONING_SENTINEL" not in repr(turn)


@pytest.mark.asyncio
async def test_chat_adapter_honors_provider_wire_compatibility_flags():
    transport = RecordingTransport(({
        "id": "request-compatible",
        "choices": [{
            "finish_reason": "stop",
            "message": {"content": '{"result":"complete"}'},
        }],
    },))
    binding = _binding(
        thinking="DISABLED",
        provider_tool_schema_strict=False,
        finalization_json_object=True,
    )
    provider = OpenAICompatibleChatProvider(
        binding=binding,
        api_key="secret-token",
        transport=transport,
    )
    request = _request(
        binding,
        (PublicMessage(role="USER", content="Run."),),
        tools=(ToolSpec(
            name="inspect",
            description="Inspect a bounded object.",
            parameters={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),),
        schema={
            "type": "object",
            "properties": {"result": {"type": "string"}},
            "required": ["result"],
            "additionalProperties": False,
        },
    )

    await provider.generate(request)

    payload = transport.requests[0]["payload"]
    assert payload["tools"][0]["function"]["strict"] is False
    assert payload["response_format"] == {"type": "json_object"}
    schema_instruction = payload["messages"][0]["content"]
    assert "Return exactly one JSON object" in schema_instruction
    assert '"required":["result"]' in schema_instruction


@pytest.mark.asyncio
async def test_private_reasoning_replays_only_on_exact_bound_tool_history():
    first = {
        "id": "request-1",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": "",
                    "reasoning_content": "PRIVATE_REASONING_SENTINEL",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "inspect",
                                "arguments": " { } ",
                            },
                        }
                    ],
                },
            }
        ],
    }
    second = {
        "id": "request-2",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": "complete"},
            }
        ],
    }
    transport = RecordingTransport((first, second))
    binding = _binding()
    provider = OpenAICompatibleChatProvider(
        binding=binding,
        api_key="secret-token",
        transport=transport,
    )
    initial = _request(binding, (PublicMessage(role="USER", content="Inspect."),))
    turn = await provider.generate(initial)
    followup = initial.model_copy(
        update={
            "turn_index": 2,
            "messages": (
                *initial.messages,
                PublicMessage(role="ASSISTANT", tool_calls=turn.tool_calls),
                PublicMessage(role="TOOL", content='{"ok":true}', tool_call_id="call-1"),
            ),
        }
    )

    await provider.generate(followup)

    replayed = transport.requests[1]["payload"]["messages"][1]
    assert replayed["reasoning_content"] == "PRIVATE_REASONING_SENTINEL"
    provider.clear_invocation(initial.kernel_run_id, initial.invocation_id)
    assert "PRIVATE_REASONING_SENTINEL" not in repr(provider)


def test_discarded_batch_removes_only_its_exact_private_continuity():
    binding = _binding()
    provider = OpenAICompatibleChatProvider(
        binding=binding, api_key="secret-token", transport=RecordingTransport(()),
    )
    initial = _request(binding, (PublicMessage(role="USER", content="Inspect."),))
    messages = list(initial.messages)
    batches = []
    for number in (1, 2):
        turn = provider._parse_response(initial, {
            "choices": [{
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "reasoning_content": f"PRIVATE_{number}",
                    "tool_calls": [{
                        "id": f"call-{number}", "type": "function",
                        "function": {"name": "inspect", "arguments": "{}"},
                    }],
                },
            }],
        })
        batches.append(turn.tool_calls)
        messages.extend((
            PublicMessage(role="ASSISTANT", tool_calls=turn.tool_calls),
            PublicMessage(
                role="TOOL", content='{"ok":true}', tool_call_id=f"call-{number}"
            ),
        ))
    request = initial.model_copy(update={"messages": tuple(messages)})
    size_before = provider.request_size_bytes(request)
    provider.discard_tool_batch(
        initial.kernel_run_id, initial.invocation_id, batches[0]
    )
    payload = provider._payload(request)
    assert "reasoning_content" not in payload["messages"][1]
    assert payload["messages"][3]["reasoning_content"] == "PRIVATE_2"
    size_after = provider.request_size_bytes(request)
    assert size_after < size_before
    assert size_after == len(json.dumps(
        payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"),
    ).encode("utf-8"))
    assert len(provider._private_reasoning) == 1
    provider.discard_tool_batch(
        initial.kernel_run_id, initial.invocation_id, batches[1],
        retained_messages=request.messages,
    )
    assert len(provider._private_reasoning) == 1


@pytest.mark.asyncio
async def test_empty_tool_turn_content_is_normalized_to_absent_content():
    transport = RecordingTransport(
        (
            {
                "id": "request-empty-content",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-empty-content",
                                    "type": "function",
                                    "function": {
                                        "name": "inspect",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        },
                    }
                ],
            },
        )
    )
    binding = _binding(thinking="DISABLED")
    provider = OpenAICompatibleChatProvider(
        binding=binding,
        api_key="secret-token",
        transport=transport,
    )

    turn = await provider.generate(
        _request(binding, (PublicMessage(role="USER", content="Run."),))
    )

    assert turn.content is None
    assert len(turn.tool_calls) == 1


@pytest.mark.asyncio
async def test_unknown_finish_is_bounded_and_malformed_response_fails_closed():
    transport = RecordingTransport(
        (
            {
                "id": "request-1",
                "choices": [
                    {
                        "finish_reason": "PRIVATE_PROVIDER_SENTINEL",
                        "message": {"content": "bounded"},
                    }
                ],
            },
            {"provider_raw_body": "PRIVATE_BODY_SENTINEL"},
        )
    )
    binding = _binding(thinking="DISABLED")
    provider = OpenAICompatibleChatProvider(
        binding=binding,
        api_key="secret-token",
        transport=transport,
    )
    request = _request(binding, (PublicMessage(role="USER", content="Run."),))

    turn = await provider.generate(request)
    assert turn.finish_reason is FinishReason.OTHER
    assert "PRIVATE_PROVIDER_SENTINEL" not in repr(turn)
    with pytest.raises(ProviderProtocolError) as raised:
        await provider.generate(request.model_copy(update={"turn_index": 2}))
    assert "PRIVATE_BODY_SENTINEL" not in str(raised.value)


@pytest.mark.asyncio
async def test_explicit_null_tool_calls_means_no_calls():
    transport = RecordingTransport(
        (
            {
                "id": "request-null-tools",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": "complete", "tool_calls": None},
                    }
                ],
            },
        )
    )
    binding = _binding(thinking="DISABLED")
    provider = OpenAICompatibleChatProvider(
        binding=binding,
        api_key="secret-token",
        transport=transport,
    )

    turn = await provider.generate(
        _request(binding, (PublicMessage(role="USER", content="Run."),))
    )

    assert turn.content == "complete"
    assert turn.tool_calls == ()


@pytest.mark.asyncio
async def test_transport_failure_is_bounded_and_never_falls_back():
    transport = RecordingTransport((RuntimeError("PRIVATE_NETWORK_SENTINEL"),))
    binding = _binding()
    provider = OpenAICompatibleChatProvider(
        binding=binding,
        api_key="secret-token",
        transport=transport,
    )
    request = _request(binding, (PublicMessage(role="USER", content="Run."),))

    with pytest.raises(ProviderTransportError) as raised:
        await provider.generate(request)
    assert len(transport.requests) == 1
    assert "PRIVATE_NETWORK_SENTINEL" not in str(raised.value)
