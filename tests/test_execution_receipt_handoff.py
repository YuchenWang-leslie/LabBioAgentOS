"""An execution receipt must survive every public handoff before more effects."""

from dataclasses import replace
import json
from uuid import uuid4

import pytest

from labbioagentos import TraceEventType, WorkflowStage
from labbioagentos.execution import JobLedger
from labbioagentos.execution.submission import ExecutionSubmissionPersistenceError
from labbioagentos.kernel import FinishReason, ModelResultProjector, ToolCall, ToolDispatcher
from labbioagentos.runtime.native_tools import NativeRuntimeCapabilityHost, build_labbio_tool_registry
from labbioagentos.runtime.observations import ObservationStore
from test_execution_inspection import inspection, _inspect, _submit  # noqa: F401
from test_execution_submission_persistence import _persistent
from test_runtime_milestone_b import _toolset, boundary  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", ["execution_submit", "execution_revise"])
@pytest.mark.parametrize("failure", [
    "result", "trace", "archive", "archive_after_save", "archive_unavailable",
    "projection", "projection_validation",
])
async def test_receipt_publication_failure_blocks_next_effect_and_preserves_binding(
    inspection, boundary, tmp_path, monkeypatch, capability, failure,
):
    from labbioagentos.runtime import tooling

    ledger = JobLedger(tmp_path / "submissions.sqlite")
    service, executor, run_id = _persistent(inspection, boundary, ledger)
    initial = None
    if capability == "execution_revise":
        initial = await _submit((service, executor, run_id), boundary)
    base = _toolset(
        boundary, WorkflowStage.EXECUTE, ("execution_submit", "execution_revise"),
        execution_submission=service,
        observation_store=ObservationStore(tmp_path / "observations"),
    )
    host = NativeRuntimeCapabilityHost(replace(base.binding, run_id=run_id), base.services)
    tools = host._host
    if failure == "result":
        original_result = tooling.ToolResult

        def fail_success_result(**kwargs):
            if kwargs["success"]:
                raise ValueError("private-result-canary")
            return original_result(**kwargs)

        monkeypatch.setattr(tooling, "ToolResult", fail_success_result)
    elif failure == "trace":
        original_emit = tools._emit

        def fail_completion(event_type, *args, **kwargs):
            if event_type == TraceEventType.CAPABILITY_COMPLETED:
                raise OSError("private-trace-canary")
            return original_emit(event_type, *args, **kwargs)

        monkeypatch.setattr(tools, "_emit", fail_completion)
    elif failure in {"archive", "archive_after_save", "archive_unavailable"}:
        original_save = tools.services.observation_store.save

        def fail_success_archive(binding, item, public_response):
            if failure == "archive_after_save" and public_response["success"]:
                original_save(binding, item, public_response)
            if public_response["success"] or failure == "archive_unavailable":
                raise OSError("private-archive-canary")
            return original_save(binding, item, public_response)

        monkeypatch.setattr(tools.services.observation_store, "save", fail_success_archive)
    registry = build_labbio_tool_registry(
        host, on_result_rejected=host.on_result_rejected,
    )
    def validate_public(_value):
        if failure == "projection_validation":
            raise ValueError("private-projection-canary")

    dispatcher = ToolDispatcher(
        registry, ModelResultProjector(
            max_bytes=10 if failure == "projection" else 60_000, validate_value=validate_public),
        stop_requested=lambda: host.stop_reason,
    )
    arguments = {"image_key": "approved", "script_content": "pass\n"}
    if initial is not None:
        arguments = {"execution_id": str(initial.execution_id),
                     "expected_script_hash": initial.script_hash, "edits": []}
    outcomes = await dispatcher.dispatch_batch((
        ToolCall(call_id="first", name=capability, arguments_json=json.dumps(arguments)),
        ToolCall(call_id="second", name="execution_submit", arguments_json=json.dumps(
            {"image_key": "approved", "script_content": "pass\n"})),
    ), finish_reason=FinishReason.TOOL_CALLS)
    assert len(executor.plans) == (2 if initial is not None else 1)
    execution_id = executor.plans[-1].execution_id
    assert host.stop_reason == "EXECUTION_SUBMISSION_PERSISTENCE_FAILED"
    assert outcomes[1].result.error.code == "TOOL_NOT_STARTED_RUNTIME_STOP"
    assert "private-" not in json.dumps([x.result.model_dump(mode="json") for x in outcomes])
    if failure in {"result", "trace", "archive"}:
        assert outcomes[0].result.content["error"]["error_code"] == host.stop_reason
        assert outcomes[0].result.content["data"]["execution_id"] == str(execution_id)
    # The receipt remains inspectable: uncertain public delivery is not loss of
    # the completed execution, nor permission to submit its program again.
    assert ledger.execution_submission(run_id, execution_id) is not None
    assert _inspect((service, executor, run_id), boundary, execution_id)["execution_id"] == str(execution_id)
    with pytest.raises(ExecutionSubmissionPersistenceError) as caught:
        await _submit((service, executor, run_id), boundary)
    assert caught.value.execution_id == execution_id
    tools.begin_model_invocation(uuid4())
    assert host.stop_reason == "EXECUTION_SUBMISSION_PERSISTENCE_FAILED"
    ledger.close()


@pytest.mark.asyncio
async def test_confirmed_receipt_handoff_allows_explicit_next_execution(
    inspection, boundary, tmp_path,
):
    ledger = JobLedger(tmp_path / "submissions.sqlite")
    service, executor, run_id = _persistent(inspection, boundary, ledger)
    base = _toolset(boundary, WorkflowStage.EXECUTE, ("execution_submit",),
                    execution_submission=service,
                    observation_store=ObservationStore(tmp_path / "observations"))
    host = NativeRuntimeCapabilityHost(replace(base.binding, run_id=run_id), base.services)
    dispatcher = ToolDispatcher(
        build_labbio_tool_registry(host, on_result_rejected=host.on_result_rejected),
        ModelResultProjector(max_bytes=60_000), stop_requested=lambda: host.stop_reason,
    )
    outcomes = await dispatcher.dispatch_batch(tuple(
        ToolCall(call_id=str(index), name="execution_submit", arguments_json=json.dumps({
            "image_key": "approved", "script_content": "pass\n",
        })) for index in range(2)
    ), finish_reason=FinishReason.TOOL_CALLS)
    assert len(executor.plans) == 2
    assert all(item.result.content["success"] for item in outcomes)
    assert host.stop_reason is None
    ledger.close()
