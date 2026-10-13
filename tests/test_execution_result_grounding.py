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
