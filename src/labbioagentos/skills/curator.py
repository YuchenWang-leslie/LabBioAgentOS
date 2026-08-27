"""Architecture-neutral port for future runtime-model Skill curation."""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import SkillProposal, SkillSourceBundle


class SkillCuratorPort(ABC):
    """Future Pantheon/runtime LLM boundary; no heuristic implementation exists."""

    @abstractmethod
    def propose(self, source: SkillSourceBundle) -> SkillProposal:
        """Return runtime-curated structured procedural content."""
