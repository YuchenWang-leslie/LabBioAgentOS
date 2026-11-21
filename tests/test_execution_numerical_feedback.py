"""Numerical failure facts survive the executor -> tool -> inspection boundary."""

from dataclasses import replace
import json

import pytest
from native_test_provider import LocalProvider

from labbioagentos import DockerExecutor, LabBioRuntimeToolSet, WorkflowStage
from labbioagentos.execution.models import ExecutionStatus, ExecutionFailureClass
from test_execution_inspection import inspection  # noqa: F401
from test_runtime_milestone_b import _toolset, boundary  # noqa: F401
from test_phase6_docker_execution import _synthetic_traceback


def diagnostic(message, exception="ValueError", source="operation()\n", line=1):
    stderr = (
        'Traceback (most recent call last):\n'
        f'  File "/labbio/script.py", line {line}, in <module>\n'
        f'{exception}: {message}\n'
    ).encode()
    return DockerExecutor._safe_python_diagnostics(stderr, script_content=source)


@pytest.mark.parametrize("message, exception, condition", [
    ("b'reciprocal condition number  2.025e-14'", "ValueError", "ILL_CONDITIONED_FIT"),
    ("b'reciprocal condition number  8e-19'", "ValueError", "ILL_CONDITIONED_FIT"),
    ("Singular matrix", "LinAlgError", "SINGULAR_MATRIX"),
    ("SVD did not converge", "LinAlgError", "DECOMPOSITION_DID_NOT_CONVERGE"),
    ("array must not contain infs or NaNs", "ValueError", "NON_FINITE_INPUT"),
    ("need at least one array to concatenate", "ValueError", "EMPTY_ARRAY_SEQUENCE"),
])
def test_finite_numerical_condition_without_message_or_values(message, exception, condition):
    item = diagnostic(message, exception)[0]
    assert item.reported_numerical_condition == condition
    assert item.exception_type == exception
    assert item.script_line_numbers == (1,)
    assert message not in item.model_dump_json()
    assert "2.025" not in item.model_dump_json()


@pytest.mark.parametrize("message", [
    "PRIVATE /private/file token=SECRET",
    "b'reciprocal condition number  2.025e-14 PRIVATE'",
    "b'reciprocal condition number  2.025e-14' /private/file",
    "b'reciprocal condition number  nan'",
    "b'reciprocal condition number  1e99999'",
    "array must not contain infs or NaNs SECRET",
    "need at least one array to concatenate PRIVATE_SECRET",
    "need at least one array to concatenate /private/file",
    "prefix need at least one array to concatenate",
    "need at least one array to concatenate ",
    "Need at least one array to concatenate",
    "need at least one array to concatenate\t",
])
def test_unknown_message_is_not_released_or_inferred(message):
    item = diagnostic(message)[0]
    assert item.reported_numerical_condition is None
    assert all(x not in item.model_dump_json() for x in ("PRIVATE", "/private", "SECRET"))


def test_numerical_reason_does_not_cross_exception_identity_or_chain():
    assert diagnostic("b'reciprocal condition number  2.025e-14'", "KeyError")[0].reported_numerical_condition is None
    assert diagnostic("need at least one array to concatenate", "IndexError")[0].reported_numerical_condition is None
    text = (
        "Traceback (most recent call last):\nValueError: array must not contain infs or NaNs\n\n"
        "During handling of the above exception, another exception occurred:\n\n"
        "Traceback (most recent call last):\nValueError: PRIVATE\n"
    ).encode()
    items = DockerExecutor._safe_python_diagnostics(text)
    assert items[0].reported_numerical_condition is None
    assert items[1].reported_numerical_condition == "NON_FINITE_INPUT"


def test_empty_array_sequence_does_not_transfer_across_exception_chain():
    text = (
        "Traceback (most recent call last):\n"
        "ValueError: need at least one array to concatenate\n\n"
        "During handling of the above exception, another exception occurred:\n\n"
        "Traceback (most recent call last):\nValueError: PRIVATE\n"
    ).encode()
    terminal, predecessor = DockerExecutor._safe_python_diagnostics(text)
    assert terminal.reported_numerical_condition is None
    assert predecessor.reported_numerical_condition == "EMPTY_ARRAY_SEQUENCE"
    assert predecessor.chain_relation == "CONTEXT"
    assert "PRIVATE" not in terminal.model_dump_json()


def test_real_non_scientific_empty_array_sequence_failure(monkeypatch):
    source = "import numpy as np\nnp.vstack([])\n"
    stderr = _synthetic_traceback(source, monkeypatch)
    item = DockerExecutor._safe_python_diagnostics(stderr, script_content=source)[0]
    assert item.exception_type == "ValueError"
    assert item.reported_numerical_condition == "EMPTY_ARRAY_SEQUENCE"
    assert item.script_line_numbers == (2,)
    assert "need at least one array to concatenate" not in item.model_dump_json()
    assert "np.vstack" not in item.model_dump_json()


def test_empty_array_sequence_condition_survives_public_dto_roundtrip():
    from pydantic import ValidationError
    from labbioagentos import ExecutionDiagnostic, ExecutionDiagnosticCode
    item = ExecutionDiagnostic(
        code=ExecutionDiagnosticCode.PYTHON_EXCEPTION,
        exception_type="ValueError", reported_numerical_condition="EMPTY_ARRAY_SEQUENCE",
    )
    assert ExecutionDiagnostic.model_validate_json(item.model_dump_json()) == item
    with pytest.raises(ValidationError):
        ExecutionDiagnostic(
            code=ExecutionDiagnosticCode.PYTHON_EXCEPTION,
            exception_type="IndexError", reported_numerical_condition="EMPTY_ARRAY_SEQUENCE",
        )


def test_real_non_scientific_linear_algebra_failure(monkeypatch):
    source = "import numpy as np\nnp.linalg.inv(np.zeros((2, 2)))\n"
    stderr = _synthetic_traceback(source, monkeypatch)
    item = DockerExecutor._safe_python_diagnostics(stderr, script_content=source)[0]
    assert item.exception_type == "numpy.linalg.LinAlgError"
    assert item.reported_numerical_condition == "SINGULAR_MATRIX"
    assert item.script_line_numbers == (2,)


def test_numerical_condition_is_optional_finite_and_exception_bound():
    from pydantic import ValidationError
    from labbioagentos import ExecutionDiagnostic, ExecutionDiagnosticCode
    base = {"code": ExecutionDiagnosticCode.PYTHON_EXCEPTION, "exception_type": "ValueError"}
    assert ExecutionDiagnostic(**base).reported_numerical_condition is None
    with pytest.raises(ValidationError):
        ExecutionDiagnostic(**base, reported_numerical_condition="PRIVATE")
    with pytest.raises(ValidationError):
        ExecutionDiagnostic(**{**base, "exception_type": "KeyError"},
            reported_numerical_condition="ILL_CONDITIONED_FIT")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["numerical", "empty_array_sequence", "missing_key"])
async def test_failure_location_inspection_audit_and_next_submission(inspection, boundary, monkeypatch, failure):
    service, executor, run_id = inspection
    original = executor.execute
    failing_line = "{}['choice']\n" if failure == "missing_key" else "operation()\n"
    source = "# model-owned filler\n" * 800 + failing_line
    line = 801
    diagnostics = (
        diagnostic(
            "need at least one array to concatenate" if failure == "empty_array_sequence"
            else "b'reciprocal condition number  2.025e-14'", source=source, line=line,
        )
        if failure != "missing_key" else DockerExecutor._safe_python_diagnostics(
            _synthetic_traceback(source, monkeypatch), script_content=source,
        )
    )
    def execute(plan):
        result = original(plan)
        if len(executor.plans) == 1:
            return result.model_copy(update={
                "status": ExecutionStatus.FAILED, "exit_code": 1,
                "error_class": ExecutionFailureClass.NON_ZERO_EXIT,
                "output_artifact_refs": (),
                "diagnostics": diagnostics,
            })
        return result
    monkeypatch.setattr(executor, "execute", execute)
    tools = _toolset(boundary, WorkflowStage.EXECUTE,
        ("execution_submit", "execution_inspect"), execution_submission=service)
    tools = LabBioRuntimeToolSet(replace(tools.binding, run_id=run_id), tools.services)
    provider = LocalProvider(tools)
    await provider.initialize()
    await provider.list_tools()
    failed = await provider.call_tool("execution_submit", {"image_key": "approved", "script_content": source})
    receipt = failed["data"]
    if failure == "numerical":
        assert receipt["diagnostics"][0]["reported_numerical_condition"] == "ILL_CONDITIONED_FIT"
    elif failure == "empty_array_sequence":
        assert receipt["diagnostics"][0]["reported_numerical_condition"] == "EMPTY_ARRAY_SEQUENCE"
    else:
        assert len(receipt["diagnostics"][0]["missing_key_source_locations"]) == 1
    first = await provider.call_tool("execution_inspect", {"execution_id": receipt["execution_id"]})
    page = first["data"]["submitted_program"]
    assert not page["complete"] and "operation()" not in page["source"]
    location = page["diagnostic_line_offsets"][0]
    assert location["line_number"] == line
    assert page["diagnostic_source_lines"][0]["source"] == failing_line
    assert page["diagnostic_source_lines"][0]["complete"] is True
    assert page["diagnostic_source_lines_truncated"] is False
    target = await provider.call_tool("execution_inspect", {
        "execution_id": receipt["execution_id"], "source_offset": location["source_offset"],
    })
    assert target["data"]["submitted_program"]["source"] == failing_line
    assert target["data"]["receipt"] == receipt
    assert len(executor.plans) == 1
    # Explicit synthetic successor; the framework must not create/alter a repair.
    succeeded = await provider.call_tool("execution_submit", {"image_key": "approved", "script_content": "pass\n"})
    assert succeeded["data"]["status"] == "SUCCEEDED"
    assert len(executor.plans) == 2
    assert executor.plans[1].script_content == "pass\n"
    events = boundary[0].read(run_id)
    pages = [e.payload["execution_inspection"] for e in events if "execution_inspection" in e.payload]
    assert len(pages) == 2
    assert pages[0]["source_offset"] == 0 and not pages[0]["complete"]
    assert pages[0]["diagnostic_source_lines"][0]["source_offset"] == location["source_offset"]
    assert pages[1]["source_offset"] == location["source_offset"] and pages[1]["complete"]
    assert all(p["script_hash"] == receipt["script_hash"] for p in pages)
    persisted = json.dumps([e.model_dump(mode="json") for e in events])
    assert "operation()" not in persisted and "model-owned filler" not in persisted
    assert "choice" not in persisted
    assert "need at least one array to concatenate" not in persisted
    # Envelope timestamps may legitimately contain seconds such as 52.025025.
    assert "2.025" not in json.dumps([event.payload for event in events])
    assert tools.evidence_items()[0].safe_result == receipt
