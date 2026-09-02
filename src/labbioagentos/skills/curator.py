"""Strict remote-runtime boundary for procedural Skill curation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping

from pantheon.agent import Agent
from pydantic import ValidationError

from .models import SkillCurationSourceView, SkillCuratorDraft


SKILL_CURATOR_INSTRUCTIONS = """\
Abstract reusable procedural guidance from the supplied safe successful-run
evidence. Describe applicability, workflow structure, collaboration guidance,
execution considerations, validation expectations, failure lessons, and
limitations. Do not summarize a transcript, reproduce scripts, claim that prior
scientific facts apply to a future task, or invent identifiers, scope,
ownership, approval, or lineage. Return only the required SkillCuratorDraft.
"""


class SkillCuratorError(RuntimeError):
    """A curator failed to return the strict untrusted draft contract."""


class SkillCuratorPort(ABC):
    """Runtime intelligence receives only a safe view and returns only a draft."""

    @abstractmethod
    async def propose(self, source: SkillCurationSourceView) -> SkillCuratorDraft:
        """Return procedural content without trusted proposal fields."""


class PantheonSkillCurator(SkillCuratorPort):
    """Use one caller-configured Pantheon Agent for strict procedural curation."""

    def __init__(
        self,
        agent: Agent,
        *,
        boundary_observer: Callable[[str, object], None] | None = None,
    ):
        if not isinstance(agent, Agent):
            raise TypeError("agent must be a Pantheon Agent")
        if agent.response_format is not SkillCuratorDraft:
            raise ValueError(
                "Pantheon Skill curator must use SkillCuratorDraft response_format"
            )
        self.agent = agent
        self.boundary_observer = boundary_observer

    async def propose(self, source: SkillCurationSourceView) -> SkillCuratorDraft:
        if not isinstance(source, SkillCurationSourceView):
            raise TypeError("source must be a SkillCurationSourceView")
        if self.boundary_observer is not None:
            self.boundary_observer("curator_source", source)
        try:
            response = await self.agent.run(source.model_dump_json())
            content = getattr(response, "content", response)
            if isinstance(content, SkillCuratorDraft):
                draft = content
            elif isinstance(content, str):
                draft = SkillCuratorDraft.model_validate_json(content)
            elif isinstance(content, Mapping):
                draft = SkillCuratorDraft.model_validate(content)
            else:
                raise TypeError("Unsupported curator response value")
        except (ValidationError, ValueError, TypeError) as exc:
            raise SkillCuratorError(
                "Pantheon curator returned an invalid SkillCuratorDraft"
            ) from exc
        if self.boundary_observer is not None:
            self.boundary_observer("curator_draft", draft)
        return draft
