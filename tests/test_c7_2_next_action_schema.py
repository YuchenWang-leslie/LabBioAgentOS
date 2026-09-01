"""C7.2 regression coverage for provider-visible next-action variants."""

from __future__ import annotations

from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from labbioagentos import (
    NextAction,
    NextActionProposal,
    PreflightStageBody,
    ResponseSchemaRef,
    RunStatus,
    WorkflowDefinition,
    WorkflowEngine,
    WorkflowStage,
    WorkflowTransition,
    default_workflow_definition,
)


def _schema_errors(value: dict) -> list:
    schema = NextActionProposal.model_json_schema()
    return list(Draft202012Validator(schema).iter_errors(value))


def _assert_schema_and_model_accept(value: dict) -> NextActionProposal:
    assert _schema_errors(value) == []
    return NextActionProposal.model_validate(value)


def _assert_schema_and_model_reject(value: dict) -> None:
    assert _schema_errors(value)
    with pytest.raises(ValidationError):
        NextActionProposal.model_validate(value)


def test_s1_transition_schema_requires_target_and_forbids_user_input_fields():
    proposal = _assert_schema_and_model_accept(
        {"action": "transition", "target_stage": "EXECUTE"}
    )
    assert proposal.action is NextAction.TRANSITION
    assert proposal.target_stage is WorkflowStage.EXECUTE
    _assert_schema_and_model_reject({"action": "transition"})
    _assert_schema_and_model_reject(
        {
            "action": "transition",
            "target_stage": "EXECUTE",
            "user_prompt": "Unexpected prompt.",
        }
    )
    _assert_schema_and_model_reject(
        {
            "action": "transition",
            "target_stage": "EXECUTE",
            "domain_reference_id": "unexpected-reference",
        }
    )


def test_s2_request_user_input_schema_requires_prompt_and_forbids_target():
    proposal = _assert_schema_and_model_accept(
        {
            "action": "request_user_input",
            "user_prompt": "Need approval.",
            "domain_reference_id": "approval-1",
        }
    )
    assert proposal.action is NextAction.REQUEST_USER_INPUT
    assert proposal.user_prompt == "Need approval."
    assert proposal.domain_reference_id == "approval-1"
    _assert_schema_and_model_reject({"action": "request_user_input"})
    _assert_schema_and_model_reject(
        {
            "action": "request_user_input",
            "target_stage": "EXECUTE",
            "user_prompt": "Unexpected target.",
        }
    )


def test_s3_retry_schema_preserves_optional_target_and_forbids_user_input_fields():
    current_stage = _assert_schema_and_model_accept({"action": "retry"})
    targeted = _assert_schema_and_model_accept(
        {
            "action": "retry",
            "target_stage": "EXECUTE",
            "reason": "Retry with revised execution evidence.",
        }
    )
    assert current_stage.target_stage is None
    assert targeted.target_stage is WorkflowStage.EXECUTE
    _assert_schema_and_model_reject(
        {"action": "retry", "user_prompt": "Unexpected prompt."}
    )
    _assert_schema_and_model_reject(
        {"action": "retry", "domain_reference_id": "unexpected-reference"}
    )


@pytest.mark.parametrize(
    "field,value",
    (
        ("target_stage", "EXECUTE"),
        ("user_prompt", "Unexpected prompt."),
        ("domain_reference_id", "unexpected-reference"),
    ),
)
def test_s4_finish_schema_forbids_action_specific_fields(field, value):
    proposal = _assert_schema_and_model_accept({"action": "finish"})
    assert proposal.action is NextAction.FINISH
    _assert_schema_and_model_reject({"action": "finish", field: value})


@pytest.mark.parametrize(
    "field,value",
    (
        ("target_stage", "EXECUTE"),
        ("user_prompt", "Unexpected prompt."),
        ("domain_reference_id", "unexpected-reference"),
    ),
)
def test_s5_fail_schema_requires_reason_and_forbids_unrelated_fields(field, value):
    proposal = _assert_schema_and_model_accept(
        {"action": "fail", "reason": "Controlled failure."}
    )
    assert proposal.action is NextAction.FAIL
    assert proposal.reason == "Controlled failure."
    _assert_schema_and_model_reject({"action": "fail"})
    _assert_schema_and_model_reject(
        {"action": "fail", "reason": "Controlled failure.", field: value}
    )


@pytest.mark.parametrize(
    "value",
    (
        {
            "action": "transition",
            "target_stage": "UNDERSTAND",
            "reason": "Optional structural rationale.",
        },
        {
            "action": "retry",
            "target_stage": "EXECUTE",
            "reason": "Retry rationale.",
        },
        {
            "action": "request_user_input",
            "user_prompt": "Need approval.",
            "domain_reference_id": "approval-1",
            "reason": "Optional gate rationale.",
        },
        {"action": "finish", "reason": "Optional completion rationale."},
        {"action": "fail", "reason": "Controlled failure."},
    ),
)
def test_s6_all_accepted_variants_round_trip_without_semantic_loss(value):
    proposal = _assert_schema_and_model_accept(value)
    dumped = proposal.model_dump(mode="json", exclude_none=True)
    assert dumped == value
    assert NextActionProposal.model_validate(dumped) == proposal


def test_provider_finalize_schema_embeds_strict_proposal_union():
    response_model = ResponseSchemaRef().response_format(WorkflowStage.PREFLIGHT)
    valid = response_model(
        stage_id=WorkflowStage.PREFLIGHT,
        summary="Deterministic preflight result.",
        body=PreflightStageBody(structurally_valid=True),
        next_action=NextActionProposal(
            action=NextAction.TRANSITION,
            target_stage=WorkflowStage.EXECUTE,
        ),
    ).model_dump(mode="json")
    schema = response_model.model_json_schema()
    validator = Draft202012Validator(schema)
    assert list(validator.iter_errors(valid)) == []

    invalid = deepcopy(valid)
    invalid["next_action"]["user_prompt"] = "Unexpected prompt."
    assert list(validator.iter_errors(invalid))


def _user_gate_definition() -> WorkflowDefinition:
    return WorkflowDefinition(
        workflow_id="c7-2-user-gate",
        nodes=frozenset(
            {
                WorkflowStage.PLAN,
                WorkflowStage.USER_GATE,
                WorkflowStage.PREFLIGHT,
            }
        ),
        allowed_transitions=frozenset(
            {
                WorkflowTransition(
                    source=WorkflowStage.PLAN,
                    target=WorkflowStage.USER_GATE,
                ),
                WorkflowTransition(
                    source=WorkflowStage.USER_GATE,
                    target=WorkflowStage.PREFLIGHT,
                ),
            }
        ),
        initial_stage=WorkflowStage.PLAN,
        terminal_stages=frozenset({WorkflowStage.PREFLIGHT}),
    )


def test_s7_workflow_consumers_preserve_all_action_behaviors():
    transition_engine = WorkflowEngine(default_workflow_definition())
    transition_run = transition_engine.create_run()
    transition_engine.start(transition_run)
    transition_engine.apply_proposal(
        transition_run,
        NextActionProposal(
            action=NextAction.TRANSITION,
            target_stage=WorkflowStage.UNDERSTAND,
        ),
    )
    assert transition_run.current_stage is WorkflowStage.UNDERSTAND

    gate_engine = WorkflowEngine(_user_gate_definition())
    gate_run = gate_engine.create_run()
    gate_engine.start(gate_run)
    gate_engine.apply_proposal(
        gate_run,
        NextActionProposal(
            action=NextAction.REQUEST_USER_INPUT,
            user_prompt="Need approval.",
            domain_reference_id="approval-1",
        ),
    )
    assert gate_run.status is RunStatus.WAITING_FOR_USER
    assert gate_run.pending_user_gate is not None
    assert gate_run.pending_user_gate.domain_reference_id == "approval-1"

    retry_engine = WorkflowEngine(default_workflow_definition())
    retry_run = retry_engine.create_run(retry_limit=1)
    retry_engine.start(retry_run)
    retry_engine.apply_proposal(
        retry_run, NextActionProposal(action=NextAction.RETRY)
    )
    assert retry_run.current_stage is WorkflowStage.INTAKE
    assert retry_run.retry_counts == {WorkflowStage.INTAKE: 1}

    finish_definition = WorkflowDefinition(
        workflow_id="c7-2-finish",
        nodes=frozenset({WorkflowStage.LEARN}),
        initial_stage=WorkflowStage.LEARN,
        terminal_stages=frozenset({WorkflowStage.LEARN}),
    )
    finish_engine = WorkflowEngine(finish_definition)
    finish_run = finish_engine.create_run()
    finish_engine.start(finish_run)
    finish_engine.apply_proposal(
        finish_run, NextActionProposal(action=NextAction.FINISH)
    )
    assert finish_run.status is RunStatus.COMPLETED

    fail_engine = WorkflowEngine(default_workflow_definition())
    fail_run = fail_engine.create_run()
    fail_engine.start(fail_run)
    fail_engine.apply_proposal(
        fail_run,
        NextActionProposal(action=NextAction.FAIL, reason="Controlled failure."),
    )
    assert fail_run.status is RunStatus.FAILED
    assert fail_run.failure_reason == "Controlled failure."
