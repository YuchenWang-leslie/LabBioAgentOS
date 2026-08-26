"""Architecture-neutral contracts for a LabBio workflow stage.

These models intentionally contain no transition engine, scientific method selection,
execution behavior, persistence, or biological interpretation.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictStr


class WorkflowStage(StrEnum):
    """Names of workflow stages; transition behavior belongs to Phase 2."""

    INTAKE = "INTAKE"
    UNDERSTAND = "UNDERSTAND"
    PLAN = "PLAN"
    PREFLIGHT = "PREFLIGHT"
    EXECUTE = "EXECUTE"
    VALIDATE = "VALIDATE"
    INTERPRET = "INTERPRET"
    REPORT = "REPORT"
    LEARN = "LEARN"
    SEARCH = "SEARCH"
    DEBUG = "DEBUG"
    USER_GATE = "USER_GATE"


class StageContext(BaseModel):
    """Immutable, minimum input supplied to one Pantheon reasoning stage."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    run_id: UUID
    stage: WorkflowStage
    instruction: StrictStr = Field(min_length=1)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class AgentStageResult(BaseModel):
    """Validated structured result returned by the Pantheon stage runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: WorkflowStage
    summary: StrictStr = Field(min_length=1)
    payload: dict[str, JsonValue] = Field(default_factory=dict)


class WorkflowRun(BaseModel):
    """Minimum mutable LabBio-owned state for the current phase.

    Recording a result does not transition to another stage. Branching, retries,
    user gates, persistence, and transition rules belong to WorkflowEngine in Phase 2.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True, strict=True)

    run_id: UUID = Field(default_factory=uuid4)
    current_stage: WorkflowStage
    stage_results: tuple[AgentStageResult, ...] = ()

    def record_stage_result(self, result: AgentStageResult) -> None:
        """Record a result for the current stage without performing a transition."""

        if result.stage is not self.current_stage:
            raise ValueError(
                f"Result stage {result.stage.value!r} does not match current stage "
                f"{self.current_stage.value!r}."
            )
        self.stage_results = (*self.stage_results, result)
