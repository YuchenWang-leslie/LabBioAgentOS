"""Native capacity rejects whole responses before effects and permits one repair."""

from dataclasses import replace
import ast
import json
import os
from pathlib import Path
import socket
from uuid import uuid4

import jsonschema
import pytest

import labbioagentos
from labbioagentos.kernel import (
    AgentInvocation, AgentLoop, AgentMode, BudgetLedger, BudgetLimits,
    BudgetExceededError, IncompleteProviderResponseError, KernelIdentity, ModelResultProjector, ProviderBinding,
    ProviderProtocolError, PublicMessage, ToolDispatcher,
)
from labbioagentos.kernel import contracts
from labbioagentos.kernel.http_stream import HttpStreamResult
from labbioagentos.kernel.openai_chat import OpenAICompatibleChatProvider
from labbioagentos.runtime.native_tools import NativeRuntimeCapabilityHost, build_labbio_tool_registry
from labbioagentos.runtime.observations import ObservationStore
from labbioagentos import WorkflowStage
from test_runtime_milestone_c2 import _stage_input, _toolset


CANARY = "PRIVATE_CAPACITY_CANARY"
DEADLINE = 10_000_000_000.0


@pytest.fixture(autouse=True)
def no_network_and_explicit_package(monkeypatch):
    expected = os.environ.get("LABBIO_TEST_EXPECTED_PACKAGE_ROOT")
    if expected:
        assert Path(labbioagentos.__file__).resolve().parent == Path(expected).resolve()
    def forbidden(*_args, **_kwargs):
        pytest.fail("Capacity repair fixtures must not contact a network/provider")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


def _call(name="working_note_write", *, text="admitted", identity="fixture-call"):
    return {"id": identity, "type": "function", "function": {"name": name,
        "arguments": json.dumps({"text": text, "source_observation_ids": []})}}


def _response(calls=(), *, usage=2):
    return {"choices": [{"message": {"role": "assistant", **({"tool_calls": calls} if calls else {"content": "Done."})},
        "finish_reason": "tool_calls" if calls else "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": usage - 1, "total_tokens": usage}}


def _oversized(field="ARGUMENTS_JSON"):
    call = _call()
    if field == "ARGUMENTS_JSON":
        # A legal harmless literal program; JSON newline escaping increases the
        # entire argument envelope above64k while its source remains60k.
        call["function"] = {"name": "execution_submit", "arguments": json.dumps({
            "image_key": "fixture-image", "script_content": "#" + CANARY + "\n" + "pass\n" * 12_000})}
    elif field == "CALL_ID":
        call["id"] = CANARY + "x" * 128
    else:
        call["function"]["name"] = CANARY + "x" * 128
    return call


class Transport:
    def __init__(self, responses, inspect=None):
        self.responses = iter(responses)
        self.requests = []
        self.inspect = inspect

    async def post_json(self, *, payload, **_kwargs):
        self.requests.append(payload)
        if self.inspect:
            self.inspect(payload, len(self.requests))
        return HttpStreamResult(body=next(self.responses))


def _loop(tmp_path, responses, *, turns=8, tokens=100_000, inspect=None):
    stage = _stage_input(WorkflowStage.EXECUTE)
    base = _toolset(tmp_path, stage)
    binding = replace(base.binding, capability_allowlist=("execution_submit", "working_note_write"))
    services = replace(base.services, observation_store=ObservationStore(tmp_path / "observations"))
    host = NativeRuntimeCapabilityHost(binding, services)
    registry = build_labbio_tool_registry(host, on_result_rejected=host.on_result_rejected)
    budget = BudgetLedger(BudgetLimits(provider_turns=turns, tokens=tokens, wall_time_ms=60_000,
        tool_calls=8, evidence_entries=8, evidence_bytes=1_000_000,
        delegations=0, delegation_depth=0, finalization_attempts=0))
    transport = Transport(responses, inspect)
    provider = OpenAICompatibleChatProvider(binding=ProviderBinding(provider="fixture", model="offline",
        endpoint="https://provider.invalid/v1", transport="OPENAI_COMPATIBLE_CHAT", thinking_mode="DISABLED"),
        api_key="OFFLINE_FIXTURE_KEY", transport=transport)
    identity = KernelIdentity(thread_id=uuid4(), workflow_run_id=stage.run_id, kernel_run_id=uuid4(),
        invocation_id=stage.invocation_id, chain=(stage.invocation_id,))
    loop = AgentLoop(provider=provider,
        invocation=AgentInvocation(identity=identity, agent_id="fixture", mode=AgentMode.CAPABILITY,
            tool_names=binding.capability_allowlist),
        budget_ledger=budget,
        dispatcher=ToolDispatcher(registry, ModelResultProjector(max_bytes=60_000), budget_ledger=budget,
            batch_observer=host.mark_feedback_delivered))
    return loop, transport, host


@pytest.mark.parametrize("shape", ["escaped_literal", "combined_edits"])
def test_backend_valid_native_schema_does_not_override_total_channel_capacity(shape):
    registry = build_labbio_tool_registry(object())
    spec = next(item for item in registry.specs if item.name == "execution_submit")
    payload = {"image_key": "fixture-image"}
    if shape == "escaped_literal":
        payload["script_content"] = "pass\n" * 12_000
        ast.parse(payload["script_content"])
        assert len(payload["script_content"]) < 64_000
    else:
        payload.update(source_revision={"source_artifact_id": str(uuid4()), "expected_script_hash": "a" * 64,
            "edits": [{"old": "x" * 32_000, "new": "y" * 32_000}]},
            resources={"cpu_cores": 1, "memory_gb": 1.0, "timeout_seconds": 60},
            input_artifact_ids=[], parameters={}, requested_outputs=[])
        # Use the canonical resource model rather than guessed field aliases.
        from labbioagentos import RequestedResources
        payload["resources"] = RequestedResources().model_dump(mode="json")
    jsonschema.validate(payload, spec.parameters)
    encoded = json.dumps(payload)
    assert len(encoded) > 64_000
    assert getattr(contracts, "MAX_TOOL_ARGUMENT_CHARS", None) == 64_000
    assert "64000" in spec.description and "serialized" in spec.description.lower()
    assert "service" in spec.description.lower() or "backend" in spec.description.lower()
    assert all("64000" in item.description for item in registry.specs)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["ARGUMENTS_JSON", "CALL_ID", "TOOL_NAME"])
async def test_capacity_error_preserves_only_safe_field_count_and_limit(tmp_path, field):
    loop, _transport, _host = _loop(tmp_path, [_response([_oversized(field)])])
    from labbioagentos.kernel import ModelRequest
    request = ModelRequest(binding=loop.provider.binding,
        messages=(PublicMessage(role="USER", content="Generic native contract."),),
        tools=loop._tool_specs(), max_output_tokens=8192, deadline_monotonic=DEADLINE,
        kernel_run_id=loop.invocation.identity.kernel_run_id, invocation_id=loop.invocation.identity.invocation_id,
        turn_index=1)
    with pytest.raises(ProviderProtocolError) as raised:
        await loop.provider.generate(request)
    issue = getattr(raised.value, "tool_call_capacity", None)
    assert issue is not None, "Size rejection must retain its safe subfield instead of flattening"
    data = issue.model_dump(mode="json")
    assert data["field"] == field and data["call_index"] == 0
    assert data["limit_chars"] == (64_000 if field == "ARGUMENTS_JSON" else 128)
    assert data["actual_chars"] > data["limit_chars"]
    assert CANARY not in json.dumps(data) and CANARY not in str(raised.value)
    assert raised.value.usage.total_tokens == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("mixed", [False, True])
async def test_whole_native_response_rejected_then_agent_chooses_one_legal_persisted_call(tmp_path, mixed):
    bad = ([_call(text="must-not-execute", identity="unadmitted-valid")] if mixed else []) + [_oversized()]
    seen = []
    def inspect(payload, count):
        if count == 2:
            assert not list((tmp_path / "observations").glob("*.json"))
            assert host.evidence_items() == () and host._host._delivered_observations == {}
            feedback = json.loads(payload["messages"][-1]["content"])
            assert feedback["authority"] == "CONTROL_STATE"
            assert feedback["reason"] == "TOOL_CALL_CAPACITY_REJECTED"
            assert feedback["batch_dispatched"] is False
            assert feedback["tool_call_capacity"]["limit_chars"] == 64_000
            assert CANARY not in json.dumps(payload)
            seen.append(feedback)
    loop, transport, host = _loop(tmp_path, [_response(bad), _response([_call(text="one-admitted-note")]),
        _response()], inspect=inspect)
    limits = loop.budget_ledger.limits
    result = await loop.run(messages=(PublicMessage(role="USER", content="Generic capacity fixture."),),
        deadline_monotonic=DEADLINE)
    assert len(seen) == 1 and len(transport.requests) == 3
    assert result.termination == "MODEL_COMPLETED"
    assert len(result.tool_results) == 1 and result.tool_results[0].content["success"]
    assert result.tool_results[0].content["data"]["text"] == "one-admitted-note"
    assert len(list((tmp_path / "observations").glob("*.json"))) == 1
    assert loop.budget_ledger.limits == limits
    assert result.budget_usage.provider_turns == 3 and result.budget_usage.tokens == 6
    assert CANARY not in result.model_dump_json()


@pytest.mark.asyncio
async def test_only_one_capacity_correction_and_no_dispatch_on_exhaustion(tmp_path):
    loop, transport, host = _loop(tmp_path, [_response([_oversized()]), _response([_oversized()])])
    with pytest.raises(ProviderProtocolError):
        await loop.run(messages=(PublicMessage(role="USER", content="Bounded correction."),),
            deadline_monotonic=DEADLINE)
    assert len(transport.requests) == 2 and host.evidence_items() == ()
    assert loop.budget_ledger.usage.provider_turns == 2 and loop.budget_ledger.usage.tokens == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("turns,tokens", [(1, 100_000), (8, 2), (8, 1)])
async def test_capacity_repair_cannot_add_budget(tmp_path, turns, tokens):
    loop, transport, host = _loop(tmp_path, [_response([_oversized()])], turns=turns, tokens=tokens)
    limits = loop.budget_ledger.limits
    with pytest.raises((ProviderProtocolError, BudgetExceededError)):
        await loop.run(messages=(PublicMessage(role="USER", content="Do not add budget."),),
            deadline_monotonic=DEADLINE)
    assert len(transport.requests) == 1 and host.evidence_items() == ()
    assert loop.budget_ledger.limits == limits
    assert loop.budget_ledger.usage.tokens == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    {"choices": []},
    _response([{"id": "fixture", "type": "unsupported", "function": {"name": "working_note_write", "arguments": "{}"}}]),
    _response([{"id": "fixture", "type": "function", "function": {"name": "working_note_write", "arguments": {"not": "a string"}}}]),
    _response([_call(), _oversized()]),  # A capacity issue cannot mask duplicate IDs.
    _response([_oversized(), {"id": "other", "type": "unsupported", "function": {"name": "working_note_write", "arguments": "{}"}}]),
])
async def test_noncapacity_protocol_errors_remain_failclosed(tmp_path, bad):
    loop, transport, host = _loop(tmp_path, [bad])
    with pytest.raises(ProviderProtocolError):
        await loop.run(messages=(PublicMessage(role="USER", content="No generic retry."),),
            deadline_monotonic=DEADLINE)
    assert len(transport.requests) == 1 and host.evidence_items() == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("filtered_by", ["refusal", "finish_reason"])
@pytest.mark.parametrize("oversized", [False, True])
async def test_refusal_and_content_filter_take_priority_over_capacity_repair(
    tmp_path, filtered_by, oversized,
):
    calls = [_call(text="must-not-execute", identity="unadmitted-valid")]
    if oversized:
        calls.append(_oversized())
    response = _response(calls)
    if filtered_by == "refusal":
        response["choices"][0]["message"]["refusal"] = "private-refusal-canary"
    else:
        response["choices"][0]["finish_reason"] = "content_filter"
    loop, transport, host = _loop(tmp_path, [response, _response([_call()]), _response()])
    with pytest.raises(IncompleteProviderResponseError) as raised:
        await loop.run(messages=(PublicMessage(role="USER", content="Keep refusal semantics."),),
            deadline_monotonic=DEADLINE)
    assert raised.value.finish_reason == "CONTENT_FILTER"
    assert len(transport.requests) == 1
    assert host.evidence_items() == () and host._host._delivered_observations == {}
    assert not list((tmp_path / "observations").glob("*.json"))
    assert loop.budget_ledger.usage.provider_turns == 1 and loop.budget_ledger.usage.tokens == 2
    assert not loop._tool_capacity_correction_used
    assert "private-refusal-canary" not in str(raised.value)
    assert not any("TOOL_CALL_CAPACITY_REJECTED" in (message.content or "") for message in loop.messages)


@pytest.mark.asyncio
async def test_unreported_usage_does_not_fabricate_capacity_correction_budget(tmp_path):
    response = _response([_oversized()])
    response["usage"] = None
    loop, transport, host = _loop(tmp_path, [response])
    with pytest.raises(ProviderProtocolError):
        await loop.run(messages=(PublicMessage(role="USER", content="Unknown remains unknown."),),
            deadline_monotonic=DEADLINE)
    assert len(transport.requests) == 1 and host.evidence_items() == ()
    assert loop.budget_ledger.usage.tokens is None


@pytest.mark.asyncio
async def test_capacity_correction_cannot_add_wall_time(tmp_path):
    now = [0.0]
    loop, transport, host = _loop(tmp_path, [_response([_oversized()])],
        inspect=lambda _payload, _count: now.__setitem__(0, 61.0))
    loop.clock = lambda: now[0]
    with pytest.raises(ProviderProtocolError):
        await loop.run(messages=(PublicMessage(role="USER", content="Existing time limit."),),
            deadline_monotonic=DEADLINE)
    assert len(transport.requests) == 1 and host.evidence_items() == ()
    assert loop.budget_ledger.limits.wall_time_ms == 60_000
    assert loop.budget_ledger.usage.wall_time_ms == 61_000


@pytest.mark.asyncio
async def test_capacity_repair_does_not_replay_prior_effects(tmp_path):
    loop, transport, _host = _loop(tmp_path, [_response([_call(text="earlier", identity="old")]),
        _response([_call(text="must-not-execute", identity="unadmitted-valid"), _oversized()]),
        _response([_call(text="later", identity="new")]), _response()])
    result = await loop.run(messages=(PublicMessage(role="USER", content="Preserve existing effects."),),
        deadline_monotonic=DEADLINE)
    assert len(transport.requests) == 4
    assert [item.content["data"]["text"] for item in result.tool_results] == ["earlier", "later"]
    assert len(list((tmp_path / "observations").glob("*.json"))) == 2
