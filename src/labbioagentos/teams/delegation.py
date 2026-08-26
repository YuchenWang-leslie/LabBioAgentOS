"""LabBio delegation controls layered around Pantheon's native team tools."""

from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator

from pantheon.agent import Agent, get_current_run_context
from pantheon.team import PantheonTeam
from pantheon.team.plugin import TeamPlugin
from pantheon.utils.misc import run_func

from labbioagentos.contracts import (
    AgentDescriptor,
    DelegationDecision,
    DelegationOutcome,
    DelegationRecord,
    StageContext,
)
from labbioagentos.policy import DelegationPolicy


def canonical_agent_name(name: str) -> str:
    """Use the same public-facing slug shape as Pantheon list_agents()."""

    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "agent"


@dataclass
class _StructuralObservation:
    parent_tool_call_id: str | None = None
    chain_path: tuple[str, ...] = ()


@dataclass
class DelegationSession:
    """Task-local Phase 3 collector; it stores no WorkflowRun reference."""

    stage_context: StageContext
    records: list[DelegationRecord] = field(default_factory=list)
    observations: dict[str, _StructuralObservation] = field(default_factory=dict)

    def observe(self, message: dict[str, Any]) -> None:
        execution_context_id = message.get("execution_context_id")
        if not isinstance(execution_context_id, str) or not execution_context_id:
            return
        metadata = message.get("_metadata")
        raw_chain = metadata.get("chain_path") if isinstance(metadata, dict) else None
        chain_path = (
            tuple(item for item in raw_chain if isinstance(item, str))
            if isinstance(raw_chain, list)
            else ()
        )
        parent_tool_call_id = message.get("parent_tool_call_id")
        self.observations[execution_context_id] = _StructuralObservation(
            parent_tool_call_id=(
                parent_tool_call_id
                if isinstance(parent_tool_call_id, str)
                else None
            ),
            chain_path=chain_path,
        )

    def add_record(
        self,
        *,
        caller: str,
        target: str,
        outcome: DelegationOutcome,
        reason: str | None = None,
        execution_context_id: str | None = None,
        parent_tool_call_id: str | None = None,
        error: Exception | None = None,
    ) -> DelegationRecord:
        observation = (
            self.observations.get(execution_context_id)
            if execution_context_id is not None
            else None
        )
        record = DelegationRecord(
            sequence=len(self.records),
            caller=caller,
            target=target,
            stage=self.stage_context.stage,
            outcome=outcome,
            reason=reason,
            execution_context_id=execution_context_id,
            parent_tool_call_id=(
                observation.parent_tool_call_id
                if observation and observation.parent_tool_call_id
                else parent_tool_call_id
            ),
            chain_path=observation.chain_path if observation else (),
            error_type=type(error).__name__ if error is not None else None,
            error_message=str(error) if error is not None else None,
        )
        self.records.append(record)
        return record


_CURRENT_DELEGATION_SESSION: ContextVar[DelegationSession | None] = ContextVar(
    "labbio_delegation_session",
    default=None,
)


@contextmanager
def delegation_session(stage_context: StageContext) -> Iterator[DelegationSession]:
    """Activate one task-local policy context for a stage invocation."""

    session = DelegationSession(stage_context=stage_context)
    token = _CURRENT_DELEGATION_SESSION.set(session)
    try:
        yield session
    finally:
        _CURRENT_DELEGATION_SESSION.reset(token)


class DelegationPolicyPlugin(TeamPlugin):
    """Wrap Pantheon discovery/delegation tools while preserving their mechanics."""

    _agent_line = re.compile(r"^- \*\*(?P<name>[^*]+)\*\*(?P<suffix>.*)$")

    def __init__(self, policy: DelegationPolicy):
        if not isinstance(policy, DelegationPolicy):
            raise TypeError("policy must implement DelegationPolicy")
        self.policy = policy

    def attach_to(self, team: PantheonTeam) -> None:
        """Attach once so Pantheon's normal TeamPlugin lifecycle remains active."""

        for plugin in team.plugins:
            if isinstance(plugin, DelegationPolicyPlugin):
                if plugin is self:
                    return
                raise ValueError("PantheonTeam already has a LabBio delegation policy")
        team.plugins.append(self)

    async def get_toolsets(self, team: PantheonTeam) -> list:
        await self.install(team)
        return []

    async def on_team_created(self, team: PantheonTeam) -> None:
        await self.install(team)

    async def install(self, team: PantheonTeam) -> None:
        """Decorate existing tools; the native implementations remain underneath."""

        descriptors = self._descriptors(team)
        for calling_agent in team.team_agents:
            if not isinstance(calling_agent, Agent):
                raise TypeError(
                    "Controlled Pantheon delegation currently requires local Agent callers"
                )
            if "list_agents" not in calling_agent.functions:
                continue
            original_list = calling_agent.functions["list_agents"]
            original_call = calling_agent.functions["call_agent"]
            if getattr(original_list, "_labbio_delegation_plugin", None) is self:
                continue
            if getattr(original_list, "_labbio_delegation_plugin", None) is not None:
                raise ValueError("list_agents already has a LabBio policy wrapper")

            caller = descriptors[canonical_agent_name(calling_agent.name)]
            filtered_list = self._make_list_wrapper(
                original_list,
                caller,
                descriptors,
            )
            controlled_call = self._make_call_wrapper(
                original_call,
                caller,
                descriptors,
            )
            setattr(filtered_list, "_labbio_delegation_plugin", self)
            setattr(controlled_call, "_labbio_delegation_plugin", self)
            calling_agent.tool(filtered_list, key="list_agents")
            calling_agent.tool(controlled_call, key="call_agent")

    @staticmethod
    def _descriptors(team: PantheonTeam) -> dict[str, AgentDescriptor]:
        descriptors: dict[str, AgentDescriptor] = {}
        for agent in team.team_agents:
            name = canonical_agent_name(agent.name)
            description = getattr(agent, "description", None)
            descriptors[name] = AgentDescriptor(
                name=name,
                description=description if isinstance(description, str) else None,
            )
        return descriptors

    def _make_list_wrapper(
        self,
        original_list,
        caller: AgentDescriptor,
        descriptors: dict[str, AgentDescriptor],
    ):
        async def list_agents(context_variables: dict | None = None):
            """List only agents allowed by LabBio policy and Pantheon ancestry."""

            session = _CURRENT_DELEGATION_SESSION.get()
            if session is None:
                return "No agents available outside an active LabBio stage context."

            upstream = await run_func(
                original_list,
                context_variables=context_variables,
            )
            if not isinstance(upstream, str):
                return upstream

            parsed_lines: list[tuple[str, str]] = []
            for line in upstream.splitlines():
                match = self._agent_line.match(line)
                if match:
                    parsed_lines.append((match.group("name"), line))
            if not parsed_lines:
                return upstream

            candidates = tuple(
                descriptors[name]
                for name, _ in parsed_lines
                if name in descriptors
            )
            try:
                allowed = self.policy.list_allowed_agents(
                    caller,
                    candidates,
                    session.stage_context,
                )
                allowed_names = {candidate.name for candidate in allowed}
            except Exception:
                allowed_names = set()
            visible_lines = [
                line for name, line in parsed_lines if name in allowed_names
            ]
            if not visible_lines:
                return "No agents available under the current LabBio delegation policy."
            return "**Available Agents:**\n\n" + "\n".join(visible_lines) + "\n"

        list_agents.__name__ = "list_agents"
        return list_agents

    def _make_call_wrapper(
        self,
        original_call,
        caller: AgentDescriptor,
        descriptors: dict[str, AgentDescriptor],
    ):
        async def call_agent(
            agent_name: str,
            instruction: str,
            context_variables: dict | None = None,
        ):
            """Validate a runtime-selected target, then call native Pantheon delegation."""

            target_name = canonical_agent_name(agent_name)
            target = descriptors.get(target_name, AgentDescriptor(name=target_name))
            session = _CURRENT_DELEGATION_SESSION.get()
            if session is None:
                return self._unscoped_denial(caller.name, target.name)

            decision = self._evaluate(caller, target, session.stage_context)
            parent_tool_call_id = self._parent_tool_call_id(context_variables)
            if not decision.allowed:
                record = session.add_record(
                    caller=caller.name,
                    target=target.name,
                    outcome=DelegationOutcome.DENIED,
                    reason=decision.reason,
                    parent_tool_call_id=parent_tool_call_id,
                )
                return self._tool_result(record)

            try:
                result = await run_func(
                    original_call,
                    agent_name=agent_name,
                    instruction=instruction,
                    context_variables=context_variables,
                )
            except Exception as exc:
                execution_context_id = self._execution_context_id(
                    parent_tool_call_id
                )
                record = session.add_record(
                    caller=caller.name,
                    target=target.name,
                    outcome=DelegationOutcome.FAILED,
                    reason="Pantheon delegation raised an exception.",
                    execution_context_id=execution_context_id,
                    parent_tool_call_id=parent_tool_call_id,
                    error=exc,
                )
                return self._tool_result(record)

            execution_context_id = self._execution_context_id(parent_tool_call_id)
            session.add_record(
                caller=caller.name,
                target=target.name,
                outcome=DelegationOutcome.SUCCEEDED,
                reason=decision.reason,
                execution_context_id=execution_context_id,
                parent_tool_call_id=parent_tool_call_id,
            )
            return result

        call_agent.__name__ = "call_agent"
        return call_agent

    def _evaluate(
        self,
        caller: AgentDescriptor,
        target: AgentDescriptor,
        stage_context: StageContext,
    ) -> DelegationDecision:
        try:
            raw_decision = self.policy.can_call(caller, target, stage_context)
            decision = DelegationDecision.model_validate(raw_decision)
        except Exception as exc:
            return DelegationDecision(
                allowed=False,
                caller=caller.name,
                target=target.name,
                reason=f"Delegation policy returned an invalid decision: {type(exc).__name__}.",
            )
        if decision.caller != caller.name or decision.target != target.name:
            return DelegationDecision(
                allowed=False,
                caller=caller.name,
                target=target.name,
                reason="Delegation policy decision did not match the proposed edge.",
            )
        return decision

    @staticmethod
    def _parent_tool_call_id(context_variables: dict | None) -> str | None:
        value = (context_variables or {}).get("tool_call_id")
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _execution_context_id(parent_tool_call_id: str | None) -> str | None:
        if parent_tool_call_id is None:
            return None
        run_context = get_current_run_context()
        if run_context is None:
            return None
        return run_context.sub_agent_exec_ids.get(parent_tool_call_id)

    @staticmethod
    def _tool_result(record: DelegationRecord) -> dict[str, Any]:
        return {
            "labbio_delegation": record.model_dump(mode="json"),
            "message": (
                f"Delegation {record.outcome.value.lower()}: "
                f"{record.caller} -> {record.target}."
            ),
        }

    @staticmethod
    def _unscoped_denial(caller: str, target: str) -> dict[str, Any]:
        return {
            "labbio_delegation": {
                "outcome": DelegationOutcome.DENIED.value,
                "caller": caller,
                "target": target,
                "reason": "No active LabBio stage context.",
            },
            "message": "Delegation denied outside an active LabBio stage context.",
        }
