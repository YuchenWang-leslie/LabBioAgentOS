"""Only parsed argument objects become canonical replay history."""

import json

import pytest

from .contracts import KernelContractBackend, ToolCallSpec, TurnSpec


def _replayed_arguments(capture, call_id: str):
    history = capture.provider_histories[1]
    assistant = next(
        item
        for item in reversed(history)
        if item.get("role") == "assistant" and item.get("tool_calls")
    )
    call = next(item for item in assistant["tool_calls"] if item["id"] == call_id)
    return call["function"]["arguments"]


@pytest.mark.asyncio
async def test_successfully_parsed_object_is_canonical_in_followup_history(
    kernel_backend: KernelContractBackend,
):
    nested_text = '{"inner":1}'
    raw = ' { "value" : "alpha", "nested" : ' + json.dumps(nested_text) + " } "
    capture = await kernel_backend.canonical_replay(
        (
            TurnSpec(tool_calls=(ToolCallSpec("canonical-call", arguments=raw),)),
            TurnSpec(content="complete"),
        )
    )

    replayed = _replayed_arguments(capture, "canonical-call")
    assert json.loads(replayed) == {"value": "alpha", "nested": nested_text}
    assert json.dumps(json.loads(replayed), separators=(",", ":")) == replayed
    assert capture.effects == ({"value": "alpha", "nested": nested_text},)


@pytest.mark.asyncio
async def test_rejected_arguments_remain_non_executable_and_are_not_replaced(
    kernel_backend: KernelContractBackend,
):
    raw = "not a JSON object"
    capture = await kernel_backend.run_agent(
        (
            TurnSpec(tool_calls=(ToolCallSpec("rejected-call", arguments=raw),)),
            TurnSpec(content="complete"),
        )
    )

    assert _replayed_arguments(capture, "rejected-call") == raw
    assert capture.effects == ()
    assert capture.pre_hook_count == capture.post_hook_count == 0
