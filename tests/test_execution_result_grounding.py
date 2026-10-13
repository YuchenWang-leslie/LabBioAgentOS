"""EXECUTE summaries select actual current receipts, not invented execution facts."""

import json
from types import MethodType, SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import create_model

from labbioagentos import (
    CapabilityEvidenceBundle,
    RuntimeStageInput,
    RuntimeWorkspaceIdentifiers, RuntimeWorkflowControlView, WorkflowStage,
)
from labbioagentos.contracts import InformationAuthority
from labbioagentos.execution.models import ExecutionReceipt, ExecutionStatus
from labbioagentos.runtime.contracts import CapabilityEvidenceItem, CapabilityEvidenceStatus


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
