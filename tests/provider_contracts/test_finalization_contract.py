"""Criterion 13: structured finalization in schema and JSON-object modes."""

from __future__ import annotations

import pytest

from labbioagentos.kernel import FinishReason, model_json_schema

from .harness import DEADLINE, PROMPT, Envelope, event_dumps, finalization_loop
from .wire import Turn

PRIVATE = "PRIVATE_CONTINUATION_FINAL"


@pytest.mark.parametrize("json_object", [False, True], ids=["schema", "json-object"])
async def test_structured_finalization_in_both_modes(wire, server, json_object):
    server.script(wire.encode(Turn(text='{"result":{"count":3}}', reasoning=PRIVATE)))
    loop = finalization_loop(wire.provider(wire.binding(json_object=json_object)))

    result = await loop.run(messages=PROMPT, deadline_monotonic=DEADLINE,
                            response_model=Envelope)

    assert result.output == {"result": {"count": 3}}
    (body,) = server.bodies
    expected = "instruction" if json_object else "native"
    assert wire.schema_location(body, model_json_schema(Envelope)) == expected
    assert PRIVATE not in event_dumps(loop) + result.model_dump_json()


async def test_truncated_finalization_is_retried_with_the_partial_turn(wire, server):
    server.script(
        wire.encode(Turn(text='{"result":', finish="LENGTH"), 1),
        wire.encode(Turn(text='{"result":{"count":3}}'), 2),
    )
    loop = finalization_loop(wire.provider(wire.binding()))

    result = await loop.run(messages=PROMPT, deadline_monotonic=DEADLINE,
                            response_model=Envelope)

    assert result.output == {"result": {"count": 3}}
    first, second = server.bodies
    assert wire.fixed(second) == wire.fixed(first)
    assert wire.history(second)[:len(wire.history(first))] == wire.history(first)
    assert [item.finish_reason for item in loop.observations] == [
        FinishReason.LENGTH, FinishReason.STOP,
    ]
