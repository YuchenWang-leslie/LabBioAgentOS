"""EXECUTE summaries select actual current receipts, not invented execution facts."""

import json
from types import MethodType, SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError, create_model

from labbioagentos import (
    CapabilityEvidenceBundle, ExecutionRuntime, RequestedResources,
    RuntimeExecutionCapabilityView,
    RuntimeStageInput,
    RuntimeWorkspaceIdentifiers, RuntimeWorkflowControlView, WorkflowStage,
)
from labbioagentos.contracts import InformationAuthority
from labbioagentos.execution.models import ExecutionReceipt, ExecutionStatus
from labbioagentos.runtime.contracts import CapabilityEvidenceItem, CapabilityEvidenceStatus
from test_runtime_milestone_b import boundary  # noqa: F401


def _input(*, minimum_outputs=0, retry_available=False, mountable_inputs=()):
    return RuntimeStageInput(run_id=uuid4(), invocation_id=uuid4(),
        stage_id=WorkflowStage.EXECUTE, instruction="Work from current task evidence.",
        workspace=RuntimeWorkspaceIdentifiers(user_id="fixture-user", lab_id="fixture-lab", project_id="fixture-project"),
        allowed_capabilities=("execution_submit",),
        workflow_control=RuntimeWorkflowControlView(current_stage=WorkflowStage.EXECUTE,
            transition_targets=(WorkflowStage.VALIDATE,), request_user_input_available=False,
            retry_available=retry_available,
            retry_transition_targets=(WorkflowStage.EXECUTE, WorkflowStage.VALIDATE)
            if retry_available else (), finish_available=False),
        execution_capability=RuntimeExecutionCapabilityView(
            runtime=ExecutionRuntime.PYTHON, image_key="fixture-image",
            resources=RequestedResources(), network_required=False,
            minimum_queryable_output_count=minimum_outputs,
            mountable_input_artifact_ids=mountable_inputs,
        ))


def _receipt(status=ExecutionStatus.FAILED, outputs=(), inputs=()):
    return ExecutionReceipt(execution_id=uuid4(), status=status, image_key="fixture-image",
        script_hash="a" * 64, exit_code=1 if status is ExecutionStatus.FAILED else 0,
        input_artifact_ids=inputs, output_artifact_ids=outputs,
        issue_messages=("MODEL_CONTEXT_SENTINEL",))


def test_execution_receipt_preserves_selected_input_identities():
    selected = (uuid4(),)
    receipt = _receipt().model_copy(update={"input_artifact_ids": selected})

    assert receipt.model_dump(mode="json")["input_artifact_ids"] == [str(selected[0])]


def test_execution_control_preserves_mechanical_issue_codes_not_model_messages():
    from labbioagentos.runtime.execution_grounding import execution_result_control

    stage = _input()
    receipt = ExecutionReceipt.model_validate_json(json.dumps({**_receipt().model_dump(mode="json"),
        "issue_codes": ["OUTPUT_CONTRACT_FAILURE", "NON_ZERO_EXIT"],
        "issue_detail_codes": ["OUTPUT_NOT_FOUND"]}))
    control = execution_result_control(stage, _bundle(stage, receipt))
    facts = control["completed_execution_receipts"][0]
    assert facts["issue_codes"] == ["OUTPUT_CONTRACT_FAILURE", "NON_ZERO_EXIT"]
    assert facts["issue_detail_codes"] == ["OUTPUT_NOT_FOUND"]
    assert facts["execution_id"] != facts["capability_invocation_id"]
    assert "MODEL_CONTEXT_SENTINEL" not in json.dumps(control)


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


def _format(stage_input, *receipts):
    from labbioagentos.runtime.contracts import runtime_stage_result_format
    from labbioagentos.runtime.execution_grounding import (
        execution_result_control, execution_result_response_format,
    )

    control = execution_result_control(stage_input, _bundle(stage_input, *receipts))
    return execution_result_response_format(
        runtime_stage_result_format(stage_input.stage_id, stage_input.workflow_control),
        control,
    )


def test_required_output_without_receipt_cannot_advance_but_can_retry_or_fail():
    stage_input = _input(minimum_outputs=1, retry_available=True)
    response_format = _format(stage_input)

    with pytest.raises(ValidationError):
        response_format.model_validate(_payload())

    retry = _payload()
    retry["next_action"] = {"action": "retry", "target_stage": "EXECUTE"}
    assert response_format.model_validate(retry).next_action.root.action.value == "retry"

    fail = _payload()
    fail["next_action"] = {"action": "fail", "reason": "No task result was produced."}
    assert response_format.model_validate(fail).next_action.root.action.value == "fail"


def test_successful_receipt_with_required_output_can_advance():
    output_id = uuid4()
    receipt = _receipt(status=ExecutionStatus.SUCCEEDED, outputs=(output_id,))

    result = _format(_input(minimum_outputs=1), receipt).model_validate(
        _payload(receipt)
    )

    assert result.next_action.root.action.value == "transition"
    assert result.body.output_artifact_references[0].reference_id == str(output_id)


def test_revised_execution_survives_provider_json_round_trip():
    from labbioagentos.runtime.contracts import RuntimeStageResult

    failed = _receipt(outputs=(uuid4(),))
    succeeded = _receipt(status=ExecutionStatus.SUCCEEDED, outputs=(uuid4(), uuid4()))
    response_format = _format(_input(minimum_outputs=1), failed, succeeded)
    payload = _payload(succeeded)
    payload["body"]["output_artifact_references"] = payload["body"]["output_artifact_references"][:1]

    parsed = response_format.model_validate_json(json.dumps(payload), strict=True)
    dumped = parsed.model_dump(mode="json")

    assert len(dumped["body"]["output_artifact_references"]) == 1
    response_format.model_validate(dumped)
    RuntimeStageResult.model_validate(parsed.model_dump(mode="python"))


def test_required_output_omission_is_rejected_before_default_fills_it():
    receipt = _receipt(status=ExecutionStatus.SUCCEEDED, outputs=(uuid4(),))
    response_format = _format(_input(minimum_outputs=1), receipt)
    omitted = _payload(receipt)
    del omitted["body"]["output_artifact_references"]

    with pytest.raises(ValidationError) as raised:
        response_format.model_validate_json(json.dumps(omitted), strict=True)
    assert raised.value.errors(include_url=False)[0]["type"] == "missing"

    optional = _payload()
    del optional["body"]["output_artifact_references"]
    assert _format(_input(minimum_outputs=0)).model_validate_json(
        json.dumps(optional), strict=True
    ).body.output_artifact_references == ()


def test_data_bearing_run_cannot_advance_from_an_empty_input_execution():
    input_id = uuid4()
    output_id = uuid4()
    stage_input = _input(
        minimum_outputs=1,
        retry_available=True,
        mountable_inputs=(input_id,),
    )
    empty_input_receipt = _receipt(
        status=ExecutionStatus.SUCCEEDED,
        outputs=(output_id,),
    )

    with pytest.raises(ValidationError):
        _format(stage_input, empty_input_receipt).model_validate(
            _payload(empty_input_receipt)
        )

    selected_input_receipt = empty_input_receipt.model_copy(
        update={"input_artifact_ids": (input_id,)}
    )
    result = _format(stage_input, selected_input_receipt).model_validate(
        _payload(selected_input_receipt)
    )
    assert result.next_action.root.action.value == "transition"


def test_required_transition_must_select_the_qualifying_receipt_and_outputs():
    failed = _receipt(outputs=(uuid4(),))
    succeeded = _receipt(status=ExecutionStatus.SUCCEEDED, outputs=(uuid4(),))
    response_format = _format(_input(minimum_outputs=1), failed, succeeded)

    with pytest.raises(ValidationError):
        response_format.model_validate(_payload(failed))

    missing_output = _payload(succeeded)
    missing_output["body"]["output_artifact_references"] = []
    with pytest.raises(ValidationError):
        response_format.model_validate(missing_output)


def test_zero_required_outputs_preserves_not_executed_transition():
    result = _format(_input(minimum_outputs=0)).model_validate(_payload())

    assert result.next_action.root.action.value == "transition"
    assert result.body.execution_status == "NOT_EXECUTED"


def test_failed_receipt_does_not_satisfy_required_output_even_with_artifact():
    receipt = _receipt(outputs=(uuid4(),))

    with pytest.raises(ValidationError):
        _format(_input(minimum_outputs=1), receipt).model_validate(_payload(receipt))


@pytest.mark.parametrize("location", ["references", "issue_references"])
@pytest.mark.parametrize("identity_kind", ["observation", "artifact", "unknown"])
@pytest.mark.parametrize("boundary", ["schema", "host"])
def test_nonexecution_id_cannot_be_claimed_as_execution(location, identity_kind, boundary):
    from labbioagentos.runtime.contracts import RuntimeStageResult, runtime_stage_result_format
    from labbioagentos.runtime.execution_grounding import (ExecutionGroundingError,
        execution_result_control, execution_result_response_format, validate_execution_result)

    stage_input = _input(minimum_outputs=1)
    receipt = _receipt(ExecutionStatus.SUCCEEDED, outputs=(uuid4(),))
    bundle = _bundle(stage_input, receipt)
    control = execution_result_control(stage_input, bundle)
    identity = {"observation": bundle.items[0].capability_invocation_id,
                "artifact": receipt.output_artifact_ids[0], "unknown": uuid4()}[identity_kind]
    payload = _payload(receipt)
    target = payload if location == "references" else payload["body"]
    target[location] = [_ref(identity)]
    if boundary == "schema":
        response_format = execution_result_response_format(
            runtime_stage_result_format(stage_input.stage_id, stage_input.workflow_control), control)
        with pytest.raises(ValidationError):
            response_format.model_validate_json(json.dumps(payload), strict=True)
    else:
        result = RuntimeStageResult.model_validate_json(json.dumps(payload), strict=True)
        with pytest.raises(ExecutionGroundingError) as raised:
            validate_execution_result(result, control)
        assert raised.value.field_path == ("references" if location == "references" else "body.issue_references")


@pytest.mark.parametrize("location", ["references", "issue_references"])
def test_actual_failed_execution_and_nonexecution_links_remain_legal(location):
    from labbioagentos.runtime.contracts import RuntimeStageResult, runtime_stage_result_format
    from labbioagentos.runtime.execution_grounding import (execution_result_control,
        execution_result_response_format, validate_execution_result)

    stage_input = _input(minimum_outputs=1)
    failed = _receipt()
    succeeded = _receipt(ExecutionStatus.SUCCEEDED, outputs=(uuid4(),))
    bundle = _bundle(stage_input, failed, succeeded)
    control = execution_result_control(stage_input, bundle)
    payload = _payload(succeeded)
    target = payload if location == "references" else payload["body"]
    target[location] = [_ref(failed.execution_id), _ref(succeeded.output_artifact_ids[0], "ARTIFACT"),
                        _ref(bundle.items[0].capability_invocation_id, "OTHER")]
    response_format = execution_result_response_format(
        runtime_stage_result_format(stage_input.stage_id, stage_input.workflow_control), control)
    result = response_format.model_validate_json(json.dumps(payload), strict=True)
    validate_execution_result(RuntimeStageResult.model_validate(result.model_dump(mode="python")), control)


def test_no_receipt_does_not_allow_an_invented_execution_issue():
    payload = _payload()
    payload["body"]["issue_references"] = [_ref(uuid4())]
    with pytest.raises(ValidationError):
        _format(_input()).model_validate_json(json.dumps(payload), strict=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("repeated", [False, True])
async def test_native_finalizer_corrects_identity_or_stops_without_host_substitution(tmp_path, boundary, repeated):
    from labbioagentos import RuntimeIntegrationError
    from labbioagentos.kernel import FinishReason, ModelTurn, TokenUsage
    from labbioagentos.runtime.assembly import RuntimeStageAssemblySpec
    from labbioagentos.runtime.native_stage import NativeKernelStageInvoker, NativeRuntimeFactory
    from test_native_stage_application import WorkflowProvider, _capability_catalog
    from test_runtime_milestone_b import _toolset

    tools = _toolset(boundary, WorkflowStage.EXECUTE, ("execution_submit",))
    stage = _input(minimum_outputs=1).model_copy(update={
        "run_id": tools.binding.run_id, "invocation_id": tools.binding.invocation_id,
        "workspace": RuntimeWorkspaceIdentifiers(user_id=tools.binding.workspace.user_id,
            lab_id=tools.binding.workspace.lab_id, project_id=tools.binding.workspace.project_id)})
    failed = _receipt()
    succeeded = _receipt(ExecutionStatus.SUCCEEDED, outputs=(uuid4(),))
    evidence = _bundle(stage, failed, succeeded)
    wrong = _payload(succeeded)
    wrong["body"]["issue_references"] = [_ref(evidence.items[0].capability_invocation_id)]
    corrected = _payload(succeeded)
    corrected["body"]["issue_references"] = [_ref(failed.execution_id)]
    outputs = iter([wrong, wrong if repeated else corrected, corrected])
    calls = []

    class Finalizer(WorkflowProvider):
        async def generate(self, request):
            calls.append(request)
            return ModelTurn(content=json.dumps(next(outputs)), finish_reason=FinishReason.STOP,
                usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2))

    catalog = _capability_catalog()
    catalog.capabilities["coordinator-capabilities"] = catalog.capabilities[
        "coordinator-capabilities"].model_copy(update={"capability_allowlist": stage.allowed_capabilities})
    invoker = NativeKernelStageInvoker(assembly=RuntimeStageAssemblySpec(stage_id=stage.stage_id,
        root_profile_key="coordinator", prompt_template_key="runtime-generic",
        capability_allowlist=stage.allowed_capabilities),
        factory=NativeRuntimeFactory(catalog, lambda _model: Finalizer(calls)),
        principal=tools.binding.principal, workspace=tools.binding.workspace, services=tools.services,
        execution_capability=stage.execution_capability)
    original = evidence.model_dump_json()
    if repeated:
        with pytest.raises(RuntimeIntegrationError) as raised:
            await invoker.finalize_recovered(stage, evidence)
        assert raised.value.error_code == "MALFORMED_RUNTIME_RESULT"
    else:
        result = await invoker.finalize_recovered(stage, evidence)
        assert result.body.issue_references[0].reference_id == str(failed.execution_id)
    assert len(calls) == 2 and all(call.tools == () for call in calls)
    assert str(evidence.items[0].capability_invocation_id) not in calls[1].messages[-1].content
    assert evidence.model_dump_json() == original
