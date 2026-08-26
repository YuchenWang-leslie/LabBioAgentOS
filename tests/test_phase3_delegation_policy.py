"""Phase 3 contract tests for controlled native Pantheon delegation."""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import MethodType

import pytest
from pantheon.agent import Agent, AgentResponse, AgentRunContext, _RUN_CONTEXT
from pantheon.team import PantheonTeam
from pydantic import ValidationError

from labbioagentos import (
    AgentDescriptor,
    DelegationDecision,
    DelegationOutcome,
    DelegationPolicy,
    InMemoryDelegationPolicy,
    PantheonStageAdapter,
    StageContext,
    WorkflowRun,
    WorkflowStage,
)


ALLOWED_EDGES = {
    "planner": {"specialist", "reviewer"},
    "specialist": {"reviewer"},
    "reviewer": set(),
    "executor": set(),
}


def _agent(name: str) -> Agent:
    return Agent(
        name=name,
        description=f"Mock {name} capability",
        instructions="Use only deterministic test fixtures.",
        model="openai/mock-model",
    )


def _contains_identity(value, target: object, seen: set[int] | None = None) -> bool:
    if value is target:
        return True
    seen = seen or set()
    value_id = id(value)
    if value_id in seen:
        return False
    seen.add(value_id)
    if isinstance(value, dict):
        return any(
            _contains_identity(key, target, seen)
            or _contains_identity(item, target, seen)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_identity(item, target, seen) for item in value)
    if hasattr(value, "__dict__"):
        return _contains_identity(vars(value), target, seen)
    return False


@contextmanager
def _run_context(agent: Agent, kwargs: dict, execution_context_id=None):
    token = _RUN_CONTEXT.set(
        AgentRunContext(
            agent=agent,
            memory=kwargs.get("memory"),
            execution_context_id=execution_context_id,
            process_step_message=kwargs.get("process_step_message"),
            process_chunk=kwargs.get("process_chunk"),
        )
    )
    try:
        yield
    finally:
        _RUN_CONTEXT.reset(token)


async def _run_stage_delegation(
    *,
    caller_name: str,
    target_name: str,
    policy: DelegationPolicy,
    target_run=None,
    agent_run_overrides: dict | None = None,
    agent_names: tuple[str, ...] = ("planner", "specialist", "reviewer", "executor"),
    call_context: dict | None = None,
    caller_execution_context_id: str | None = None,
    max_delegate_depth: int | None = 5,
    dispatch_via_agent: bool = False,
):
    ordered_names = (caller_name,) + tuple(
        name for name in agent_names if name != caller_name
    )
    agents = {name: _agent(name) for name in ordered_names}
    team = PantheonTeam(
        agents=list(agents.values()),
        use_summary=False,
        max_delegate_depth=max_delegate_depth,
    )
    observed: dict = {"target_calls": 0}

    if target_run is None:
        async def target_run(self, message, **kwargs):
            observed["target_calls"] += 1
            observed["target_message"] = message
            observed["target_kwargs"] = kwargs
            await kwargs["process_step_message"](
                {"role": "assistant", "content": "mock child step"}
            )
            return AgentResponse(
                agent_name=self.name,
                content=f"{self.name} completed",
                details=None,
            )

    agents[target_name].run = MethodType(target_run, agents[target_name])
    for name, run_override in (agent_run_overrides or {}).items():
        agents[name].run = MethodType(run_override, agents[name])

    async def caller_run(self, message, **kwargs):
        observed["caller_message"] = message
        observed["caller_kwargs"] = kwargs
        context_variables = dict(kwargs["context_variables"])
        context_variables.update(call_context or {})
        context_variables.setdefault("tool_call_id", f"call-{target_name}")
        with _run_context(
            self,
            kwargs,
            execution_context_id=caller_execution_context_id,
        ):
            observed["listed_agents"] = await self.functions["list_agents"](
                context_variables=context_variables,
            )
            if dispatch_via_agent:
                tool_messages = await self._handle_tool_calls(
                    [
                        {
                            "id": f"call-{target_name}",
                            "function": {
                                "name": "call_agent",
                                "arguments": json.dumps(
                                    {
                                        "agent_name": target_name,
                                        "instruction": (
                                            f"Mock instruction for {target_name}"
                                        ),
                                    }
                                ),
                            },
                        }
                    ],
                    context_variables=kwargs["context_variables"],
                    timeout=5,
                )
                observed["tool_message"] = tool_messages[0]
                observed["delegation_result"] = tool_messages[0]["raw_content"]
            else:
                observed["delegation_result"] = await self.functions["call_agent"](
                    agent_name=target_name,
                    instruction=f"Mock instruction for {target_name}",
                    context_variables=context_variables,
                )
        return AgentResponse(
            agent_name=self.name,
            content={
                "stage": "PLAN",
                "summary": "Mock stage completed.",
                "payload": {"fixture": "phase-3"},
            },
            details=None,
        )

    agents[caller_name].run = MethodType(caller_run, agents[caller_name])
    run = WorkflowRun(current_stage=WorkflowStage.PLAN)
    context = StageContext(
        run_id=run.run_id,
        stage=WorkflowStage.PLAN,
        instruction="Run the deterministic delegation fixture.",
        metadata={"fixture": {"phase": 3}},
    )
    adapter = PantheonStageAdapter(team, delegation_policy=policy)
    result = await adapter.run_stage(context)
    return result, observed, run, context, adapter, team


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("caller", "target"),
    [
        ("planner", "specialist"),
        ("planner", "reviewer"),
        ("specialist", "reviewer"),
    ],
)
async def test_allowed_delegation_succeeds(caller, target):
    result, observed, _, _, _, _ = await _run_stage_delegation(
        caller_name=caller,
        target_name=target,
        policy=InMemoryDelegationPolicy(ALLOWED_EDGES),
    )

    assert observed["target_calls"] == 1
    assert observed["delegation_result"] == f"{target} completed"
    assert len(result.delegations) == 1
    assert result.delegations[0].caller == caller
    assert result.delegations[0].target == target
    assert result.delegations[0].outcome is DelegationOutcome.SUCCEEDED


@pytest.mark.asyncio
async def test_forbidden_delegation_is_structured_and_target_never_runs():
    result, observed, _, _, _, _ = await _run_stage_delegation(
        caller_name="executor",
        target_name="planner",
        policy=InMemoryDelegationPolicy(ALLOWED_EDGES),
    )

    assert observed["target_calls"] == 0
    assert observed["delegation_result"]["labbio_delegation"]["outcome"] == "DENIED"
    assert result.delegations[0].outcome is DelegationOutcome.DENIED
    assert result.delegations[0].caller == "executor"
    assert result.delegations[0].target == "planner"
    assert result.delegations[0].execution_context_id is None


@pytest.mark.asyncio
async def test_denial_remains_structured_through_pantheon_tool_dispatch():
    result, observed, _, _, _, _ = await _run_stage_delegation(
        caller_name="executor",
        target_name="planner",
        policy=InMemoryDelegationPolicy(ALLOWED_EDGES),
        dispatch_via_agent=True,
    )

    assert observed["target_calls"] == 0
    assert observed["tool_message"]["tool_name"] == "call_agent"
    assert observed["tool_message"]["raw_content"]["labbio_delegation"][
        "outcome"
    ] == "DENIED"
    assert "DENIED" in observed["tool_message"]["content"]
    assert result.delegations[0].outcome is DelegationOutcome.DENIED


@pytest.mark.asyncio
async def test_list_agents_exposes_only_policy_allowed_candidates():
    _, observed, _, _, _, _ = await _run_stage_delegation(
        caller_name="planner",
        target_name="specialist",
        policy=InMemoryDelegationPolicy(ALLOWED_EDGES),
    )

    listed = observed["listed_agents"]
    assert "specialist" in listed
    assert "reviewer" in listed
    assert "executor" not in listed
    assert "Mock specialist capability" in listed


@pytest.mark.asyncio
async def test_runtime_caller_still_selects_target_without_policy_ranking():
    result, observed, _, _, _, _ = await _run_stage_delegation(
        caller_name="planner",
        target_name="reviewer",
        policy=InMemoryDelegationPolicy(ALLOWED_EDGES),
    )

    assert "specialist" in observed["listed_agents"]
    assert "reviewer" in observed["listed_agents"]
    assert observed["target_message"] == "Mock instruction for reviewer"
    assert result.delegations[0].target == "reviewer"
    assert not hasattr(InMemoryDelegationPolicy, "select_agent")


@pytest.mark.asyncio
async def test_native_execution_context_parent_id_and_chain_path_survive():
    result, observed, _, _, _, _ = await _run_stage_delegation(
        caller_name="planner",
        target_name="specialist",
        policy=InMemoryDelegationPolicy(ALLOWED_EDGES),
    )

    child_kwargs = observed["target_kwargs"]
    execution_context_id = child_kwargs["execution_context_id"]
    assert "|d1|specialist|" in execution_context_id
    assert child_kwargs["context_variables"]["execution_context_id"] == execution_context_id
    assert child_kwargs["context_variables"]["_metadata"]["chain_path"] == [
        "planner",
        "specialist:call-specialist",
    ]
    record = result.delegations[0]
    assert record.execution_context_id == execution_context_id
    assert record.parent_tool_call_id == "call-specialist"
    assert record.chain_path == (
        "planner",
        "specialist:call-specialist",
    )


@pytest.mark.asyncio
async def test_native_delegation_depth_protection_remains_effective():
    target_called = False

    async def target_run(self, message, **kwargs):
        nonlocal target_called
        target_called = True
        return AgentResponse(agent_name=self.name, content="unexpected", details=None)

    result, observed, _, _, _, _ = await _run_stage_delegation(
        caller_name="specialist",
        target_name="reviewer",
        policy=InMemoryDelegationPolicy({"specialist": {"reviewer"}}),
        target_run=target_run,
        call_context={
            "_metadata": {
                "chain_path": ["planner", "specialist:call-specialist"]
            }
        },
        caller_execution_context_id="root|d1|specialist|abcd",
        max_delegate_depth=1,
    )

    assert not target_called
    assert "depth limit" in observed["delegation_result"]["labbio_delegation"]["error_message"]
    assert result.delegations[0].outcome is DelegationOutcome.FAILED


@pytest.mark.asyncio
async def test_native_ancestor_loop_protection_remains_effective():
    target_called = False

    async def target_run(self, message, **kwargs):
        nonlocal target_called
        target_called = True
        return AgentResponse(agent_name=self.name, content="unexpected", details=None)

    result, observed, _, _, _, _ = await _run_stage_delegation(
        caller_name="specialist",
        target_name="planner",
        policy=InMemoryDelegationPolicy({"specialist": {"planner"}}),
        target_run=target_run,
        call_context={
            "_metadata": {
                "chain_path": ["planner", "specialist:call-specialist"]
            }
        },
        caller_execution_context_id="root|d1|specialist|abcd",
    )

    assert not target_called
    assert "planner" not in observed["listed_agents"]
    assert "parent or ancestor" in observed["delegation_result"][
        "labbio_delegation"
    ]["error_message"]
    assert result.delegations[0].outcome is DelegationOutcome.FAILED


class InspectingPolicy(DelegationPolicy):
    def __init__(self):
        self.observed_contexts: list[StageContext] = []

    def can_call(self, caller, target, stage_context):
        self.observed_contexts.append(stage_context)
        return DelegationDecision(
            allowed=True,
            caller=caller.name,
            target=target.name,
            reason="Allowed by the inspecting test policy.",
        )


@pytest.mark.asyncio
async def test_stage_context_is_policy_readable_but_workflow_run_stays_external():
    policy = InspectingPolicy()
    result, observed, run, context, adapter, team = await _run_stage_delegation(
        caller_name="planner",
        target_name="specialist",
        policy=policy,
    )

    assert policy.observed_contexts
    assert all(item is context for item in policy.observed_contexts)
    assert policy.observed_contexts[0].stage is WorkflowStage.PLAN
    assert policy.observed_contexts[0].metadata == {"fixture": {"phase": 3}}
    with pytest.raises(ValidationError):
        context.stage = WorkflowStage.EXECUTE
    assert run.stage_results == ()
    assert run.current_stage is WorkflowStage.PLAN
    assert not _contains_identity(observed, run)
    assert not _contains_identity(adapter, run)
    assert not _contains_identity(team, run)
    assert result.stage is WorkflowStage.PLAN


@pytest.mark.asyncio
async def test_nested_child_runtime_failure_is_structurally_observable():
    nested: dict = {}

    async def reviewer_run(self, message, **kwargs):
        raise RuntimeError("mock nested child failure")

    async def specialist_run(self, message, **kwargs):
        nested["specialist_context"] = kwargs["context_variables"]
        with _run_context(
            self,
            kwargs,
            execution_context_id=kwargs["execution_context_id"],
        ):
            inner_context = dict(kwargs["context_variables"])
            inner_context["tool_call_id"] = "call-reviewer"
            nested["failure_result"] = await self.functions["call_agent"](
                agent_name="reviewer",
                instruction="Raise the deterministic child exception.",
                context_variables=inner_context,
            )
        await kwargs["process_step_message"](
            {"role": "assistant", "content": "specialist observed child failure"}
        )
        return AgentResponse(
            agent_name=self.name,
            content="specialist returned after observing failure",
            details=None,
        )

    policy = InMemoryDelegationPolicy(
        {
            "planner": {"specialist"},
            "specialist": {"reviewer"},
        }
    )
    result, _, _, _, _, _ = await _run_stage_delegation(
        caller_name="planner",
        target_name="specialist",
        policy=policy,
        target_run=specialist_run,
        agent_run_overrides={"reviewer": reviewer_run},
        agent_names=("planner", "specialist", "reviewer"),
    )

    assert nested["failure_result"]["labbio_delegation"]["outcome"] == "FAILED"
    assert nested["failure_result"]["labbio_delegation"]["error_type"] == "RuntimeError"
    failed = [
        record
        for record in result.delegations
        if record.outcome is DelegationOutcome.FAILED
    ]
    assert len(failed) == 1
    assert failed[0].caller == "specialist"
    assert failed[0].target == "reviewer"
    assert failed[0].error_message == "mock nested child failure"
    assert failed[0].execution_context_id is not None
    assert any(
        record.caller == "planner"
        and record.target == "specialist"
        and record.outcome is DelegationOutcome.SUCCEEDED
        for record in result.delegations
    )


class MalformedPolicy(DelegationPolicy):
    def can_call(self, caller, target, stage_context):
        return {"allowed": True}


@pytest.mark.asyncio
async def test_malformed_policy_decision_fails_closed():
    result, observed, _, _, _, _ = await _run_stage_delegation(
        caller_name="planner",
        target_name="specialist",
        policy=MalformedPolicy(),
    )

    assert observed["target_calls"] == 0
    assert result.delegations[0].outcome is DelegationOutcome.DENIED
    assert "invalid decision" in result.delegations[0].reason


def test_agent_descriptor_contract_is_capability_tag_future_compatible():
    descriptor = AgentDescriptor(
        name="reviewer",
        description="Mock review capability",
        tags=frozenset({"review"}),
    )

    assert descriptor.tags == frozenset({"review"})
    assert not hasattr(descriptor, "rank")
