"""Ground accepted Skill retrieval judgments in current capability evidence."""

from functools import lru_cache

from pydantic import create_model

from labbioagentos.contracts import WorkflowStage

from .contracts import (
    CapabilityEvidenceBundle, CapabilityEvidenceStatus, PlanStageBody,
    RuntimeStageInput, RuntimeStageResult, SkillAssessment,
)


def requires_skill_assessment(stage_input: RuntimeStageInput) -> bool:
    return stage_input.stage_id is WorkflowStage.PLAN and bool(
        {"skill_search", "skill_propose_use", "skill_view"}
        .intersection(stage_input.allowed_capabilities)
    )


@lru_cache(maxsize=128)
def skill_assessment_response_format(
    base_format: type[RuntimeStageResult],
) -> type[RuntimeStageResult]:
    body_format = create_model(
        "SkillGroundedPlanBody", __base__=PlanStageBody,
        skill_assessment=(SkillAssessment, ...),
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
            raise ValueError("Skill-enabled PLAN requires a retrieval assessment")
        return
    control = skill_retrieval_control(stage_input, evidence)
    searches = set(control["completed_search_capability_invocation_ids"])
    if any(str(item) not in searches for item in assessment.search_capability_invocation_ids):
        raise ValueError("Skill assessment search reference lacks current completed evidence")
    if assessment.status == "USE_PROPOSED" and str(assessment.proposal_id) not in {
        item["proposal_id"] for item in control["completed_use_proposals"]
    }:
        raise ValueError("Skill assessment proposal lacks current completed evidence")
