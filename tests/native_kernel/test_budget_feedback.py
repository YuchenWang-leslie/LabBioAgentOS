"""The public control snapshot and durable usage retain actual budget facts."""

import asyncio
import json

import pytest

from labbioagentos.kernel import (
    FinishReason, InterruptReason, KernelDeadlineExceededError, KernelInterruptedError,
    ModelTurn, ProviderProtocolError,
    ProviderTransportError, PublicMessage, TokenUsage, ToolCall,
)
from test_agent_loop import _observed_batch, _tool_loop


@pytest.mark.asyncio
@pytest.mark.parametrize("tokens,remaining", [(4, 9_996), (None, None)])
async def test_tool_feedback_includes_actual_provider_budget_and_unknown_tokens(tokens, remaining):
    now = [0.0]
    loop, provider, _effects = _tool_loop((
        ModelTurn(
            tool_calls=(ToolCall(call_id="actual", name="record_value",
                                 arguments_json='{"value":"synthetic"}'),),
            finish_reason=FinishReason.TOOL_CALLS,
            usage=TokenUsage(total_tokens=tokens),
        ),
        ModelTurn(content="Complete.", finish_reason=FinishReason.STOP),
    ), clock=lambda: now[0])
    generate = provider.generate

    async def measured(request):
        turn = await generate(request)
        now[0] += 1.25
        return turn

    provider.generate = measured
    result = await loop.run(
        messages=(PublicMessage(role="USER", content="Synthetic budget check."),),
        deadline_monotonic=60.0,
    )
    feedback = provider.requests[1].messages[-1]
    assert json.loads(feedback.content)["control_state"]["kernel_budget"] == {
        "tool_calls_remaining": 7,
        "evidence_entries_remaining": 7,
        "provider_turns_remaining": 7,
        "tokens_remaining": remaining,
        "wall_time_ms_remaining": 58_750,
    }
    assert loop._feedback_result(feedback.content) == result.tool_results[0]
    prepared = [event for event in result.events if event.kind.value == "MODEL_REQUEST_PREPARED"]
    assert prepared[1].numeric_facts["public_request_bytes"] == len(
        provider.requests[1].model_dump_json().encode("utf-8")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("elapsed_ms", [3_000, 65_000])
@pytest.mark.parametrize("failure", [
    ProviderTransportError, ProviderProtocolError, RuntimeError, asyncio.CancelledError,
])
async def test_started_provider_failure_records_one_unknown_usage_without_a_model_turn(
    failure, elapsed_ms,
):
    now = [0.0]
    loop, provider, _effects = _tool_loop((
        ModelTurn(
            tool_calls=(ToolCall(call_id="kept", name="record_value",
                                 arguments_json='{"value":"synthetic"}'),),
            finish_reason=FinishReason.TOOL_CALLS,
            usage=TokenUsage(total_tokens=4),
        ),
    ), clock=lambda: now[0])
    generate = provider.generate
    calls = 0

    async def fail_after_one(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return await generate(request)
        now[0] += elapsed_ms / 1000
        raise failure()

    provider.generate = fail_after_one
    invocation = loop.run(
        messages=(PublicMessage(role="USER", content="Synthetic failure."),),
        deadline_monotonic=60.0,
    )
    if failure is asyncio.CancelledError:
        with pytest.raises(asyncio.CancelledError):
            await invocation
    else:
        await invocation
    unknown = [event for event in loop.events if event.kind.value == "MODEL_USAGE_UNREPORTED"]
    assert len(unknown) == 1
    assert unknown[0].status_code == "UNKNOWN_USAGE"
    assert unknown[0].numeric_facts == {
        "turn_index": 2, "elapsed_ms": elapsed_ms,
        "input_tokens": None, "output_tokens": None, "total_tokens": None,
    }
    assert len(loop.observations) == 1
    assert loop.budget_ledger.usage.tokens is None
    assert loop.budget_ledger.usage.wall_time_ms == elapsed_ms
    assert len(loop.tool_results) == 1


@pytest.mark.asyncio
async def test_deadline_before_provider_entry_does_not_claim_unknown_provider_usage():
    loop, provider, _effects = _tool_loop((), clock=lambda: 0.0)
    bound = loop._bound_request

    def deadline_exhausted(request):
        request = bound(request)
        loop.current_deadline_monotonic = 0.0
        return request

    loop._bound_request = deadline_exhausted
    with pytest.raises(KernelDeadlineExceededError):
        await loop.run(
            messages=(PublicMessage(role="USER", content="No provider call."),),
            deadline_monotonic=60.0,
        )
    assert provider.requests == ()
    assert loop.budget_ledger.usage.tokens == 0
    assert not any(event.kind.value == "MODEL_USAGE_UNREPORTED" for event in loop.events)


@pytest.mark.asyncio
@pytest.mark.parametrize("tokens,interrupted", [(4, False), (11, False), (11, True), (None, False)])
async def test_returned_model_turn_never_becomes_an_unreported_usage_event(tokens, interrupted):
    loop, provider, _effects = _tool_loop((
        ModelTurn(content="Synthetic complete.", finish_reason=FinishReason.STOP,
                  usage=TokenUsage(total_tokens=tokens)),
    ), tokens=10)
    generate = provider.generate

    async def returned(request):
        turn = await generate(request)
        if interrupted:
            loop.interrupt_token.request(InterruptReason.USER_REQUESTED, reason_code="USER_STOP")
        return turn

    provider.generate = returned
    pending = loop.run(
        messages=(PublicMessage(role="USER", content="Observed return."),),
        deadline_monotonic=10_000_000_000.0,
    )
    if interrupted:
        with pytest.raises(KernelInterruptedError):
            await pending
    else:
        await pending
    assert loop.budget_ledger.usage.tokens == tokens
    assert len(loop.observations) == 1
    assert not any(event.kind.value == "MODEL_USAGE_UNREPORTED" for event in loop.events)


@pytest.mark.parametrize("key,value", [
    ("provider_turns_remaining", None), ("tokens_remaining", "1"),
    ("wall_time_ms_remaining", -1), ("tool_calls_remaining", True),
    ("untrusted_field", 1),
])
def test_context_compaction_rejects_invalid_extended_budget_metadata(key, value):
    from labbioagentos.kernel import AgentLoop

    _identity, (_assistant, feedback) = _observed_batch(1)
    payload = json.loads(feedback.content)
    payload["control_state"]["kernel_budget"].update(
        provider_turns_remaining=7, tokens_remaining=None, wall_time_ms_remaining=1000,
    )
    payload["control_state"]["kernel_budget"][key] = value
    with pytest.raises(ValueError):
        AgentLoop._feedback_result(json.dumps(payload))
