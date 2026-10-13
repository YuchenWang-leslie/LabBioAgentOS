"""Bind current EXECUTE finalization to real, typed execution receipts."""

import json
from typing import Literal, Union

from pydantic import Field, ValidationError, create_model

from labbioagentos.contracts import (
    InformationAuthority, NextAction, WorkflowStage,
    governed_next_action_proposal_format,
)
from labbioagentos.execution.models import ExecutionReceipt, ExecutionStatus

from .contracts import (
    CapabilityEvidenceBundle, CapabilityEvidenceStatus, ExecuteStageBody,
    RuntimeReference, RuntimeReferenceKind, RuntimeStageInput, RuntimeStageResult,
)


class ExecutionGroundingError(ValueError):
    """Fixed diagnostic codes and schema paths, never rejected values or prose."""

    _FIELDS = {
        "execution_receipt_not_current": "body.execution_reference",
        "execution_status_mismatch": "body.execution_status",
        "execution_outputs_mismatch": "body.output_artifact_references",
        "execution_reference_kind_mismatch": "references",
        "execution_issue_reference_kind_mismatch": "body.issue_references",
    }

    def __init__(self, code: str):
        self.error_code = code
        self.field_path = self._FIELDS[code]
        super().__init__("Execution result does not match current execution evidence.")


def requires_execution_grounding(stage_input: RuntimeStageInput) -> bool:
    return stage_input.stage_id is WorkflowStage.EXECUTE and bool(
        {"execution_submit", "execution_revise"}.intersection(stage_input.allowed_capabilities))


def execution_result_control(
    stage_input: RuntimeStageInput, evidence: CapabilityEvidenceBundle | None,
) -> dict:
    if evidence is not None and (
        evidence.run_id != stage_input.run_id or evidence.stage_id is not stage_input.stage_id
        or evidence.invocation_id != stage_input.invocation_id
    ):
        raise ValueError("Execution evidence does not match the current invocation")
    receipts = []
    for item in evidence.items if evidence is not None else ():
        if item.capability_name not in {"execution_submit", "execution_revise"} or item.status is not CapabilityEvidenceStatus.COMPLETED:
            continue
        if item.information_authority is not InformationAuthority.AUTHORITATIVE_EVIDENCE:
            raise ValueError("Execution receipt requires authoritative evidence")
        try:
            receipt = ExecutionReceipt.model_validate_json(json.dumps(item.safe_result))
        except (ValidationError, TypeError, ValueError):
            raise ValueError("Completed execution evidence requires a valid typed receipt") from None
        receipts.append({"capability_invocation_id": str(item.capability_invocation_id),
            "execution_id": str(receipt.execution_id), "status": receipt.status.value,
            "input_artifact_ids": list(map(str, receipt.input_artifact_ids)),
            "output_artifact_ids": list(map(str, receipt.output_artifact_ids))})
    minimum_queryable_output_count = (
        stage_input.execution_capability.minimum_queryable_output_count
        if stage_input.execution_capability is not None else 0
    )
    minimum_selected_input_count = int(
        minimum_queryable_output_count > 0
        and stage_input.execution_capability is not None
        and bool(stage_input.execution_capability.mountable_input_artifact_ids)
    )
    qualifying_execution_ids = [
        item["execution_id"] for item in receipts
        if item["status"] == ExecutionStatus.SUCCEEDED.value
        and len(item["input_artifact_ids"]) >= minimum_selected_input_count
        and len(item["output_artifact_ids"]) >= minimum_queryable_output_count
    ]
    return {
        "authority": "CONTROL_STATE",
        "completed_execution_receipts": receipts,
        "minimum_selected_input_count": minimum_selected_input_count,
        "minimum_queryable_output_count": minimum_queryable_output_count,
        "qualifying_execution_ids": qualifying_execution_ids,
        "workflow_control": (
            stage_input.workflow_control.model_dump(mode="python")
            if stage_input.workflow_control is not None else None
        ),
        "known_execution_ids": sorted({item["execution_id"] for item in receipts} | {
            item.reference_id for item in stage_input.authoritative_evidence_references
            if item.kind is RuntimeReferenceKind.EXECUTION}),
        "result_contract": (
            "Select any one current execution receipt to summarize, preserving its EXECUTION "
            "identity and exact technical status. Output ARTIFACT references may be a subset "
            "of that same receipt's output_artifact_ids. With no current receipt, use "
            "NOT_EXECUTED with null execution_reference and no output references. Execution "
            "identities retain kind EXECUTION, including in issue_references and references. "
            "A failed execution remains a valid technical outcome; this does not judge "
            "scientific results. When the minimum queryable output count is not satisfied "
            "by a successful current receipt, do not advance to a downstream stage. "
            "When the run has mountable task inputs and requires queryable output, the "
            "selected successful receipt must include at least one of those task inputs."
        ),
    }


def execution_result_response_format(
    base_format: type[RuntimeStageResult], control: dict,
) -> type[RuntimeStageResult]:
    variants = []
    qualifying_execution_ids = set(control["qualifying_execution_ids"])
    minimum_queryable_output_count = control["minimum_queryable_output_count"]
    for index, receipt in enumerate(control["completed_execution_receipts"]):
        if (minimum_queryable_output_count > 0 and qualifying_execution_ids
                and receipt["execution_id"] not in qualifying_execution_ids):
            continue
        execution_ref = create_model(f"CurrentExecutionReference{index}", __base__=RuntimeReference,
            kind=(Literal[RuntimeReferenceKind.EXECUTION], ...),
            reference_id=(Literal[receipt["execution_id"]], ...))
        output_ids = tuple(receipt["output_artifact_ids"])
        output_ref = RuntimeReference
        if output_ids:
            output_ref = create_model(f"CurrentExecutionOutputReference{index}", __base__=RuntimeReference,
                kind=(Literal[RuntimeReferenceKind.ARTIFACT], ...),
                reference_id=(Literal[output_ids], ...))
        variants.append(create_model(f"CurrentExecutionBody{index}", __base__=ExecuteStageBody,
            execution_status=(Literal[receipt["status"]], ...),
            execution_reference=(execution_ref, ...),
            output_artifact_references=(tuple[output_ref, ...],
                Field(default=(),
                    min_length=(minimum_queryable_output_count
                                if receipt["execution_id"] in qualifying_execution_ids else 0),
                    max_length=128 if output_ids else 0))))
    if not variants:
        variants.append(create_model("NotExecutedBody", __base__=ExecuteStageBody,
            execution_status=(Literal["NOT_EXECUTED"], ...), execution_reference=(type(None), None),
            output_artifact_references=(tuple[RuntimeReference, ...], Field(default=(), max_length=0))))
    fields = {"body": (Union[tuple(variants)], ...)}
    if (control["minimum_queryable_output_count"] > 0
            and not control["qualifying_execution_ids"]):
        workflow_control = control["workflow_control"] or {
            "current_stage": WorkflowStage.EXECUTE,
            "request_user_input_available": False,
            "retry_available": False,
            "retry_transition_targets": (),
            "clarification_available": False,
            "continue_stage_available": False,
            "clarification_followup_ids": (),
        }
        retry_targets = tuple(
            target for target in workflow_control["retry_transition_targets"]
            if target == workflow_control["current_stage"]
        )
        fields["next_action"] = (
            governed_next_action_proposal_format(
                transition_targets=(),
                request_user_input_available=workflow_control[
                    "request_user_input_available"
                ],
                retry_available=(
                    workflow_control["retry_available"] and bool(retry_targets)
                ),
                retry_transition_targets=retry_targets,
                finish_available=False,
                clarification_available=workflow_control["clarification_available"],
                continue_stage_available=workflow_control[
                    "continue_stage_available"
                ],
                clarification_followup_ids=tuple(
                    workflow_control["clarification_followup_ids"]
                ),
            ),
            ...,
        )
    return create_model("ExecutionGroundedResult", __base__=base_format, **fields)


def validate_execution_result(result: RuntimeStageResult, control: dict) -> None:
    body = result.body
    if not isinstance(body, ExecuteStageBody):
        return
    receipts = control["completed_execution_receipts"]
    if not receipts:
        if body.execution_reference is not None:
            raise ExecutionGroundingError("execution_receipt_not_current")
        if body.execution_status != "NOT_EXECUTED":
            raise ExecutionGroundingError("execution_status_mismatch")
        if body.output_artifact_references:
            raise ExecutionGroundingError("execution_outputs_mismatch")
    else:
        reference = body.execution_reference
        matching = [item for item in receipts if reference is not None
            and reference.kind is RuntimeReferenceKind.EXECUTION
            and item["execution_id"] == reference.reference_id]
        if not matching:
            raise ExecutionGroundingError("execution_receipt_not_current")
        matching = [item for item in matching if item["status"] == body.execution_status]
        if not matching:
            raise ExecutionGroundingError("execution_status_mismatch")
        if not any(all(ref.kind is RuntimeReferenceKind.ARTIFACT
            and ref.reference_id in item["output_artifact_ids"]
            for ref in body.output_artifact_references) for item in matching):
            raise ExecutionGroundingError("execution_outputs_mismatch")
    if (result.next_action.action is NextAction.TRANSITION
            and control["minimum_queryable_output_count"] > 0):
        reference = body.execution_reference
        if reference is None or reference.reference_id not in set(
            control["qualifying_execution_ids"]
        ) or len(body.output_artifact_references) < control[
            "minimum_queryable_output_count"
        ]:
            raise ExecutionGroundingError("execution_outputs_mismatch")
    known_execution_ids = set(control["known_execution_ids"])
    for references, code in ((result.references, "execution_reference_kind_mismatch"),
                            (body.issue_references, "execution_issue_reference_kind_mismatch")):
        if any(ref.kind is not RuntimeReferenceKind.EXECUTION and ref.reference_id in known_execution_ids
               for ref in references):
            raise ExecutionGroundingError(code)
