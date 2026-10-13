"""Carry selected, already accessed Gold to specialists without model paraphrase."""

from labbioagentos.contracts import WorkflowStage
from uuid import UUID
from .contracts import RuntimeSkillUseDecisionView
from .tooling import LabBioRuntimeToolSet, SKILL_SECTION_FIELDS


def gold_decisions_for_stage(stage_input, service, principal, workspace):
    """Resolve approved/rejected IDs to their exact subject without reading Gold."""
    if service is None:
        return stage_input.gate_decisions
    decisions = []
    for decision in stage_input.gate_decisions:
        reference = decision.domain_reference_id or ''
        if not reference.startswith('skill-use:'):
            decisions.append(decision)
            continue
        proposal = service.store.get_use_proposal(UUID(reference.removeprefix('skill-use:')))
        authorization = service.store.get_authorization(UUID(decision.decision_reference_id))
        scope = (stage_input.run_id, principal.user_id, workspace.project_id, principal.lab_id)
        if (proposal.run_id, proposal.requesting_user_id, proposal.project_id, proposal.lab_id) != scope or (
            authorization.run_id, authorization.authorized_user_id,
            authorization.project_id, authorization.lab_id) != scope:
            raise ValueError('Skill decision scope differs from the current stage')
        if (authorization.use_proposal_id, authorization.skill_id, authorization.skill_version,
            authorization.approved) != (proposal.proposal_id, proposal.skill_id,
                                       proposal.skill_version, decision.approved):
            raise ValueError('Skill decision does not match its recorded proposal')
        subject = RuntimeSkillUseDecisionView(proposal_id=proposal.proposal_id,
            skill_id=proposal.skill_id, skill_version=proposal.skill_version,
            proposed_mode=proposal.proposed_mode.value, proposed_deviations=proposal.proposed_deviations)
        decisions.append(decision.model_copy(update={'skill_use': subject}))
    return tuple(decisions)


def gold_context_for_stage(stage_input, service, principal, workspace) -> tuple[dict, ...]:
    if service is None:
        return ()
    plan = next((item for item in reversed(stage_input.prior_results)
                 if item.stage_id is WorkflowStage.PLAN), None)
    assessment = plan.model_body.get('skill_assessment') if plan is not None else None
    if not isinstance(assessment, dict) or assessment.get('status') != 'USE_PROPOSED':
        return ()
    contexts = []
    for access in service.store.accesses_for_run(stage_input.run_id):
        authorization = service.store.get_authorization(access.authorization_id)
        if str(authorization.use_proposal_id) != assessment.get('proposal_id'):
            continue
        # Revalidate exact run, user, project, approval and current Skill access.
        # Only a prior actual access can reach this point; approval alone cannot.
        skill = service.get_authorized_context(access.authorization_id,
            run_id=stage_input.run_id, project_id=workspace.project_id, principal=principal)
        proposal = service.store.get_use_proposal(authorization.use_proposal_id)
        guidance = LabBioRuntimeToolSet.skill_detail(skill, access.authorization_id).model_dump(mode='json')
        projection = {}
        if 'skill_view' in stage_input.allowed_capabilities:
            # Keep original workflow/applicability/limits visible. Only the Agent
            # chooses extra sections; no stage-to-scientific-method routing.
            retained = {
                'authority', 'skill_id', 'version', 'name', 'description', 'authorization_id',
                'source_run_id', 'parent_skill_id', 'parent_version', 'applicability',
                'workflow_outline', 'limitations', 'adaptation_points', 'truncated_fields',
            }
            guidance = {key: value for key, value in guidance.items() if key in retained}
            projection = {
                'projection': 'WORKFLOW_WITH_ON_DEMAND_SECTIONS',
                'section_catalog': {name: len(getattr(skill.procedure, field))
                                    for name, field in SKILL_SECTION_FIELDS.items()},
                'omitted_sections': [name for name in SKILL_SECTION_FIELDS if name not in guidance],
            }
        contexts.append({
            'authority': 'MODEL_CONTEXT', 'source_plan_result_id': str(plan.result_id),
            'access_id': str(access.access_id), 'authorization_id': str(access.authorization_id),
            'mode': proposal.proposed_mode.value,
            'proposed_deviations': list(proposal.proposed_deviations),
            'guidance': guidance, **projection,
        })
    return tuple(contexts)
