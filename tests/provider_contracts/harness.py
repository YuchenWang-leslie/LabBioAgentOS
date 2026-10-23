"""Real AgentLoop assembly shared by the provider-format contracts."""

from __future__ import annotations

from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from labbioagentos.kernel import (
    AgentInvocation,
    AgentLoop,
    AgentMode,
    BudgetLedger,
    BudgetLimits,
    KernelIdentity,
    ModelResultProjector,
    PublicMessage,
    ToolDispatcher,
    ToolRegistry,
)

DEADLINE = 10_000_000_000.0
PROMPT = (PublicMessage(role="SYSTEM", content="Contract rules."),
          PublicMessage(role="USER", content="Synthetic contract request."))


class RecordArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    value: str


class Count(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    count: int


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    result: Count


def _limits(finalization_attempts: int = 2) -> BudgetLimits:
    return BudgetLimits(
        provider_turns=16,
        tokens=1_000_000,
        wall_time_ms=600_000,
        tool_calls=16,
        evidence_entries=16,
        evidence_bytes=256_000,
        delegations=0,
        delegation_depth=0,
        finalization_attempts=finalization_attempts,
    )


def capability_loop(provider):
    """A capability loop with one synthetic tool; returns the loop and its effects."""

    budget = BudgetLedger(_limits())
    registry = ToolRegistry()
    effects: list[str] = []

    async def record(arguments: RecordArguments):
        effects.append(arguments.value)
        return {"recorded": arguments.value}

    registry.register(name="record_value", description="Record one synthetic bounded value.",
                      arguments_model=RecordArguments, handler=record)
    invocation_id = uuid4()
    loop = AgentLoop(
        provider=provider,
        invocation=AgentInvocation(
            identity=KernelIdentity(
                thread_id=uuid4(), workflow_run_id=uuid4(), kernel_run_id=uuid4(),
                invocation_id=invocation_id, chain=(invocation_id,),
            ),
            agent_id="contract-agent", mode=AgentMode.CAPABILITY,
            tool_names=("record_value",),
        ),
        budget_ledger=budget,
        dispatcher=ToolDispatcher(registry, ModelResultProjector(max_bytes=16_000),
                                  budget_ledger=budget),
    )
    return loop, effects


def finalization_loop(provider, *, finalization_attempts: int = 2):
    invocation_id = uuid4()
    return AgentLoop(
        provider=provider,
        invocation=AgentInvocation(
            identity=KernelIdentity(
                thread_id=uuid4(), workflow_run_id=uuid4(), kernel_run_id=uuid4(),
                invocation_id=invocation_id, chain=(invocation_id,),
            ),
            agent_id="contract-finalizer", mode=AgentMode.FINALIZE,
        ),
        budget_ledger=BudgetLedger(_limits(finalization_attempts)),
    )


def event_dumps(loop) -> str:
    return "".join(event.model_dump_json() for event in loop.events)


def observation_dumps(loop) -> str:
    return "".join(item.model_dump_json() for item in loop.observations)
