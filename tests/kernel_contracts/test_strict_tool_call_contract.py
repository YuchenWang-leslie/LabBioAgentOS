"""Strict tool-call parsing rejects unsafe input before any effect."""

import pytest

from .contracts import KernelContractBackend, ToolCallSpec, TurnSpec


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    (
        '{"value":"unterminated',
        '{"value":"ok"} trailing',
        '{"value":"first","value":"second"}',
        '{"value":NaN}',
        '{"value":Infinity}',
        '{"value":1e999}',
        '["not-an-object"]',
        "",
        None,
    ),
)
async def test_invalid_argument_forms_never_dispatch(
    kernel_backend: KernelContractBackend,
    raw,
):
    capture = await kernel_backend.run_agent(
        (
            TurnSpec(tool_calls=(ToolCallSpec("invalid-call", arguments=raw),)),
            TurnSpec(content="complete"),
        )
    )

    assert capture.error is None
    assert capture.effects == ()
    assert capture.pre_hook_count == 0
    assert capture.post_hook_count == 0
    rejected = capture.observations[0]["tool_argument_observations"][0]
    assert rejected["parse_mode"] == "REJECTED"
    assert rejected["rejection_reason"] == "INVALID_JSON_OBJECT"
    assert capture.tool_feedback[0]["raw_content"]["error_code"] == "INVALID_JSON_OBJECT"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("finish_reason", "error_code"),
    (("length", "RESPONSE_TRUNCATED"), ("content_filter", "RESPONSE_FILTERED")),
)
async def test_known_incomplete_responses_reject_even_valid_json(
    kernel_backend: KernelContractBackend,
    finish_reason,
    error_code,
):
    capture = await kernel_backend.run_agent(
        (
            TurnSpec(
                tool_calls=(
                    ToolCallSpec("incomplete-call", arguments='{"value":"valid"}'),
                ),
                finish_reason=finish_reason,
                completion_tokens=8192,
            ),
            TurnSpec(content="complete"),
        )
    )

    assert capture.effects == ()
    assert capture.pre_hook_count == capture.post_hook_count == 0
    assert capture.observations[0]["finish_reason"] == finish_reason
    assert capture.observations[0]["completion_tokens"] == 8192
    rejected = capture.observations[0]["tool_argument_observations"][0]
    assert rejected["rejection_reason"] == error_code
    assert capture.tool_feedback[0]["raw_content"]["error_code"] == error_code


@pytest.mark.asyncio
async def test_valid_neighbor_dispatches_once_through_both_hooks(
    kernel_backend: KernelContractBackend,
):
    capture = await kernel_backend.run_agent(
        (
            TurnSpec(
                tool_calls=(
                    ToolCallSpec("valid-call", arguments='{"value":"valid"}'),
                )
            ),
            TurnSpec(content="complete"),
        )
    )

    assert capture.error is None
    assert capture.effects == ({"value": "valid", "nested": None},)
    assert capture.pre_hook_count == capture.post_hook_count == 1
    observed = capture.observations[0]["tool_argument_observations"][0]
    assert observed["parse_mode"] == "STRICT_JSON"
    assert observed["rejection_reason"] is None


@pytest.mark.asyncio
async def test_unknown_finish_reason_is_bounded_without_inventing_truncation(
    kernel_backend: KernelContractBackend,
):
    private_finish = "PRIVATE_PROVIDER_FINISH_SENTINEL"
    capture = await kernel_backend.run_agent(
        (
            TurnSpec(
                tool_calls=(
                    ToolCallSpec("unknown-finish", arguments='{"value":"valid"}'),
                ),
                finish_reason=private_finish,
            ),
            TurnSpec(content="complete"),
        )
    )

    assert capture.error is None
    assert capture.effects == ({"value": "valid", "nested": None},)
    assert capture.observations[0]["finish_reason"] == "OTHER"
    assert private_finish not in repr(capture.observations)
