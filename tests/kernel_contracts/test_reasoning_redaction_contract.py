"""Private reasoning redaction never creates an invalid empty history turn."""

import json

import pytest

from .contracts import KernelContractBackend, TurnSpec


@pytest.mark.asyncio
async def test_reasoning_only_turn_is_omitted_from_followup_history(
    kernel_backend: KernelContractBackend,
):
    secret = "PRIVATE_REASONING_ONLY_SENTINEL"
    capture = await kernel_backend.run_agent(
        (TurnSpec(reasoning=secret), TurnSpec(content="complete"))
    )

    assert capture.error is None
    assert capture.provider_histories[1] == capture.provider_histories[0]
    assert not any(
        item.get("role") == "assistant"
        and not item.get("content")
        and not item.get("tool_calls")
        for item in capture.provider_histories[1]
    )
    public = json.dumps(
        {
            "steps": capture.public_steps,
            "chunks": capture.public_chunks,
            "events": capture.public_events,
            "observations": capture.observations,
        },
        default=str,
    )
    assert secret not in public
    assert "reasoning_content" not in public
