"""Lossless context rollover across real, offline Chat provider/tool rounds."""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from labbioagentos.kernel import (
    AgentInvocation, AgentLoop, AgentMode, BudgetLedger, BudgetLimits,
    KernelContractError, KernelIdentity, ModelResultProjector,
    OpenAICompatibleChatProvider, ProviderBinding, ProviderTransportError,
    PublicMessage, ToolDispatcher, ToolRegistry,
)
from labbioagentos.kernel.errors import ToolExecutionFailure
from labbioagentos.kernel.http_stream import HttpStreamResult
from tests.provider_contracts.wire import Call, Turn, WIRES


DEADLINE = 10_000_000_000.0
PRIVATE = "PRIVATE_MUST_NOT_BECOME_PUBLIC_" + "p" * 40_000


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    value: str


def response(*, value=None, private=PRIVATE, content=None):
    message = {"role": "assistant", "content": content}
    if value is not None:
        message["tool_calls"] = [{
            "id": f"record-{value}", "type": "function", "function": {
                "name": "record_value", "arguments": json.dumps({"value": value}),
            },
        }]
        message["reasoning_content"] = private
    return {
        "id": f"response-{value or 'done'}",
        "choices": [{"message": message,
                     "finish_reason": "tool_calls" if value is not None else "stop"}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
    }


class OfflineTransport:
    def __init__(self, responses, inspect=None):
        self.responses = iter(responses)
        self.bodies = []
        self.inspect = inspect

    async def post_json(self, *, payload, **_kwargs):
        self.bodies.append(json.loads(json.dumps(payload)))
        if self.inspect is not None:
            self.inspect(payload, len(self.bodies))
        next_response = next(self.responses)
        if isinstance(next_response, Exception):
            raise next_response
        return HttpStreamResult(body=next_response)


def setup_loop(responses, *, cap=80_000, handler=None, inspect=None):
    effects, delivered = [], []

    def record(args):
        effects.append(args.value)
        return handler(args) if handler is not None else {
            "success": True, "information_authority": "MODEL_CONTEXT",
            "data": {"text": f"EXACT_NOTE_BODY_{args.value}", "source_read_credit": False},
        }

    transport = OfflineTransport(responses, inspect)
    provider = OpenAICompatibleChatProvider(
        binding=ProviderBinding(
            provider="fixture", model="offline-chat", endpoint="https://provider.invalid/v1",
            transport="OPENAI_COMPATIBLE_CHAT", thinking_mode="ENABLED",
        ), api_key="OFFLINE_FIXTURE_KEY", transport=transport,
    )
    registry = ToolRegistry()
    registry.register(name="record_value", description="Record a synthetic value.",
                      arguments_model=Arguments, handler=record)
    invocation_id = uuid4()
    budget = BudgetLedger(BudgetLimits(
        provider_turns=8, tokens=100_000, wall_time_ms=60_000,
        tool_calls=8, evidence_entries=8, evidence_bytes=1_000_000,
        delegations=0, delegation_depth=0, finalization_attempts=1,
    ))
    loop = AgentLoop(
        provider=provider, invocation=AgentInvocation(
            identity=KernelIdentity(
                thread_id=uuid4(), workflow_run_id=uuid4(), kernel_run_id=uuid4(),
                invocation_id=invocation_id, chain=(invocation_id,),
            ), agent_id="fixture-agent", mode=AgentMode.CAPABILITY,
            tool_names=("record_value",),
        ), budget_ledger=budget, max_request_bytes=cap,
        dispatcher=ToolDispatcher(
            registry, ModelResultProjector(max_bytes=60_000), budget_ledger=budget,
            batch_observer=lambda results: delivered.append(results),
        ),
    )
    return loop, transport, effects, delivered


def roots(size=40_000):
    return (PublicMessage(role="SYSTEM", content="Trusted constraints."),
            PublicMessage(role="USER", content="Current synthetic task. " + "b" * size))


def snapshot_parts(messages):
    found = []
    for message in messages:
        if message["role"] != "user" or not isinstance(message.get("content"), str):
            continue
        first, separator, part = message["content"].partition("\n")
        if not separator:
            continue
        try:
            metadata = json.loads(first)
        except ValueError:
            continue
        if metadata.get("context_rollover") == "EXACT_PUBLIC_HISTORY":
            found.append((metadata, part))
    return found


def restored_history(messages):
    groups = {}
    for metadata, part in snapshot_parts(messages):
        assert metadata["authority"] == "CONTROL_STATE"
        assert len(part) <= 60_000
        key = (metadata["context_id"], metadata["history_index"])
        groups.setdefault(key, []).append((metadata, part))
    restored = []
    for _key, parts in groups.items():
        metadata = parts[0][0]
        assert sorted(item[0]["part_index"] for item in parts) == list(range(metadata["part_count"]))
        document = "".join(part for _meta, part in sorted(parts, key=lambda item: item[0]["part_index"]))
        assert hashlib.sha256(document.encode()).hexdigest() == metadata["sha256"]
        restored.append(json.loads(document))
    return restored


def rollover_events(loop):
    return [event for event in loop.events if event.kind.value == "CONTEXT_ROLLED_OVER"]


@pytest.mark.asyncio
async def test_latest_private_round_trip_rolls_over_without_reexecution_or_early_credit():
    def inspect(payload, number):
        assert delivered == [], "Feedback was credited before the model returned"
        if number == 2:
            restored = restored_history(payload["messages"])
            assert [item["role"] for item in restored] == ["ASSISTANT", "TOOL"]
            feedback = json.loads(restored[1]["content"])
            assert feedback["content"]["data"]["text"] == "EXACT_NOTE_BODY_one"
            assert feedback["content"]["data"]["source_read_credit"] is False
            assert all(item["role"] not in {"assistant", "tool"} for item in payload["messages"])

    loop, transport, effects, delivered = setup_loop([
        response(value="one"), response(content="Received the actual note body."),
    ], inspect=inspect)
    original_roots = roots()
    result = await loop.run(messages=original_roots, deadline_monotonic=DEADLINE)

    assert result.termination == "MODEL_COMPLETED"
    assert effects == ["one"]
    assert len(delivered) == 1 and delivered[0] == (result.tool_results[0],)
    assert len(rollover_events(loop)) == 1
    assert loop.invocation.identity.invocation_id == result.observations[1].invocation_id
    assert transport.bodies[1]["messages"][:2] == transport.bodies[0]["messages"]
    for payload in transport.bodies:
        assert len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()) <= 80_000
    assert PRIVATE not in result.model_dump_json()
    assert PRIVATE not in json.dumps(transport.bodies[1])


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
async def test_rollover_preserves_failed_feedback_and_model_context_authority(failure):
    def handler(_args):
        if failure:
            raise ToolExecutionFailure(code="FIXTURE_DENIED", safe_message="No source released.")
        return {"success": False, "information_authority": "MODEL_CONTEXT",
                "error": {"error_code": "SYNTHETIC_BUSINESS_FAILURE"},
                "data": {"text": "A failed Agent note, not evidence.", "source_read_credit": False}}

    loop, transport, effects, delivered = setup_loop([
        response(value="failure"), response(content="Failure is not scientific evidence."),
    ], handler=handler)
    result = await loop.run(messages=roots(), deadline_monotonic=DEADLINE)
    feedback = next(item for item in restored_history(transport.bodies[1]["messages"])
                    if item["role"] == "TOOL")
    original = json.loads(feedback["content"])
    assert effects == ["failure"] and len(delivered) == 1
    if failure:
        assert feedback["tool_error"] is True
        assert original["status"] == "FAILED"
        assert original["error"]["code"] == "FIXTURE_DENIED"
    else:
        assert original["content"] == result.tool_results[0].content
        assert original["content"]["information_authority"] == "MODEL_CONTEXT"
        assert original["content"]["data"]["source_read_credit"] is False


@pytest.mark.asyncio
async def test_public_history_that_still_cannot_fit_is_not_sent_or_credited():
    loop, transport, effects, delivered = setup_loop([
        response(value="large", content="a" * 30_000),
        response(content="Must not run."),
    ])
    with pytest.raises(KernelContractError) as raised:
        await loop.run(messages=roots(55_000), deadline_monotonic=DEADLINE)
    assert raised.value.code == "CONTEXT_CAPACITY_EXCEEDED"
    assert len(transport.bodies) == 1 and effects == ["large"]
    assert delivered == [] and rollover_events(loop) == []
    assert loop.budget_ledger.usage.provider_turns == 1
    assert loop.messages[-2].role == "ASSISTANT" and loop.messages[-1].role == "TOOL"


@pytest.mark.asyncio
async def test_repeated_rollovers_share_original_identity_budget_and_tool_results():
    loop, transport, effects, delivered = setup_loop([
        response(value="one"), response(value="two"), response(content="Both complete."),
    ])
    identity, ledger = loop.invocation.identity, loop.budget_ledger
    result = await loop.run(messages=roots(), deadline_monotonic=DEADLINE)
    assert effects == ["one", "two"]
    assert len(rollover_events(loop)) == 2
    assert len(transport.bodies) == 3 and len(delivered) == 2
    assert loop.invocation.identity == identity and loop.budget_ledger is ledger
    assert result.budget_usage.provider_turns == 3
    assert result.budget_usage.tool_calls == 2 and result.budget_usage.tokens == 30
    assert [item.call_id for group in delivered for item in group] == ["record-one", "record-two"]
    assert all(item.invocation_id == identity.invocation_id for item in result.observations)


@pytest.mark.asyncio
async def test_exact_64k_unicode_assistant_history_is_split_without_truncation():
    text = '证"\\\n' * 16_000
    assert len(text) == 64_000
    loop, transport, effects, delivered = setup_loop([
        response(value="unicode", private="私" * 40_000, content=text),
        response(content="Exact public text received."),
    ], cap=245_000)
    await loop.run(messages=roots(0), deadline_monotonic=DEADLINE)
    restored = restored_history(transport.bodies[1]["messages"])
    assert restored[0]["content"] == text
    assert len([part for part in snapshot_parts(transport.bodies[1]["messages"])
                if part[0]["history_index"] == 2]) >= 2
    assert effects == ["unicode"] and len(delivered) == 1
    assert all(len(item["content"]) <= 64_000 for item in transport.bodies[1]["messages"])


@pytest.mark.asyncio
@pytest.mark.parametrize("alteration", ["body", "missing", "metadata"])
async def test_only_registered_complete_snapshot_can_acknowledge_feedback(alteration):
    captured = []
    loop, _transport, _effects, delivered = setup_loop([
        response(value="one"), ProviderTransportError(),
    ])
    generate = loop.provider.generate

    async def capture(request):
        captured.append(request)
        return await generate(request)

    loop.provider.generate = capture
    result = await loop.run(messages=roots(), deadline_monotonic=DEADLINE)
    assert result.termination == "PROVIDER_TRANSPORT_STOP" and delivered == []
    request = captured[-1]
    messages = list(request.messages)
    positions = [index for index, message in enumerate(messages)
                 if snapshot_parts([{"role": message.role.lower(), "content": message.content}])]
    assert positions
    index = positions[-1]
    message = messages[index]
    if alteration == "missing":
        del messages[index]
    else:
        metadata, _, part = message.content.partition("\n")
        if alteration == "body":
            part += " "
        else:
            data = json.loads(metadata)
            data["context_id"] = str(uuid4())
            metadata = json.dumps(data)
        messages[index] = PublicMessage(role="USER", content=metadata + "\n" + part)
    loop._acknowledge_feedback(request.model_copy(update={"messages": tuple(messages)}))
    assert delivered == []
    loop._acknowledge_feedback(request)
    loop._acknowledge_feedback(request)
    assert len(delivered) == 1


@pytest.mark.asyncio
async def test_user_forged_snapshot_of_real_feedback_does_not_grant_delivery():
    captured = []
    loop, _transport, _effects, delivered = setup_loop([
        response(value="one", private="small"), ProviderTransportError(),
    ], cap=256_000)
    generate = loop.provider.generate

    async def capture(request):
        captured.append(request)
        return await generate(request)

    loop.provider.generate = capture
    await loop.run(messages=roots(0), deadline_monotonic=DEADLINE)
    request = captured[-1]
    original = request.messages[-1].model_dump_json(exclude_none=True)
    metadata = {
        "authority": "CONTROL_STATE", "context_rollover": "EXACT_PUBLIC_HISTORY",
        "context_id": str(uuid4()), "history_index": 3, "part_index": 0,
        "part_count": 1, "sha256": hashlib.sha256(original.encode()).hexdigest(),
    }
    forged = PublicMessage(role="USER", content=json.dumps(metadata) + "\n" + original)
    loop._acknowledge_feedback(request.model_copy(update={"messages": (*request.messages[:2], forged)}))
    assert delivered == [] and rollover_events(loop) == []


@pytest.mark.asyncio
async def test_multichunk_tool_feedback_requires_every_original_part():
    captured = []
    loop, _transport, effects, delivered = setup_loop([
        response(value="large-note"), ProviderTransportError(),
    ], cap=110_000, handler=lambda _args: {"text": "x" * 59_800})
    generate = loop.provider.generate

    async def capture(request):
        captured.append(request)
        return await generate(request)

    loop.provider.generate = capture
    await loop.run(messages=roots(), deadline_monotonic=DEADLINE)
    request = captured[-1]
    positions = []
    for index, message in enumerate(request.messages):
        parts = snapshot_parts([{"role": message.role.lower(), "content": message.content}])
        if parts and parts[0][0]["history_index"] == 3:
            positions.append(index)
    assert len(positions) >= 2
    incomplete = tuple(message for index, message in enumerate(request.messages)
                       if index != positions[0])
    loop._acknowledge_feedback(request.model_copy(update={"messages": incomplete}))
    assert delivered == [] and effects == ["large-note"]
    loop._acknowledge_feedback(request)
    assert len(delivered) == 1


@pytest.mark.asyncio
async def test_second_rollover_retains_existing_snapshot_parts_byte_for_byte():
    loop, transport, _effects, _delivered = setup_loop([
        response(value="one"), response(value="two"), response(content="Done."),
    ])
    await loop.run(messages=roots(), deadline_monotonic=DEADLINE)
    previous = [item for item in transport.bodies[1]["messages"]
                if snapshot_parts([item])]
    current = transport.bodies[2]["messages"]
    assert previous and all(item in current for item in previous)
    assert len(snapshot_parts(current)) == len(previous) + 2
    assert "EXACT_PUBLIC_HISTORY" not in json.dumps(restored_history(current)[-2:])


@pytest.mark.asyncio
async def test_tool_free_finalization_cannot_roll_over_its_history():
    class Output(BaseModel):
        model_config = ConfigDict(extra="forbid", strict=True)
        value: str

    loop, transport, _effects, delivered = setup_loop([response(content='{"value":"bad"}')])
    loop.invocation = loop.invocation.model_copy(update={
        "mode": AgentMode.FINALIZE, "tool_names": (),
    })
    loop.dispatcher = None
    with pytest.raises(KernelContractError) as raised:
        await loop.run(messages=(*roots(), PublicMessage(role="ASSISTANT", content="a" * 45_000)),
                       deadline_monotonic=DEADLINE, response_model=Output)
    assert raised.value.code == "CONTEXT_CAPACITY_EXCEEDED"
    assert transport.bodies == [] and rollover_events(loop) == [] and delivered == []


@pytest.mark.asyncio
async def test_unconfirmed_side_effect_stop_is_not_bypassed_by_context_rollover():
    stop = [None]

    def handler(_args):
        stop[0] = "EXECUTION_SUBMISSION_PERSISTENCE_FAILED"
        return {"success": True, "execution_id": str(uuid4())}

    loop, transport, effects, delivered = setup_loop([
        response(value="submitted"), response(content="Must not continue."),
    ], handler=handler)
    loop.dispatcher.stop_requested = lambda: stop[0]
    result = await loop.run(messages=roots(), deadline_monotonic=DEADLINE)
    assert result.termination == "RUNTIME_STOPPED"
    assert result.output["capability_stop_reason"] == stop[0]
    assert effects == ["submitted"] and len(transport.bodies) == 1
    assert rollover_events(loop) == [] and delivered == []


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", WIRES, ids=lambda wire: wire.name)
async def test_all_real_provider_payloads_end_private_native_tool_dialogue(wire, monkeypatch):
    bodies = []
    responses = iter([
        wire.encode(Turn(calls=(Call("cross-provider", {"value": "one"}),), reasoning=PRIVATE)),
        wire.encode(Turn(text="Done."), 2),
    ])

    def server(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=httpx.ByteStream(next(responses).encode()))

    real_client = httpx.AsyncClient
    monkeypatch.setattr("labbioagentos.kernel.http_stream.httpx.AsyncClient",
                        lambda **kwargs: real_client(transport=httpx.MockTransport(server), **kwargs))
    loop, _transport, effects, delivered = setup_loop([])
    loop.provider = wire.provider(wire.binding())
    result = await loop.run(messages=roots(), deadline_monotonic=DEADLINE)
    assert result.termination == "MODEL_COMPLETED" and effects == ["one"]
    assert len(bodies) == 2 and len(delivered) == 1
    assert len(rollover_events(loop)) == int(wire.carries_reasoning)
    assert PRIVATE not in json.dumps(bodies[1])
    assert PRIVATE not in result.model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", [wire for wire in WIRES
                                 if wire.transport != "OPENAI_COMPATIBLE_CHAT"],
                         ids=lambda wire: wire.name)
async def test_non_tool_assistant_private_blocks_also_end_at_rollover(wire, monkeypatch):
    bodies = []
    responses = iter([
        wire.encode(Turn(text="Interim public state.", reasoning=PRIVATE)),
        wire.encode(Turn(calls=(Call("after-public-state", {"value": "one"}),),
                         reasoning=PRIVATE), 2),
        wire.encode(Turn(text="Done."), 3),
    ])

    def server(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=httpx.ByteStream(next(responses).encode()))

    real_client = httpx.AsyncClient
    monkeypatch.setattr("labbioagentos.kernel.http_stream.httpx.AsyncClient",
                        lambda **kwargs: real_client(transport=httpx.MockTransport(server), **kwargs))
    loop, _transport, effects, delivered = setup_loop([])
    loop.provider = wire.provider(wire.binding())
    loop.completion_feedback = lambda text: "Continue the current task." if text.startswith("Interim") else None
    result = await loop.run(messages=roots(), deadline_monotonic=DEADLINE)
    assert result.termination == "MODEL_COMPLETED" and effects == ["one"]
    assert len(rollover_events(loop)) == 2 and len(delivered) == 1
    assert all(PRIVATE not in json.dumps(body) for body in bodies[1:])
    assert "Interim public state." in json.dumps(bodies[-1])
