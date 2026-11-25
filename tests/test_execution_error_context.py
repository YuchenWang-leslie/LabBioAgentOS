"""Authorized error text survives execution, tools, inspection and evidence."""

from dataclasses import replace
import hashlib
import json

import pytest
from native_test_provider import LocalProvider

from labbioagentos import ExecutionReceipt, LabBioRuntimeToolSet, WorkflowStage
from labbioagentos.execution.models import ExecutionFailureClass, ExecutionStatus
from labbioagentos.execution.error_context import bounded_error_context, redact_process_text
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


@pytest.mark.parametrize('label', ['A+/B+', 'A-/B-', 'A+/B-/C+',
    'CD4+/CD25 T Reg', 'CD4+/CD45RA+/CD25- Naive T'])
def test_inline_identifier_slashes_are_not_absolute_paths(label):
    assert redact_process_text(label) == label


@pytest.mark.parametrize('path', ['/private/input.csv', '../private/input.csv',
    r'C:\private\input.csv', r'prefix+C:\private\input.csv', '~/.cache/private.txt'])
def test_real_paths_still_redacted_at_their_actual_boundaries(path):
    assert 'private' not in redact_process_text(path)


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


@pytest.mark.parametrize("timed_out", [False, True])
def test_filtered_apptainer_context_keeps_original_stderr_identity(tmp_path, timed_out):
    from apptainer_fixtures import apptainer_environment

    stderr = b"FATAL: runtime setup /private/host/RUNTIME_PRIVATE\n" + TRACE
    runner = FakeDockerRunner(exit_code=1, stdout=b"", stderr=stderr, timed_out=timed_out)
    _, executor, _, _, _ = apptainer_environment(tmp_path, runner)
    executor.execution_policy = executor.execution_policy.model_copy(update={"include_error_context": True})
    result = executor.execute(_plan())
    context = result.error_context
    assert context is not None
    assert context.stderr_sha256 == hashlib.sha256(stderr).hexdigest()
    assert "RUNTIME_PRIVATE" not in context.text
    assert "(16566,)" in context.text and context.redacted


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
        backend='docker', image_key='python-analysis', image_reference='sha256:' + '1' * 64,
        resources={})
    assert settings.include_error_context is False
    assert settings.model_copy(update={'include_error_context': True}).model_dump()['include_error_context']
    with pytest.raises(ValidationError):
        ExecutionPlanDraft(image_key='approved', script_content='pass', include_error_context=True)


@pytest.mark.parametrize('ending', [b'', b'\n', b'\r\n'])
@pytest.mark.parametrize('size', [65_536, 90_000])
def test_oversized_exception_preserves_frames_and_omits_entire_payload(ending, size):
    stderr = (b'private progress\nTraceback (most recent call last):\n'
              b'  File "/labbio/script.py", line 7, in run\n'
              b'    PRIVATE_LOCAL_EXPRESSION\n'
              b'package.LookupFailure: PRIVATE_ROW_ID api_key=PRIVATE_TOKEN '
              + b'x' * size + ending)
    result = bounded_error_context(stderr)
    assert result.source == 'PYTHON_TRACEBACK'
    assert 'File "<submitted_script>", line 7' in result.text
    assert 'package.LookupFailure: [oversized line omitted]' in result.text
    assert 'PRIVATE' not in result.text and 'private progress' not in result.text
    assert len(result.text) <= 6000 and result.truncated and result.redacted
    assert result.stderr_sha256 == hashlib.sha256(stderr).hexdigest()


def test_line_omitted_at_scan_boundary_does_not_hide_following_short_exception():
    stderr = (b'Traceback (most recent call last):\n  File "/labbio/script.py", line 12\n'
              b'KeyError: PRIVATE_ROW ' + b'x' * 100_000
              + b'\nRuntimeError: retry rejected token=PRIVATE_TOKEN\n')
    result = bounded_error_context(stderr)
    assert result.source == 'PYTHON_TRACEBACK'
    assert 'KeyError: [oversized line omitted]' in result.text
    assert 'RuntimeError: retry rejected token=[REDACTED]' in result.text
    assert 'PRIVATE' not in result.text


def test_unstructured_oversized_tail_is_withheld_without_inventing_an_exception():
    result = bounded_error_context(b'PRIVATE_DATA: ' + b'x' * 90_000)
    assert result.text == '[oversized line omitted]'
    assert result.source == 'STDERR_TAIL' and result.truncated


def test_line_just_below_scan_limit_does_not_expose_the_payload_prefix():
    stderr = (b'Traceback (most recent call last):\n  File "/labbio/script.py", line 8\n'
              b'ValueError: PRIVATE_ROW_ID ' + b'x' * 64000)
    result = bounded_error_context(stderr)
    assert result.text.endswith('ValueError: [oversized line omitted]')
    assert 'PRIVATE' not in result.text and len(result.text) <= 6000


def test_short_traceback_preserves_unchanged_authorized_diagnostic():
    stderr = b'Traceback (most recent call last):\n  File "main.py", line 7\nTypeError: invalid dimension\n'
    result = bounded_error_context(stderr)
    assert result.text == stderr.decode().rstrip('\n')
    assert not result.truncated and not result.redacted


def test_private_key_redaction_survives_cut_and_traceback_shaped_secret_text():
    stderr = (b'Traceback (most recent call last):\n  File "main.py", line 7\n'
              b'-----BEGIN PRIVATE KEY-----\nPrivateIdentifier: ' + b'x' * 90_000
              + b'\nTraceback (most recent call last):\nPRIVATE_SUFFIX\n-----END PRIVATE KEY-----\n'
              b'RuntimeError: operation failed\n')
    result = bounded_error_context(stderr, raw_stderr=b'PRIVATE_BACKEND_LINE\n' + stderr)
    assert result.text.endswith('RuntimeError: operation failed') and '[REDACTED_KEY]' in result.text
    assert 'PrivateIdentifier' not in result.text and 'PRIVATE' not in result.text
    assert result.stderr_sha256 == hashlib.sha256(b'PRIVATE_BACKEND_LINE\n' + stderr).hexdigest()


def test_receipt_budget_discards_complete_lines_and_retains_terminal_message():
    lines = [str(index) + 'x' * 1199 for index in range(7)] + ['RuntimeError: failed']
    result = bounded_error_context('\n'.join(lines).encode())
    assert result.text.splitlines() == lines[-5:]
    assert len(result.text) <= 6000 and result.truncated


def test_byte_window_on_a_newline_retains_the_first_complete_line():
    header = b'Traceback (most recent call last):\n'
    last = b'\nRuntimeError: failed\n'
    tail = header + b'\n' * (65_536 - len(header + last)) + last
    result = bounded_error_context(b'progress\n' + tail)
    assert result.source == 'PYTHON_TRACEBACK' and result.text.endswith('RuntimeError: failed')


@pytest.mark.parametrize('size', [1200, 1201])
def test_line_budget_boundary(size):
    prefix = b'RuntimeError: '
    line = prefix + b'x' * (size - len(prefix))
    result = bounded_error_context(b'Traceback (most recent call last):\n' + line)
    expected = 'RuntimeError: [oversized line omitted]' if size > 1200 else line.decode()
    assert result.text.splitlines()[-1] == expected
    assert result.truncated == (size > 1200)
