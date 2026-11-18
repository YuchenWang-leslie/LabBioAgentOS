"""Read credit is conditional on exact public messages and a completed provider turn."""

import asyncio
from time import monotonic

import pytest

from labbioagentos.kernel import (
    FinishReason, KernelDeadlineExceededError, ModelTurn, ProviderTransportError,
    PublicMessage, TokenUsage,
)
from test_agent_loop import _tool_loop


def stopped():
    return ModelTurn(content="Synthetic completion.", finish_reason=FinishReason.STOP,
        usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2))


@pytest.mark.asyncio
async def test_success_acknowledges_exact_final_request_not_an_earlier_window():
    loop, provider, _ = _tool_loop((stopped(),))
    received = []
    loop.request_consumed = received.append
    await loop.run(messages=(PublicMessage(role="USER", content="Original public context."),),
                   deadline_monotonic=monotonic() + 30)
    assert received == [provider.requests[0].messages]


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["transport", "deadline", "cancel"])
async def test_uncertain_or_cancelled_request_never_grants_context_credit(stop):
    loop, provider, _ = _tool_loop((stopped(),))
    received = []
    loop.request_consumed = received.append

    async def fail(_request):
        if stop == "transport":
            raise ProviderTransportError()
        if stop == "cancel":
            raise asyncio.CancelledError()
        await asyncio.sleep(1)
        return stopped()

    provider.generate = fail
    expected = {"transport": ProviderTransportError, "deadline": KernelDeadlineExceededError,
                "cancel": asyncio.CancelledError}[stop]
    with pytest.raises(expected):
        await loop.run(messages=(PublicMessage(role="USER", content="No confirmed delivery."),),
                       deadline_monotonic=monotonic() + (0.01 if stop == "deadline" else 30))
    assert not received
