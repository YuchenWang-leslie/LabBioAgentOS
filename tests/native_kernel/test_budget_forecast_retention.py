"""A future-cost forecast must not evict source pages that fit this request."""

import json

import pytest

from labbioagentos.kernel import BudgetDelta, FinishReason, ModelTurn, PublicMessage, TokenUsage
from test_agent_loop import _enable_evidence_reader, _observed_batch, _tool_loop


SOURCE = "# Generic source, not task data.\n" * 1_200


def _page_batch(number, *, start, end):
    observation_id, (assistant, feedback) = _observed_batch(number)
    payload = json.loads(feedback.content)
    payload["content"]["data"] = {
        "source_text": SOURCE[start:end], "start": start, "end": end,
        "total_chars": len(SOURCE), "eof": end == len(SOURCE),
    }
    return observation_id, (
        assistant.model_copy(update={"content": "Read a generic source page."}),
        feedback.model_copy(update={"content": json.dumps(payload)}),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cap,expected_compacted", [(1_048_576, 0), (20_000, 1)])
async def test_future_turn_forecast_retains_fitting_pages_and_compacts_only_to_capacity(
    cap, expected_compacted,
):
    first_id, first = _page_batch(1, start=0, end=32_000)
    _second_id, second = _page_batch(2, start=32_000, end=len(SOURCE))
    _latest_id, latest = _page_batch(3, start=0, end=1)
    roots = (
        PublicMessage(role="SYSTEM", content="Keep exact source facts; no scientific route."),
        PublicMessage(role="USER", content="Revise only the requested generic source detail."),
    )
    loop, provider, _effects = _tool_loop((
        ModelTurn(content="Source facts retained.", finish_reason=FinishReason.STOP,
                  usage=TokenUsage(input_tokens=32_377, output_tokens=1, total_tokens=32_378)),
    ), provider_turns=64, tokens=2_031_616)
    _enable_evidence_reader(loop)
    loop.max_request_bytes = cap
    loop.observations.append(loop._observe(ModelTurn(
        content="Previous response.", finish_reason=FinishReason.STOP,
        usage=TokenUsage(input_tokens=32_377, output_tokens=2_288, total_tokens=34_665),
    ), 3, 0))
    loop.budget_ledger.reserve(BudgetDelta(provider_turns=3))
    loop.budget_ledger.record_observed_usage(tokens=83_689, wall_time_ms=0)
    original_limits = loop.budget_ledger.limits
    discarded = []
    provider.discard_tool_batch = lambda *_args, **kwargs: discarded.append(kwargs)

    result = await loop.run(
        messages=(*roots, *first, *second, *latest),
        deadline_monotonic=10_000_000_000.0,
    )

    request, = provider.requests
    event = next(item for item in loop.events if item.kind == "MODEL_REQUEST_PREPARED")
    assert event.numeric_facts["context_budget_risk"] == 1
    assert event.numeric_facts["last_observed_input_tokens"] == 32_377
    assert event.numeric_facts["provider_turns_remaining"] == 61
    assert event.numeric_facts["compacted_batches"] == expected_compacted
    assert event.numeric_facts["request_bytes"] <= cap
    assert len(request.model_dump_json().encode()) <= cap
    assert request.messages[:2] == roots
    assert request.messages[-4:] == (*second, *latest)
    assert not any(item.kind == "CONTEXT_ROLLED_OVER" for item in loop.events)
    assert len(discarded) == expected_compacted
    if expected_compacted:
        marker = json.loads(request.messages[2].content)
        assert marker["context_replacement"] == "COMPLETED_TOOL_BATCH"
        assert marker["observations"][0]["observation_id"] == first_id
        assert marker["reread_tool"] == "evidence_read"
        assert SOURCE[:32_000] not in request.messages[2].content
    else:
        assert request.messages == (*roots, *first, *second, *latest)
        retained_source = "".join(json.loads(batch[1].content)["content"]["data"]["source_text"]
                                  for batch in (first, second))
        assert retained_source == SOURCE
    assert loop.budget_ledger.limits == original_limits
    assert result.budget_usage.provider_turns == 4
    assert result.budget_usage.tokens == 83_689 + 32_378
    assert result.termination == "MODEL_COMPLETED"
