"""Authorized error text survives execution, tools, inspection and evidence."""

from dataclasses import replace
import json

import pytest
from pantheon.providers import LocalProvider

from labbioagentos import ExecutionReceipt, LabBioRuntimeToolSet, WorkflowStage
from labbioagentos.execution.models import ExecutionFailureClass, ExecutionStatus
from labbioagentos.execution.error_context import bounded_error_context
from test_execution_inspection import inspection  # noqa: F401
from test_phase6_docker_execution import FakeDockerRunner, _environment, _plan
from test_runtime_milestone_b import _toolset, boundary  # noqa: F401


TRACE = b'''unrelated progress must not enter traceback context
Traceback (most recent call last):
  File "/labbio/script.py", line 12, in <module>
    integrate(data)
  File "/usr/local/lib/python3.11/site-packages/example/align.py", line 99, in assign
    result[key] = corrected.T
ValueError: Value passed for key 'embedding' is of incorrect shape.
Value had shape (50,) while it should have had (16566,).
'''


def test_generic_multiline_error_not_a_method_specific_enum():
    context = bounded_error_context(TRACE)
    assert context.source == "PYTHON_TRACEBACK"
    assert "(50,)" in context.text and "(16566,)" in context.text
    assert "example.align.py" in context.text and "line 99" in context.text
    assert "integrate(data)" in context.text
    assert "unrelated progress" not in context.text
    assert "/usr/local" not in context.text and "/labbio" not in context.text
    assert context.redacted and not context.truncated


def test_unknown_exception_and_explicit_exit_have_error_context():
    for content in (b"package.CustomFailure: unsupported dtype complex128\n",
                    b"cannot continue: missing input descriptor\n"):
        context = bounded_error_context(content)
        assert context.source == "STDERR_TAIL"
        assert content.decode().strip() in context.text
    assert bounded_error_context(b"") is None


def test_redaction_precedes_truncation_and_retains_technical_explanation():
    content = (
        "ValueError: size (3, 8) not (8, 3)\n"
        'api_key="VERY_PRIVATE_KEY" password=PRIVATE_PASSWORD\n'
        "Authorization: Bearer PRIVATE_BEARER\n"
        "https://user:PRIVATE_URL@example.invalid/data?token=PRIVATE_QUERY\n"
        "/private/patient/input.csv C:\\private\\input.csv\n"
        "-----BEGIN PRIVATE KEY-----\nPRIVATE_KEY_BYTES\n-----END PRIVATE KEY-----\n"
    ).encode()
    context = bounded_error_context(content)
    assert "(3, 8)" in context.text
    for secret in ("VERY_PRIVATE_KEY", "PRIVATE_PASSWORD", "PRIVATE_BEARER",
                   "PRIVATE_URL", "PRIVATE_QUERY", "PRIVATE_KEY_BYTES", "/private", "C:\\private"):
        assert secret not in context.text
    assert context.redacted
    long = bounded_error_context(b"x" * 100_000 + b"\nRuntimeError: final diagnostic\n")
    assert long.truncated and len(long.text) <= 6000
    assert "final diagnostic" in long.text


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("timed_out", [False, True])
def test_policy_controls_real_executor_receipt_not_agent_arguments(tmp_path, enabled, timed_out):
    runner = FakeDockerRunner(exit_code=1, stdout=b"PRIVATE_MATRIX", stderr=TRACE, timed_out=timed_out)
    environment = _environment(tmp_path, runner)
    executor = environment[1]
    executor.execution_policy = executor.execution_policy.model_copy(
        update={"include_error_context": enabled})
    result = executor.execute(_plan())
    receipt = ExecutionReceipt.from_result(result)
    assert (receipt.error_context is not None) == enabled
    assert "PRIVATE_MATRIX" not in receipt.model_dump_json()
    if enabled:
        assert receipt.error_context == result.error_context
        assert "(16566,)" in receipt.error_context.text


@pytest.mark.asyncio
async def test_context_reaches_model_inspection_and_durable_trace(inspection, boundary, monkeypatch):
    service, executor, run_id = inspection
    original = executor.execute

    def execute(plan):
        result = original(plan)
        return result.model_copy(update={
            "status": ExecutionStatus.FAILED, "exit_code": 1,
            "error_class": ExecutionFailureClass.NON_ZERO_EXIT,
            "output_artifact_refs": (), "error_context": bounded_error_context(TRACE)})

    monkeypatch.setattr(executor, "execute", execute)
    toolset = _toolset(boundary, WorkflowStage.EXECUTE,
        ("execution_submit", "execution_inspect"), execution_submission=service)
    toolset = LabBioRuntimeToolSet(replace(toolset.binding, run_id=run_id), toolset.services)
    provider = LocalProvider(toolset)
    await provider.initialize()
    await provider.list_tools()
    submitted = await provider.call_tool("execution_submit", {
        "image_key": "approved", "script_content": "pass\n"})
    assert submitted["success"]
    receipt = submitted["data"]
    assert "(16566,)" in receipt["error_context"]["text"]
    inspected = await provider.call_tool("execution_inspect", {"execution_id": receipt["execution_id"]})
    assert inspected["data"]["receipt"]["error_context"] == receipt["error_context"]
    assert toolset.evidence_items()[0].safe_result["error_context"] == receipt["error_context"]
    events = boundary[0].read(run_id)
    durable = [e.payload["error_context"] for e in events if "error_context" in e.payload]
    assert durable == [receipt["error_context"]]
    assert ExecutionReceipt.model_validate_json(json.dumps(receipt)).error_context.text


def test_exception_chain_and_unrecognized_types_keep_their_actual_messages():
    context = bounded_error_context(
        b'Traceback (most recent call last):\npackage.CustomError: expected integer, got str\n\n'
        b'The above exception was the direct cause of the following exception:\n\n'
        b'Traceback (most recent call last):\nRuntimeError: conversion failed\n')
    assert 'expected integer, got str' in context.text
    assert 'direct cause' in context.text and 'conversion failed' in context.text


def test_local_setting_is_explicit_and_part_of_runtime_identity(tmp_path):
    from labbioagentos.local_config import LocalExecutionSettings
    from labbioagentos import ExecutionPlanDraft
    from pydantic import ValidationError
    settings = LocalExecutionSettings(
        image_key='python-analysis', image_reference='sha256:' + '1' * 64,
        resources={})
    assert settings.include_error_context is False
    assert settings.model_copy(update={'include_error_context': True}).model_dump()['include_error_context']
    with pytest.raises(ValidationError):
        ExecutionPlanDraft(image_key='approved', script_content='pass', include_error_context=True)
