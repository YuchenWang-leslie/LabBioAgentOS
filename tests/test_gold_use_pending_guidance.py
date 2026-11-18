"""The model can turn an optional Gold-use proposal into an explicit user gate."""

import json
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from labbioagentos import RuntimeStageInput, RuntimeWorkspaceIdentifiers
from labbioagentos.runtime.contracts import CapabilityEvidenceBundle, RuntimeStageResult
from labbioagentos.runtime.handoff import finalization_evidence
from labbioagentos.runtime.native_tools import build_labbio_tool_registry
from labbioagentos.runtime.profiles import ResponseSchemaRef
from labbioagentos.runtime.skill_grounding import (
    skill_assessment_response_format, skill_retrieval_control, validate_skill_assessment,
)
from test_c9_gold_skill_lifecycle import _create_gold, _decide_use, _governed, _toolset


@pytest.fixture
def plan(tmp_path):
    principal, workspace, artifacts, exposure, store, service, _, recorder = _governed(tmp_path)
    _, _, gold = _create_gold(service, store, principal, artifacts)
    tools = _toolset(principal, workspace, artifacts, exposure, service, uuid4(),
                     ("skill_search", "skill_propose_use", "skill_view"), recorder)
    return tools, gold


@pytest.mark.asyncio
async def test_pending_receipt_provides_approval_and_voluntary_opt_out(plan):
    tools, gold = plan
    receipt = await tools.skill_propose_use(str(gold.skill_id), gold.version, "ADAPT", "Applicable here.")
    item = receipt["data"]
    assert receipt["data"] == tools.evidence_items()[-1].safe_result
    steps = item["next_step"]
    for token in ("USE_PROPOSED", "request_user_input", "user_prompt", item["domain_reference_id"],
                  "USE_NOT_PURSUED", "decision_reference_id"):
        assert token in steps
    assert "text handoff" in steps and "prepare_handoff" not in steps
    stage = RuntimeStageInput(run_id=tools.binding.run_id, stage_id=tools.binding.stage_id,
        invocation_id=tools.binding.invocation_id, instruction="Consider an optional Skill.",
        workspace=RuntimeWorkspaceIdentifiers(**tools.binding.workspace.model_dump()),
        allowed_capabilities=tools.binding.capability_allowlist)
    evidence = CapabilityEvidenceBundle(run_id=stage.run_id, stage_id=stage.stage_id,
        invocation_id=stage.invocation_id, items=tools.evidence_items())
    assert finalization_evidence(evidence)["items"][0]["safe_result"] == item
    control = skill_retrieval_control(stage, evidence)
    assert control["use_proposals"][0]["status"] == "PENDING"
    fmt = skill_assessment_response_format(ResponseSchemaRef().response_format(stage.stage_id),
        (), (item["proposal_id"],), resolve_proposal=True,
        pending_proposals=((item["proposal_id"], item["domain_reference_id"]),))
    for status, action in (
        ("USE_PROPOSED", {"action": "request_user_input", "user_prompt": "Approve for this run?",
                          "domain_reference_id": item["domain_reference_id"]}),
        ("USE_NOT_PURSUED", {"action": "transition", "target_stage": "PREFLIGHT"}),
    ):
        response = {"stage_id": "PLAN", "summary": "Fixture decision", "next_action": action,
                    "body": {"kind": "PLAN", "procedure_steps": ["Check the inputs."],
                             "skill_assessment": {"status": status, "reason": "Test decision.",
                                                  "proposal_id": item["proposal_id"]}}}
        selected = fmt.model_validate_json(json.dumps(response))
        validate_skill_assessment(RuntimeStageResult.model_validate_json(selected.model_dump_json()), stage, evidence)
        # A real pending PLAN may carry an exact Agent-authored action from prepare_handoff.
        from types import SimpleNamespace
        from jsonschema import Draft202012Validator
        from labbioagentos.kernel.tooling import model_json_schema
        from labbioagentos.runtime.native_stage import NativeKernelStageInvoker
        actual = selected.model_dump(mode="json")["next_action"]
        response["next_action"] = actual
        bound, _ = NativeKernelStageInvoker._finalization_model_input(stage,
            {"skill_retrieval_control": control, "stage_decision_control": {"status": "BOUND", "next_action": actual}},
            (), "fixture", (None, SimpleNamespace(sanitized_text="fixture"), None, ResponseSchemaRef(), None))
        wire = Draft202012Validator(model_json_schema(bound))
        assert wire.is_valid(response)
        validated = bound.model_validate_json(json.dumps(response))
        validate_skill_assessment(RuntimeStageResult.model_validate_json(validated.model_dump_json()), stage, evidence)
        wrong_action = {**response, "next_action": {**actual, "reason": "different decision"}}
        wrong_body = {**response, "body": {**response["body"], "skill_assessment": {
            **response["body"]["skill_assessment"], "status": "USE_NOT_PURSUED" if status == "USE_PROPOSED" else "USE_PROPOSED"}}}
        bad_responses = [wrong_action, wrong_body]
        if status == "USE_PROPOSED":
            bad_responses.append({**response, "next_action": {**actual, "domain_reference_id": "skill-use:other"}})
            bad_responses.append({**response, "body": {**response["body"], "skill_assessment": {
                **response["body"]["skill_assessment"], "proposal_id": str(uuid4())}}})
        for invalid in bad_responses:
            assert not wire.is_valid(invalid)
            with pytest.raises(ValueError):
                bound.model_validate_json(json.dumps(invalid))


@pytest.mark.asyncio
async def test_id_confusion_returns_only_delivered_matching_pending_reference(plan):
    tools, gold = plan
    receipt = await tools.skill_propose_use(str(gold.skill_id), gold.version, "ADAPT", "Test conditions.")
    item = receipt["data"]
    early = await tools.skill_view(item["proposal_id"])
    assert item["domain_reference_id"] not in early["error"]["safe_message"]
    tools.mark_feedback_delivered((receipt,))
    for wrong in (item["proposal_id"], item["approval_gate_id"].split(":", 1)[1]):
        denied = await tools.skill_view(wrong)
        assert denied["error"]["error_code"] == "SKILL_AUTHORIZATION_NOT_FOUND"
        assert item["domain_reference_id"] in denied["error"]["safe_message"]
        assert "request_user_input" in denied["error"]["safe_message"]
    elsewhere = await tools.skill_view(str(uuid4()))
    assert item["domain_reference_id"] not in elsewhere["error"]["safe_message"]
    service = tools.services.skill_service
    proposal = service.pending_use_proposal(UUID(item["proposal_id"]))
    authorization = _decide_use(service, tools.binding.principal, proposal)
    stale = await tools.skill_view(item["proposal_id"])
    assert item["domain_reference_id"] not in stale["error"]["safe_message"]
    viewed = await tools.skill_view(str(authorization.authorization_id))
    assert viewed["data"]["skill_id"] == str(gold.skill_id)
    assert viewed["data"]["authorization_id"] == str(authorization.authorization_id)


@pytest.mark.asyncio
async def test_rejected_use_cannot_be_reopened_by_rewording_without_new_user_input(plan):
    tools, gold = plan
    service = tools.services.skill_service
    initial = await tools.skill_propose_use(str(gold.skill_id), gold.version, "REUSE", "Use the approved guide.")
    proposal = service.pending_use_proposal(UUID(initial["data"]["proposal_id"]))
    _decide_use(service, tools.binding.principal, proposal, approved=False)
    again = await tools.skill_propose_use(str(gold.skill_id), gold.version, "REUSE", "Try the same guide again.")
    assert again["error"]["error_code"] == "SKILL_USE_ALREADY_DECLINED"
    assert str(proposal.proposal_id) in again["error"]["safe_message"]
    assert "USE_NOT_PURSUED" in again["error"]["safe_message"]
    assert service.store.accesses_for_run(tools.binding.run_id) == ()
    stage = RuntimeStageInput(run_id=tools.binding.run_id, stage_id=tools.binding.stage_id,
        invocation_id=tools.binding.invocation_id, instruction="Continue the task.",
        workspace=RuntimeWorkspaceIdentifiers(**tools.binding.workspace.model_dump()),
        allowed_capabilities=tools.binding.capability_allowlist, gate_decisions=({
            "gate_id": "fixture:gate:1", "approved": False, "source_stage": "PLAN",
            "domain_reference_id": initial["data"]["domain_reference_id"],
        },))
    fmt = skill_assessment_response_format(ResponseSchemaRef().response_format(stage.stage_id),
        proposal_ids=(str(proposal.proposal_id),), rejected_proposal_ids=(str(proposal.proposal_id),),
        resolve_proposal=True)
    payload = {"stage_id": "PLAN", "summary": "Continue independently",
        "body":{"kind":"PLAN", "procedure_steps":["Analyze the current inputs."],
                "skill_assessment":{"status":"USE_NOT_PURSUED","reason":"The user declined this use.",
                                    "proposal_id":str(proposal.proposal_id)}},
        "next_action":{"action":"transition","target_stage":"PREFLIGHT"}}
    selected = fmt.model_validate_json(json.dumps(payload))
    validate_skill_assessment(RuntimeStageResult.model_validate_json(selected.model_dump_json()), stage, None)
    payload["body"]["skill_assessment"]["status"] = "USE_PROPOSED"
    with pytest.raises(ValueError):
        fmt.model_validate_json(json.dumps(payload))
    different_mode = await tools.skill_propose_use(str(gold.skill_id), gold.version, "ADAPT",
                                                   "Use only the compatible portion.", ["Omit the old step."])
    assert different_mode["data"]["status"] == "USER_APPROVAL_REQUIRED"
    adapt = service.pending_use_proposal(UUID(different_mode["data"]["proposal_id"]))
    _decide_use(service, tools.binding.principal, adapt, approved=False)
    blocked = await tools.skill_propose_use(str(gold.skill_id), gold.version, "ADAPT",
                                            "Alternative wording.", ["Rephrase the changed step."])
    assert blocked["error"]["error_code"] == "SKILL_USE_ALREADY_DECLINED"
    tools.binding = replace(tools.binding, latest_user_answer_id="new-user-answer")
    revised = await tools.skill_propose_use(str(gold.skill_id), gold.version, "ADAPT",
                                            "The researcher later clarified the target.", ["Use the new input."])
    assert revised["data"]["status"] == "USER_APPROVAL_REQUIRED"


def test_native_model_tools_explain_auth_identity_and_approval_step(plan):
    tools, _ = plan
    specs = {s.name: s.model_dump_json() for s in build_labbio_tool_registry(tools).specs}
    assert "decision_reference_id" in specs["skill_view"]
    assert "request_user_input" in specs["skill_propose_use"]
    assert "domain_reference_id" in specs["skill_propose_use"]


@pytest.mark.asyncio
async def test_identical_pending_retry_returns_one_gate_and_preserves_alternative(plan):
    tools, gold = plan
    inputs = (str(gold.skill_id), gold.version, "ADAPT", "Use the grouping rule.")
    first = (await tools.skill_propose_use(*inputs, ["Check units."]))["data"]
    duplicate = (await tools.skill_propose_use(*inputs, ["Check units."]))["data"]
    alternative = (await tools.skill_propose_use(*inputs, ["Check input identities."]))["data"]
    assert duplicate["proposal_id"] == first["proposal_id"]
    assert duplicate["approval_gate_id"] == first["approval_gate_id"]
    assert duplicate["domain_reference_id"] == first["domain_reference_id"]
    assert alternative["proposal_id"] != first["proposal_id"]
    assert [t.event_type.value for t in tools.services.trace_recorder.events(tools.binding.run_id)].count("SKILL_USE_PROPOSED") == 2
    assert tools.services.skill_service.store.accesses_for_run(tools.binding.run_id) == ()
