"""Chat protocol failures retain only a safely parsed finish diagnostic."""

from time import monotonic
from uuid import uuid4

import pytest

from labbioagentos.kernel import (
    FinishReason,
    ModelRequest,
    OpenAICompatibleChatProvider,
    ProviderBinding,
    ProviderProtocolError,
    PublicMessage,
)
from test_openai_chat_provider import RecordingTransport


def _provider_and_request(response):
    binding = ProviderBinding(
        provider="fixture", model="fixture", endpoint="https://provider.invalid/v1",
        transport="OPENAI_COMPATIBLE_CHAT", thinking_mode="ENABLED",
    )
    transport = RecordingTransport((response,))
    provider = OpenAICompatibleChatProvider(
        binding=binding, api_key="fixture-only", transport=transport,
    )
    request = ModelRequest(
        binding=binding, messages=(PublicMessage(role="USER", content="Inspect."),),
        max_output_tokens=8192, deadline_monotonic=monotonic() + 5,
        kernel_run_id=uuid4(), invocation_id=uuid4(), turn_index=1,
    )
    return provider, request, transport


def _response(finish="length", content=""):
    return {
        "choices": [{"finish_reason": finish, "message": {
            "role": "assistant", "content": content,
            "reasoning_content": "PRIVATE_REASONING_CANARY",
        }}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 8192,
                  "total_tokens": 8203},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("finish,expected", [
    ("stop", FinishReason.STOP),
    ("PRIVATE_UNKNOWN_FINISH_CANARY", FinishReason.OTHER),
])
async def test_empty_chat_text_stays_rejected_with_safe_finish(finish, expected):
    provider, request, transport = _provider_and_request(_response(finish))
    with pytest.raises(ProviderProtocolError) as caught:
        await provider.generate(request)
    error = caught.value
    assert error.provider_finish_reason is expected
    assert error.code == "PROVIDER_PROTOCOL_ERROR"
    assert error.validation_type == "string_too_short"
    assert error.validation_fields == ("model_turn", "content")
    assert error.usage.input_tokens == 11
    assert error.usage.output_tokens == 8192
    assert error.usage.total_tokens == 8203
    assert error.transport_retries == ()
    assert len(transport.requests) == 1
    assert not provider._private_reasoning._entries
    safe_error = str(error) + repr(vars(error))
    assert "PRIVATE_REASONING_CANARY" not in safe_error
    assert "PRIVATE_UNKNOWN_FINISH_CANARY" not in safe_error


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [
    "multiple_choices", "missing_message", "invalid_role", "invalid_content",
    "missing_finish", "invalid_finish",
])
async def test_unparsed_or_invalid_chat_finish_stays_unknown(mutation):
    response = _response()
    choice = response["choices"][0]
    if mutation == "multiple_choices":
        response["choices"].append(choice.copy())
    elif mutation == "missing_message":
        del choice["message"]
    elif mutation == "invalid_role":
        choice["message"]["role"] = "PRIVATE_INVALID_ROLE_CANARY"
    elif mutation == "invalid_content":
        choice["message"]["content"] = {"private": "PRIVATE_CONTENT_CANARY"}
    elif mutation == "missing_finish":
        del choice["finish_reason"]
    else:
        choice["finish_reason"] = {"private": "PRIVATE_FINISH_CANARY"}
    provider, request, transport = _provider_and_request(response)
    with pytest.raises(ProviderProtocolError) as caught:
        await provider.generate(request)
    assert caught.value.provider_finish_reason is None
    assert caught.value.usage.total_tokens == 8203
    assert len(transport.requests) == 1
    assert "PRIVATE_" not in str(caught.value) + repr(vars(caught.value))


@pytest.mark.parametrize("finish", [FinishReason.LENGTH, FinishReason.OTHER, None])
def test_protocol_finish_diagnostic_accepts_only_enum_or_none(finish):
    error = ProviderProtocolError(provider_finish_reason=finish)
    assert error.provider_finish_reason is finish


@pytest.mark.parametrize("finish", ["LENGTH", "PRIVATE_FINISH_CANARY", 1, {}])
def test_protocol_finish_diagnostic_rejects_raw_values(finish):
    with pytest.raises(TypeError) as caught:
        ProviderProtocolError(provider_finish_reason=finish)
    assert "PRIVATE_FINISH_CANARY" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("finish,content,expected", [
    ("length", None, FinishReason.LENGTH),
    ("stop", "Valid public content", FinishReason.STOP),
    ("PRIVATE_UNKNOWN_FINISH_CANARY", "Valid public content", FinishReason.OTHER),
])
async def test_valid_chat_turn_contract_does_not_change(finish, content, expected):
    provider, request, transport = _provider_and_request(_response(finish, content))
    turn = await provider.generate(request)
    assert turn.finish_reason is expected
    assert turn.content == content
    assert turn.usage.total_tokens == 8203
    assert len(transport.requests) == 1
    assert "PRIVATE_" not in turn.model_dump_json()
