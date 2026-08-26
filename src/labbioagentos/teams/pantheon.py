"""Composition adapter from LabBio stage contracts to an existing PantheonTeam."""

from __future__ import annotations

import json
from typing import Any

from pantheon.team import PantheonTeam
from pydantic import BaseModel, ValidationError

from labbioagentos.contracts import AgentStageResult, StageContext


class StageAdapterError(RuntimeError):
    """Base error raised at the LabBio/Pantheon stage boundary."""


class StageInvocationError(StageAdapterError):
    """A PantheonTeam invocation failed before yielding a valid stage result."""

    def __init__(self, context: StageContext, cause: Exception):
        self.run_id = context.run_id
        self.stage = context.stage
        self.cause = cause
        super().__init__(
            f"Pantheon stage {context.stage.value!r} failed for run "
            f"{context.run_id}: {cause}"
        )


class StageResultValidationError(StageAdapterError):
    """Pantheon returned content that violates the AgentStageResult contract."""


class PantheonStageAdapter:
    """Invoke a PantheonTeam for one stage without sharing WorkflowRun state."""

    def __init__(self, team: PantheonTeam):
        if not isinstance(team, PantheonTeam):
            raise TypeError("team must be an existing PantheonTeam instance")
        self._team = team

    @property
    def team(self) -> PantheonTeam:
        """The wrapped runtime team; no WorkflowRun is stored by this adapter."""

        return self._team

    async def run_stage(self, context: StageContext) -> AgentStageResult:
        """Run one reasoning stage and validate its structured response.

        Only immutable stage identifiers and JSON metadata cross into Pantheon.
        The owning WorkflowRun is deliberately absent from both the adapter and
        Pantheon context variables.
        """

        serialized_context = context.model_dump(mode="json")
        runtime_context = {
            "labbio": {
                "run_id": serialized_context["run_id"],
                "stage": serialized_context["stage"],
                "metadata": serialized_context["metadata"],
            }
        }

        try:
            response = await self._team.run(
                context.instruction,
                context_variables=runtime_context,
            )
        except Exception as exc:
            raise StageInvocationError(context, exc) from exc

        content = getattr(response, "content", response)
        result = self._validate_result(content)
        if result.stage is not context.stage:
            raise StageResultValidationError(
                f"Pantheon result stage {result.stage.value!r} does not match "
                f"requested stage {context.stage.value!r}."
            )
        return result

    @staticmethod
    def _validate_result(content: Any) -> AgentStageResult:
        if isinstance(content, AgentStageResult):
            return content
        if isinstance(content, BaseModel):
            content = content.model_dump(mode="python")
        elif isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError as exc:
                raise StageResultValidationError(
                    "Pantheon stage result must be an AgentStageResult, mapping, "
                    "or JSON object string."
                ) from exc

        try:
            return AgentStageResult.model_validate(content)
        except (ValidationError, TypeError) as exc:
            raise StageResultValidationError(
                f"Malformed Pantheon stage result: {exc}"
            ) from exc
