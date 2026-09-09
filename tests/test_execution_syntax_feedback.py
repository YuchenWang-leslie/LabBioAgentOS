"""Parser failures retain bounded locations, never rejected program content."""

import ast
import json
from hashlib import sha256
from uuid import uuid4

import pytest

from labbioagentos import (
    ExecutionPlanDraft, ExecutionScriptValidationError, ExecutionSubmissionService,
    WorkflowStage,
)
from labbioagentos.execution.errors import ExecutionInputSelectionError
from test_runtime_milestone_b import MockExecutor, boundary


def _service(boundary, executor=None):
    _, recorder, access, principal, workspace, store, _ = boundary
    executor = executor or MockExecutor(store)
    return ExecutionSubmissionService(
        artifact_store=store, access_service=access, executor=executor,
        trace_recorder=recorder,
    ), executor, dict(
        principal=principal, workspace=workspace, run_id=uuid4(),
        stage_id=WorkflowStage.EXECUTE, invocation_id=uuid4(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("source, exception_type, lines, locations", [
    ("value = 1 2\n", "SyntaxError", (1,), [(1, 10, 11)]),
    ("if True:\npass\n", "IndentationError", (2,), [(2, 0, 4)]),
    ("if True:\n\tpass\n        pass\n", "TabError", (3,), []),
    ('value = "中文"; 1 2\n', "SyntaxError", (1,), []),
    ("\tvalue = )\n", "IndentationError", (1,), []),
    ("value = (\n", "SyntaxError", (1,), []),
    ("if True:\n", "IndentationError", (1,), []),
    ("value = )\n", "SyntaxError", (1,), []),
    ("first = 1\r\nvalue = 1 2\r\n", "SyntaxError", (2,), [(2, 10, 11)]),
    ("first = 1\rvalue = 1 2\r", "SyntaxError", (2,), [(2, 10, 11)]),
])
async def test_real_parser_failure_has_exact_safe_identity_and_locations(
    boundary, source, exception_type, lines, locations,
):
    service, executor, context = _service(boundary)
    with pytest.raises(ExecutionScriptValidationError) as caught:
        await service.submit(
            ExecutionPlanDraft(image_key="approved", script_content=source), **context,
        )
    failure = caught.value
    assert failure.script_hash == sha256(source.encode()).hexdigest()
    assert len(failure.diagnostics) == 1
    diagnostic = failure.diagnostics[0]
    assert diagnostic.code.value == "PYTHON_EXCEPTION"
    assert diagnostic.exception_type == exception_type
    assert diagnostic.script_line_numbers == lines
    assert [(item.line_number, item.start_column, item.end_column)
            for item in diagnostic.script_error_locations] == locations
    assert executor.plans == []
    assert boundary[5].list_refs() == ()


@pytest.mark.asyncio
async def test_parser_feedback_leaks_no_program_exception_text_or_path(boundary):
    service, _, context = _service(boundary)
    source = '# PRIVATE_PROGRAM /private/input token=PRIVATE_CREDENTIAL\nvalue = 1 2\n'
    with pytest.raises(ExecutionScriptValidationError) as caught:
        await service.submit(
            ExecutionPlanDraft(image_key="approved", script_content=source), **context,
        )
    failure = caught.value
    encoded = json.dumps({
        "script_hash": failure.script_hash,
        "diagnostics": [item.model_dump(mode="json") for item in failure.diagnostics],
        "safe_message": str(failure),
    })
    for forbidden in ("PRIVATE", "/private", "value", "<unknown>", "invalid syntax"):
        assert forbidden not in encoded


@pytest.mark.asyncio
@pytest.mark.parametrize("denied", ["principal", "input_selection"])
async def test_authorization_rejects_before_syntax_inspection(boundary, monkeypatch, denied):
    service, executor, context = _service(boundary)
    draft = ExecutionPlanDraft(image_key="approved", script_content="value = )\n")
    expected = PermissionError
    if denied == "principal":
        context["principal"] = context["principal"].model_copy(update={"user_id": "another-user"})
    else:
        draft = draft.model_copy(update={"input_artifact_ids": (uuid4(),)})
        context["mountable_input_artifact_ids"] = ()
        expected = ExecutionInputSelectionError

    def unexpected_parse(*args, **kwargs):
        pytest.fail("Unauthorized request reached the Python parser")

    monkeypatch.setattr(ast, "parse", unexpected_parse)
    with pytest.raises(expected):
        await service.submit(draft, **context)
    assert executor.plans == []


@pytest.mark.asyncio
async def test_valid_neighbor_is_submitted_unchanged_to_async_executor(boundary):
    class AsyncExecutor(MockExecutor):
        async def execute(self, plan):
            return super().execute(plan)

    executor = AsyncExecutor(boundary[5])
    service, _, context = _service(boundary, executor)
    source = "value = 12\n"
    receipt = await service.submit(
        ExecutionPlanDraft(image_key="approved", script_content=source), **context,
    )
    assert receipt.status.value == "SUCCEEDED"
    assert receipt.diagnostics == ()
    assert len(executor.plans) == 1
    assert executor.plans[0].script_content == source


def test_script_validation_error_keeps_no_argument_compatibility():
    failure = ExecutionScriptValidationError()
    assert failure.script_hash is None
    assert failure.diagnostics == ()


@pytest.mark.parametrize("line, start, end_line, end, expected_lines", [
    (None, None, None, None, ()),
    (0, 1, 0, 2, ()),
    (3, 1, 3, 2, ()),
    (1, 0, 1, 2, (1,)),
    (1, 1, 2, 2, (1,)),
    (1, 1, 1, 1000, (1,)),
])
def test_parser_coordinates_outside_source_are_not_manufactured(
    line, start, end_line, end, expected_lines,
):
    error = SyntaxError("PRIVATE_ERROR_MESSAGE")
    error.lineno, error.offset = line, start
    error.end_lineno, error.end_offset = end_line, end
    diagnostic = ExecutionSubmissionService._syntax_diagnostic(error, "pass\n")
    assert diagnostic.script_line_numbers == expected_lines
    assert diagnostic.script_error_locations == ()
    assert "PRIVATE" not in diagnostic.model_dump_json()
