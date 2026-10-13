"""Private tool reasoning is wire-only, scoped, and fail-closed."""

import json

import pytest

from .contracts import KernelContractBackend


def _tool_reasoning(request):
    return [
        item.get("reasoning_content")
        for item in request["messages"]
        if item.get("role") == "assistant" and item.get("tool_calls")
    ]


@pytest.mark.asyncio
async def test_private_reasoning_replays_only_on_bound_tool_wire(
    kernel_backend: KernelContractBackend,
):
    capture = await kernel_backend.private_reasoning_roundtrip()
    first = "PRIVATE_REASONING_FIRST_SENTINEL"
    second = "PRIVATE_REASONING_SECOND_SENTINEL"

    assert len(capture.requests) == 4
    assert _tool_reasoning(capture.requests[0]) == []
    assert _tool_reasoning(capture.requests[1]) == [first]
    assert _tool_reasoning(capture.requests[2]) == [first, second]
    assert all(
        "reasoning_content" not in item
        for item in capture.requests[3]["messages"]
    )
    assert capture.effects == (True, True)


@pytest.mark.asyncio
async def test_private_reasoning_never_enters_public_or_cache_surfaces(
    kernel_backend: KernelContractBackend,
):
    capture = await kernel_backend.private_reasoning_roundtrip()
    public = json.dumps(
        {
            "cache": capture.cache_snapshots,
            "steps": capture.public_steps,
            "chunks": capture.public_chunks,
            "events": capture.public_events,
            "results": capture.result_documents,
        },
        default=str,
    )

    for forbidden in (
        "PRIVATE_REASONING_FIRST_SENTINEL",
        "PRIVATE_REASONING_SECOND_SENTINEL",
        "PRIVATE_REASONING_FINAL_SENTINEL",
        "reasoning_content",
    ):
        assert forbidden not in public


def test_private_continuity_requires_strict_argument_integrity(
    kernel_backend: KernelContractBackend,
):
    assert kernel_backend.private_reasoning_policy(strict_arguments=True) is None
    rejected = kernel_backend.private_reasoning_policy(strict_arguments=False)
    assert rejected is not None
    assert rejected.kind == "ValueError"
