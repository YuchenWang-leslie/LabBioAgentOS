"""Criterion 13: tool round trips, parallel calls and reasoning continuity."""

from __future__ import annotations

import json

from labbioagentos.kernel import FinishReason

from .harness import DEADLINE, PROMPT, capability_loop, event_dumps, observation_dumps
from .wire import KEY, Call, Turn

FIRST = "PRIVATE_CONTINUATION_FIRST"
SECOND = "PRIVATE_CONTINUATION_SECOND"


def _assert_append_only(wire, earlier, later):
    assert wire.fixed(later) == wire.fixed(earlier)
    assert wire.history(later)[:len(wire.history(earlier))] == wire.history(earlier)


def _two_rounds(wire, server):
    server.script(
        wire.encode(Turn(calls=(Call("call-a", {"value": "a"}),), reasoning=FIRST), 1),
        wire.encode(Turn(calls=(Call("call-b", {"value": "b"}),), reasoning=SECOND), 2),
        wire.encode(Turn(text="done"), 3),
    )
    return capability_loop(wire.provider(wire.binding()))


async def test_parallel_calls_round_trip_with_an_append_only_history(wire, server):
    server.script(
        wire.encode(Turn(text="Recording both.", reasoning=FIRST, calls=(
            Call("call-a", {"value": "a"}), Call("call-b", {"value": "b"}),
        )), 1),
        wire.encode(Turn(text="done"), 2),
    )
    loop, effects = capability_loop(wire.provider(wire.binding()))

    result = await loop.run(messages=PROMPT, deadline_monotonic=DEADLINE)

    assert result.output == "done"
    assert effects == ["a", "b"]
    first, second = server.bodies
    _assert_append_only(wire, first, second)
    assert wire.result_call_ids(first) == []
    assert wire.result_call_ids(second) == ["call-a", "call-b"]
    assert [(item.finish_reason, item.tool_call_count) for item in loop.observations] == [
        (FinishReason.TOOL_CALLS, 2), (FinishReason.STOP, 0),
    ]


async def test_private_continuation_returns_verbatim_across_tool_rounds(wire, server):
    loop, effects = _two_rounds(wire, server)

    await loop.run(messages=PROMPT, deadline_monotonic=DEADLINE)

    assert effects == ["a", "b"]
    first, second, third = server.bodies
    _assert_append_only(wire, first, second)
    _assert_append_only(wire, second, third)
    sent = [json.dumps(body) for body in (first, second, third)]
    if wire.carries_reasoning:
        assert FIRST not in sent[0] and SECOND not in sent[0]
        assert FIRST in sent[1] and SECOND not in sent[1]
        assert FIRST in sent[2] and SECOND in sent[2]
    else:
        # The format returns nothing private, so nothing private is sent back.
        assert not any(value in text for value in (FIRST, SECOND) for text in sent)
        assert not any("reasoning_content" in text for text in sent)


async def test_private_continuation_stays_out_of_public_records(wire, server):
    loop, _effects = _two_rounds(wire, server)

    result = await loop.run(messages=PROMPT, deadline_monotonic=DEADLINE)

    public = (event_dumps(loop) + observation_dumps(loop) + result.model_dump_json()
              + "".join(message.model_dump_json() for message in loop.messages))
    for private in (FIRST, SECOND, KEY):
        assert private not in public
