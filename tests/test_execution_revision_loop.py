"""Synthetic tool cooperation, not a test of model intelligence or live execution."""

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from uuid import UUID

import pytest
from pantheon.providers import LocalProvider

from labbioagentos import LabBioRuntimeToolSet, TraceEventType, WorkflowStage
from labbioagentos.execution.models import (
    ExecutionDiagnostic, ExecutionDiagnosticCode, ExecutionFailureClass, ExecutionStatus,
)
from test_execution_inspection import inspection  # noqa: F401
from test_runtime_milestone_b import _toolset, boundary  # noqa: F401


@pytest.mark.asyncio
async def test_three_explicit_synthetic_submissions_keep_originals_and_failure_evidence(
    inspection, boundary, monkeypatch,
):
    # These independent fixtures are supplied explicitly; no runtime chooses or
    # repairs a program, and the fixture executor does not run scientific code.
    sources = (
        "# SYNTHETIC_FIRST_PROGRAM\nraise IndexError('fixture first')\n",
        "# SYNTHETIC_SECOND_PROGRAM\nraise ValueError('fixture second')\n",
        "# SYNTHETIC_THIRD_PROGRAM\npass\n",
    )
    exception_types = ("IndexError", "ValueError", None)
    service, executor, run_id = inspection
    original_execute = executor.execute

    def synthetic_execute(plan):
        index = len(executor.plans)
        assert index < len(sources), "Unexpected implicit execution or retry"
        assert plan.script_content == sources[index], "Submitted source was rewritten"
        result = original_execute(plan)
        exception_type = exception_types[index]
        if exception_type is None:
            return result
        return result.model_copy(update={
            "status": ExecutionStatus.FAILED, "exit_code": 1,
            "error_class": ExecutionFailureClass.NON_ZERO_EXIT,
            "output_artifact_refs": (),
            "diagnostics": (ExecutionDiagnostic(
                code=ExecutionDiagnosticCode.PYTHON_EXCEPTION,
                exception_type=exception_type, script_line_numbers=(2,),
            ),),
        })

    monkeypatch.setattr(executor, "execute", synthetic_execute)
    toolsets, providers, receipts, evidence_snapshots = [], [], [], []
    for index, source in enumerate(sources):
        tools = _toolset(boundary, WorkflowStage.EXECUTE,
            ("execution_submit", "execution_inspect"), execution_submission=service)
        tools = LabBioRuntimeToolSet(replace(tools.binding, run_id=run_id), tools.services)
        provider = LocalProvider(tools)
        await provider.initialize()
        await provider.list_tools()
        toolsets.append(tools)
        providers.append(provider)
        assert len(executor.plans) == index

        if index:
            # Use the previous actual tool return, not a prefilled execution ID.
            inspected = await provider.call_tool("execution_inspect", {
                "execution_id": receipts[-1]["execution_id"],
            })
            assert inspected["success"] is True
            assert inspected["data"]["receipt"] == receipts[-1]
            assert inspected["data"]["receipt"]["status"] == "FAILED"
            assert inspected["data"]["receipt"]["diagnostics"][0]["exception_type"] == exception_types[index - 1]
            page = inspected["data"]["submitted_program"]
            assert page["authority"] == "MODEL_CONTEXT"
            assert page["source"] == sources[index - 1]
            assert page["script_hash"] == receipts[-1]["script_hash"]
            assert page["complete"] is True
            assert len(executor.plans) == index  # Inspection did not resubmit.
            safe_inspection = tools.evidence_items()[-1].safe_result
            assert safe_inspection["receipt"] == receipts[-1]
            assert "source" not in safe_inspection["submitted_program"]

        submitted = await provider.call_tool("execution_submit", {
            "image_key": "approved", "script_content": source,
        })
        assert submitted["success"] is True  # Tool completion, not process success.
        receipt = submitted["data"]
        assert receipt["status"] == ("FAILED" if index < 2 else "SUCCEEDED")
        assert receipt["exit_code"] == (1 if index < 2 else 0)
        assert receipt["script_hash"] == sha256(source.encode("utf-8")).hexdigest()
        assert receipt["stdout_artifact_id"] is None and receipt["stderr_artifact_id"] is None
        assert len(receipt["output_artifact_ids"]) == (0 if index < 2 else 1)
        assert receipt["diagnostics"] == ([] if index == 2 else [
            ExecutionDiagnostic(code=ExecutionDiagnosticCode.PYTHON_EXCEPTION,
                exception_type=exception_types[index], script_line_numbers=(2,)).model_dump(mode="json")
        ])
        receipts.append(receipt)
        item = tools.evidence_items()[-1]
        assert item.capability_name == "execution_submit" and item.status.value == "COMPLETED"
        assert item.safe_result == receipt
        evidence_snapshots.append(item.model_dump_json())
        assert len(executor.plans) == index + 1

    assert len({receipt["execution_id"] for receipt in receipts}) == 3
    assert len({receipt["script_hash"] for receipt in receipts}) == 3
    assert len({tools.binding.invocation_id for tools in toolsets}) == 3
    assert [plan.invocation_id for plan in executor.plans] == [tools.binding.invocation_id for tools in toolsets]

    # A later success cannot replace the exact original source or failed receipt.
    for source, receipt, tools, snapshot in zip(sources, receipts, toolsets, evidence_snapshots, strict=True):
        inspected = await providers[-1].call_tool("execution_inspect", {
            "execution_id": receipt["execution_id"],
        })
        assert inspected["success"] is True
        assert inspected["data"]["receipt"] == receipt
        assert inspected["data"]["submitted_program"]["source"] == source
        ref = executor.script_refs[UUID(receipt["execution_id"])]
        assert Path(ref.storage_locator).read_bytes() == source.encode("utf-8")
        original_item = next(item for item in tools.evidence_items() if item.capability_name == "execution_submit")
        assert original_item.model_dump_json() == snapshot
    assert len(executor.plans) == 3

    events = boundary[0].read(run_id)
    execution_events = [event for event in events
        if event.event_type in (TraceEventType.EXECUTION_FAILED, TraceEventType.EXECUTION_COMPLETED)]
    assert [event.status for event in execution_events] == ["FAILED", "FAILED", "SUCCEEDED"]
    for event, receipt in zip(execution_events, receipts, strict=True):
        assert event.payload["execution_id"] == receipt["execution_id"]
        assert event.payload["script_hash"] == receipt["script_hash"]
        assert event.payload["diagnostics"] == receipt["diagnostics"]
    persisted = json.dumps([
        *[item.model_dump(mode="json") for tools in toolsets for item in tools.evidence_items()],
        *[event.model_dump(mode="json") for event in events],
    ])
    for forbidden in (*sources, "SYNTHETIC_FIRST_PROGRAM", "SYNTHETIC_SECOND_PROGRAM",
                      "SYNTHETIC_THIRD_PROGRAM", "storage_locator", '"source":'):
        assert forbidden not in persisted
