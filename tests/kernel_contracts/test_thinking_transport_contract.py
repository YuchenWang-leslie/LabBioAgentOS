"""Explicit thinking configuration has one immutable compatible wire shape."""

import pytest

from .contracts import KernelContractBackend


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", (False, True))
async def test_thinking_extension_is_reusable_and_does_not_mutate_source_params(
    kernel_backend: KernelContractBackend,
    enabled,
):
    capture = await kernel_backend.thinking_transport(enabled=enabled, calls=2)
    state = "enabled" if enabled else "disabled"

    assert capture.error is None
    assert capture.source_parameters == {
        "thinking": {"type": state},
        "max_tokens": 1024,
    }
    assert capture.requests == (
        {"extra_body": {"thinking": {"type": state}}, "max_tokens": 1024},
        {"extra_body": {"thinking": {"type": state}}, "max_tokens": 1024},
    )
