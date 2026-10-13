"""Approved, actually read guidance survives the Planner-to-specialist boundary."""

from uuid import UUID, uuid4

import pytest

from labbioagentos.runtime.contracts import RuntimePriorResultView
from labbioagentos.runtime.gold_handoff import gold_context_for_stage
from test_skill_retrieval_grounding import boundary, _pending_proposal  # noqa: F401
from test_c9_gold_skill_lifecycle import _decide_use


@pytest.mark.asyncio
@pytest.mark.parametrize('approved', [True, False])
async def test_resume_exposes_decided_skill_identity_without_reading_guidance(boundary, approved):
    from labbioagentos.runtime.contracts import RuntimeGateDecisionView
    from labbioagentos.runtime.gold_handoff import gold_decisions_for_stage
    tools, stage, _ = boundary
    response = await _pending_proposal(boundary)
    service = tools.services.skill_service
    proposal = service.store.get_use_proposal(UUID(response['proposal_id']))
    auth = _decide_use(service, tools.binding.principal, proposal, approved=approved)
    decision = RuntimeGateDecisionView(gate_id=proposal.approval_gate_id,
        source_stage=stage.stage_id, approved=approved,
        domain_reference_id=response['domain_reference_id'],
        decision_reference_id=str(auth.authorization_id))
    stage = stage.model_copy(update={'gate_decisions': (decision,)})
    args = (service, tools.binding.principal, tools.binding.workspace)
    enriched = gold_decisions_for_stage(stage, *args)
    visible = enriched[0].model_dump(mode='json')
    assert visible['skill_use']['skill_id'] == str(proposal.skill_id)
    assert visible['skill_use']['skill_version'] == proposal.skill_version
    assert visible['skill_use']['proposal_id'] == response['proposal_id']
    assert visible['skill_use']['proposed_mode'] == proposal.proposed_mode.value
    assert visible['decision_reference_id'] == str(auth.authorization_id)
    assert visible['approved'] is approved
    assert service.store.accesses_for_run(stage.run_id) == ()
    assert 'guidance' not in visible['skill_use']
    assert gold_decisions_for_stage(stage, None, *args[1:]) == stage.gate_decisions
    with pytest.raises(ValueError, match='scope'):
        gold_decisions_for_stage(stage.model_copy(update={'run_id': uuid4()}), *args)
    changed = decision.model_copy(update={'approved': not approved})
    with pytest.raises(ValueError, match='decision'):
        gold_decisions_for_stage(stage.model_copy(update={'gate_decisions': (changed,)}), *args)


@pytest.mark.asyncio
async def test_only_approved_read_and_selected_gold_is_handed_off(boundary):
    tools, stage, _ = boundary
    proposal = await _pending_proposal(boundary)
    service = tools.services.skill_service
    proposal = service.store.get_use_proposal(UUID(proposal['proposal_id']))
    auth = _decide_use(service, tools.binding.principal, proposal, approved=True)
    prior = RuntimePriorResultView.model_validate({
        'result_id': str(uuid4()), 'stage_id': 'PLAN', 'model_summary': 'Selected guidance.',
        'body_kind': 'PLAN', 'model_body': {'kind': 'PLAN', 'procedure_steps': ['Fixture task.'],
                 'skill_assessment': {'status': 'USE_PROPOSED', 'proposal_id': str(proposal.proposal_id)}},
    })
    stage = stage.model_copy(update={'prior_results': (prior,)})
    args = (stage, service, tools.binding.principal, tools.binding.workspace)
    assert gold_context_for_stage(*args) == ()  # Approval alone is not a read.
    viewed = tools._skill_view(auth.authorization_id)
    contexts = gold_context_for_stage(*args)
    assert len(contexts) == 1
    assert contexts[0]['guidance'] == viewed.model_dump(mode='json')
    assert contexts[0]['authority'] == 'MODEL_CONTEXT'
    assert contexts[0]['authorization_id'] == str(auth.authorization_id)
    assert gold_context_for_stage(stage, None, *args[2:]) == ()
    assert gold_context_for_stage(stage.model_copy(update={'run_id': uuid4()}), *args[1:]) == ()
    assert gold_context_for_stage(stage.model_copy(update={'prior_results': ()}), *args[1:]) == ()
    refused = prior.model_copy(update={'model_body': {'skill_assessment': {
        'status': 'USE_NOT_PURSUED', 'proposal_id': str(proposal.proposal_id)}}})
    assert gold_context_for_stage(stage.model_copy(update={'prior_results': (refused,)}), *args[1:]) == ()


@pytest.mark.asyncio
async def test_compact_handoff_retains_original_workflow_and_on_demand_sections(boundary):
    from dataclasses import replace
    from labbioagentos import LabBioRuntimeToolSet
    tools, stage, _ = boundary
    proposed = await _pending_proposal(boundary)
    tools = LabBioRuntimeToolSet(replace(tools.binding,
        capability_allowlist=(*tools.binding.capability_allowlist, 'skill_view')), tools.services)
    service = tools.services.skill_service
    proposal = service.store.get_use_proposal(UUID(proposed['proposal_id']))
    auth = _decide_use(service, tools.binding.principal, proposal, approved=True)
    # Section access alone must not create a "full guidance read" receipt.
    unseen = await tools.skill_view(str(auth.authorization_id), section='workflow_outline')
    assert not unseen['success'] and service.store.accesses_for_run(stage.run_id) == ()
    full = await tools.skill_view(str(auth.authorization_id))
    assert full['success']
    prior = RuntimePriorResultView.model_validate({
        'result_id': str(uuid4()), 'stage_id': 'PLAN', 'model_summary': 'Selected guidance.',
        'body_kind': 'PLAN', 'model_body': {'kind': 'PLAN', 'procedure_steps': ['Fixture task.'],
            'skill_assessment': {'status': 'USE_PROPOSED', 'proposal_id': str(proposal.proposal_id)}},
    })
    stage = stage.model_copy(update={'prior_results': (prior,), 'allowed_capabilities': ('skill_view',)})
    args = (service, tools.binding.principal, tools.binding.workspace)
    handoff = gold_context_for_stage(stage, *args)[0]
    assert handoff['guidance']['workflow_outline'] == full['data']['workflow_outline']
    assert 'execution_guidance' not in handoff['guidance']
    assert handoff['section_catalog']['execution_guidance'] >= 0
    assert handoff['guidance']['limitations'] == full['data']['limitations']
    page = await tools.skill_view(str(auth.authorization_id), section='execution_guidance', limit=1)
    assert page['success']
    assert page['data']['items'] == full['data']['execution_guidance'][:1]
    assert page['data']['authorization_id'] == str(auth.authorization_id)
    assert len(service.store.accesses_for_run(stage.run_id)) == 1
    # Existing hosts without a section-reading tool keep their complete projection.
    legacy = gold_context_for_stage(stage.model_copy(update={'allowed_capabilities': ()}), *args)[0]
    assert legacy['guidance'] == full['data']
