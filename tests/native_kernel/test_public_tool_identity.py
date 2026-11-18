"""Public feedback exposes observation identities, not private dispatch UUIDs."""

import json
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

import pytest

from labbioagentos.kernel import (
    AgentLoop, FinishReason, ModelTurn, PublicMessage, ToolCall, ToolError, ToolResult,
)
from test_agent_loop import _enable_evidence_reader, _observed_batch, _tool_loop


CONTROL = {"authority": "CONTROL_STATE", "kernel_budget": {
    "tool_calls_remaining": 8, "evidence_entries_remaining": 8,
}}


def governed_result(*, success=True):
    return ToolResult(
        tool_invocation_id=uuid4(), call_id="public-call", name="record_value",
        status="SUCCEEDED", content={
            "success": success, "capability_invocation_id": str(uuid4()),
            "observation_archived": True, "data": {"value": "synthetic"},
        }, evidence_references=("artifact:synthetic",),
    )


@pytest.mark.parametrize("kind", ["business_success", "business_failure", "protocol_failure", "generic"])
def test_only_public_result_identity_is_projected_and_internal_result_is_unchanged(kind):
    result = governed_result(success=kind != "business_failure")
    if kind == "protocol_failure":
        result = ToolResult(
            tool_invocation_id=uuid4(), call_id="failed-call", name="record_value",
            status="FAILED", error=ToolError(code="TOOL_ERROR", message="Synthetic failure."),
        )
    elif kind == "generic":
        result = result.model_copy(update={"content": {"synthetic": True}})
    original = result.model_dump_json()
    feedback = AgentLoop._tool_feedback(result, CONTROL)
    payload = json.loads(feedback.content)
    assert "tool_invocation_id" not in payload
    assert str(result.tool_invocation_id) not in feedback.content
    assert payload["call_id"] == feedback.tool_call_id == result.call_id
    assert payload["status"] == result.status
    assert payload["control_state"] == CONTROL
    assert payload.get("content") == result.content
    assert feedback.tool_error == (kind == "protocol_failure")
    assert result.model_dump_json() == original


@pytest.mark.parametrize("tamper", [
    None, "body", "numeric_type", "boolean_type", "name", "status", "references", "extra", "payload_call", "message_call", "message_error", "legacy_uuid",
])
@pytest.mark.parametrize("legacy", [False, True])
def test_only_exact_known_projection_and_provider_correlation_credit_delivery(tamper, legacy):
    loop, _provider, _effects = _tool_loop(())
    delivered = []
    loop.dispatcher.batch_observer = lambda results: delivered.extend(results)
    result = governed_result(success=False)
    if tamper in {"numeric_type", "boolean_type"}:
        result = result.model_copy(update={"content": {
            **result.content, "data": {"value": 1},
        }})
    loop.tool_results.append(result)
    payload = json.loads(AgentLoop._tool_feedback(result, CONTROL).content)
    payload.pop("tool_invocation_id", None)
    if legacy:
        payload["tool_invocation_id"] = str(result.tool_invocation_id)
    message_call = result.call_id
    if tamper == "body":
        payload["content"]["data"]["value"] = "not the released observation"
    elif tamper in {"numeric_type", "boolean_type"}:
        payload["content"]["data"]["value"] = 1.0 if tamper == "numeric_type" else True
    elif tamper == "name":
        payload["name"] = "evidence_read"
    elif tamper == "status":
        payload.update(status="FAILED", error={"code": "UNTRUSTED", "message": "Not original."})
    elif tamper == "references":
        payload["evidence_references"] = []
    elif tamper == "extra":
        payload["invented_field"] = "not public"
    elif tamper == "payload_call":
        payload["call_id"] = "another-provider-call"
    elif tamper == "message_call":
        message_call = "another-provider-call"
    elif tamper == "legacy_uuid":
        payload["tool_invocation_id"] = str(uuid4())
    message = PublicMessage(role="TOOL", content=json.dumps(payload), tool_call_id=message_call,
                            tool_error=tamper == "message_error")
    request = SimpleNamespace(messages=(message,))
    loop._acknowledge_feedback(request)
    loop._acknowledge_feedback(request)
    assert delivered == ([result] if tamper is None else [])
    assert loop._delivered_tool_invocations == ({result.tool_invocation_id} if tamper is None else set())


def test_legacy_feedback_with_exact_dispatch_identity_retains_delivery_credit():
    loop, _provider, _effects = _tool_loop(())
    delivered = []
    loop.dispatcher.batch_observer = lambda results: delivered.extend(results)
    result = governed_result()
    loop.tool_results.append(result)
    payload = result.model_dump(mode="json", exclude_none=True)
    payload["control_state"] = CONTROL
    loop._acknowledge_feedback(SimpleNamespace(messages=(PublicMessage(
        role="TOOL", content=json.dumps(payload), tool_call_id=result.call_id,
    ),)))
    assert delivered == [result]


@pytest.mark.parametrize("case", ["generic", "generic_prior_delivered", "governed", "legacy"])
def test_reused_provider_call_id_requires_unique_exact_result_match(case):
    loop, _provider, _effects = _tool_loop(())
    delivered = []
    loop.dispatcher.batch_observer = lambda results: delivered.extend(results)
    first = governed_result()
    second = first.model_copy(update={"tool_invocation_id": uuid4()})
    if case == "governed":
        second = second.model_copy(update={"content": {
            **first.content, "capability_invocation_id": str(uuid4()),
        }})
    else:
        first = first.model_copy(update={"content": {"synthetic": True}})
        second = second.model_copy(update={"content": first.content})
    loop.tool_results.extend((first, second))
    if case == "generic_prior_delivered":
        loop._delivered_tool_invocations.add(first.tool_invocation_id)
    payload = json.loads(AgentLoop._tool_feedback(second, CONTROL).content)
    if case == "legacy":
        payload["tool_invocation_id"] = str(second.tool_invocation_id)
    loop._acknowledge_feedback(SimpleNamespace(messages=(PublicMessage(
        role="TOOL", content=json.dumps(payload), tool_call_id=second.call_id,
    ),)))
    assert delivered == ([second] if case in {"governed", "legacy"} else [])


@pytest.mark.parametrize("field", [
    "name", "call_id", "evidence_references", "null_error", "null_dispatch", "uppercase_dispatch",
])
@pytest.mark.parametrize("legacy", [False, True])
def test_normalized_identifiers_and_null_extras_are_not_exact_released_feedback(field, legacy):
    loop, _provider, _effects = _tool_loop(())
    delivered = []
    loop.dispatcher.batch_observer = lambda results: delivered.extend(results)
    result = governed_result()
    loop.tool_results.append(result)
    payload = json.loads(AgentLoop._tool_feedback(result, CONTROL).content)
    if legacy:
        payload["tool_invocation_id"] = str(result.tool_invocation_id)
    if field == "evidence_references":
        payload[field] = [" " + payload[field][0] + " "]
    elif field == "null_error":
        payload["error"] = None
    elif field == "null_dispatch":
        payload["tool_invocation_id"] = None
    elif field == "uppercase_dispatch":
        payload["tool_invocation_id"] = str(result.tool_invocation_id).upper()
    else:
        payload[field] = " " + payload[field] + " "
    loop._acknowledge_feedback(SimpleNamespace(messages=(PublicMessage(
        role="TOOL", content=json.dumps(payload), tool_call_id=result.call_id,
    ),)))
    assert delivered == []
    assert not loop._delivered_tool_invocations


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_public_and_legacy_history_compact_to_exact_observation_id_not_dispatch_id(legacy):
    observation_id, old = _observed_batch(1, failed=True)
    _latest_id, latest = _observed_batch(2)
    private_id = str(uuid4())
    payload = json.loads(old[1].content)
    payload.pop("tool_invocation_id", None)
    if legacy:
        payload["tool_invocation_id"] = private_id
    old = old[0], old[1].model_copy(update={"content": json.dumps(payload)})
    loop, provider, _effects = _tool_loop((ModelTurn(content="Done.", finish_reason=FinishReason.STOP),))
    _enable_evidence_reader(loop)
    loop.max_request_bytes = 110_000
    await loop.run(messages=(PublicMessage(role="USER", content="Review existing observations."), *old, *latest),
                   deadline_monotonic=monotonic() + 60)
    sent = provider.requests[0].messages
    marker = json.loads(sent[1].content)
    assert marker["observations"][0]["observation_id"] == observation_id
    assert marker["observations"][0]["capability_request_succeeded"] is False
    assert private_id not in sent[1].content
    assert sent[-2:] == latest


@pytest.mark.asyncio
async def test_real_dispatch_keeps_private_identity_in_result_and_event_only():
    loop, provider, _effects = _tool_loop((
        ModelTurn(tool_calls=(ToolCall(call_id="actual", name="record_value",
            arguments_json='{"value":"synthetic"}'),), finish_reason=FinishReason.TOOL_CALLS),
        ModelTurn(content="Done.", finish_reason=FinishReason.STOP),
    ))
    delivered = []
    loop.dispatcher.batch_observer = lambda results: delivered.extend(results)
    result = await loop.run(messages=(PublicMessage(role="USER", content="Synthetic identity check."),),
                            deadline_monotonic=monotonic() + 60)
    internal = result.tool_results[0]
    assert delivered == [internal]
    assert str(internal.tool_invocation_id) not in provider.requests[1].messages[-1].content
    finished = [event for event in result.events if event.kind.value == "TOOL_CALL_FINISHED"]
    assert len(finished) == 1 and finished[0].tool_invocation_id == internal.tool_invocation_id
