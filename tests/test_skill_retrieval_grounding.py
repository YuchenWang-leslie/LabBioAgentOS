"""Current tool receipts, not model prose, ground PLAN Skill assessments."""

import json
from types import MethodType, SimpleNamespace
from uuid import uuid4

import pytest

from labbioagentos import (
    CapabilityEvidenceBundle, PantheonRuntimeFactory, PantheonTypedStageInvoker,
    PlanStageBody, ResponseSchemaRef, RuntimeInvocationMode, RuntimeStageInput,
    RuntimeStageResult, RuntimeWorkspaceIdentifiers, SQLiteSkillStore, TraceEventType,
)
from labbioagentos.runtime.pantheon import PantheonRuntimeIntegrationError
from test_c9_gold_skill_lifecycle import _create_gold, _governed, _toolset
from test_runtime_milestone_c2 import _catalog, _profile


@pytest.fixture
def boundary(tmp_path):
    store = SQLiteSkillStore(tmp_path / "skills.sqlite")
    principal, workspace, artifacts, exposure, _, service, sink, recorder = _governed(tmp_path, store=store)
    tools = _toolset(principal, workspace, artifacts, exposure, service, uuid4(),
                     ("skill_search", "skill_propose_use"), recorder)
    binding = tools.binding
    stage_input = RuntimeStageInput(
        run_id=binding.run_id, stage_id=binding.stage_id,
        invocation_id=binding.invocation_id, instruction="Evaluate optional references.",
        workspace=RuntimeWorkspaceIdentifiers(**workspace.model_dump()),
        allowed_capabilities=binding.capability_allowlist,
    )
    yield tools, stage_input, sink
    store.close()


def _bundle(tools):
    return CapabilityEvidenceBundle(run_id=tools.binding.run_id,
        stage_id=tools.binding.stage_id, invocation_id=tools.binding.invocation_id,
        items=tools.evidence_items())


async def _finalize(boundary, assessment, *, evidence=None, stage_input=None, pre_return=False,
                    observe_format=None):
    tools, original_input, sink = boundary
    stage_input = stage_input or original_input
    evidence = evidence if evidence is not None else _bundle(tools)
    factory = PantheonRuntimeFactory(_catalog())
    team, prompts = await factory.create_team(("coordinator",), invocation_mode=RuntimeInvocationMode.FINALIZE)

    async def run(_self, message, **kwargs):
        if observe_format is not None:
            observe_format(_self.team_agents[0].response_format)
        visible = json.loads(message)
        body = {"kind": "PLAN", "procedure_steps": ["Work from current task evidence."]}
        if assessment is not None:
            body["skill_assessment"] = assessment(visible) if callable(assessment) else assessment
        payload = {
            "stage_id": "PLAN", "summary": "Reference assessment.", "body": body,
            "next_action": {"action": "transition", "target_stage": "PREFLIGHT"},
        }
        if pre_return:
            from pydantic import create_model
            response = create_model("Response", result=(_self.team_agents[0].response_format, ...))
            return SimpleNamespace(content=response.model_validate_json(
                json.dumps({"result": payload})).result)
        return SimpleNamespace(content=payload)

    team.run = MethodType(run, team)
    return await PantheonTypedStageInvoker(team, profile=_profile(), prompt=prompts["coordinator"],
        response_schema=ResponseSchemaRef(), trace_recorder=tools.services.trace_recorder,
    ).invoke(stage_input, capability_evidence=evidence)


def _assessment(status, ids=(), proposal_id=None):
    return {"status": status, "search_capability_invocation_ids": list(map(str, ids)),
            "proposal_id": str(proposal_id) if proposal_id else None,
            "reason": "Bounded fixture assessment, not scientific guidance."}


def test_plan_exposes_structured_assessment_with_legacy_default():
    assert "skill_assessment" in PlanStageBody.model_fields
    assert PlanStageBody(procedure_steps=("Legacy plan.",)).skill_assessment is None


@pytest.mark.asyncio
async def test_enabled_plan_cannot_omit_assessment(boundary):
    with pytest.raises(PantheonRuntimeIntegrationError):
        await _finalize(boundary, None)


@pytest.mark.asyncio
async def test_no_search_cannot_claim_no_match_and_emits_no_completion(boundary):
    tools, stage_input, sink = boundary
    with pytest.raises(PantheonRuntimeIntegrationError) as caught:
        await _finalize(boundary, _assessment("NO_SUITABLE_RETURNED_CANDIDATE"))
    assert caught.value.error_code == "MALFORMED_RUNTIME_RESULT"
    events = sink.read(stage_input.run_id)
    assert any(event.event_type is TraceEventType.FINALIZATION_PHASE_FAILED for event in events)
    assert not any(event.event_type is TraceEventType.AGENT_COMPLETED for event in events)
    assert tools.evidence_items() == ()  # No hidden search or corrected retry.


@pytest.mark.asyncio
@pytest.mark.parametrize("searched", [False, True])
async def test_honest_not_assessed_never_requires_search_or_skill_use(boundary, searched):
    tools, _, _ = boundary
    if searched:
        assert (await tools.skill_search())["success"]
    result = await _finalize(boundary, _assessment("NOT_ASSESSED"))
    assert result.body.skill_assessment.status == "NOT_ASSESSED"
    assert len(tools.evidence_items()) == int(searched)


@pytest.mark.asyncio
@pytest.mark.parametrize("search_args", [{}, {"required_tags": ["fixture-no-match"]}, {"limit": 1}])
async def test_real_empty_filtered_or_partial_page_can_ground_scoped_no_match(boundary, search_args):
    tools, _, _ = boundary
    if search_args.get("limit"):
        for _ in range(2):
            _create_gold(tools.services.skill_service, tools.services.skill_service.store,
                         tools.binding.principal, tools.services.artifact_store)
    assert (await tools.skill_search(**search_args))["success"]

    def assess(visible):
        control = visible["skill_retrieval_control"]
        assert control["authority"] == "CONTROL_STATE"
        ids = control["completed_search_capability_invocation_ids"]
        assert ids == [visible["capability_evidence"]["items"][0]["capability_invocation_id"]]
        return _assessment("NO_SUITABLE_RETURNED_CANDIDATE", ids)

    result = await _finalize(boundary, assess)
    assert result.body.skill_assessment.status == "NO_SUITABLE_RETURNED_CANDIDATE"
    assert RuntimeStageResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["failed", "invented", "wrong-tool", "other-invocation"])
async def test_only_current_completed_search_receipts_support_claim(boundary, kind):
    tools, _, _ = boundary
    if kind == "failed":
        assert not (await tools.skill_search(limit=0))["success"]
    elif kind == "wrong-tool":
        await tools._call("skill_propose_use", lambda: {"proposal_id": str(uuid4())})
    else:
        await tools.skill_search()
    evidence = _bundle(tools)
    evidence_id = uuid4() if kind == "invented" else evidence.items[0].capability_invocation_id
    if kind == "other-invocation":
        evidence = evidence.model_copy(update={"invocation_id": uuid4()})
    with pytest.raises((PantheonRuntimeIntegrationError, ValueError)):
        await _finalize(boundary, _assessment("NO_SUITABLE_RETURNED_CANDIDATE", (evidence_id,)), evidence=evidence)


@pytest.mark.asyncio
async def test_claimed_proposal_requires_real_current_proposal(boundary):
    with pytest.raises(PantheonRuntimeIntegrationError):
        await _finalize(boundary, _assessment("USE_PROPOSED", proposal_id=uuid4()))


@pytest.mark.asyncio
async def test_real_search_proposal_persists_and_reaches_finalization(boundary):
    tools, _, _ = boundary
    service = tools.services.skill_service
    _create_gold(service, service.store, tools.binding.principal, tools.services.artifact_store)
    page = (await tools.skill_search())["data"]
    candidate = page["items"][0]
    response = await tools.skill_propose_use(candidate["skill_id"], candidate["version"],
        "REFERENCE", "Fixture selection from the returned catalog.")
    assert response["success"]

    def assess(visible):
        control = visible["skill_retrieval_control"]
        return _assessment("USE_PROPOSED", control["completed_search_capability_invocation_ids"],
                           control["completed_use_proposals"][0]["proposal_id"])

    result = await _finalize(boundary, assess)
    proposal = service.store.get_use_proposal(result.body.skill_assessment.proposal_id)
    assert str(proposal.skill_id) == candidate["skill_id"]
    assert proposal.skill_version == candidate["version"]
    assert len(tools.evidence_items()) == 2


@pytest.mark.asyncio
async def test_legacy_non_skill_plan_does_not_acquire_new_gate(boundary):
    _, stage_input, _ = boundary
    result = await _finalize(boundary, None,
                             stage_input=stage_input.model_copy(update={"allowed_capabilities": ()}))
    assert result.body.skill_assessment is None


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt_field", ["search", "proposal"])
async def test_non_skill_plan_wire_preserves_optional_assessment_shape(boundary, receipt_field):
    from jsonschema import Draft202012Validator
    from pantheon.utils.adapters.openai_adapter import _normalize_response_format

    _, stage_input, _ = boundary
    formats = []
    result = await _finalize(boundary, None, pre_return=True,
        stage_input=stage_input.model_copy(update={"allowed_capabilities": ()}),
        observe_format=formats.append)
    assert result.body.skill_assessment is None
    schema = _normalize_response_format(formats[0])["json_schema"]["schema"]
    body_ref = schema["properties"]["body"]["$ref"].rsplit("/", 1)[-1]
    field = schema["$defs"][body_ref]["properties"]["skill_assessment"]
    validator = Draft202012Validator({**field, "$defs": schema["$defs"]})
    assert validator.is_valid(None)
    assert validator.is_valid(_assessment("NOT_ASSESSED"))
    malformed = _assessment("NOT_ASSESSED",
        (uuid4(),) if receipt_field == "search" else (),
        uuid4() if receipt_field == "proposal" else None)
    assert not validator.is_valid(malformed)
    assert not validator.is_valid(_assessment("NO_SUITABLE_RETURNED_CANDIDATE", (uuid4(),)))
    assert not validator.is_valid(_assessment("USE_PROPOSED", proposal_id=uuid4()))


def test_provider_schema_requires_assessment_without_widening_next_action():
    from jsonschema import Draft202012Validator
    from labbioagentos import RuntimeWorkflowControlView, WorkflowStage
    from labbioagentos.runtime.skill_grounding import skill_assessment_response_format

    base = ResponseSchemaRef().response_format(WorkflowStage.PLAN, RuntimeWorkflowControlView(
        current_stage=WorkflowStage.PLAN, transition_targets=(WorkflowStage.PREFLIGHT,),
        request_user_input_available=False, retry_available=False, finish_available=False,
    ))
    schema = skill_assessment_response_format(base).model_json_schema()
    assert "skill_assessment" in schema["$defs"]["SkillGroundedPlanBody"]["required"]
    payload = {"stage_id": "PLAN", "summary": "Not assessed.",
        "body": {"kind": "PLAN", "procedure_steps": ["Continue independently."],
                 "skill_assessment": _assessment("NOT_ASSESSED")},
        "next_action": {"action": "transition", "target_stage": "PREFLIGHT"}}
    validator = Draft202012Validator(schema)
    assert not list(validator.iter_errors(payload))
    payload["next_action"]["target_stage"] = "EXECUTE"
    assert list(validator.iter_errors(payload))


@pytest.mark.asyncio
async def test_authoritative_projection_does_not_promote_candidate_text(boundary):
    from labbioagentos.runtime.skill_grounding import skill_retrieval_control
    tools, stage_input, _ = boundary
    await tools._call("skill_search", lambda: {"items": [{"description": "MODEL_CONTEXT_SENTINEL"}]})
    control = skill_retrieval_control(stage_input, _bundle(tools))
    assert control["completed_search_capability_invocation_ids"]
    assert "MODEL_CONTEXT_SENTINEL" not in json.dumps(control)
    assert "MODEL_CONTEXT_SENTINEL" in _bundle(tools).model_dump_json()


@pytest.mark.asyncio
@pytest.mark.parametrize("assessment", [
    _assessment("NOT_ASSESSED", (uuid4(),)),
    _assessment("NO_SUITABLE_RETURNED_CANDIDATE", (uuid4(),), uuid4()),
    _assessment("USE_PROPOSED"),
    _assessment("NO_MATCH_IN_ENTIRE_LIBRARY"),
])
async def test_inconsistent_or_unbounded_claim_shapes_are_rejected(boundary, assessment):
    with pytest.raises(PantheonRuntimeIntegrationError):
        await _finalize(boundary, assessment)


@pytest.mark.parametrize("status", [
    "NOT_ASSESSED", "NO_SUITABLE_RETURNED_CANDIDATE", "USE_PROPOSED",
])
@pytest.mark.parametrize("has_search", [False, True])
@pytest.mark.parametrize("has_proposal", [False, True])
def test_actual_provider_assessment_schema_matches_internal_shapes(status, has_search, has_proposal):
    from jsonschema import Draft202012Validator
    from openai.lib._parsing._completions import type_to_response_format_param
    from pantheon.utils.adapters.openai_adapter import _normalize_response_format
    from pydantic import ValidationError, create_model
    from labbioagentos import WorkflowStage
    from labbioagentos.runtime.contracts import SkillAssessment
    from labbioagentos.runtime.skill_grounding import skill_assessment_response_format

    base = ResponseSchemaRef().response_format(WorkflowStage.PLAN)
    search_id, proposal_id = uuid4(), uuid4()
    response = create_model("Response", result=(skill_assessment_response_format(
        base, (str(search_id),), (str(proposal_id),)), ...))
    wire_format = _normalize_response_format(response)
    assert wire_format == type_to_response_format_param(response)
    schema = wire_format["json_schema"]["schema"]
    assessment = _assessment(status, (search_id,) if has_search else (), proposal_id if has_proposal else None)
    try:
        SkillAssessment.model_validate(assessment)
        accepted = True
    except ValidationError:
        accepted = False
    # Validate just this nested contract, retaining the actual provider's definitions.
    body = schema["$defs"]["SkillGroundedPlanBody"]["properties"]["skill_assessment"]
    validator = Draft202012Validator({**body, "$defs": schema["$defs"]})
    assert validator.is_valid(assessment) is accepted


@pytest.mark.parametrize("assessment,code", [
    (_assessment("NOT_ASSESSED", (uuid4(),)), "skill_unassessed_has_evidence"),
    (_assessment("NO_SUITABLE_RETURNED_CANDIDATE"), "skill_candidate_judgment_requires_search_only"),
    (_assessment("USE_PROPOSED"), "skill_use_requires_proposal"),
])
def test_internal_shape_errors_have_fixed_safe_codes(assessment, code):
    from pydantic import ValidationError
    from labbioagentos.runtime.contracts import SkillAssessment
    with pytest.raises(ValidationError) as caught:
        SkillAssessment.model_validate(assessment)
    assert caught.value.errors()[0]["type"] == code


@pytest.mark.asyncio
@pytest.mark.parametrize("pre_return", [False, True])
async def test_rejected_assessment_extra_key_is_never_logged(boundary, pre_return):
    tools, stage_input, sink = boundary
    secret = "/private/provider-body-credential-sentinel"
    assessment = {**_assessment("NOT_ASSESSED"), secret: "untrusted-body-sentinel"}
    with pytest.raises(PantheonRuntimeIntegrationError) as caught:
        await _finalize(boundary, assessment, pre_return=pre_return)
    failures = [e for e in sink.read(stage_input.run_id) if e.status == "FAILED"]
    assert failures
    serialized = json.dumps([e.model_dump(mode="json") for e in failures])
    assert secret not in serialized and "untrusted-body-sentinel" not in serialized
    assert "<extra_field>" in serialized
    assert "extra_forbidden" in caught.value.validation_error_types
    assert not any(e.event_type is TraceEventType.AGENT_COMPLETED for e in sink.read(stage_input.run_id))
    assert tools.evidence_items() == ()


@pytest.mark.asyncio
async def test_real_empty_search_can_transition_through_pantheon_response_validation(boundary):
    tools, _, _ = boundary
    assert (await tools.skill_search())["data"]["available_count"] == 0
    result = await _finalize(boundary, lambda visible: _assessment(
        "NO_SUITABLE_RETURNED_CANDIDATE",
        visible["skill_retrieval_control"]["completed_search_capability_invocation_ids"],
    ), pre_return=True)
    assert result.next_action.target_stage.value == "PREFLIGHT"


@pytest.mark.parametrize("searched,proposed", [(False, False), (True, False), (False, True), (True, True)])
def test_wire_contract_only_allows_current_receipt_identities(searched, proposed):
    from jsonschema import Draft202012Validator
    from pantheon.utils.adapters.openai_adapter import _normalize_response_format
    from labbioagentos import WorkflowStage
    from labbioagentos.runtime.skill_grounding import skill_assessment_response_format
    sid, pid, foreign = uuid4(), uuid4(), uuid4()
    model = skill_assessment_response_format(ResponseSchemaRef().response_format(WorkflowStage.PLAN),
        (str(sid),) if searched else (), (str(pid),) if proposed else ())
    schema = _normalize_response_format(model)["json_schema"]["schema"]
    field = schema["$defs"]["SkillGroundedPlanBody"]["properties"]["skill_assessment"]
    validator = Draft202012Validator({**field, "$defs": schema["$defs"]})
    assert validator.is_valid(_assessment("NOT_ASSESSED"))
    assert validator.is_valid(_assessment("NO_SUITABLE_RETURNED_CANDIDATE", (sid,))) is searched
    assert validator.is_valid(_assessment("USE_PROPOSED", proposal_id=pid)) is proposed
    assert not validator.is_valid(_assessment("NO_SUITABLE_RETURNED_CANDIDATE", (foreign,)))
    assert not validator.is_valid(_assessment("USE_PROPOSED", proposal_id=foreign))
    assert validator.is_valid(_assessment("USE_PROPOSED", (sid,), pid)) is (searched and proposed)
    assert not validator.is_valid(_assessment("USE_PROPOSED", (foreign,), pid))
    # Preserve the invoker's existing BaseModel/Python UUID path, not just wire strings.
    if searched:
        body = model.model_fields["body"].annotation
        value = body.model_validate({"procedure_steps": ["Fixture."], "skill_assessment": {
            **_assessment("NO_SUITABLE_RETURNED_CANDIDATE", (sid,)),
            "search_capability_invocation_ids": (sid,)}})
        assert value.skill_assessment.search_capability_invocation_ids == (sid,)


@pytest.mark.asyncio
async def test_current_receipt_rejection_has_safe_semantic_diagnostics(boundary):
    tools, _, _ = boundary
    await tools.skill_search()
    with pytest.raises(PantheonRuntimeIntegrationError) as caught:
        await _finalize(boundary, _assessment("NO_SUITABLE_RETURNED_CANDIDATE", (uuid4(),)),
                        pre_return=True)
    assert caught.value.validation_error_types == ("skill_search_receipt_not_current",)
    assert caught.value.validation_error_field_paths == ("body.skill_assessment",)
