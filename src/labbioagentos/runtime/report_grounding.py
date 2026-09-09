"""Compact REPORT control output, grounded in current submission receipts."""

import json
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from labbioagentos.contracts import InformationAuthority, WorkflowStage

from .contracts import (
    CapabilityEvidenceBundle, CapabilityEvidenceStatus, ReportStageBody,
    RuntimeReference, RuntimeReferenceKind, RuntimeStageInput, RuntimeStageResult, ShortText,
)
from .reporting import ReportReceipt


class ReportGroundingError(ValueError):
    """A fixed safe error; the rejected identity is not exposed."""

    def __init__(self):
        super().__init__("Selected report lacks a current completed submission receipt.")


def requires_report_grounding(stage_input: RuntimeStageInput) -> bool:
    return stage_input.stage_id is WorkflowStage.REPORT and "report_submit" in stage_input.allowed_capabilities


def report_result_control(
    stage_input: RuntimeStageInput, evidence: CapabilityEvidenceBundle | None,
) -> dict:
    if evidence is not None and (
        evidence.run_id != stage_input.run_id or evidence.stage_id is not stage_input.stage_id
        or evidence.invocation_id != stage_input.invocation_id
    ):
        raise ValueError("Report evidence does not match the current invocation")
    receipts = []
    for item in evidence.items if evidence is not None else ():
        if item.capability_name != "report_submit" or item.status is not CapabilityEvidenceStatus.COMPLETED:
            continue
        if item.information_authority is not InformationAuthority.AUTHORITATIVE_EVIDENCE:
            raise ValueError("Report receipt requires authoritative evidence")
        try:
            receipt = ReportReceipt.model_validate_json(json.dumps(item.safe_result))
        except (ValidationError, TypeError, ValueError):
            raise ValueError("Completed report evidence requires a valid typed receipt") from None
        if receipt.status != "REGISTERED":
            raise ValueError("Completed report evidence requires a registered report")
        receipts.append({"capability_invocation_id": str(item.capability_invocation_id),
            "report_artifact_id": str(receipt.report_artifact_id), "status": receipt.status})
    return {"authority": "CONTROL_STATE", "completed_report_receipts": receipts,
        "result_contract": (
            "The response is a compact handoff: one short summary, a selected current report "
            "Artifact UUID or null, and the next action. Null remains valid even after a report "
            "was registered. Report content and its citations remain in the saved report; "
            "registration does not establish scientific success or choose the next action."
        )}


def report_result_response_format(
    base_format: type[RuntimeStageResult], control: dict,
) -> type[BaseModel]:
    report_ids = [item["report_artifact_id"] for item in control["completed_report_receipts"]]
    report_identity = Annotated[UUID, Field(json_schema_extra={"enum": report_ids})] | None if report_ids else type(None)
    return create_model("CompactReportResult", __config__=ConfigDict(extra="forbid", frozen=True),
        summary=(ShortText, ...), report_artifact_id=(report_identity, ...),
        next_action=(base_format.model_fields["next_action"].annotation, ...))


def expand_report_result(compact: BaseModel, control: dict) -> RuntimeStageResult:
    """Adapt the model's decisions mechanically to the existing persisted format."""
    chosen = compact.report_artifact_id
    if chosen is not None and str(chosen) not in {
        item["report_artifact_id"] for item in control["completed_report_receipts"]
    }:
        raise ReportGroundingError()
    reference = RuntimeReference(reference_id=str(chosen), kind=RuntimeReferenceKind.ARTIFACT) if chosen is not None else None
    return RuntimeStageResult.model_validate({
        "stage_id": WorkflowStage.REPORT, "summary": compact.summary,
        "body": ReportStageBody(report_summary=compact.summary, report_reference=reference),
        "references": (reference,) if reference is not None else (),
        "next_action": compact.next_action.model_dump(mode="python"),
    })
