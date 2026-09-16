"""Scoped original-program inspection, without execution or RAW discovery."""

from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import socket
from uuid import uuid4

import pytest
from pantheon.providers import LocalProvider

from labbioagentos import (
    ArtifactRepresentation, AuthorizationDenied, CAPABILITY_CEILINGS, ExecutionPlanDraft,
    ExecutionSubmissionService, LabBioRuntimeToolSet, Principal, Project,
    TraceEventType, WorkflowStage, WorkspaceContext,
)
from labbioagentos.execution import ExecutionSubmissionError
from test_runtime_milestone_b import MockExecutor, _toolset, boundary  # noqa: F401


class OriginalScriptExecutor(MockExecutor):
    def __init__(self, store, root):
        super().__init__(store)
        self.root = root
        self.script_refs = {}

    def execute(self, plan):
        result = super().execute(plan)
        path = self.root / f"{plan.execution_id}.py"
        path.write_bytes(plan.script_content.encode("utf-8"))
        script_ref = self.store.register_file(
            path, artifact_type="execution-script", exposure_class=result.script_ref.exposure_class,
            representation=ArtifactRepresentation(),
            owner_user_id=plan.owner_user_id, project_id=plan.project_id,
            lab_id=plan.lab_id, run_id=plan.run_id, stage_id=plan.stage_id,
            producer_invocation_id=plan.invocation_id,
        )
        self.script_refs[plan.execution_id] = script_ref
        return result.model_copy(update={
            "script_ref": script_ref,
            "script_hash": sha256(plan.script_content.encode("utf-8")).hexdigest(),
        })


@pytest.fixture
def inspection(boundary, tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        pytest.fail("Inspection tests must not contact a provider or network")

    for name in ("connect", "connect_ex"):
        monkeypatch.setattr(socket.socket, name, no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    _, recorder, access, _, _, store, _ = boundary
    executor = OriginalScriptExecutor(store, tmp_path)
    service = ExecutionSubmissionService(
        artifact_store=store, access_service=access, executor=executor,
        trace_recorder=recorder,
    )
    return service, executor, uuid4()


async def _submit(inspection, boundary, source="pass\n"):
    service, _, run_id = inspection
    _, _, _, principal, workspace, _, _ = boundary
    return await service.submit(
        ExecutionPlanDraft(image_key="approved", script_content=source),
        principal=principal, workspace=workspace, run_id=run_id,
        stage_id=WorkflowStage.EXECUTE, invocation_id=uuid4(),
    )


def _inspect(inspection, boundary, execution_id, **kwargs):
    service, _, run_id = inspection
    _, _, _, principal, workspace, _, _ = boundary
    return service.inspect(
        execution_id, principal=principal, workspace=workspace, run_id=run_id, **kwargs,
    )


async def _inspection_provider(inspection, boundary):
    service, _, run_id = inspection
    tools = _toolset(boundary, WorkflowStage.EXECUTE, ("execution_inspect",), execution_submission=service)
    tools = LabBioRuntimeToolSet(replace(tools.binding, run_id=run_id), tools.services)
    provider = LocalProvider(tools)
    await provider.initialize()
    return tools, provider


@pytest.mark.asyncio
async def test_original_source_pages_preserve_unicode_newlines_hash_and_no_execution(
    inspection, boundary, monkeypatch,
):
    source = "# original café\r\npass\r\n# final\tcomment\r\n"
    receipt = await _submit(inspection, boundary, source)
    _, executor, _ = inspection
    monkeypatch.setattr(executor, "execute", lambda plan: pytest.fail("inspect executed a program"))
    pieces, offset = [], 0
    while offset < len(source):
        result = _inspect(inspection, boundary, receipt.execution_id, source_offset=offset, source_limit=7)
        assert result["receipt"] == receipt.model_dump(mode="json")
        page = result["submitted_program"]
        end = min(offset + 7, len(source))
        assert page == {
            "authority": "MODEL_CONTEXT", "script_hash": receipt.script_hash,
            "source_offset": offset, "source_end": end, "total_characters": len(source),
            "complete": end == len(source), "source": source[offset:end],
            "diagnostic_line_offsets": [],
            "diagnostic_source_lines": [], "diagnostic_source_lines_truncated": False,
        }
        pieces.append(page["source"])
        offset = page["source_end"]
    assert "".join(pieces).encode("utf-8") == source.encode("utf-8")
    assert receipt.script_hash == sha256(source.encode("utf-8")).hexdigest()
    last = _inspect(inspection, boundary, receipt.execution_id, source_offset=len(source))
    assert last["submitted_program"]["source"] == ""
    assert last["submitted_program"]["complete"] is True
    assert len(executor.plans) == 1


@pytest.mark.asyncio
async def test_revisions_keep_separate_originals(inspection, boundary):
    first = await _submit(inspection, boundary, "# first\npass\n")
    second = await _submit(inspection, boundary, "# second\npass\n")
    assert first.execution_id != second.execution_id
    assert first.script_hash != second.script_hash
    for receipt, source in ((first, "# first\npass\n"), (second, "# second\npass\n")):
        result = _inspect(inspection, boundary, receipt.execution_id)
        assert result["receipt"] == receipt.model_dump(mode="json")
        assert result["submitted_program"]["source"] == source


@pytest.mark.asyncio
async def test_diagnostic_lines_are_exact_bounded_and_not_page_coverage(inspection, boundary, monkeypatch):
    from labbioagentos import ExecutionDiagnostic, ExecutionDiagnosticCode
    service, executor, _ = inspection
    original = executor.execute
    # Synthetic diagnostic locations; no inference about runtime model repair.
    source = ("# café " + "x" * 600 + "\n") * 10 + "pass\n"
    diagnostic = ExecutionDiagnostic(code=ExecutionDiagnosticCode.PYTHON_EXCEPTION,
        exception_type="ValueError", script_line_numbers=tuple(range(1, 11)))
    monkeypatch.setattr(executor, "execute", lambda plan: original(plan).model_copy(
        update={"diagnostics": (diagnostic,)}))
    receipt = await _submit(inspection, boundary, source)
    page = _inspect(inspection, boundary, receipt.execution_id,
        source_offset=len(source), source_limit=1)["submitted_program"]
    assert page["source"] == "" and page["complete"]
    assert len(page["diagnostic_source_lines"]) == 8
    assert page["diagnostic_source_lines_truncated"]
    for line in page["diagnostic_source_lines"]:
        assert line["source"] == source[line["source_offset"]:line["source_end"]]
        assert len(line["source"]) == 512 and not line["complete"]
        assert line["line_end"] > line["source_end"]
    assert len(executor.plans) == 1


@pytest.mark.asyncio
async def test_default_and_maximum_page_sizes_are_bounded(inspection, boundary):
    source = "# original\n" * 3200 + "pass\n"
    receipt = await _submit(inspection, boundary, source)
    for kwargs, limit in (({}, 12000), ({"source_limit": 32000}, 32000)):
        page = _inspect(inspection, boundary, receipt.execution_id, **kwargs)["submitted_program"]
        assert page["source"] == source[:limit]
        assert page["source_end"] == limit and page["complete"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["user", "project", "lab", "run", "unknown"])
async def test_inspection_rejects_wrong_scope_and_unknown_identity(inspection, boundary, scope):
    receipt = await _submit(inspection, boundary)
    service, _, run_id = inspection
    _, _, access, principal, workspace, _, _ = boundary
    execution_id = receipt.execution_id
    if scope == "user":
        principal = Principal(user_id="user-b", lab_id="lab-a")
        workspace = WorkspaceContext(user_id="user-b", project_id="project-b", lab_id="lab-a")
    elif scope == "project":
        access.projects.register(Project(project_id="project-c", lab_id="lab-a", owner_user_id="user-a"))
        workspace = workspace.model_copy(update={"project_id": "project-c"})
    elif scope == "lab":
        principal = Principal(user_id="user-a", lab_id="lab-b")
        workspace = workspace.model_copy(update={"lab_id": "lab-b"})
    elif scope == "run":
        run_id = uuid4()
    else:
        execution_id = uuid4()
    with pytest.raises((AuthorizationDenied, ExecutionSubmissionError, ValueError)):
        service.inspect(execution_id, principal=principal, workspace=workspace, run_id=run_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [
    {"source_offset": -1}, {"source_offset": 6}, {"source_offset": True},
    {"source_limit": 0}, {"source_limit": -1}, {"source_limit": 32001},
    {"source_limit": True},
])
async def test_invalid_pagination_never_returns_source(inspection, boundary, arguments):
    receipt = await _submit(inspection, boundary)
    with pytest.raises((ExecutionSubmissionError, ValueError)):
        _inspect(inspection, boundary, receipt.execution_id, **arguments)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["same_size", "different_size", "symlink"])
async def test_original_file_tampering_is_rejected(inspection, boundary, mutation):
    receipt = await _submit(inspection, boundary)
    _, executor, _ = inspection
    path = Path(executor.script_refs[receipt.execution_id].storage_locator)
    if mutation == "symlink":
        replacement = path.parent / "replacement-source"
        replacement.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(replacement)
    else:
        path.write_bytes(b"exit\n" if mutation == "same_size" else b"pass\n# changed\n")
    with pytest.raises((AuthorizationDenied, ExecutionSubmissionError, ValueError)):
        _inspect(inspection, boundary, receipt.execution_id)


@pytest.mark.asyncio
async def test_fresh_service_cannot_reconstruct_registry_from_raw_artifacts(
    inspection, boundary, monkeypatch,
):
    receipt = await _submit(inspection, boundary)
    _, executor, run_id = inspection
    _, recorder, access, _, _, store, _ = boundary
    for name in ("list_refs", "get_ref", "load_for_view"):
        monkeypatch.setattr(store, name, lambda *args: pytest.fail("RAW discovery is forbidden"))
    fresh = ExecutionSubmissionService(
        artifact_store=store, access_service=access, executor=executor, trace_recorder=recorder,
    )
    with pytest.raises((ExecutionSubmissionError, ValueError)):
        _inspect((fresh, executor, run_id), boundary, receipt.execution_id)


@pytest.mark.asyncio
async def test_next_invocation_provider_gets_source_but_evidence_and_trace_do_not(
    inspection, boundary,
):
    sentinels = ("PRIVATE_SOURCE_CANARY", "/private/source-canary", "Bearer TOKEN_CANARY", "HIDDEN_REASONING_CANARY")
    source = "# " + " | ".join(sentinels) + "\npass\n"
    receipt = await _submit(inspection, boundary, source)
    service, executor, run_id = inspection
    original_invocation = executor.plans[0].invocation_id
    tools = _toolset(boundary, WorkflowStage.EXECUTE, ("execution_inspect",), execution_submission=service)
    tools = LabBioRuntimeToolSet(replace(tools.binding, run_id=run_id), tools.services)
    assert tools.binding.invocation_id != original_invocation
    provider = LocalProvider(tools)
    await provider.initialize()
    await provider.list_tools()
    result = await provider.call_tool("execution_inspect", {"execution_id": str(receipt.execution_id)})
    assert result["success"] is True
    assert result["data"]["submitted_program"]["source"] == source
    assert result["data"]["receipt"] == receipt.model_dump(mode="json")
    evidence = tools.evidence_items()
    events = boundary[0].read(run_id)
    persisted = json.dumps([item.model_dump(mode="json") for item in (*evidence, *events)])
    for sentinel in sentinels:
        assert sentinel not in persisted
    assert "storage_locator" not in persisted and "script_content" not in persisted
    assert len(evidence) == 1 and evidence[0].status.value == "COMPLETED"
    invoked = next(e for e in events if e.event_type is TraceEventType.CAPABILITY_INVOKED)
    completed = next(e for e in events if e.event_type is TraceEventType.CAPABILITY_COMPLETED)
    assert invoked.payload["capability_invocation_id"] == completed.payload["capability_invocation_id"]
    assert len(executor.plans) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["identifier", "offset", "limit", "unknown"])
async def test_tool_failures_remain_bounded_and_do_not_execute(inspection, boundary, invalid):
    receipt = await _submit(inspection, boundary, "# PRIVATE_SOURCE_CANARY\npass\n")
    service, executor, run_id = inspection
    tools = _toolset(boundary, WorkflowStage.EXECUTE, ("execution_inspect",), execution_submission=service)
    tools = LabBioRuntimeToolSet(replace(tools.binding, run_id=run_id), tools.services)
    params = {"execution_id": str(receipt.execution_id)}
    if invalid == "identifier":
        params["execution_id"] = "/private/INVALID_IDENTIFIER_CANARY"
    elif invalid == "unknown":
        params["execution_id"] = str(uuid4())
    else:
        params["source_" + invalid] = -1
    result = await tools.execution_inspect(**params)
    assert result["success"] is False and result["error"]["error_code"]
    evidence = tools.evidence_items()
    assert len(evidence) == 1 and evidence[0].status.value == "FAILED"
    encoded = json.dumps([result, *[item.model_dump(mode="json") for item in evidence],
                          *[event.model_dump(mode="json") for event in boundary[0].read(run_id)]])
    assert all(value not in encoded for value in (
        "PRIVATE_SOURCE_CANARY", "INVALID_IDENTIFIER_CANARY", "/private/", "storage_locator",
    ))
    assert len(executor.plans) == 1


@pytest.mark.asyncio
async def test_submission_failure_never_claims_inspection_did_not_execute(
    inspection, boundary, monkeypatch,
):
    service, executor, run_id = inspection

    def reject_result(*args):
        raise ExecutionSubmissionError("PRIVATE_SUBMISSION_FAILURE")

    monkeypatch.setattr(service, "_validate_result", reject_result)
    tools = _toolset(boundary, WorkflowStage.EXECUTE, ("execution_submit",), execution_submission=service)
    tools = LabBioRuntimeToolSet(replace(tools.binding, run_id=run_id), tools.services)
    result = await tools.execution_submit(image_key="approved", script_content="pass\n")
    assert result["success"] is False
    assert result["error"]["error_code"] == "CAPABILITY_FAILED"
    assert "inspection" not in result["error"]["safe_message"].lower()
    assert "no program" not in result["error"]["safe_message"].lower()
    assert len(executor.plans) == 1
    encoded = json.dumps([result, *[event.model_dump(mode="json") for event in boundary[0].read(run_id)]])
    assert "PRIVATE_SUBMISSION_FAILURE" not in encoded


def test_inspection_capability_is_limited_to_execute():
    assert "execution_inspect" in CAPABILITY_CEILINGS[WorkflowStage.EXECUTE]
    assert all("execution_inspect" not in tools for stage, tools in CAPABILITY_CEILINGS.items()
               if stage is not WorkflowStage.EXECUTE)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [
    "# CONTROL_SOURCE_CANARY " + chr(27) + "[31mred" + chr(27) + "[0m\npass\n",
    r"# CONTROL_SOURCE_CANARY literal \x1b[31m" + "\npass\n",
    "# CONTROL_SOURCE_CANARY data:image/png;base64,iVBORw0KGgo" + "A" * 120 + "\npass\n",
])
async def test_transport_unsupported_source_fails_without_release_or_completed_evidence(
    inspection, boundary, source,
):
    receipt = await _submit(inspection, boundary, source)
    tools, provider = await _inspection_provider(inspection, boundary)
    result = await provider.call_tool("execution_inspect", {"execution_id": str(receipt.execution_id)})
    assert result["success"] is False
    assert result["error"]["error_code"] == "EXECUTION_SOURCE_TRANSPORT_UNSUPPORTED"
    assert tools.evidence_items()[-1].status.value == "FAILED"
    encoded = json.dumps([result, *[item.model_dump(mode="json") for item in tools.evidence_items()],
        *[event.model_dump(mode="json") for event in boundary[0].read(inspection[2])]])
    assert "CONTROL_SOURCE_CANARY" not in encoded
    assert '"source":' not in encoded
    assert _inspect(inspection, boundary, receipt.execution_id)["submitted_program"]["source"] == source
    assert len(inspection[1].plans) == 1


@pytest.mark.asyncio
async def test_transport_large_escaped_page_can_be_requested_smaller_without_execution(
    inspection, boundary,
):
    source = "# " + "\\" * 31990 + "\npass\n"
    receipt = await _submit(inspection, boundary, source)
    tools, provider = await _inspection_provider(inspection, boundary)
    result = await provider.call_tool("execution_inspect", {
        "execution_id": str(receipt.execution_id), "source_limit": 32000,
    })
    assert result["success"] is False
    assert result["error"]["error_code"] == "EXECUTION_SOURCE_PAGE_TOO_LARGE"
    assert tools.evidence_items()[-1].status.value == "FAILED"
    assert '"source":' not in json.dumps(result)
    smaller = await provider.call_tool("execution_inspect", {
        "execution_id": str(receipt.execution_id), "source_limit": 1000,
    })
    assert smaller["success"] is True
    page = smaller["data"]["submitted_program"]
    assert page["source"] == source[:1000]
    assert page["complete"] is False and page["source_end"] == 1000
    assert page["script_hash"] == receipt.script_hash
    assert [item.status.value for item in tools.evidence_items()] == ["FAILED", "COMPLETED"]
    assert len(inspection[1].plans) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["# ASCII\npass\n", "# CRLF\r\npass\r\n", "# 中文注释\npass\n"])
async def test_supported_source_survives_actual_pantheon_pure_transport_filters(
    inspection, boundary, source,
):
    from pantheon.utils.llm import filter_base64_in_tool_result, filter_tool_messages

    receipt = await _submit(inspection, boundary, source)
    _, provider = await _inspection_provider(inspection, boundary)
    result = await provider.call_tool("execution_inspect", {"execution_id": str(receipt.execution_id)})
    assert result["success"] is True
    original = json.dumps(result, ensure_ascii=False)
    filtered = filter_base64_in_tool_result(json.loads(original))
    messages = filter_tool_messages([{"role": "tool", "content": json.dumps(filtered, ensure_ascii=False)}])
    assert messages[0]["content"] == original
    assert json.loads(messages[0]["content"])["data"]["submitted_program"]["source"] == source
    assert result["data"]["submitted_program"]["complete"] is True


@pytest.mark.asyncio
async def test_provider_inspection_schema_expresses_pagination_bounds(inspection, boundary):
    _, provider = await _inspection_provider(inspection, boundary)
    schema = next(item.inputSchema for item in await provider.list_tools()
                  if item.name == "execution_inspect")["parameters"]
    assert schema["required"] == ["execution_id"]
    props = schema["properties"]
    assert props["source_offset"]["type"] == "integer"
    assert props["source_offset"]["minimum"] == 0
    assert props["source_limit"]["type"] == "integer"
    assert props["source_limit"]["minimum"] == 1
    assert props["source_limit"]["maximum"] == 32000
