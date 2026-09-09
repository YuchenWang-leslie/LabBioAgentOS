"""EXECUTE summaries select actual current receipts, not invented execution facts."""

import json
from types import MethodType, SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import create_model

from labbioagentos import (
    CapabilityEvidenceBundle, InMemoryTraceSink, PantheonRuntimeFactory, PantheonTypedStageInvoker,
    ResponseSchemaRef, RuntimeInvocationMode, RuntimeStageInput, RuntimeStageResult,
    RuntimeWorkspaceIdentifiers, RuntimeWorkflowControlView, RunTraceRecorder, TraceEventType, WorkflowStage,
)
from labbioagentos.contracts import InformationAuthority
from labbioagentos.execution.models import ExecutionReceipt, ExecutionStatus
from labbioagentos.runtime.contracts import CapabilityEvidenceItem, CapabilityEvidenceStatus
from labbioagentos.runtime.pantheon import PantheonRuntimeIntegrationError
from test_runtime_milestone_c2 import _catalog, _profile


def _input():
    return RuntimeStageInput(run_id=uuid4(), invocation_id=uuid4(),
        stage_id=WorkflowStage.EXECUTE, instruction="Work from current task evidence.",
        workspace=RuntimeWorkspaceIdentifiers(user_id="fixture-user", lab_id="fixture-lab", project_id="fixture-project"),
        allowed_capabilities=("execution_submit",),
        workflow_control=RuntimeWorkflowControlView(current_stage=WorkflowStage.EXECUTE,
            transition_targets=(WorkflowStage.VALIDATE,), request_user_input_available=False,
            retry_available=False, finish_available=False))


def _receipt(status=ExecutionStatus.FAILED, outputs=()):
    return ExecutionReceipt(execution_id=uuid4(), status=status, image_key="fixture-image",
        script_hash="a" * 64, exit_code=1 if status is ExecutionStatus.FAILED else 0,
        output_artifact_ids=outputs, issue_messages=("MODEL_CONTEXT_SENTINEL",))


def _bundle(stage_input, *receipts):
    return CapabilityEvidenceBundle(run_id=stage_input.run_id, stage_id=stage_input.stage_id,
        invocation_id=stage_input.invocation_id, items=tuple(CapabilityEvidenceItem(
            capability_invocation_id=uuid4(), actor_profile_key="coordinator",
            actor_agent_name="coordinator", capability_name="execution_submit",
            information_authority=InformationAuthority.AUTHORITATIVE_EVIDENCE,
            status=CapabilityEvidenceStatus.COMPLETED, safe_result=r.model_dump(mode="json"),
        ) for r in receipts))


def _ref(identity, kind="EXECUTION"):
    return {"reference_id": str(identity), "kind": kind}


def _payload(receipt=None):
    return {"stage_id": "EXECUTE", "summary": "Fixture outcome, not a scientific conclusion.",
        "body": {"kind": "EXECUTE",
            "execution_status": receipt.status.value if receipt else "NOT_EXECUTED",
            "execution_reference": _ref(receipt.execution_id) if receipt else None,
            "output_artifact_references": [_ref(i, "ARTIFACT") for i in receipt.output_artifact_ids] if receipt else []},
        "next_action": {"action": "transition", "target_stage": "VALIDATE"}}


async def _finalize(stage_input, evidence, payload, *, pre_return=False, observed=None, recorder=None):
    team, prompts = await PantheonRuntimeFactory(_catalog()).create_team(("coordinator",),
        invocation_mode=RuntimeInvocationMode.FINALIZE)

    async def run(_self, message, **kwargs):
        if observed is not None:
            observed.update(json.loads(message))
        chosen = payload(json.loads(message)) if callable(payload) else payload
        if pre_return:
            response = create_model("Response", result=(_self.team_agents[0].response_format, ...))
            return SimpleNamespace(content=response.model_validate_json(json.dumps({"result": chosen})).result)
        return SimpleNamespace(content=chosen)

    team.run = MethodType(run, team)
    return await PantheonTypedStageInvoker(team, profile=_profile(), prompt=prompts["coordinator"],
        response_schema=ResponseSchemaRef(), trace_recorder=recorder).invoke(stage_input, capability_evidence=evidence)


@pytest.mark.asyncio
@pytest.mark.parametrize("pre_return", [False, True])
async def test_real_failed_receipt_cannot_be_finalized_as_success(pre_return):
    stage_input, receipt = _input(), _receipt()
    payload = _payload(receipt)
    payload["body"]["execution_status"] = "SUCCEEDED"
    sink = InMemoryTraceSink()
    with pytest.raises(PantheonRuntimeIntegrationError) as caught:
        await _finalize(stage_input, _bundle(stage_input, receipt), payload,
                        pre_return=pre_return, recorder=RunTraceRecorder(sink))
    assert caught.value.error_code == "MALFORMED_RUNTIME_RESULT"
    assert caught.value.validation_error_field_paths
    assert caught.value.validation_error_types
    events = sink.read(stage_input.run_id)
    assert any(event.event_type is TraceEventType.FINALIZATION_PHASE_FAILED for event in events)
    assert not any(event.event_type in (TraceEventType.AGENT_COMPLETED,
        TraceEventType.FINALIZATION_PHASE_COMPLETED) for event in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", list(ExecutionStatus))
async def test_real_receipt_status_can_transition_validate_and_roundtrip(status):
    stage_input, receipt = _input(), _receipt(status, (uuid4(),) if status is ExecutionStatus.SUCCEEDED else ())
    observed = {}
    def select(visible):
        facts = visible["execution_result_control"]["completed_execution_receipts"]
        selected = facts[0]
        payload = _payload()
        payload["body"].update(execution_status=selected["status"],
            execution_reference=_ref(selected["execution_id"]),
            output_artifact_references=[_ref(i, "ARTIFACT") for i in selected["output_artifact_ids"]])
        return payload
    result = await _finalize(stage_input, _bundle(stage_input, receipt), select,
                             pre_return=True, observed=observed)
    assert result.body.execution_status == status.value
    assert result.body.execution_reference.reference_id == str(receipt.execution_id)
    assert tuple(r.reference_id for r in result.body.output_artifact_references) == tuple(map(str, receipt.output_artifact_ids))
    assert result.next_action.target_stage is WorkflowStage.VALIDATE
    assert RuntimeStageResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.asyncio
async def test_no_receipt_is_explicit_not_executed_and_null_reference():
    stage_input = _input()
    result = await _finalize(stage_input, None, _payload())
    assert result.body.execution_status == "NOT_EXECUTED"
    assert result.body.execution_reference is None
    payload = _payload(_receipt())
    with pytest.raises(PantheonRuntimeIntegrationError):
        await _finalize(stage_input, _bundle(stage_input), payload)


@pytest.mark.asyncio
async def test_failed_tool_request_is_not_an_execution_receipt():
    stage_input, receipt = _input(), _receipt()
    bundle = _bundle(stage_input, receipt)
    failed = bundle.items[0].model_copy(update={"status": CapabilityEvidenceStatus.FAILED,
        "safe_result": None, "error_code": "INVALID_EXECUTION_SCRIPT"})
    result = await _finalize(stage_input, bundle.model_copy(update={"items": (failed,)}), _payload())
    assert result.body.execution_status == "NOT_EXECUTED"


@pytest.mark.asyncio
async def test_multiple_receipts_allow_either_without_selecting_latest_or_success():
    stage_input = _input()
    receipts = (_receipt(), _receipt(ExecutionStatus.SUCCEEDED, (uuid4(),)))
    bundle = _bundle(stage_input, *receipts)
    for receipt in receipts:
        result = await _finalize(stage_input, bundle, _payload(receipt))
        assert result.body.execution_reference.reference_id == str(receipt.execution_id)
    payload = _payload(receipts[0])
    payload["body"]["output_artifact_references"] = [_ref(receipts[1].output_artifact_ids[0], "ARTIFACT")]
    with pytest.raises(PantheonRuntimeIntegrationError):
        await _finalize(stage_input, bundle, payload)
    with pytest.raises(PantheonRuntimeIntegrationError):
        await _finalize(stage_input, bundle, _payload())


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["execution_reference", "output_artifact_references", "issue_references", "references"])
async def test_execution_identity_cannot_masquerade_as_artifact(field):
    stage_input, receipt = _input(), _receipt()
    payload = _payload(receipt)
    bad = _ref(receipt.execution_id, "ARTIFACT")
    if field == "references":
        payload[field] = [bad]
    else:
        payload["body"][field] = bad if field == "execution_reference" else [bad]
    with pytest.raises(PantheonRuntimeIntegrationError) as caught:
        await _finalize(stage_input, _bundle(stage_input, receipt), payload)
    assert caught.value.validation_error_field_paths
    assert "skill_assessment" not in str(caught.value.validation_error_field_paths)


@pytest.mark.asyncio
async def test_stale_receipt_and_other_bundle_scope_are_rejected():
    stage_input, current, old = _input(), _receipt(), _receipt()
    with pytest.raises(PantheonRuntimeIntegrationError):
        await _finalize(stage_input, _bundle(stage_input, current), _payload(old))
    for field in ("run_id", "invocation_id", "stage_id"):
        evidence = _bundle(stage_input, current).model_copy(update={field:
            WorkflowStage.PLAN if field == "stage_id" else uuid4()})
        with pytest.raises(ValueError):
            await _finalize(stage_input, evidence, _payload(current))


@pytest.mark.asyncio
async def test_legacy_persistence_and_disabled_capability_remain_compatible():
    stage_input = _input().model_copy(update={"allowed_capabilities": ()})
    payload = _payload()
    payload["body"]["execution_status"] = "historical_free_form_status"
    result = await _finalize(stage_input, None, payload)
    assert RuntimeStageResult.model_validate_json(result.model_dump_json()) == result


def test_projection_is_bounded_and_does_not_promote_receipt_text():
    from labbioagentos.runtime.execution_grounding import execution_result_control
    stage_input, receipt = _input(), _receipt()
    bundle = _bundle(stage_input, receipt)
    control = execution_result_control(stage_input, bundle)
    assert control["authority"] == "CONTROL_STATE"
    assert control["completed_execution_receipts"] == [{
        "capability_invocation_id": str(bundle.items[0].capability_invocation_id),
        "execution_id": str(receipt.execution_id), "status": "FAILED", "output_artifact_ids": []}]
    assert "MODEL_CONTEXT_SENTINEL" not in json.dumps(control)
    assert not {"script_hash", "image_key", "issue_messages", "diagnostics", "stdout", "stderr"}.intersection(control)


@pytest.mark.parametrize("mutation", ["authority", "malformed"])
def test_corrupt_completed_receipt_is_not_silently_treated_as_no_execution(mutation):
    from labbioagentos.runtime.execution_grounding import execution_result_control
    stage_input, receipt = _input(), _receipt()
    bundle = _bundle(stage_input, receipt)
    update = {"information_authority": InformationAuthority.MODEL_CONTEXT} if mutation == "authority" else {"safe_result": {"status": "SECRET_SENTINEL"}}
    with pytest.raises(ValueError) as caught:
        execution_result_control(stage_input, bundle.model_copy(update={"items": (bundle.items[0].model_copy(update=update),)}))
    assert "SECRET_SENTINEL" not in str(caught.value)


def test_provider_normalized_schema_preserves_receipt_relation_and_next_action():
    from jsonschema import Draft202012Validator
    from openai.lib._parsing._completions import type_to_response_format_param
    from pantheon.utils.adapters.openai_adapter import _normalize_response_format
    from labbioagentos.runtime.execution_grounding import execution_result_control, execution_result_response_format
    stage_input = _input()
    receipts = (_receipt(), _receipt(ExecutionStatus.SUCCEEDED, (uuid4(),)))
    control = execution_result_control(stage_input, _bundle(stage_input, *receipts))
    base = ResponseSchemaRef().response_format(stage_input.stage_id, stage_input.workflow_control)
    wire_type = execution_result_response_format(base, control)
    response = create_model("Response", result=(wire_type, ...))
    wire = _normalize_response_format(response)
    assert wire == type_to_response_format_param(response)
    validator = Draft202012Validator(wire["json_schema"]["schema"])
    payload = {"result": wire_type.model_validate(_payload(receipts[0])).model_dump(mode="json")}
    assert not list(validator.iter_errors(payload))
    payload["result"]["body"]["execution_status"] = "SUCCEEDED"
    assert list(validator.iter_errors(payload))
    payload["result"]["body"]["execution_status"] = "FAILED"
    payload["result"]["next_action"]["target_stage"] = "INTERPRET"
    assert list(validator.iter_errors(payload))


def test_response_schemas_do_not_carry_receipts_across_invocations():
    from labbioagentos.runtime.execution_grounding import execution_result_control, execution_result_response_format
    stage_input = _input()
    receipt = _receipt()
    base = ResponseSchemaRef().response_format(stage_input.stage_id, stage_input.workflow_control)
    first = execution_result_response_format(base, execution_result_control(stage_input, _bundle(stage_input, receipt)))
    second = execution_result_response_format(base, execution_result_control(_input(), None))
    assert str(receipt.execution_id) in json.dumps(first.model_json_schema())
    assert str(receipt.execution_id) not in json.dumps(second.model_json_schema())
    assert str(receipt.execution_id) not in json.dumps(base.model_json_schema())


@pytest.mark.parametrize("mutation,code", [
    ("status", "execution_status_mismatch"),
    ("identity", "execution_receipt_not_current"),
    ("outputs", "execution_outputs_mismatch"),
    ("references", "execution_reference_kind_mismatch"),
    ("issue_references", "execution_issue_reference_kind_mismatch"),
])
def test_local_guard_independently_checks_stable_legacy_model(mutation, code):
    from labbioagentos.runtime.execution_grounding import ExecutionGroundingError, execution_result_control, validate_execution_result
    stage_input, receipt = _input(), _receipt()
    payload = _payload(receipt)
    if mutation == "status":
        payload["body"]["execution_status"] = "SUCCEEDED"
    elif mutation == "identity":
        payload["body"]["execution_reference"] = _ref(uuid4())
    elif mutation == "outputs":
        payload["body"]["output_artifact_references"] = [_ref(uuid4(), "ARTIFACT")]
    elif mutation == "references":
        payload["references"] = [_ref(receipt.execution_id, "ARTIFACT")]
    else:
        payload["body"]["issue_references"] = [_ref(receipt.execution_id, "ARTIFACT")]
    result = RuntimeStageResult.model_validate(payload)
    with pytest.raises(ExecutionGroundingError) as caught:
        validate_execution_result(result, execution_result_control(stage_input, _bundle(stage_input, receipt)))
    assert caught.value.error_code == code
    assert str(receipt.execution_id) not in str(caught.value)


@pytest.mark.asyncio
async def test_output_subset_and_unrelated_references_are_not_new_restrictions():
    stage_input, receipt = _input(), _receipt(ExecutionStatus.SUCCEEDED, (uuid4(), uuid4()))
    payload = _payload(receipt)
    payload["body"]["output_artifact_references"] = payload["body"]["output_artifact_references"][:1]
    payload["references"] = [_ref(uuid4(), "ARTIFACT"), _ref(receipt.execution_id)]
    payload["body"]["issue_references"] = [_ref(receipt.execution_id)]
    result = await _finalize(stage_input, _bundle(stage_input, receipt), payload)
    assert len(result.body.output_artifact_references) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["RESULT", "OTHER"])
@pytest.mark.parametrize("field", ["references", "issue_references"])
async def test_known_execution_identity_retains_its_kind_in_all_reference_lists(kind, field):
    stage_input, receipt = _input(), _receipt()
    payload = _payload(receipt)
    target = payload if field == "references" else payload["body"]
    target[field] = [_ref(receipt.execution_id, kind)]
    with pytest.raises(PantheonRuntimeIntegrationError):
        await _finalize(stage_input, _bundle(stage_input, receipt), payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("misstate", [True, False])
async def test_coordinator_records_and_transitions_only_after_grounded_finalization(misstate):
    from labbioagentos import (
        RuntimeCoordinatorService, StageRuntimeRegistry, StageRuntimeSpec,
        WorkflowEngine, runtime_workflow_definition,
    )
    receipt = _receipt()

    async def invoke(stage_input):
        payload = _payload(receipt)
        if misstate:
            payload["body"]["execution_status"] = "SUCCEEDED"
        return await _finalize(stage_input, _bundle(stage_input, receipt), payload)

    registry = StageRuntimeRegistry((StageRuntimeSpec(stage_id=WorkflowStage.EXECUTE,
        profile_key="coordinator", prompt_template_key="runtime-generic",
        capability_allowlist=("execution_submit",), invoker=invoke),))
    engine = WorkflowEngine(runtime_workflow_definition())
    coordinator = RuntimeCoordinatorService(engine, registry)
    run = engine.create_run(retry_limit=1)
    engine.start(run)
    for stage in (WorkflowStage.UNDERSTAND, WorkflowStage.PLAN, WorkflowStage.PREFLIGHT, WorkflowStage.EXECUTE):
        engine.transition(run, stage)
    if misstate:
        with pytest.raises(PantheonRuntimeIntegrationError):
            await coordinator.run_current_stage(run, instruction="Summarize the current technical evidence.")
        assert run.current_stage is WorkflowStage.EXECUTE
        assert not run.stage_results
        assert not coordinator.results(run.run_id)
    else:
        result = await coordinator.run_current_stage(run, instruction="Summarize the current technical evidence.")
        assert run.current_stage is WorkflowStage.VALIDATE
        assert len(run.stage_results) == 1
        assert coordinator.results(run.run_id) == (result,)
        assert result.body.execution_status == "FAILED"
