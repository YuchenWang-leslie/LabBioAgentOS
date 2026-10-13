"""Provider-turn accounting and bounded no-progress behavior."""

import json

import pytest

from .contracts import KernelContractBackend, ToolCallSpec, TurnSpec


@pytest.mark.asyncio
async def test_turns_are_classified_and_observations_are_bounded(
    kernel_backend: KernelContractBackend,
):
    secret = "PRIVATE_REASONING_SENTINEL"
    capture = await kernel_backend.run_agent(
        (
            TurnSpec(reasoning=secret),
            TurnSpec(
                tool_calls=(
                    ToolCallSpec("call-progress", arguments='{"value":"ok"}'),
                )
            ),
            TurnSpec(content="complete"),
        )
    )

    assert capture.error is None
    assert [item["progress_kind"] for item in capture.observations] == [
        "REASONING_ONLY",
        "TOOL_CALL",
        "CONTENT",
    ]
    assert [item["observable_progress"] for item in capture.observations] == [
        False,
        True,
        True,
    ]
    assert capture.observations[1]["tool_names"] == ("record_value",)
    public = json.dumps(
        {
            "observations": capture.observations,
            "steps": capture.public_steps,
            "chunks": capture.public_chunks,
            "events": capture.public_events,
        },
        default=str,
    )
    assert secret not in public
    assert "reasoning_content" not in public


@pytest.mark.asyncio
async def test_no_progress_wall_clock_fails_with_bounded_facts(
    kernel_backend: KernelContractBackend,
):
    capture = await kernel_backend.run_agent(
        (TurnSpec(reasoning="private-one"), TurnSpec(reasoning="private-two")),
        no_progress_seconds=3,
        clock_values=(0.0, 2.0, 2.0, 4.0),
    )

    assert capture.error is not None
    assert capture.error.code == "NO_OBSERVABLE_PROGRESS"
    assert capture.error.elapsed_ms == 4000
    assert capture.error.turn_count == 2
    assert len(capture.provider_histories) == 2


@pytest.mark.asyncio
async def test_observable_progress_resets_no_progress_clock(
    kernel_backend: KernelContractBackend,
):
    capture = await kernel_backend.run_agent(
        (
            TurnSpec(reasoning="private-before"),
            TurnSpec(
                tool_calls=(
                    ToolCallSpec("call-reset", arguments='{"value":"ok"}'),
                )
            ),
            TurnSpec(reasoning="private-after"),
            TurnSpec(content="complete"),
        ),
        no_progress_seconds=3,
        clock_values=(0.0, 2.0, 2.0, 4.0, 4.0, 6.0, 6.0, 8.0),
    )

    assert capture.error is None
    assert capture.result_content == "complete"
    assert len(capture.provider_histories) == 4
