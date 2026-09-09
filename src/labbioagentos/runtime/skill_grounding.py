"""Ground accepted Skill retrieval judgments in current capability evidence."""

from functools import lru_cache
from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import Field, create_model
from pydantic_core import PydanticCustomError

from labbioagentos.contracts import WorkflowStage

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
) -> type[RuntimeStageResult]:
    # The provider sees the same current receipt identities as local validation.
    # Keep UUID types for Python/JSON response compatibility; enums constrain wire values.
    search_item = Annotated[UUID, Field(json_schema_extra={"enum": list(search_ids)})]
    variants = [_SkillNotAssessed]
    if search_ids:
        variants.append(create_model("CurrentSkillReturnedCandidates", __base__=_SkillReturnedCandidates,
            search_capability_invocation_ids=(tuple[search_item, ...], Field(min_length=1, max_length=32))))
    if proposal_ids:
        proposal_item = Annotated[UUID, Field(json_schema_extra={"enum": list(proposal_ids)})]
        variants.append(create_model("CurrentSkillUseProposed", __base__=_SkillUseProposed,
            search_capability_invocation_ids=(tuple[search_item, ...] if search_ids else tuple[UUID, ...],
                Field(default=(), max_length=32 if search_ids else 0)),
            proposal_id=(proposal_item, ...)))
    body_format = create_model(
        "SkillGroundedPlanBody", __base__=PlanStageBody,
        skill_assessment=(Union[tuple(variants)], ...),
    )
    return create_model(
        "SkillGroundedPlanResult", __base__=base_format, body=(body_format, ...),
    )


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
    return {
        "authority": "CONTROL_STATE",
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
    if assessment.status == "USE_PROPOSED" and str(assessment.proposal_id) not in {
        item["proposal_id"] for item in control["completed_use_proposals"]
    }:
        raise PydanticCustomError("skill_proposal_receipt_not_current",
            "Skill assessment proposal lacks current completed evidence")
