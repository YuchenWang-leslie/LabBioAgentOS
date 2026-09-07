"""Action semantics survive provider serialization without overriding decisions."""

from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator
from pantheon.utils.adapters.openai_adapter import _normalize_response_format
from pydantic import ValidationError, create_model

from labbioagentos import (
    LearnStageBody,
    NextActionProposal,
    ResponseSchemaRef,
    RunStatus,
    RuntimeCoordinatorService,
    RuntimeStageResult,
    StageRuntimeRegistry,
    StageRuntimeSpec,
    ValidateStageBody,
    WorkflowEngine,
    WorkflowStage,
    runtime_workflow_definition,
)
from labbioagentos.workflow import InvalidRunStateError


def _provider_schema(response_model):
    # Pantheon Agent wraps a typed response before its OpenAI adapter serializes it.
    response = create_model("Response", result=(response_model, ...))
    wire = _normalize_response_format(response)
    assert wire["type"] == "json_schema"
    assert wire["json_schema"]["strict"] is True
    return wire["json_schema"]["schema"]


def _actions(schema):
    return {
        value["properties"]["action"]["const"]: value
        for value in schema["$defs"].values()
        if "action" in value.get("properties", {})
    }


def _stage(stage):
    async def unused_invoker(_):  # pragma: no cover
        raise AssertionError("This regression must not invoke a provider")

    registry = StageRuntimeRegistry((StageRuntimeSpec(
        stage_id=stage,
        profile_key="control-test",
        prompt_template_key="control-test",
        capability_allowlist=(),
        invoker=unused_invoker,
        retry_enabled=False,
        user_input_enabled=False,
    ),))
    engine = WorkflowEngine(runtime_workflow_definition())
    coordinator = RuntimeCoordinatorService(engine, registry)
    run = engine.create_run(retry_limit=1)
    engine.start(run)
    for target in (
        WorkflowStage.UNDERSTAND, WorkflowStage.PLAN, WorkflowStage.PREFLIGHT,
        WorkflowStage.EXECUTE, WorkflowStage.VALIDATE, WorkflowStage.INTERPRET,
        WorkflowStage.REPORT, WorkflowStage.LEARN,
    ):
        if run.current_stage is stage:
            break
        engine.transition(run, target)
    stage_input = coordinator.build_stage_input(run, instruction="Synthetic task.")
    response_model = ResponseSchemaRef().response_format(
        stage, stage_input.workflow_control,
    )
    return coordinator, run, stage_input, response_model


def _result(stage, action):
    body = (
        ValidateStageBody(technical_status="COMPLETED", runtime_assessment="PASS")
        if stage is WorkflowStage.VALIDATE
        else LearnStageBody(learning_summary="No additional learning requested.")
    )
    return RuntimeStageResult(
        stage_id=stage,
        summary="The technical step is complete.",
        body=body,
        next_action=NextActionProposal.model_validate(action),
    )


@pytest.mark.parametrize("action,effect", (
    ("transition", "RUNNING"),
    ("retry", "retry allowance"),
    ("request_user_input", "WAITING_FOR_USER"),
    ("finish", "COMPLETED"),
    ("fail", "FAILED"),
))
def test_provider_action_schema_describes_actual_control_effect(action, effect):
    schema = _provider_schema(ResponseSchemaRef().response_format(WorkflowStage.VALIDATE))
    variant = _actions(schema)[action]
    assert effect in variant["properties"]["action"]["description"]
    assert variant["additionalProperties"] is False


def test_governed_schema_preserves_action_meanings_and_exact_legal_targets():
    _, _, stage_input, response_model = _stage(WorkflowStage.VALIDATE)
    control = stage_input.workflow_control
    assert control.transition_targets == (WorkflowStage.EXECUTE, WorkflowStage.INTERPRET)
    assert not control.finish_available
    assert not control.retry_available
    assert not control.request_user_input_available
    actions = _actions(_provider_schema(response_model))
    assert set(actions) == {"transition", "fail"}
    assert actions["transition"]["properties"]["target_stage"]["enum"] == [
        "EXECUTE", "INTERPRET",
    ]
    assert "RUNNING" in actions["transition"]["properties"]["action"]["description"]
    assert "FAILED" in actions["fail"]["properties"]["action"]["description"]
    assert "failure" in actions["fail"]["properties"]["reason"]["description"]


@pytest.mark.parametrize("target", (WorkflowStage.EXECUTE, WorkflowStage.INTERPRET))
def test_valid_model_transition_is_preserved(target):
    coordinator, run, stage_input, response_model = _stage(WorkflowStage.VALIDATE)
    result = _result(WorkflowStage.VALIDATE, {
        "action": "transition", "target_stage": target.value,
    })
    payload = result.model_dump(mode="json")
    assert not list(Draft202012Validator(_provider_schema(response_model)).iter_errors(
        {"result": payload},
    ))
    response_model.model_validate(payload)
    coordinator.accept_trusted_stage_result(run, result, stage_input.invocation_id)
    assert run.current_stage is target
    assert run.status is RunStatus.RUNNING
    assert run.stage_results[-1].payload["next_stage"] == target.value


def test_explicit_failure_is_not_rewritten_from_passing_technical_text():
    coordinator, run, stage_input, response_model = _stage(WorkflowStage.VALIDATE)
    result = _result(WorkflowStage.VALIDATE, {
        "action": "fail",
        "reason": "Technical execution passed, but a required deliverable is unavailable.",
    })
    payload = result.model_dump(mode="json")
    assert not list(Draft202012Validator(_provider_schema(response_model)).iter_errors(
        {"result": payload},
    ))
    response_model.model_validate(payload)
    coordinator.accept_trusted_stage_result(run, result, stage_input.invocation_id)
    assert run.status is RunStatus.FAILED
    assert run.current_stage is WorkflowStage.VALIDATE
    assert run.stage_results[-1].payload["next_action"] == "fail"


@pytest.mark.parametrize("stage", (WorkflowStage.VALIDATE, WorkflowStage.LEARN))
def test_finish_only_completes_a_terminal_stage(stage):
    coordinator, run, stage_input, response_model = _stage(stage)
    result = _result(stage, {"action": "finish"})
    payload = result.model_dump(mode="json")
    errors = list(Draft202012Validator(_provider_schema(response_model)).iter_errors(
        {"result": payload},
    ))
    if stage is WorkflowStage.VALIDATE:
        assert errors
        with pytest.raises(ValidationError):
            response_model.model_validate(payload)
        with pytest.raises(InvalidRunStateError):
            coordinator.accept_trusted_stage_result(run, result, stage_input.invocation_id)
        assert run.status is RunStatus.RUNNING
        assert not run.stage_results
    else:
        assert not errors
        response_model.model_validate(payload)
        coordinator.accept_trusted_stage_result(run, result, stage_input.invocation_id)
        assert run.status is RunStatus.COMPLETED
        assert run.stage_results[-1].payload["next_action"] == "finish"
