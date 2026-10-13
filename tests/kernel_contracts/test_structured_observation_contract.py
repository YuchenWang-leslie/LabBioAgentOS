"""Structured turns are observed before validation can reject their content."""

import json

import pytest

from .contracts import KernelContractBackend


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "validation_type"),
    (
        ('{"result":{"count":"PRIVATE_RESPONSE_SENTINEL', "json_invalid"),
        ('{"result":{"count":"PRIVATE_RESPONSE_SENTINEL"}}', "int_type"),
    ),
)
async def test_rejected_response_is_observed_without_publication_or_retry(
    kernel_backend: KernelContractBackend,
    content,
    validation_type,
):
    capture = await kernel_backend.structured_response(content, finish_reason="length")

    assert capture.error is not None
    assert capture.error.validation_type == validation_type
    assert capture.provider_calls == 1
    assert len(capture.observations) == 1
    assert capture.observations[0]["finish_reason"] == "length"
    assert capture.observations[0]["completion_tokens"] == 16_384
    assert capture.callback_order == ("observation",)
    public = json.dumps(
        {
            "observations": capture.observations,
            "steps": capture.public_steps,
            "chunks": capture.public_chunks,
            "events": capture.public_events,
        },
        default=str,
    )
    for forbidden in (
        "PRIVATE_RESPONSE_SENTINEL",
        "PRIVATE_REASONING_SENTINEL",
        "PRIVATE_PROVIDER_BODY_SENTINEL",
        "reasoning_content",
        "provider_raw_body",
    ):
        assert forbidden not in public


@pytest.mark.asyncio
async def test_valid_observation_precedes_public_assistant_completion(
    kernel_backend: KernelContractBackend,
):
    capture = await kernel_backend.structured_response(
        '{"result":{"count":7}}',
        finish_reason="stop",
    )

    assert capture.error is None
    assert capture.result_content == {"count": 7}
    assert capture.provider_calls == 1
    assert capture.callback_order == ("observation", "assistant")
