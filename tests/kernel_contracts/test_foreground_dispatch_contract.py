"""Strict provider schemas and foreground-only dispatch are deterministic."""

import pytest

from .contracts import KernelContractBackend


@pytest.mark.asyncio
async def test_strict_schema_reaches_supported_wire_without_mutating_registry(
    kernel_backend: KernelContractBackend,
):
    capture = await kernel_backend.strict_schema_transport(supported=True)

    assert capture.error is None
    assert len(capture.requests) == 1
    assert capture.schemas_after == capture.schemas_before
    for entry in capture.requests[0]["tools"]:
        function = entry["function"]
        assert function["strict"] is True
        assert function["parameters"]["additionalProperties"] is False
    inspect = next(
        item["function"]
        for item in capture.requests[0]["tools"]
        if item["function"]["name"] == "inspect"
        or item["function"]["name"].endswith("__inspect")
    )
    assert inspect["parameters"]["properties"]["limit"]["anyOf"] == [
        {"maximum": 100, "minimum": 1, "type": "integer"},
        {"type": "null"},
    ]


@pytest.mark.asyncio
async def test_unsupported_strict_transport_fails_before_provider_call(
    kernel_backend: KernelContractBackend,
):
    capture = await kernel_backend.strict_schema_transport(supported=False)

    assert capture.error is not None
    assert capture.error.kind == "ValueError"
    assert capture.requests == ()
    assert capture.schemas_after == capture.schemas_before


@pytest.mark.asyncio
async def test_background_control_is_absent_and_explicit_request_has_no_effect(
    kernel_backend: KernelContractBackend,
):
    capture = await kernel_backend.foreground_dispatch("background_rejected")

    for entry in capture.schemas:
        parameters = entry["function"]["parameters"]
        assert "_background" not in parameters["properties"]
        assert "_background" not in parameters.get("required", [])
    assert capture.effects == ()
    assert capture.pre_hook_count == capture.post_hook_count == 0
    assert capture.background_task_count == 0
    assert capture.feedback[0]["raw_content"]["error_code"] == "BACKGROUND_EXECUTION_DISABLED"


@pytest.mark.asyncio
async def test_long_foreground_result_is_delivered_without_preview_truncation(
    kernel_backend: KernelContractBackend,
):
    capture = await kernel_backend.foreground_dispatch("long_result")

    assert capture.effects == ("long_result",)
    assert capture.pre_hook_count == capture.post_hook_count == 1
    assert capture.background_task_count == 0
    assert capture.feedback[0]["raw_content"] == {
        "success": True,
        "payload": "x" * 2500,
    }


@pytest.mark.asyncio
async def test_timeout_does_not_detach_foreground_work(
    kernel_backend: KernelContractBackend,
):
    capture = await kernel_backend.foreground_dispatch("timeout")

    assert capture.effects == ("timeout",)
    assert capture.background_task_count == 0
    assert capture.feedback[0]["raw_content"] == {
        "success": True,
        "value": "finished",
    }


@pytest.mark.asyncio
async def test_parallel_foreground_calls_return_under_original_ids(
    kernel_backend: KernelContractBackend,
):
    capture = await kernel_backend.foreground_dispatch("parallel")

    assert set(capture.effects) == {"A", "B"}
    assert [item["tool_call_id"] for item in capture.feedback] == ["call-a", "call-b"]
    assert [item["raw_content"]["value"] for item in capture.feedback] == ["A", "B"]
    assert capture.background_task_count == 0
