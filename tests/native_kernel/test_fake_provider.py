"""P1 acceptance for the deterministic provider test double."""

from uuid import uuid4

import pytest

from labbioagentos.kernel import (
    FakeProvider,
    FakeProviderExhaustedError,
    FinishReason,
    ModelRequest,
    ModelTurn,
    ProviderBinding,
    ProviderBindingMismatchError,
    PublicMessage,
    TokenUsage,
)


def _binding(model: str = "model-a") -> ProviderBinding:
    return ProviderBinding(
        provider="provider-a",
        model=model,
        endpoint="https://provider.invalid/v1",
        transport="OPENAI_COMPATIBLE_CHAT",
        thinking_mode="DISABLED",
    )


def _request(binding: ProviderBinding) -> ModelRequest:
    return ModelRequest(
        binding=binding,
        messages=(PublicMessage(role="USER", content="Return a bounded result."),),
        max_output_tokens=256,
        deadline_monotonic=20.0,
        kernel_run_id=uuid4(),
        invocation_id=uuid4(),
        turn_index=1,
    )


@pytest.mark.asyncio
async def test_fake_provider_returns_scripted_turns_and_records_exact_requests():
    binding = _binding()
    turns = (
        ModelTurn(
            content="first",
            finish_reason=FinishReason.STOP,
            usage=TokenUsage(input_tokens=4, output_tokens=2, total_tokens=6),
            provider_request_id="request-1",
        ),
        ModelTurn(
            content="second",
            finish_reason=FinishReason.STOP,
            usage=TokenUsage(),
            provider_request_id="request-2",
        ),
    )
    provider = FakeProvider(binding=binding, turns=turns)
    first_request = _request(binding)
    second_request = first_request.model_copy(update={"turn_index": 2})

    assert await provider.generate(first_request) == turns[0]
    assert await provider.generate(second_request) == turns[1]
    assert provider.requests == (first_request, second_request)
    assert turns[1].usage.total_tokens is None


@pytest.mark.asyncio
async def test_fake_provider_never_switches_binding_or_falls_back():
    binding = _binding()
    provider = FakeProvider(
        binding=binding,
        turns=(ModelTurn(content="unused", finish_reason=FinishReason.STOP),),
    )

    with pytest.raises(ProviderBindingMismatchError):
        await provider.generate(_request(_binding(model="model-b")))
    assert provider.requests == ()
    assert provider.remaining_turns == 1


@pytest.mark.asyncio
async def test_fake_provider_exhaustion_is_explicit():
    binding = _binding()
    provider = FakeProvider(binding=binding, turns=())

    with pytest.raises(FakeProviderExhaustedError):
        await provider.generate(_request(binding))
