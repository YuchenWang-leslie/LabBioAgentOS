"""A complete oversized final response can be regenerated, never truncated."""

import pytest

from labbioagentos.kernel import (
    AgentLoop, BudgetLedger, FinalizationRejectedError, OpenAICompatibleChatProvider,
    ProviderProtocolError, PublicMessage,
)
from test_agent_loop import FinalEnvelope, _binding, _invocation, _limits
from test_openai_chat_provider import RecordingTransport


def response(finish="length", *, usage=True, role="assistant", valid=False):
    return {"choices": [{"finish_reason": finish, "message": {
        "role": role,
        "content": '{"result":{"count":7}}' if valid else "PRIVATE_OUTPUT_CANARY" * 4000,
    }}], **({"usage": {"prompt_tokens": 11, "completion_tokens": 8192, "total_tokens": 8203}}
            if usage else {})}


def loop_for(responses, *, attempts=2, final=True):
    transport = RecordingTransport(tuple(responses))
    provider = OpenAICompatibleChatProvider(
        binding=_binding(), api_key="fixture-only", transport=transport,
    )
    invocation = _invocation(final=final)
    if not final:
        invocation = invocation.model_copy(update={"tool_names": ()})
    ledger = BudgetLedger(_limits(tokens=100_000, finalization_attempts=attempts))
    return AgentLoop(provider=provider, invocation=invocation, budget_ledger=ledger), transport


async def run(loop, *, final=True):
    return await loop.run(messages=(PublicMessage(role="USER", content="Finalize engineering result."),),
        deadline_monotonic=10_000_000_000.0, response_model=FinalEnvelope if final else None)


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ("stop", "length"))
async def test_complete_oversized_public_output_gets_safe_bounded_feedback(finish):
    loop, transport = loop_for((response(finish), response("stop", valid=True)))
    result = await run(loop)
    assert result.output == {"result": {"count": 7}}
    assert len(transport.requests) == 2
    assert result.budget_usage.finalization_attempts == 2
    assert result.budget_usage.tokens == 2 * 8203
    feedback = loop.messages[-1].content
    assert "string_too_long" in feedback and "model_turn.content" in feedback
    assert "shorter" in feedback
    assert "PRIVATE_OUTPUT_CANARY" not in repr(loop.messages) + repr(loop.events)


@pytest.mark.asyncio
@pytest.mark.parametrize("attempts", (1, 2))
async def test_output_bound_repair_uses_existing_finalization_attempt_limit(attempts):
    loop, transport = loop_for((response(), response(), response("stop", valid=True)), attempts=attempts)
    with pytest.raises(FinalizationRejectedError) as caught:
        await run(loop)
    assert caught.value.validation_type == "string_too_long"
    assert len(transport.requests) == attempts
    assert loop.budget_ledger.usage.tokens == attempts * 8203


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ("unknown_usage", "unknown_finish", "wire_error", "capability"))
async def test_other_or_uncertain_provider_failures_are_not_retried(case):
    bad = response(usage=case != "unknown_usage",
        finish="unrecognized" if case == "unknown_finish" else "length",
        role="user" if case == "wire_error" else "assistant")
    final = case != "capability"
    loop, transport = loop_for((bad, response("stop", valid=True)), final=final)
    with pytest.raises(ProviderProtocolError):
        await run(loop, final=final)
    assert len(transport.requests) == 1
    if case == "unknown_usage":
        assert loop.budget_ledger.usage.tokens is None
