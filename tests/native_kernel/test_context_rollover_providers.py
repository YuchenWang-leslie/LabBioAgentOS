"""Lossless context boundaries over all five real, offline wire serializers."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest

from labbioagentos.kernel import (
    AgentInvocation, AgentLoop, AgentMode, BudgetLedger, BudgetLimits,
    KernelIdentity, PublicMessage, ToolCall, create_provider,
)
from labbioagentos.kernel.errors import ContextCapacityError

from test_context_rollover import restored_history
from test_request_metrics import CASES, _request


CAP = 80_000
PRIVATE = "PRIVATE_WIRE_CONTINUATION_CANARY_" + "p" * 90_000


def fixture(transport, dialect, *, mode=AgentMode.CAPABILITY, plain_only=False,
            public_size=40_000):
    assistant = PublicMessage(role="ASSISTANT", content='Agent note: "测量".\nNo tool.')
    calls = PublicMessage(role="ASSISTANT", content="Inspect both original results.",
                          tool_calls=(
                              ToolCall(call_id="call-a", name="inspect", arguments_json='{"x":1}'),
                              ToolCall(call_id="call-b", name="inspect", arguments_json='{"x":2}'),
                          ))
    history = (assistant,) if plain_only else (
        assistant, PublicMessage(role="USER", content="Continue the current task."), calls,
        PublicMessage(role="TOOL", tool_call_id="call-a", content=json.dumps({
            "success": True, "information_authority": "MODEL_CONTEXT",
            "data": {"text": "EXACT_UNREAD_BODY", "source_read_credit": False},
        })),
        PublicMessage(role="TOOL", tool_call_id="call-b", tool_error=True,
                      content='{"success":false,"error":{"code":"FIXTURE_DENIED"}}'),
    )
    request = _request(transport, dialect, response_schema=None, messages=(
        PublicMessage(role="SYSTEM", content="Original permissions and authority."),
        PublicMessage(role="USER", content="Current task. " + "b" * public_size),
        *history,
    ))
    provider = create_provider(request.binding, api_key="OFFLINE_KEY_CANARY")
    store = provider._private_reasoning if transport == "OPENAI_COMPATIBLE_CHAT" else provider._turns
    for message in history:
        if message.role != "ASSISTANT":
            continue
        if transport == "OPENAI_COMPATIBLE_CHAT":
            if not message.tool_calls:
                continue  # Chat replays reasoning only alongside tool_calls.
            private = PRIVATE
        elif transport == "OPENAI_RESPONSES":
            private = json.dumps([
                {"type": "reasoning", "encrypted_content": PRIVATE},
                *provider._assistant_items(request, message),
            ])
        elif transport == "ANTHROPIC_MESSAGES":
            private = json.dumps([
                {"type": "thinking", "thinking": PRIVATE, "signature": "signature"},
                *provider._assistant_content(request, message),
            ])
        elif transport == "GEMINI_GENERATE_CONTENT":
            private = json.dumps([
                {"text": PRIVATE, "thought": True, "thoughtSignature": "signature"},
                *provider._model_parts(request, message),
            ])
        else:
            assert transport == "GEMINI_INTERACTIONS"
            private = json.dumps([
                {"type": "thought", "signature": PRIVATE, "summary": []},
                *provider._model_steps(request, message),
            ])
        store.put(request.kernel_run_id, request.invocation_id, message, private)
    loop = AgentLoop(
        provider=provider, invocation=AgentInvocation(
            identity=KernelIdentity(
                thread_id=uuid4(), workflow_run_id=uuid4(),
                kernel_run_id=request.kernel_run_id, invocation_id=request.invocation_id,
                chain=(request.invocation_id,),
            ), agent_id="fixture-agent", mode=mode,
        ), budget_ledger=BudgetLedger(BudgetLimits(
            provider_turns=8, tokens=100_000, wall_time_ms=60_000,
            tool_calls=8, evidence_entries=8, evidence_bytes=1_000_000,
            delegations=0, delegation_depth=0, finalization_attempts=1,
        )), max_request_bytes=CAP,
    )
    return request, provider, loop, store


def wire_bytes(provider, request):
    body = json.dumps(provider._payload(request), ensure_ascii=False,
                      allow_nan=False, separators=(",", ":"))
    assert len(body.encode()) == provider.request_size_bytes(request)
    return body


@pytest.mark.parametrize("transport,dialect", CASES, ids=[item[0] for item in CASES])
@pytest.mark.parametrize("plain_only", [False, True], ids=["tool-round", "plain-assistant"])
def test_rollover_rebuilds_exact_public_history_without_old_provider_state(
    transport, dialect, plain_only,
):
    request, provider, loop, store = fixture(transport, dialect, plain_only=plain_only)
    before = wire_bytes(provider, request)
    if transport == "OPENAI_COMPATIBLE_CHAT" and plain_only:
        assert PRIVATE not in before and not store._entries
        assert loop._bound_request(request) == request
        assert not loop._context_feedback
        return
    assert PRIVATE in before and len(before.encode()) > CAP
    assert len(request.model_dump_json().encode()) < CAP
    original_history = [item.model_dump(mode="json", exclude_none=True)
                        for item in request.messages if item.role in {"ASSISTANT", "TOOL"}]
    original_usage = loop.budget_ledger.usage.model_dump()

    bounded = loop._bound_request(request)

    after = wire_bytes(provider, bounded)
    assert len(after.encode()) <= CAP and PRIVATE not in after
    assert restored_history([
        {"role": item.role.lower(), "content": item.content} for item in bounded.messages
    ]) == original_history
    assert bounded.messages[:2] == request.messages[:2]
    assert all(item.role not in {"ASSISTANT", "TOOL"} for item in bounded.messages)
    assert not store._entries and not store._sizes and not store._tool_batches
    assert loop.budget_ledger.usage.model_dump() == original_usage
    assert bounded.kernel_run_id == request.kernel_run_id
    assert bounded.invocation_id == request.invocation_id
    assert bounded.deadline_monotonic == request.deadline_monotonic
    assert bounded.tools == request.tools and bounded.max_output_tokens == request.max_output_tokens
    assert [event.kind.value for event in loop.events].count("CONTEXT_ROLLED_OVER") == 1
    assert all(PRIVATE not in event.model_dump_json() for event in loop.events)


@pytest.mark.parametrize("transport,dialect", CASES, ids=[item[0] for item in CASES])
@pytest.mark.parametrize("reason", ["finalizer", "public-too-large"])
def test_unavailable_rollover_preserves_private_store_and_rejects_without_mutation(
    transport, dialect, reason,
):
    request, provider, loop, store = fixture(
        transport, dialect,
        mode=AgentMode.FINALIZE if reason == "finalizer" else AgentMode.CAPABILITY,
        public_size=40_000 if reason == "finalizer" else 63_000,
    )
    if reason == "finalizer":
        request = request.model_copy(update={"tools": (), "response_schema": {
            "type": "object", "properties": {"answer": {"type": "string"}},
        }})
    else:
        request = request.model_copy(update={"messages": (
            *request.messages, PublicMessage(role="USER", content="Unchanged task. " + "z" * 30_000),
        )})
    before = wire_bytes(provider, request)
    entries, sizes, batches = dict(store._entries), dict(store._sizes), dict(store._tool_batches)
    context_id = loop._context_id

    with pytest.raises(ContextCapacityError):
        loop._bound_request(request)

    assert wire_bytes(provider, request) == before
    assert store._entries == entries and store._sizes == sizes and store._tool_batches == batches
    assert loop.messages == () and loop._context_id == context_id
    assert loop._context_feedback == [] and loop._delivered_tool_invocations == set()
    assert not any(event.kind.value == "CONTEXT_ROLLED_OVER" for event in loop.events)
    assert loop.budget_ledger.usage.provider_turns == 0
