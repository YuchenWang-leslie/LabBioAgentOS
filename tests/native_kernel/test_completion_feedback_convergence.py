"""Rejected completion is not progress or a valid completion at the turn limit."""

from time import monotonic

import pytest

from labbioagentos.kernel import CompletionRejection, FinishReason, ModelTurn, PublicMessage, TokenUsage, ToolCall
from test_agent_loop import _tool_loop


ACK = "Acknowledged CONTROL_STATE item (authority: CONTROL_STATE)."


def stop(text=ACK):
    return ModelTurn(content=text, finish_reason=FinishReason.STOP,
        usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2))


@pytest.mark.asyncio
async def test_identical_rejected_completion_without_tool_progress_stops_truthfully():
    loop, provider, effects = _tool_loop((stop(),) * 8)
    loop.completion_feedback = lambda _text: CompletionRejection(feedback="The same unmet host condition.")
    result = await loop.run(messages=(PublicMessage(role="USER", content="Current task."),),
                            deadline_monotonic=monotonic() + 30)
    assert result.termination == "COMPLETION_NO_PROGRESS"
    assert result.output == ACK  # Preserve the actual public intent, not a fabricated result.
    assert len(provider.requests) == 3 and not effects
    assert result.budget_usage.tokens == 6


@pytest.mark.asyncio
async def test_last_turn_completion_is_still_validated_and_preserves_limit_reason():
    loop, provider, _ = _tool_loop((stop(),), provider_turns=1)
    checked = []

    def reject(text):
        checked.append(text)
        return CompletionRejection(feedback="Required work has not occurred.")

    loop.completion_feedback = reject
    result = await loop.run(messages=(PublicMessage(role="USER", content="Current task."),),
                            deadline_monotonic=monotonic() + 30)
    assert checked == [ACK]
    assert result.termination == "PROVIDER_TURN_LIMIT" and result.output == ACK
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_different_rejected_assessments_use_the_actual_available_turns():
    loop, provider, _ = _tool_loop(tuple(stop(f"Different assessment {i}.") for i in range(4)), provider_turns=4)
    loop.completion_feedback = lambda _text: CompletionRejection(feedback="Same unmet condition.")
    result = await loop.run(messages=(PublicMessage(role="USER", content="Current task."),),
                            deadline_monotonic=monotonic() + 30)
    assert result.termination == "PROVIDER_TURN_LIMIT" and len(provider.requests) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("new_tool_result", [False, True])
async def test_closeout_changed_feedback_or_new_tool_result_resets_rejections(new_tool_result):
    from labbioagentos.kernel import FakeProvider
    from native_kernel.test_agent_loop import _binding
    from native_kernel.test_completion_capacity import setup, result as tool_turn

    turns = [tool_turn("work"), stop("First."), stop("Second.")]
    if new_tool_result:
        turns.append(tool_turn("status"))
    turns.extend((stop("Third."), stop("Fourth."), stop("Done.")))
    provider = FakeProvider(binding=_binding(), turns=tuple(turns))
    loop, _, effects = setup(provider, limits={"tool_calls": 3, "evidence_entries": 8, "provider_turns": 12})

    def feedback(text):
        if text == "Done.":
            return None
        condition = "First condition." if new_tool_result or text in ("First.", "Second.") else "Second condition."
        return CompletionRejection(feedback=condition)

    loop.completion_feedback = feedback
    outcome = await loop.run(messages=(PublicMessage(role="USER", content="Finish existing work."),),
                             deadline_monotonic=monotonic()+60)
    assert outcome.termination == "MODEL_COMPLETED" and len(provider.requests) == len(turns)
    assert effects == (["work", "status"] if new_tool_result else ["work"])
    assert {s.name for s in provider.requests[1].tools} == {"status", "finish"}


@pytest.mark.asyncio
async def test_changed_host_feedback_is_not_identical_no_progress():
    loop, provider, _ = _tool_loop((stop(),) * 4, provider_turns=4)
    feedback = iter(("First unmet condition.", "Second condition.", "Third condition.", None))
    def changed_feedback(_text):
        value = next(feedback)
        return CompletionRejection(feedback=value) if value is not None else None

    loop.completion_feedback = changed_feedback
    result = await loop.run(messages=(PublicMessage(role="USER", content="Current task."),),
                            deadline_monotonic=monotonic() + 30)
    assert result.termination == "MODEL_COMPLETED" and len(provider.requests) == 4


@pytest.mark.asyncio
async def test_actual_tool_work_resets_duplicate_completion_count():
    tool = ModelTurn(tool_calls=(ToolCall(call_id="actual-work", name="record_value",
        arguments_json='{"value":"synthetic evidence"}'),), finish_reason=FinishReason.TOOL_CALLS,
        usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2))
    loop, provider, effects = _tool_loop((stop(), stop(), tool, stop(), stop(), stop("Done.")))
    loop.completion_feedback = lambda text: None if text == "Done." else CompletionRejection(feedback="Same unmet condition.")
    result = await loop.run(messages=(PublicMessage(role="USER", content="Current task."),),
                            deadline_monotonic=monotonic() + 30)
    assert result.termination == "MODEL_COMPLETED" and len(provider.requests) == 6
    assert len(effects) == 1


@pytest.mark.asyncio
async def test_rejected_completion_is_not_committed_as_an_accepted_assistant_turn():
    loop, provider, _ = _tool_loop((stop(), stop("Corrected public handoff.")))
    loop.completion_feedback = lambda text: CompletionRejection(feedback="Host completion conditions are unmet.") if text == ACK else None
    result = await loop.run(messages=(PublicMessage(role="USER", content="The unchanged current user task."),),
                            deadline_monotonic=monotonic() + 30)
    correction = provider.requests[1].messages
    assert correction[-1].role == "USER"  # Preserve the existing trusted feedback transport.
    assert not any(message.role == "ASSISTANT" and message.content == ACK for message in correction)
    assert correction[0].content == "The unchanged current user task."
    assert result.termination == "MODEL_COMPLETED"
