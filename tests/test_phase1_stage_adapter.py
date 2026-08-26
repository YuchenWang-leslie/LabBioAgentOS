"""Phase 1 boundary tests. All Pantheon agent behavior is mocked locally."""

from __future__ import annotations

from types import MethodType

import pytest
from pantheon.agent import Agent, AgentResponse
from pantheon.team import PantheonTeam

from labbioagentos import (
    AgentStageResult,
    PantheonStageAdapter,
    StageContext,
    StageInvocationError,
    StageResultValidationError,
    WorkflowRun,
    WorkflowStage,
)


def _contains_identity(value, target: object) -> bool:
    if value is target:
        return True
    if isinstance(value, dict):
        return any(
            _contains_identity(key, target) or _contains_identity(item, target)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_identity(item, target) for item in value)
    return False


def _mock_team(*, content=None, error: Exception | None = None):
    agent = Agent(
        name="mock_reasoner",
        instructions="Return only the test-provided structured protocol result.",
        model="openai/mock-model",
    )
    observed: dict = {}

    async def mock_run(self, message, **kwargs):
        observed["message"] = message
        observed["kwargs"] = kwargs
        if error is not None:
            raise error
        return AgentResponse(
            agent_name=self.name,
            content=content,
            details=None,
        )

    agent.run = MethodType(mock_run, agent)
    return PantheonTeam(agents=[agent]), observed


@pytest.mark.asyncio
async def test_stage_adapter_preserves_workflow_ownership_boundary():
    run = WorkflowRun(current_stage=WorkflowStage.UNDERSTAND)
    context = StageContext(
        run_id=run.run_id,
        stage=run.current_stage,
        instruction="Return the mock stage protocol result.",
        metadata={"fixture": {"kind": "phase-1"}},
    )
    team, observed = _mock_team(
        content={
            "stage": "UNDERSTAND",
            "summary": "Mock reasoning completed.",
            "payload": {"protocol": "mock-only"},
        }
    )
    adapter = PantheonStageAdapter(team)

    result = await adapter.run_stage(context)

    assert result.stage is WorkflowStage.UNDERSTAND
    assert result.payload == {"protocol": "mock-only"}
    assert run.stage_results == ()
    assert not _contains_identity(vars(adapter), run)
    assert not _contains_identity(vars(team), run)
    assert not _contains_identity(observed, run)
    assert observed["message"] == "Return the mock stage protocol result."
    assert observed["kwargs"]["context_variables"] == {
        "labbio": {
            "run_id": str(run.run_id),
            "stage": "UNDERSTAND",
            "metadata": {"fixture": {"kind": "phase-1"}},
        }
    }
    observed["kwargs"]["context_variables"]["labbio"]["metadata"]["fixture"][
        "kind"
    ] = "mutated-by-runtime"
    assert context.metadata == {"fixture": {"kind": "phase-1"}}

    run.record_stage_result(result)

    assert run.stage_results == (result,)
    assert run.current_stage is WorkflowStage.UNDERSTAND


@pytest.mark.asyncio
async def test_json_object_result_is_normalized_safely():
    run = WorkflowRun(current_stage=WorkflowStage.INTAKE)
    context = StageContext(
        run_id=run.run_id,
        stage=run.current_stage,
        instruction="Return a JSON mock result.",
    )
    team, _ = _mock_team(
        content='{"stage":"INTAKE","summary":"Accepted mock input.","payload":{}}'
    )

    result = await PantheonStageAdapter(team).run_stage(context)

    assert result.stage is WorkflowStage.INTAKE
    assert result.summary == "Accepted mock input."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        "unstructured prose",
        {"stage": "UNDERSTAND", "payload": {}},
        {
            "stage": "PLAN",
            "summary": "Wrong stage.",
            "payload": {},
        },
    ],
)
async def test_malformed_or_mismatched_stage_result_is_rejected(content):
    run = WorkflowRun(current_stage=WorkflowStage.UNDERSTAND)
    context = StageContext(
        run_id=run.run_id,
        stage=run.current_stage,
        instruction="Return a mock result.",
    )
    team, _ = _mock_team(content=content)

    with pytest.raises(StageResultValidationError):
        await PantheonStageAdapter(team).run_stage(context)

    assert run.stage_results == ()


@pytest.mark.asyncio
async def test_pantheon_agent_exception_is_observable_by_adapter():
    run = WorkflowRun(current_stage=WorkflowStage.PREFLIGHT)
    context = StageContext(
        run_id=run.run_id,
        stage=run.current_stage,
        instruction="Raise the local mock exception.",
    )
    original_error = RuntimeError("mock Pantheon failure")
    team, _ = _mock_team(error=original_error)

    with pytest.raises(StageInvocationError) as caught:
        await PantheonStageAdapter(team).run_stage(context)

    assert caught.value.cause is original_error
    assert caught.value.__cause__ is original_error
    assert caught.value.stage is WorkflowStage.PREFLIGHT
    assert run.stage_results == ()


def test_workflow_run_rejects_result_for_another_stage():
    run = WorkflowRun(current_stage=WorkflowStage.VALIDATE)

    with pytest.raises(ValueError, match="does not match current stage"):
        run.record_stage_result(
            AgentStageResult(
                stage=WorkflowStage.REPORT,
                summary="Mock result for another stage.",
            )
        )

    assert run.stage_results == ()
