"""Necessary current-output content reading before VALIDATE can advance."""
import json

from pydantic import create_model

from labbioagentos.artifacts import ArtifactNotFoundError
from labbioagentos.contracts import (
    NextAction, WorkflowStage, governed_next_action_proposal_format,
)
from labbioagentos.execution.models import ExecutionReceipt, ExecutionStatus
from labbioagentos.execution.submission import (
    ExecutionInspectionError, ExecutionSubmissionPersistenceError,
)
from labbioagentos.governance import AuthorizationDenied


_FORWARD = frozenset({WorkflowStage.INTERPRET, WorkflowStage.REPORT, WorkflowStage.LEARN})


class ValidationGroundingError(ValueError):
    error_code = "VALIDATION_CURRENT_CONTENT_NOT_READ"
    field_path = "next_action"

    def __init__(self):
        super().__init__("Current output content has not been delivered for validation.")


def requires_validation_output_grounding(stage_input):
    return (stage_input.stage_id is WorkflowStage.VALIDATE
        and stage_input.execution_capability is not None
        and stage_input.execution_capability.minimum_queryable_output_count > 0
        and stage_input.last_execution_activity is not None)


def is_result_content_read(receipt):
    kind = receipt.scope.get("kind")
    return (
        receipt.source_identity == "ARTIFACT_REPRESENTATION"
        and kind in {"ARTIFACT_SUMMARY", "TABLE_PAGE", "TABLE_AGGREGATE"}
    ) or (receipt.source_identity == "GENERATED_FILE" and kind == "FILE_TEXT_PAGE")


def validation_result_control(stage_input, evidence, *, services, principal, workspace):
    """Use host activity/verified receipts and host-attested consumed read scopes."""
    if evidence is not None and (
        evidence.run_id != stage_input.run_id or evidence.stage_id is not stage_input.stage_id
        or evidence.invocation_id != stage_input.invocation_id
    ):
        raise ValueError("Validation evidence does not match the current invocation")
    required = requires_validation_output_grounding(stage_input)
    execution_ids, output_ids = set(), set()
    if required:
        activity = stage_input.last_execution_activity
        if activity.run_id != stage_input.run_id:
            raise ValueError("Execution activity belongs to another run")
        service = services.execution_submission
        for entry in activity.receipts:
            if entry.status is not ExecutionStatus.SUCCEEDED or service is None:
                continue
            try:
                details = service.inspect(entry.execution_id, principal=principal,
                    workspace=workspace, run_id=stage_input.run_id, source_limit=1,
                    imported_source_artifact_ids=())
                receipt = ExecutionReceipt.model_validate_json(json.dumps(details["receipt"]))
                if (details["producer_run_id"] != str(stage_input.run_id)
                        or details["execution_id"] != str(entry.execution_id)
                        or receipt.execution_id != entry.execution_id
                        or receipt.status is not ExecutionStatus.SUCCEEDED):
                    continue
            except ExecutionSubmissionPersistenceError:
                raise  # Never reinterpret an unconfirmed effect as missing read credit.
            except (ExecutionInspectionError, AuthorizationDenied, ArtifactNotFoundError,
                    ValueError, TypeError, KeyError, OSError):
                continue
            execution_ids.add(str(receipt.execution_id))
            output_ids.update(map(str, receipt.output_artifact_ids))
    reads = ()
    if required and evidence is not None:
        from .tooling import LabBioRuntimeToolSet, RuntimeCapabilityContext

        verifier = LabBioRuntimeToolSet(RuntimeCapabilityContext(
            principal=principal, workspace=workspace, run_id=stage_input.run_id,
            stage_id=stage_input.stage_id, invocation_id=stage_input.invocation_id,
            actor_profile_key="validation-control", actor_agent_name="ValidationControl",
            capability_allowlist=stage_input.allowed_capabilities,
            context_references=(*stage_input.model_context_references,
                *stage_input.authoritative_evidence_references),
        ), services)
        reads = verifier.reauthorize_artifact_reads(
            evidence.validated_artifact_reads, items=evidence.items)
    read_ids = {
        str(receipt.artifact_id)
        for receipt in reads
        if receipt.delivered_invocation_id == stage_input.invocation_id
        and str(receipt.artifact_id) in output_ids and is_result_content_read(receipt)
    }
    return {
        "authority": "CONTROL_STATE", "required": required,
        "current_execution_ids": sorted(execution_ids),
        "current_queryable_output_ids": sorted(output_ids),
        "delivered_current_content_artifact_ids": sorted(read_ids),
        "satisfied": not required or bool(read_ids),
        "workflow_control": (stage_input.workflow_control.model_dump(mode="python")
            if stage_input.workflow_control is not None else None),
        "result_contract": (
            "Before forwarding VALIDATE to INTERPRET, REPORT, LEARN or finish, consume "
            "an authorized SUMMARY/TOP_N, aggregate or file-content read of at least one current "
            "queryable output. Receipt identity, source inspection, metadata, schema, "
            "historical outputs and prose do not replace current content. A prior "
            "observation qualifies only after full redelivery and reauthorization to "
            "this actor. The Agent chooses the output, methods and verdict. This is "
            "a necessary reading check, never proof of scientific correctness. Failure, "
            "rework, retry, clarification and gates remain available under workflow control."
        ),
    }


def validation_result_response_format(base_format, control):
    if not control["required"] or control["satisfied"]:
        return base_format
    workflow = control["workflow_control"]
    # A legacy unbound action format was broad. Preserve its non-forward actions.
    workflow = workflow or {
        "transition_targets": tuple(WorkflowStage),
        "request_user_input_available": True,
        "retry_available": True, "retry_transition_targets": tuple(WorkflowStage),
        "clarification_available": True, "continue_stage_available": True,
        "clarification_followup_ids": (),
    }
    retry_targets = tuple(target for target in workflow["retry_transition_targets"]
        if target not in _FORWARD)
    action = governed_next_action_proposal_format(
        transition_targets=tuple(target for target in workflow["transition_targets"]
            if target not in _FORWARD),
        request_user_input_available=workflow["request_user_input_available"],
        retry_available=workflow["retry_available"] and bool(retry_targets),
        retry_transition_targets=retry_targets,
        finish_available=False,
        clarification_available=workflow["clarification_available"],
        continue_stage_available=workflow["continue_stage_available"],
        clarification_followup_ids=tuple(workflow["clarification_followup_ids"]),
    )
    return create_model("ValidationContentGroundedResult", __base__=base_format,
        next_action=(action, ...))


def validate_validation_result(result, control):
    if control["required"] and not control["satisfied"] and (
        result.next_action.action is NextAction.FINISH
        or (result.next_action.action in {NextAction.TRANSITION, NextAction.RETRY}
            and result.next_action.target_stage in _FORWARD)
    ):
        raise ValidationGroundingError()
