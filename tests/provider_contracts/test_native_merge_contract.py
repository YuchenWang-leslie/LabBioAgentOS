"""Provider formats preserve the current Native accounting and context boundaries."""

import json

import pytest

from labbioagentos.kernel import ModelRequest, ProviderProtocolError, PublicMessage
from labbioagentos.kernel.errors import ContextCapacityError

from .harness import DEADLINE, PROMPT, capability_loop
from .wire import Call, KNOWN_USAGE, Turn


async def test_rejected_complete_output_retains_independent_usage(wire, server):
    server.script(wire.encode(Turn(calls=(Call("invalid id", {"value": "PRIVATE_CANARY"}),))))
    loop, effects = capability_loop(wire.provider(wire.binding()))

    with pytest.raises(ProviderProtocolError) as caught:
        await loop.run(messages=PROMPT, deadline_monotonic=DEADLINE)

    assert caught.value.usage.total_tokens == KNOWN_USAGE[2]
    assert caught.value.usage_snapshot is None
    assert loop.budget_ledger.usage.tokens == KNOWN_USAGE[2]
    assert not effects and not loop.observations
    assert "PRIVATE_CANARY" not in str(caught.value)
    assert len(server.requests) == 1


async def test_private_continuation_counts_toward_request_admission(wire, server):
    server.script(
        wire.encode(Turn(calls=(Call("call-a", {"value": "a"}),), reasoning="PRIVATE" * 5_000)),
        wire.encode(Turn(text="done"), 2),
    )
    loop, effects = capability_loop(wire.provider(wire.binding()))
    loop.max_request_bytes = 18_000

    if wire.carries_reasoning:
        with pytest.raises(ContextCapacityError):
            await loop.run(messages=PROMPT, deadline_monotonic=DEADLINE)
        assert len(server.requests) == 1
    else:
        assert (await loop.run(messages=PROMPT, deadline_monotonic=DEADLINE)).output == "done"
        assert len(server.requests) == 2
    assert effects == ["a"]


async def test_discard_releases_only_unreferenced_batch_and_size_matches_wire(wire, server):
    server.script(*(
        wire.encode(Turn(calls=(Call(f"call-{n}", {"value": str(n)}),), reasoning=f"PRIVATE_{n}"), n)
        for n in (1, 2)
    ))
    provider = wire.provider(wire.binding())
    loop, _effects = capability_loop(provider)
    identity = loop.invocation.identity
    request = ModelRequest(
        binding=provider.binding, messages=PROMPT, tools=loop._tool_specs(),
        max_output_tokens=128, deadline_monotonic=DEADLINE,
        kernel_run_id=identity.kernel_run_id, invocation_id=identity.invocation_id, turn_index=1,
    )
    calls = []
    for n in (1, 2):
        turn = await provider.generate(request)
        assert provider.request_size_bytes(request) == len(server.requests[-1].content)
        calls.append(turn.tool_calls)
        request = request.model_copy(update={"messages": (
            *request.messages, PublicMessage(role="ASSISTANT", tool_calls=turn.tool_calls),
            PublicMessage(role="TOOL", tool_call_id=f"call-{n}", content='{"ok":true}'),
        ), "turn_index": n + 1})
    retained = (*PROMPT, *request.messages[-2:])
    provider.discard_tool_batch(identity.kernel_run_id, identity.invocation_id, calls[0],
                                retained_messages=retained)
    provider.discard_tool_batch(identity.kernel_run_id, identity.invocation_id, calls[1],
                                retained_messages=retained)
    payload = json.dumps(provider._payload(request))
    assert "PRIVATE_1" not in payload
    assert ("PRIVATE_2" in payload) is wire.carries_reasoning
    store = provider._private_reasoning if wire.transport == "OPENAI_COMPATIBLE_CHAT" else provider._turns
    assert len(store._tool_batches) == (1 if wire.carries_reasoning or wire.transport != "OPENAI_COMPATIBLE_CHAT" else 0)
    provider.clear_invocation(identity.kernel_run_id, identity.invocation_id)
    assert not store._entries and not store._sizes and not store._tool_batches


async def test_retry_does_not_turn_unknown_attempt_cost_into_known_total(wire, server):
    import httpx

    server.script(httpx.Response(429), wire.encode(Turn(text="done")))
    loop, _effects = capability_loop(wire.provider(wire.binding()))
    assert (await loop.run(messages=PROMPT, deadline_monotonic=DEADLINE)).output == "done"
    assert loop.observations[0].usage.total_tokens == KNOWN_USAGE[2]
    assert loop.budget_ledger.usage.tokens is None
