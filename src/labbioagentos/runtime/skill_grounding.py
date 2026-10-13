"""Ground accepted Skill retrieval judgments in current capability evidence."""

from functools import lru_cache
from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import BaseModel, Field, RootModel, create_model
from pydantic_core import PydanticCustomError

from labbioagentos.contracts import WorkflowStage, _RequestUserInputActionProposal

from .contracts import (
    CapabilityEvidenceBundle, CapabilityEvidenceStatus, PlanStageBody,
    RuntimeStageInput, RuntimeStageResult, SkillAssessment,
)


class _SkillNotAssessed(SkillAssessment):
    status: Literal["NOT_ASSESSED"]
    search_capability_invocation_ids: tuple[UUID, ...] = Field(default=(), max_length=0)
    proposal_id: None = None


class _SkillReturnedCandidates(SkillAssessment):
    status: Literal["NO_SUITABLE_RETURNED_CANDIDATE"]
    search_capability_invocation_ids: tuple[UUID, ...] = Field(min_length=1, max_length=32)
    proposal_id: None = None


class _SkillUseProposed(SkillAssessment):
    status: Literal["USE_PROPOSED"]
    proposal_id: UUID


class _SkillUseNotPursued(_SkillUseProposed):
    status: Literal["USE_NOT_PURSUED"]


def requires_skill_assessment(stage_input: RuntimeStageInput) -> bool:
    return stage_input.stage_id is WorkflowStage.PLAN and bool(
        {"skill_search", "skill_propose_use", "skill_view"}
        .intersection(stage_input.allowed_capabilities)
    )


@lru_cache(maxsize=128)
def skill_assessment_response_format(
    base_format: type[RuntimeStageResult],
    search_ids: tuple[str, ...] = (),
    proposal_ids: tuple[str, ...] = (),
    *, required: bool = True,
    resolve_proposal: bool = False,
    pending_proposals: tuple[tuple[str, str], ...] = (),
) -> type[BaseModel]:
    # The provider sees the same current receipt identities as local validation.
    # Keep UUID types for Python/JSON response compatibility; enums constrain wire values.
    search_item = Annotated[UUID, Field(json_schema_extra={"enum": list(search_ids)})]
    variants = [] if resolve_proposal else [_SkillNotAssessed]
    if search_ids and not resolve_proposal:
        variants.append(create_model("CurrentSkillReturnedCandidates", __base__=_SkillReturnedCandidates,
            search_capability_invocation_ids=(tuple[search_item, ...], Field(min_length=1, max_length=32))))
    pending_ids = {identity for identity, _ in pending_proposals}
    ordinary_ids = tuple(identity for identity in proposal_ids if identity not in pending_ids)
    if ordinary_ids:
        proposal_item = Annotated[UUID, Field(json_schema_extra={"enum": list(ordinary_ids)})]
        variants.append(create_model("CurrentSkillUseProposed", __base__=_SkillUseProposed,
            search_capability_invocation_ids=(tuple[search_item, ...] if search_ids else tuple[UUID, ...],
                Field(default=(), max_length=32 if search_ids else 0)),
            proposal_id=(proposal_item, ...)))
    if proposal_ids:
        proposal_item = Annotated[UUID, Field(json_schema_extra={"enum": list(proposal_ids)})]
        variants.append(create_model("CurrentSkillUseNotPursued", __base__=_SkillUseNotPursued,
            search_capability_invocation_ids=(tuple[search_item, ...] if search_ids else tuple[UUID, ...],
                Field(default=(), max_length=32 if search_ids else 0)),
            proposal_id=(proposal_item, ...)))
    assessment_format = Union[tuple(variants)]
    body_format = create_model(
        "SkillGroundedPlanBody", __base__=PlanStageBody,
        skill_assessment=(assessment_format if required else assessment_format | None,
                          ... if required else None),
    )
    ordinary = create_model(
        "SkillGroundedPlanResult", __base__=base_format, body=(body_format, ...),
    )
    if not pending_proposals:
        return ordinary
    # Couple the model's use decision and exact approval action in the wire
    # contract, not only in a post-response validator. RootModel serializes the
    # chosen ordinary stage result unchanged; it does not choose an action.
    choices = [ordinary]
    for index, (identity, reference) in enumerate(pending_proposals):
        assessment = create_model(f"PendingSkillUse{index}", __base__=_SkillUseProposed,
            search_capability_invocation_ids=(tuple[search_item, ...] if search_ids else tuple[UUID, ...],
                Field(default=(), max_length=32 if search_ids else 0)),
            proposal_id=(Annotated[UUID, Field(json_schema_extra={"enum": [identity]})], ...))
        body = create_model(f"PendingSkillPlanBody{index}", __base__=PlanStageBody,
            skill_assessment=(assessment, ...))
        action = create_model(f"PendingSkillApprovalAction{index}", __base__=_RequestUserInputActionProposal,
            domain_reference_id=(Literal[reference], ...))
        choices.append(create_model(f"PendingSkillPlanResult{index}", __base__=base_format,
            body=(body, ...), next_action=(action, ...)))
    return create_model("SkillGatePlanResult", __base__=RootModel[Union[tuple(choices)]])


def skill_retrieval_control(
    stage_input: RuntimeStageInput,
    evidence: CapabilityEvidenceBundle | None,
) -> dict:
    if evidence is not None and (
        evidence.run_id != stage_input.run_id
        or evidence.stage_id is not stage_input.stage_id
        or evidence.invocation_id != stage_input.invocation_id
    ):
        raise ValueError("Skill evidence does not match the current invocation")
    completed = tuple(
        item for item in evidence.items
        if item.status is CapabilityEvidenceStatus.COMPLETED
    ) if evidence is not None else ()
    proposals = {}
    for decision in stage_input.gate_decisions:
        reference = decision.domain_reference_id or ""
        if not reference.startswith("skill-use:"):
            continue
        proposal_id = reference.removeprefix("skill-use:")
        proposals[proposal_id] = {
            "proposal_id": proposal_id, "domain_reference_id": reference,
            "status": "APPROVED" if decision.approved else "REJECTED",
            "authorization_id": decision.decision_reference_id if decision.approved else None,
        }
    for item in completed:
        value = item.safe_result
        if item.capability_name == "skill_propose_use" and isinstance(value, dict) and value.get("proposal_id"):
            identity = value["proposal_id"]
            proposals.setdefault(identity, {
                "proposal_id": identity,
                "domain_reference_id": value.get("domain_reference_id"),
                "status": "PENDING", "authorization_id": None,
            })
    accessed = [str(item.safe_result["authorization_id"]) for item in completed
        if item.capability_name == "skill_view" and isinstance(item.safe_result, dict)
        and item.safe_result.get("authorization_id")]
    return {
        "authority": "CONTROL_STATE",
        "use_proposals": list(proposals.values()),
        "accessed_authorization_ids": accessed,
        "completed_search_capability_invocation_ids": [
            str(item.capability_invocation_id) for item in completed
            if item.capability_name == "skill_search"
        ],
        "completed_use_proposals": [
            {"capability_invocation_id": str(item.capability_invocation_id),
             "proposal_id": item.safe_result["proposal_id"]}
            for item in completed
            if item.capability_name == "skill_propose_use"
            and isinstance(item.safe_result, dict)
            and isinstance(item.safe_result.get("proposal_id"), str)
        ],
        "assessment_contract": (
            "This invocation only: no completed search is not an empty library. "
            "NOT_ASSESSED is valid without a retrieval judgment. "
            "NO_SUITABLE_RETURNED_CANDIDATE requires completed search IDs and "
            "judges only returned candidates, not the entire library. "
            "USE_PROPOSED requires an actual completed proposal ID. "
            "A recorded use proposal cannot be replaced by NOT_ASSESSED or a no-candidate judgment. "
            "For PENDING use, USE_PROPOSED requires request_user_input with its exact domain_reference_id; "
            "USE_NOT_PURSUED explicitly records the decision not to pursue that proposal and its reason. "
            "For APPROVED use, the gate decision_reference_id is the authorization_id for skill_view, "
            "not the Skill UUID. Continuing with USE_PROPOSED requires a successful view of that authorization. "
            "A REJECTED proposal cannot be used. Authorized guidance stays MODEL_CONTEXT, not current evidence. "
            "These facts do not rank candidates or require Skill use."
        ),
    }


def validate_skill_assessment(
    result: RuntimeStageResult,
    stage_input: RuntimeStageInput,
    evidence: CapabilityEvidenceBundle | None,
) -> None:
    if not isinstance(result.body, PlanStageBody):
        return
    assessment = result.body.skill_assessment
    if assessment is None:
        if requires_skill_assessment(stage_input):
            raise PydanticCustomError("skill_assessment_required",
                "Skill-enabled PLAN requires a retrieval assessment")
        return
    control = skill_retrieval_control(stage_input, evidence)
    searches = set(control["completed_search_capability_invocation_ids"])
    if any(str(item) not in searches for item in assessment.search_capability_invocation_ids):
        raise PydanticCustomError("skill_search_receipt_not_current",
            "Skill assessment search reference lacks current completed evidence")
    proposals = {item["proposal_id"]: item for item in control["use_proposals"]}
    if proposals and assessment.status not in {"USE_PROPOSED", "USE_NOT_PURSUED"}:
        raise PydanticCustomError("skill_proposal_unaccounted",
            "A recorded use proposal requires an explicit use or not-pursued decision")
    if assessment.status not in {"USE_PROPOSED", "USE_NOT_PURSUED"}:
        return
    proposal = proposals.get(str(assessment.proposal_id))
    if proposal is None:
        raise PydanticCustomError("skill_proposal_receipt_not_current",
            "Skill assessment proposal lacks current completed evidence")
    if assessment.status == "USE_NOT_PURSUED":
        if result.next_action.action.value == "request_user_input" and result.next_action.domain_reference_id == proposal["domain_reference_id"]:
            raise PydanticCustomError("skill_not_pursued_requests_approval",
                "Not-pursued use cannot simultaneously request its approval")
        return
    if proposal["status"] == "PENDING":
        if (result.next_action.action.value != "request_user_input"
            or not proposal["domain_reference_id"]
            or result.next_action.domain_reference_id != proposal["domain_reference_id"]):
            raise PydanticCustomError("skill_pending_use_requires_gate",
                "Pending proposed use requires the exact user approval gate")
    elif proposal["status"] == "REJECTED":
        raise PydanticCustomError("skill_use_rejected", "Rejected use cannot guide this plan")
    elif proposal["authorization_id"] not in control["accessed_authorization_ids"]:
        raise PydanticCustomError("skill_authorized_guidance_not_read",
            "Proposed use of approved guidance requires its successful authorized view")
