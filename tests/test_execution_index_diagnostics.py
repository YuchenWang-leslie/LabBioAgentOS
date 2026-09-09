"""Only finite process-reported indexing conditions cross the model boundary."""

import pytest
from pydantic import ValidationError

from labbioagentos import DockerExecutor, ExecutionDiagnostic, ExecutionDiagnosticCode, ExecutionReceipt
from test_phase6_docker_execution import (
    FakeDockerRunner, _environment, _plan, _synthetic_traceback,
)


def _reported(terminal):
    return DockerExecutor._safe_python_diagnostics(
        ("Traceback (most recent call last):\n" + terminal + "\n").encode()
    )


@pytest.mark.parametrize("size, index, expected", [
    (0, -1, "OUT_OF_BOUNDS_EMPTY_AXIS"),
    (2, 3, "OUT_OF_BOUNDS"),
    (2, -3, "OUT_OF_BOUNDS"),
])
def test_real_numpy_index_failures_keep_only_reported_condition(
    monkeypatch, size, index, expected,
):
    source = f"import numpy as np\narray = np.empty({size})\nresult = array[{index}]\n"
    stderr = _synthetic_traceback(source, monkeypatch)
    diagnostic = DockerExecutor._safe_python_diagnostics(stderr, script_content=source)[0]
    assert diagnostic.reported_index_condition == expected
    assert diagnostic.exception_type == "IndexError"
    assert diagnostic.script_line_numbers == (3,)
    location = diagnostic.script_error_locations[0]
    assert location.line_number == 3
    assert source.splitlines()[2][location.start_column:location.end_column] == f"array[{index}]"
    encoded = diagnostic.model_dump_json()
    assert "array" not in encoded and "numpy" not in encoded
    assert "is out of bounds for axis" not in encoded


@pytest.mark.parametrize("expression", ["[][0]", "()[0]", "''[0]"])
def test_real_builtin_index_failures_do_not_claim_empty_axis(monkeypatch, expression):
    source = f"result = {expression}\n"
    diagnostic = DockerExecutor._safe_python_diagnostics(
        _synthetic_traceback(source, monkeypatch), script_content=source,
    )[0]
    assert diagnostic.reported_index_condition == "OUT_OF_BOUNDS"
    assert diagnostic.exception_type == "IndexError"


@pytest.mark.parametrize("terminal", [
    "IndexError: PRIVATE_MESSAGE",
    "IndexError: index -1 is out of bounds for axis 0 with size 0 PRIVATE_SECRET",
    "IndexError: index -1 is out of bounds for axis 0 with size 0 /private/path",
    "IndexError: index -1 is out of bounds for axis 0 with size 0 ",
    "IndexError: prefix index -1 is out of bounds for axis 0 with size 0",
    "IndexError: index -1 is out of bounds for axis -1 with size 0",
    "IndexError: index -1 is out of bounds for axis 0 with size -1",
    "IndexError: index +1 is out of bounds for axis 0 with size 0",
    "IndexError: index 1.0 is out of bounds for axis 0 with size 0",
    "IndexError: index 01 is out of bounds for axis 0 with size 0",
    "IndexError: index -1 is out of bounds for axis 0 with size 00",
    "IndexError: index -1 is out of bounds for axis 0 with size " + "9" * 100,
    "IndexError: list index out of range PRIVATE_SECRET",
    "IndexError: List index out of range",
    "IndexError: pop index out of range",
])
def test_unknown_or_malformed_index_messages_have_no_inferred_condition(terminal):
    diagnostic = _reported(terminal)[0]
    assert diagnostic.exception_type == "IndexError"
    assert diagnostic.reported_index_condition is None
    assert "PRIVATE" not in diagnostic.model_dump_json()
    assert "/private" not in diagnostic.model_dump_json()


@pytest.mark.parametrize("exception", ["ValueError", "KeyError", "RuntimeError"])
def test_index_message_does_not_override_exception_identity(exception):
    diagnostic = _reported(
        f"{exception}: index -1 is out of bounds for axis 0 with size 0"
    )[0]
    assert diagnostic.exception_type == exception
    assert diagnostic.reported_index_condition is None


@pytest.mark.parametrize("terminal, expected", [
    ("IndexError: list index out of range", "OUT_OF_BOUNDS"),
    ("IndexError: PRIVATE_CURRENT", None),
    ("ValueError: PRIVATE_CURRENT", None),
])
def test_chained_exception_does_not_inherit_prior_empty_condition(terminal, expected):
    stderr = (
        "Traceback (most recent call last):\n"
        "IndexError: index -1 is out of bounds for axis 0 with size 0\n\n"
        "During handling of the above exception, another exception occurred:\n\n"
        "Traceback (most recent call last):\n" + terminal + "\n"
    ).encode()
    diagnostic = DockerExecutor._safe_python_diagnostics(stderr)[0]
    assert diagnostic.reported_index_condition == expected
    assert "PRIVATE" not in diagnostic.model_dump_json()


def test_index_numbers_and_unrelated_process_content_are_not_released(tmp_path):
    stderr = (
        "PRIVATE_PROCESS token=PRIVATE_SECRET /private/patient\n"
        "Traceback (most recent call last):\n"
        "IndexError: index 876543210987654321 is out of bounds for axis 123 with size 456\n"
    ).encode()
    runner = FakeDockerRunner(exit_code=1, stderr=stderr)
    _, executor, _ = _environment(tmp_path, runner)
    result = executor.execute(_plan())
    receipt = ExecutionReceipt.from_result(result)
    assert receipt.diagnostics == result.diagnostics
    assert receipt.diagnostics[0].reported_index_condition == "OUT_OF_BOUNDS"
    assert receipt.status.value == "FAILED"
    assert len(runner.calls) == 1
    encoded = receipt.model_dump_json()
    for forbidden in ("PRIVATE", "/private", "876543210987654321", "axis 123", "size 456"):
        assert forbidden not in encoded
    assert receipt.script_hash == result.script_hash


@pytest.mark.parametrize("condition", ["OUT_OF_BOUNDS", "OUT_OF_BOUNDS_EMPTY_AXIS"])
def test_reported_index_condition_requires_index_error(condition):
    with pytest.raises(ValidationError):
        ExecutionDiagnostic(
            code=ExecutionDiagnosticCode.PYTHON_EXCEPTION,
            exception_type="ValueError", reported_index_condition=condition,
        )


def test_reported_index_condition_remains_optional_and_finite():
    diagnostic = ExecutionDiagnostic(
        code=ExecutionDiagnosticCode.PYTHON_EXCEPTION, exception_type="IndexError",
    )
    assert diagnostic.reported_index_condition is None
    with pytest.raises(ValidationError):
        ExecutionDiagnostic(
            code=ExecutionDiagnosticCode.PYTHON_EXCEPTION,
            exception_type="IndexError", reported_index_condition="PRIVATE_CONDITION",
        )
