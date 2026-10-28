"""Criterion 13: finish reasons and usage, where unknown values stay unknown."""

from __future__ import annotations

import pytest

from labbioagentos.kernel import KernelContractError, KernelEventKind

from .harness import DEADLINE, PROMPT, capability_loop
from .wire import KNOWN_USAGE, Turn


@pytest.mark.parametrize("finish", ["STOP", "LENGTH", "CONTENT_FILTER", "OTHER"])
async def test_finish_reasons_reach_the_loop_record(wire, server, finish):
    server.script(wire.encode(Turn(text="PRIVATE_PARTIAL_OUTPUT", finish=finish)))
    loop, _effects = capability_loop(wire.provider(wire.binding()))

    try:
        await loop.run(messages=PROMPT, deadline_monotonic=DEADLINE)
    except KernelContractError:
        # What the loop does next with each finish is its own contract.
        pass

    observed = [(event.status_code, event.reason_code) for event in loop.events
                if event.kind is KernelEventKind.MODEL_TURN_OBSERVED]
    category = wire.filter_category if finish == "CONTENT_FILTER" else None
    assert observed == [(finish, category)]
    (observation,) = loop.observations
    assert observation.finish_reason.value == finish
    # A filtered turn keeps no output in any format (DECIDED-C14-9).
    assert observation.content_present is (finish != "CONTENT_FILTER")


@pytest.mark.parametrize("known", [True, False], ids=["known", "unknown"])
async def test_usage_is_normalized_and_unknown_stays_unknown(wire, server, known):
    server.script(wire.encode(Turn(text="done", usage=known)))
    loop, _effects = capability_loop(wire.provider(wire.binding()))

    await loop.run(messages=PROMPT, deadline_monotonic=DEADLINE)

    usage = loop.observations[0].usage
    expected = KNOWN_USAGE if known else (None, None, None)
    assert (usage.input_tokens, usage.output_tokens, usage.total_tokens) == expected
