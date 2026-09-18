"""Opt-in real interpretation stage; no dataset, executor, or workflow replay."""

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from labbioagentos import CapabilityEvidenceBundle, RuntimeStageInput, WorkflowStage
from labbioagentos.local_config import build_application, load_settings, runtime_manifest
from labbioagentos.runtime.assembly import PerInvocationPantheonStageInvoker
from labbioagentos.runtime.contracts import RuntimeWorkflowControlView


def _wire_configuration(request):
    # The SDK's NOT_GIVEN sentinel denotes no tools in finalization.
    return {
        "thinking": request.get("extra_body", {}).get("thinking", request.get("thinking")),
        "max_tokens": request.get("max_tokens", request.get("max_completion_tokens")),
        "tools": [item["function"] for item in (request.get("tools") or [])],
    }


def _check_wire_budget(wire):
    assert sum(bool(item["tools"]) for item in wire) <= 8, "Capability smoke budget exhausted"
    assert sum(not item["tools"] for item in wire) <= 1, "Only one finalization request is allowed"


def _write_interpretation(output, result):
    # Extract the Agent's prose and references, without rewriting their content.
    (output / "INTERPRETATION.md").write_text("\n\n".join((
        result.summary, *result.body.findings, *result.body.hypotheses,
        *result.body.limitations,
        *("\n".join(filter(None, (ref.label, ref.reference_id))) for ref in result.references),
    )) + "\n")


def test_wire_observer_accepts_sdk_no_tools_without_serializing_messages():
    from openai import NOT_GIVEN
    for absent in (None, NOT_GIVEN, []):
        observed = _wire_configuration({"tools": absent, "messages": "PRIVATE_SENTINEL",
                                        "extra_body": {"thinking": {"type": "enabled"}}})
        assert observed["tools"] == []
        assert "PRIVATE_SENTINEL" not in json.dumps(observed)


def test_smoke_budget_reserves_finalization_without_expanding_tool_phase():
    wire = [{"tools": ["tool"]}] * 8
    _check_wire_budget([*wire, {"tools": []}])
    with pytest.raises(AssertionError, match="Capability"):
        _check_wire_budget([*wire, {"tools": ["tool"]}])
    with pytest.raises(AssertionError, match="finalization"):
        _check_wire_budget([*wire, {"tools": []}, {"tools": []}])


@pytest.mark.skipif(os.environ.get("LABBIO_INTERPRET_LIVE") != "1",
                    reason="Explicit interpretation-only live verification required")
@pytest.mark.asyncio
async def test_agent_searches_literature_with_thinking_and_finalizes(monkeypatch):
    from pantheon.utils.adapters.openai_adapter import OpenAIAdapter
    from pantheon.utils.log import logger

    logger.remove()  # Never expose provider exceptions, bodies, or private reasoning.
    output = Path(os.environ["LABBIO_INTERPRET_OUTPUT"])
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    settings = load_settings(Path(os.environ["LABBIO_INTERPRET_CONFIG"]))
    app = build_application(settings, output / "runtime")
    assembly = next(spec for spec in app.configuration.stage_assemblies
                    if spec.stage_id is WorkflowStage.INTERPRET)
    assert assembly.root_profile_key == "interpretation"
    stage_input = RuntimeStageInput(
        run_id=uuid4(), stage_id=WorkflowStage.INTERPRET,
        instruction=("请检索公开文献，简要讨论单细胞测序中仅凭一个 marker 做细胞类型注释的局限。"
                     "提供可核查的来源链接，并区分文献观点与本次数据结论。"
                     "本次没有提供分析数据，不要运行分析。"),
        workspace=settings.workspace.model_dump(), allowed_capabilities=assembly.capability_allowlist,
        workflow_control=RuntimeWorkflowControlView(
            current_stage=WorkflowStage.INTERPRET, transition_targets=(WorkflowStage.REPORT,),
            request_user_input_available=False, retry_available=False, finish_available=False,
        ),
    )
    wire = []
    make_client = OpenAIAdapter._make_client

    def observe_client(self, *args, **kwargs):
        client = make_client(self, *args, **kwargs)
        create = client.chat.completions.create

        async def observe_request(**request):
            wire.append(_wire_configuration(request))
            _check_wire_budget(wire)
            return await create(**request)

        monkeypatch.setattr(client.chat.completions, "create", observe_request)
        return client

    monkeypatch.setattr(OpenAIAdapter, "_make_client", observe_client)
    result, failure = None, None
    try:
        invoker = PerInvocationPantheonStageInvoker(
            assembly=assembly, factory=app.runtime_factory,
            principal=settings.principal, workspace=settings.workspace,
            services=app.capability_services, trace_recorder=app.trace_recorder,
            boundary_observer=app.configuration.boundary_observer,
        )
        result = await asyncio.wait_for(invoker.invoke(stage_input), timeout=360)
    except Exception as exc:
        failure = {"type": type(exc).__name__, "code": getattr(exc, "error_code", None),
                   "cause_type": type(exc.__cause__).__name__ if exc.__cause__ else None,
                   "field_paths": list(getattr(exc, "validation_error_field_paths", ())),
                   "error_types": list(getattr(exc, "validation_error_types", ()))}
    finally:
        app.run_state_store.close()
        rows = [json.loads(line) for line in (output / "runtime/model-boundaries.jsonl").read_text().splitlines()]
        evidence = [row["payload"] for row in rows if row["kind"] == "capability_evidence"]
        (output / "AUDIT.json").write_text(json.dumps({
            "scope": "ISOLATED_INTERPRETATION_LIVE_NOT_PBMC_WORKFLOW_ACCEPTANCE",
            "manifest": runtime_manifest(settings), "instruction": stage_input.instruction,
            "wire_configuration": wire, "capability_evidence": evidence, "failure": failure,
            "result": result.model_dump(mode="json") if result else None,
        }, ensure_ascii=False, indent=2) + "\n")
        if result is not None:
            _write_interpretation(output, result)
    assert failure is None
    assert wire and all(item["thinking"] == {"type": "enabled"} for item in wire)
    assert any(item["tools"] for item in wire) and not wire[-1]["tools"]
    items = [item for bundle in evidence for item in bundle["items"]]
    assert items and all(item["capability_name"] == "literature_search" for item in items)
    assert all(item["status"] == "COMPLETED" for item in items)
    urls = {article["url"] for item in items for article in item["safe_result"]["items"]}
    assert urls and any(url in result.model_dump_json() for url in urls)
    references = (*result.references, *result.body.evidence_references)
    assert references and all(ref.kind.value == "OTHER" and ref.reference_id in urls
                              for ref in references)
    assert result.stage_id is WorkflowStage.INTERPRET
    assert result.body.limitations
    assert result.next_action.target_stage is WorkflowStage.REPORT


@pytest.mark.skipif(os.environ.get("LABBIO_INTERPRET_FINALIZE_LIVE") != "1",
                    reason="Explicit single-request checkpoint finalization diagnostic required")
@pytest.mark.asyncio
async def test_finalize_completed_literature_checkpoint_without_retrieval(monkeypatch):
    from pantheon.utils.adapters.openai_adapter import OpenAIAdapter
    from pantheon.utils.log import logger
    from labbioagentos.literature import LiteratureSearchService

    logger.remove()
    source = Path(os.environ["LABBIO_INTERPRET_CHECKPOINT"])
    saved = json.loads((source / "AUDIT.json").read_text())
    rows = [json.loads(line) for line in (source / "runtime/model-boundaries.jsonl").read_text().splitlines()]
    stage = RuntimeStageInput.model_validate_json(json.dumps(next(
        row["payload"] for row in rows if row["kind"] == "stage_input")))
    evidence = CapabilityEvidenceBundle.model_validate_json(json.dumps(saved["capability_evidence"][-1]))
    settings = load_settings(Path(os.environ["LABBIO_INTERPRET_CONFIG"]))
    assert runtime_manifest(settings) == saved["manifest"], "Checkpoint runtime must be unchanged"
    output = Path(os.environ["LABBIO_INTERPRET_OUTPUT"])
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    app = build_application(settings, output / "runtime")
    assembly = next(spec for spec in app.configuration.stage_assemblies
                    if spec.stage_id is WorkflowStage.INTERPRET)
    monkeypatch.setattr(LiteratureSearchService, "search", lambda *a, **kw: pytest.fail("search replay"))
    wire = []
    make_client = OpenAIAdapter._make_client

    def observe_client(self, *args, **kwargs):
        client = make_client(self, *args, **kwargs)
        create = client.chat.completions.create

        async def observe_request(**request):
            wire.append(_wire_configuration(request))
            assert len(wire) == 1 and not wire[0]["tools"], "Finalization only"
            return await create(**request)

        monkeypatch.setattr(client.chat.completions, "create", observe_request)
        return client

    monkeypatch.setattr(OpenAIAdapter, "_make_client", observe_client)
    result, failure = None, None
    try:
        invoker = PerInvocationPantheonStageInvoker(
            assembly=assembly, factory=app.runtime_factory, principal=settings.principal,
            workspace=settings.workspace, services=app.capability_services,
            trace_recorder=app.trace_recorder, boundary_observer=app.configuration.boundary_observer,
        )
        result = await asyncio.wait_for(invoker.finalize_recovered(stage, evidence), timeout=240)
    except Exception as exc:
        failure = {"type": type(exc).__name__, "code": getattr(exc, "error_code", None)}
    finally:
        app.run_state_store.close()
        (output / "AUDIT.json").write_text(json.dumps({
            "scope": "ISOLATED_CHECKPOINT_FINALIZATION_NOT_FRESH_WORKFLOW_ACCEPTANCE",
            "manifest": runtime_manifest(settings), "run_id": str(stage.run_id),
            "invocation_id": str(stage.invocation_id), "wire_configuration": wire,
            "capability_evidence": evidence.model_dump(mode="json"), "failure": failure,
            "result": result.model_dump(mode="json") if result else None,
        }, ensure_ascii=False, indent=2) + "\n")
        if result is not None:
            _write_interpretation(output, result)
    assert failure is None
    assert wire[0]["thinking"] == {"type": "enabled"}
    urls = {article["url"] for item in evidence.items if item.status.value == "COMPLETED"
            for article in item.safe_result["items"]}
    references = (*result.references, *result.body.evidence_references)
    assert references and all(ref.kind.value == "OTHER" and ref.reference_id in urls for ref in references)
    assert result.body.limitations
    assert result.next_action.target_stage is WorkflowStage.REPORT
