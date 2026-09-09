"""Compact REPORT control output selects only current real submission receipts."""

import json
from types import MethodType, SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import create_model

from labbioagentos import (
    CapabilityEvidenceBundle, InMemoryTraceSink, InformationAuthority,
    PantheonRuntimeFactory, PantheonTypedStageInvoker, ResponseSchemaRef,
    RunTraceRecorder, RuntimeInvocationMode, RuntimeStageInput, RuntimeStageResult,
    RuntimeWorkflowControlView, RuntimeWorkspaceIdentifiers, TraceEventType, WorkflowStage,
)
from labbioagentos.runtime.contracts import CapabilityEvidenceItem, CapabilityEvidenceStatus
from labbioagentos.runtime.pantheon import PantheonRuntimeIntegrationError
from labbioagentos.runtime.reporting import ReportReceipt, ReportSubmissionService
from test_runtime_milestone_b import _toolset, boundary
from test_runtime_milestone_c2 import _catalog, _profile


def _input():
    return RuntimeStageInput(run_id=uuid4(), invocation_id=uuid4(), stage_id=WorkflowStage.REPORT,
        instruction="Summarize the current reporting outcome.",
        workspace=RuntimeWorkspaceIdentifiers(user_id="fixture", lab_id="fixture", project_id="fixture"),
        allowed_capabilities=("report_submit",),
        workflow_control=RuntimeWorkflowControlView(current_stage=WorkflowStage.REPORT,
            transition_targets=(WorkflowStage.LEARN,), request_user_input_available=False,
            retry_available=False, finish_available=True))


def _bundle(stage_input, *receipts):
    return CapabilityEvidenceBundle(run_id=stage_input.run_id, stage_id=stage_input.stage_id,
        invocation_id=stage_input.invocation_id, items=tuple(CapabilityEvidenceItem(
            actor_profile_key="coordinator", actor_agent_name="CoordinatorAgent",
            capability_name="report_submit", information_authority=InformationAuthority.AUTHORITATIVE_EVIDENCE,
            status=CapabilityEvidenceStatus.COMPLETED, safe_result=r.model_dump(mode="json"),
        ) for r in receipts))


def _compact(report_id=None, *, action="transition"):
    return {"summary": "Agent-authored bounded handoff.",
        "report_artifact_id": str(report_id) if report_id is not None else None,
        "next_action": {"action": action, **({"target_stage": "LEARN"} if action == "transition" else {})}}


async def _finalize(stage_input, evidence, payload, *, pre_return=False, sink=None, observed=None):
    team, prompts = await PantheonRuntimeFactory(_catalog()).create_team(("coordinator",),
        invocation_mode=RuntimeInvocationMode.FINALIZE)

    async def run(_self, message, **kwargs):
        visible = json.loads(message)
        if observed is not None:
            observed.update(message=visible, schema=_self.team_agents[0].response_format.model_json_schema())
        chosen = payload(visible) if callable(payload) else payload
        if pre_return:
            response = create_model("Response", result=(_self.team_agents[0].response_format, ...))
            return SimpleNamespace(content=response.model_validate_json(json.dumps({"result": chosen})).result)
        return SimpleNamespace(content=chosen)

    team.run = MethodType(run, team)
    return await PantheonTypedStageInvoker(team, profile=_profile(), prompt=prompts["coordinator"],
        response_schema=ResponseSchemaRef(), trace_recorder=RunTraceRecorder(sink) if sink else None,
    ).invoke(stage_input, capability_evidence=evidence)


@pytest.mark.asyncio
@pytest.mark.parametrize("pre_return", [False, True])
async def test_compact_response_uses_visible_receipt_then_roundtrips_legacy_model(pre_return):
    stage_input = _input()
    receipt = ReportReceipt(report_artifact_id=uuid4())
    observed = {}

    def choose(visible):
        control = visible["report_result_control"]
        assert control["authority"] == "CONTROL_STATE"
        return _compact(control["completed_report_receipts"][0]["report_artifact_id"])

    result = await _finalize(stage_input, _bundle(stage_input, receipt), choose,
                             pre_return=pre_return, observed=observed)
    assert set(observed["schema"]["properties"]) == {"summary", "report_artifact_id", "next_action"}
    assert observed["schema"]["properties"]["summary"]["maxLength"] == 1000
    assert result.stage_id is WorkflowStage.REPORT
    assert result.summary == result.body.report_summary == _compact()["summary"]
    assert result.body.report_reference.reference_id == str(receipt.report_artifact_id)
    assert result.body.report_reference.kind.value == "ARTIFACT"
    assert result.references == (result.body.report_reference,)
    assert result.body.cited_references == ()
    assert result.next_action.target_stage is WorkflowStage.LEARN
    assert RuntimeStageResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.asyncio
@pytest.mark.parametrize("has_receipt", [False, True])
@pytest.mark.parametrize("action", ["transition", "finish"])
async def test_null_selection_never_forces_report_selection_or_action(has_receipt, action):
    stage_input = _input()
    receipts = (ReportReceipt(report_artifact_id=uuid4()),) if has_receipt else ()
    result = await _finalize(stage_input, _bundle(stage_input, *receipts), _compact(action=action))
    assert result.body.report_reference is None
    assert result.references == ()
    assert result.next_action.action.value == action


@pytest.mark.asyncio
async def test_any_current_receipt_can_be_chosen_without_latest_selection():
    stage_input = _input()
    receipts = (ReportReceipt(report_artifact_id=uuid4()), ReportReceipt(report_artifact_id=uuid4()))
    for receipt in receipts:
        result = await _finalize(stage_input, _bundle(stage_input, *receipts), _compact(receipt.report_artifact_id))
        assert result.body.report_reference.reference_id == str(receipt.report_artifact_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["foreign", "missing-receipt", "long-summary", "extra-full-body", "wrong-reference-shape", "illegal-action"])
async def test_rejected_control_output_never_emits_completion(invalid):
    stage_input = _input()
    receipt = ReportReceipt(report_artifact_id=uuid4())
    evidence = _bundle(stage_input, receipt)
    payload = _compact(receipt.report_artifact_id)
    if invalid == "foreign":
        payload["report_artifact_id"] = str(uuid4())
    elif invalid == "missing-receipt":
        evidence = _bundle(stage_input)
    elif invalid == "long-summary":
        payload["summary"] = "x" * 1001
    elif invalid == "extra-full-body":
        payload["body"] = {"report_summary": "UNTRUSTED_FULL_REPORT_SENTINEL"}
    elif invalid == "wrong-reference-shape":
        payload["report_artifact_id"] = {"reference_id": str(receipt.report_artifact_id), "kind": "EXECUTION"}
    else:
        payload["next_action"]["target_stage"] = "EXECUTE"
    sink = InMemoryTraceSink()
    with pytest.raises(PantheonRuntimeIntegrationError) as caught:
        await _finalize(stage_input, evidence, payload, sink=sink)
    assert caught.value.error_code == "MALFORMED_RUNTIME_RESULT"
    assert caught.value.validation_error_field_paths
    assert "UNTRUSTED_FULL_REPORT_SENTINEL" not in str(caught.value)
    events = sink.read(stage_input.run_id)
    assert any(e.event_type is TraceEventType.FINALIZATION_PHASE_FAILED for e in events)
    assert not any(e.event_type in (TraceEventType.AGENT_COMPLETED, TraceEventType.FINALIZATION_PHASE_COMPLETED) for e in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("pre_return", [False, True])
async def test_compact_errors_describe_only_the_effective_response_schema(pre_return):
    stage_input = _input()
    payload = _compact()
    payload["summary"] = "x" * 1001
    with pytest.raises(PantheonRuntimeIntegrationError) as caught:
        await _finalize(stage_input, _bundle(stage_input), payload, pre_return=pre_return)
    error = caught.value
    assert error.validation_error_field_paths == (("result.summary",) if pre_return else ("summary",))
    assert error.validation_error_types == ("string_too_long",)


@pytest.mark.asyncio
async def test_failed_submission_remains_null_not_a_registered_report():
    stage_input = _input()
    evidence = _bundle(stage_input, ReportReceipt(report_artifact_id=uuid4()))
    failed = evidence.items[0].model_copy(update={"status": CapabilityEvidenceStatus.FAILED,
        "safe_result": None, "error_code": "ARTIFACT_NOT_FOUND"})
    result = await _finalize(stage_input, evidence.model_copy(update={"items": (failed,)}), _compact())
    assert result.body.report_reference is None


@pytest.mark.asyncio
async def test_legacy_report_contract_is_unchanged_without_capability():
    stage_input = _input().model_copy(update={"allowed_capabilities": ()})
    payload = {"stage_id": "REPORT", "summary": "Legacy separate summary.",
        "body": {"kind": "REPORT", "report_summary": "Legacy longer report summary."},
        "next_action": {"action": "transition", "target_stage": "LEARN"}}
    result = await _finalize(stage_input, None, payload)
    assert result.summary != result.body.report_summary
    assert RuntimeStageResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.asyncio
async def test_actual_submission_receipt_finalizes_without_copying_or_changing_report(boundary):
    sink, recorder, access, _, workspace, store, _ = boundary
    toolset = _toolset(boundary, WorkflowStage.REPORT, ("report_submit",),
        report_submission=ReportSubmissionService(store, access, trace_recorder=recorder))
    stored_text = "SYNTHETIC_REPORT_CONTENT_NOT_FINALIZATION_CONTROL"
    submission = await toolset.report_submit("Synthetic title", stored_text, [])
    assert submission["success"]
    stage_input = _input().model_copy(update={"run_id": toolset.binding.run_id,
        "invocation_id": toolset.binding.invocation_id,
        "workspace": RuntimeWorkspaceIdentifiers(user_id=workspace.user_id,
            lab_id=workspace.lab_id, project_id=workspace.project_id)})
    evidence = CapabilityEvidenceBundle(run_id=stage_input.run_id, stage_id=stage_input.stage_id,
        invocation_id=stage_input.invocation_id, items=toolset.evidence_items())
    observed = {}

    def choose(visible):
        return _compact(visible["report_result_control"]["completed_report_receipts"][0]["report_artifact_id"])

    result = await _finalize(stage_input, evidence, choose, pre_return=True, sink=sink, observed=observed)
    report_id = result.body.report_reference.reference_id
    assert report_id == submission["data"]["report_artifact_id"]
    saved = store.load_for_view(report_id)
    assert saved.representation.stored_content == stored_text
    assert saved.representation.summary["evidence_artifact_ids"] == []
    assert stored_text not in json.dumps(observed)
    assert stored_text not in result.model_dump_json()
    assert stored_text not in "".join(event.model_dump_json() for event in sink.read(stage_input.run_id))


@pytest.mark.parametrize("mutation", ["run", "invocation", "stage", "authority", "status", "malformed"])
def test_receipt_projection_requires_exact_scope_and_real_registered_receipt(mutation):
    from labbioagentos.runtime.report_grounding import report_result_control
    stage_input = _input()
    evidence = _bundle(stage_input, ReportReceipt(report_artifact_id=uuid4()))
    if mutation in {"run", "invocation", "stage"}:
        field = {"run": "run_id", "invocation": "invocation_id", "stage": "stage_id"}[mutation]
        evidence = evidence.model_copy(update={field: WorkflowStage.EXECUTE if mutation == "stage" else uuid4()})
    else:
        update = {"information_authority": InformationAuthority.MODEL_CONTEXT} if mutation == "authority" else {
            "safe_result": {"report_artifact_id": str(uuid4()), "status": "SECRET_INVALID_STATUS"} if mutation == "status" else {"raw": "SECRET_PAYLOAD"}}
        evidence = evidence.model_copy(update={"items": (evidence.items[0].model_copy(update=update),)})
    with pytest.raises(ValueError) as caught:
        report_result_control(stage_input, evidence)
    assert "SECRET" not in str(caught.value)


def test_wire_schema_is_compact_preserves_next_action_and_does_not_leak_between_invocations():
    from jsonschema import Draft202012Validator
    from openai.lib._parsing._completions import type_to_response_format_param
    from pantheon.utils.adapters.openai_adapter import _normalize_response_format
    from labbioagentos.runtime.report_grounding import report_result_control, report_result_response_format
    stage_input, receipt = _input(), ReportReceipt(report_artifact_id=uuid4())
    evidence = _bundle(stage_input, receipt)
    control = report_result_control(stage_input, evidence)
    assert control["completed_report_receipts"] == [{"capability_invocation_id": str(evidence.items[0].capability_invocation_id),
        "report_artifact_id": str(receipt.report_artifact_id), "status": "REGISTERED"}]
    base = ResponseSchemaRef().response_format(stage_input.stage_id, stage_input.workflow_control)
    wire_type = report_result_response_format(base, control)
    response = create_model("Response", result=(wire_type, ...))
    wire = _normalize_response_format(response)
    assert wire == type_to_response_format_param(response)
    schema = wire["json_schema"]["schema"]
    validator = Draft202012Validator(schema)
    payload = {"result": wire_type.model_validate(_compact(receipt.report_artifact_id)).model_dump(mode="json")}
    assert not list(validator.iter_errors(payload))
    payload["result"]["report_artifact_id"] = str(uuid4())
    assert list(validator.iter_errors(payload))
    payload["result"]["report_artifact_id"] = None
    assert not list(validator.iter_errors(payload))
    other = report_result_response_format(base, report_result_control(_input(), None))
    assert str(receipt.report_artifact_id) not in json.dumps(other.model_json_schema())
    assert str(receipt.report_artifact_id) not in json.dumps(base.model_json_schema())
