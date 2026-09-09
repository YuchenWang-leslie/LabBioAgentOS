"""Safe failure identity reaches the model and its immediate durable audit."""

from hashlib import sha256
import json

import pytest

from labbioagentos import ExecutionSubmissionService, TraceEventType, WorkflowStage
from test_runtime_milestone_b import boundary, _toolset, MockExecutor


@pytest.mark.asyncio
async def test_syntax_failure_identity_reaches_tool_evidence_and_trace(boundary):
    sink, recorder, access, _, _, store, _ = boundary
    executor = MockExecutor(store)
    service = ExecutionSubmissionService(
        artifact_store=store, access_service=access, executor=executor,
        trace_recorder=recorder,
    )
    toolset = _toolset(boundary, WorkflowStage.EXECUTE, ("execution_submit",),
                       execution_submission=service)
    source = "# PRIVATE_SOURCE_SENTINEL\nfor item in ()\n    pass\n"
    result = await toolset.execution_submit(image_key="approved", script_content=source)
    assert result["success"] is False
    assert result["error"]["error_code"] == "INVALID_EXECUTION_SCRIPT"
    validation = result["error"]["script_validation"]
    assert validation["script_hash"] == sha256(source.encode()).hexdigest()
    assert validation["diagnostics"][0]["exception_type"] == "SyntaxError"
    assert validation["diagnostics"][0]["script_line_numbers"] == [2]
    item = toolset.evidence_items()[-1]
    assert item.execution_submit_request.validation_status.value == "VALID"
    assert item.error_details.script_validation.model_dump(mode="json") == validation
    failure = next(event for event in sink.read()
                   if event.event_type is TraceEventType.CAPABILITY_FAILED)
    assert failure.payload["error_details"]["script_validation"] == validation
    assert executor.plans == []
    encoded = json.dumps([result, item.model_dump(mode="json"),
                          failure.model_dump(mode="json")])
    assert "PRIVATE_SOURCE_SENTINEL" not in encoded
    assert "for item" not in encoded
    assert "<unknown>" not in encoded
    assert "expected ':'" not in encoded

    # A separate model-selected, valid request is not blocked or auto-repaired.
    valid = await toolset.execution_submit(image_key="approved", script_content="pass\n")
    assert valid["success"] is True
    assert valid["data"]["status"] == "SUCCEEDED"
    assert len(executor.plans) == 1
    assert executor.plans[0].script_content == "pass\n"
