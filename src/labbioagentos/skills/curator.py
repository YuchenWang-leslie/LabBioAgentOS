"""Strict remote-runtime boundary for procedural Skill curation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
import json

from pantheon.agent import Agent
from pydantic import ValidationError

from .models import (
    SkillAdaptiveCuratorDraft,
    SkillCurationSourceView,
    SkillCuratorAudit,
    SkillCuratorDraft,
)


SKILL_CURATOR_INSTRUCTIONS = """\
Abstract reusable procedural guidance from the supplied safe successful-run
evidence, including any bounded governed Artifact views and historical Agent
stage context. Give a short task-oriented name, a distinguishing description,
and a few readable reusable tags; identify the applicable input/artifact types
without inventing a fixed task taxonomy. Describe applicability, a useful
reference workflow, collaboration, execution and parameter guidance, validation
expectations, failure lessons, and limitations. Retain specific reference steps,
methods, tools, inputs, outputs and source parameter choices when the supplied
source supports them, with conditions and adaptation points for a future task.
Historical Agent plans and statements are MODEL_CONTEXT, not execution proof;
distinguish proposed steps from actions supported by execution refs or governed
Artifact views. Do not summarize a transcript, reproduce scripts, claim that prior
scientific facts apply to a future task, or invent identifiers, scope,
ownership, approval, or lineage. Treat Artifact and execution identifiers as
source lineage, never reusable contract identifiers. RAW script, stdout, and
stderr descriptors prove provenance only and must not be recommended for model
content access. A reference workflow does not prescribe the future Agent's
methods, parameters, code, delegation or tool order. Current-task evidence and
user requirements take precedence; do not turn source values into unconditional
defaults or fill missing procedural details by guessing. If no exact contract
identifier is explicitly present in the safe source view, leave the corresponding
contract-ID list empty. When a governed artifact-query capability is exposed,
use it as needed to ground
procedural lessons in queryable source evidence; it does not authorize access to
RAW content. Return only the required SkillCuratorDraft.
"""


SKILL_CURATOR_AUDIT_INSTRUCTIONS = """\
Audit the supplied safe successful-run evidence and untrusted
SkillCuratorDraft. Return only SkillCuratorAudit findings. Classify every draft
statement that is unsupported by the safe source, turns a source-run fact into
an unconditional future default, makes a future method, parameter, code, agent,
or tool order mandatory regardless of current evidence, misuses an Artifact or
execution UUID as a contract identifier, recommends RAW content access, adds a
hidden fallback, or invents a failure cause. Source-grounded reference steps,
methods, tools and parameter choices
with applicability conditions and adaptation points are allowed: specificity or
an ordered reference workflow alone is not PRESCRIPTIVE_FUTURE_CHOICE. Historical
Agent stage context proves what was proposed or stated, not what executed;
cross-check claimed executed procedures with execution refs and governed views.
Do not mistake an unexecuted plan for an established successful procedure.
Exact source observations do not authorize unconditional future defaults. Do
not propose replacement scientific content or infer facts absent from the source.
"""


SKILL_CURATOR_REVISION_INSTRUCTIONS = """\
Produce one corrected draft in the required response schema from the supplied
safe successful-run evidence, untrusted draft, and independent
SkillCuratorAudit. Resolve every finding without inventing replacement science.
Preserve supported, conditional reference steps and guidance; do not erase a
useful workflow merely because it names a method, tool or source parameter.
Correct unsupported claims and unconditional defaults while keeping current-task
methods, parameters, code, specialists and tool order open to the future Agent.
Distinguish historical Agent plans from execution evidence; do not fill missing
steps or failure causes by guessing. Keep a short task-oriented name and readable
tags that distinguish this Skill from other tasks. Artifact and execution UUIDs
remain source lineage, not contract identifiers. RAW content is not model-readable.
Return only the required draft.
"""


SKILL_CURATOR_ADAPTIVE_INSTRUCTIONS = """\
Create adaptable procedural guidance from the supplied safe successful-run
evidence. Give a short task-oriented name, a distinguishing description, and a
few readable reusable tags; describe the applicable inputs without inventing a
fixed task taxonomy. Include a useful reference task workflow: preserve source-
supported steps, methods, tools, inputs, outputs, checks and parameter choices
with their conditions, not just general safety reminders. Omit unsupported
details rather than inventing them. Historical Agent stage context is
MODEL_CONTEXT: label proposed or stated procedures as such, and corroborate
claims about executed steps with execution refs or governed Artifact views.
Separate reusable principles and reference choices from adaptation points that
the future Agent must decide from current-task evidence. Every adaptation point
must state the evidence, selection considerations and revalidation needed. A
reference sequence is advisory, not a runtime-enforced method, parameter, code,
specialist or tool order; current user requirements take precedence. Do not
copy source-run values as unconditional defaults, invent failure causes,
recommend RAW model access, or copy source identifiers into the procedure. The
resulting procedure will guide a future Agent solving a new compatible task;
it is not a procedure
for curating, reviewing, or editing the completed source run. Retain concrete
guidance where supported while marking what a new task should reconsider.
Return only SkillAdaptiveCuratorDraft.
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


class PantheonAdaptiveSkillCurator(SkillCuratorPort):
    """Use one Agent schema that makes future-task choices explicit."""

    def __init__(
        self,
        agent: Agent,
        *,
        boundary_observer: Callable[[str, object], None] | None = None,
    ):
        if not isinstance(agent, Agent):
            raise TypeError("agent must be a Pantheon Agent")
        if agent.response_format is not SkillAdaptiveCuratorDraft:
            raise ValueError(
                "Adaptive curator must use SkillAdaptiveCuratorDraft response_format"
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
            if isinstance(content, SkillAdaptiveCuratorDraft):
                adaptive = content
            elif isinstance(content, str):
                adaptive = SkillAdaptiveCuratorDraft.model_validate_json(content)
            elif isinstance(content, Mapping):
                adaptive = SkillAdaptiveCuratorDraft.model_validate(content)
            else:
                raise TypeError("Unsupported adaptive curator response value")
        except (ValidationError, ValueError, TypeError) as exc:
            raise SkillCuratorError(
                "Pantheon adaptive curator returned an invalid draft"
            ) from exc
        if self.boundary_observer is not None:
            self.boundary_observer("curator_adaptive_draft", adaptive)
        return adaptive.to_curator_draft()


class PantheonAuditedAdaptiveSkillCurator(SkillCuratorPort):
    """Create, independently audit, and revise one adaptive Agent draft."""

    def __init__(
        self,
        drafting_agent: Agent,
        audit_agent: Agent,
        revision_agent: Agent,
        *,
        boundary_observer: Callable[[str, object], None] | None = None,
    ):
        self.drafting_curator = PantheonAdaptiveSkillCurator(drafting_agent)
        if not isinstance(audit_agent, Agent):
            raise TypeError("audit_agent must be a Pantheon Agent")
        if audit_agent.response_format is not SkillCuratorAudit:
            raise ValueError(
                "Pantheon Skill auditor must use SkillCuratorAudit response_format"
            )
        self.audit_agent = audit_agent
        self.revision_curator = PantheonAdaptiveSkillCurator(revision_agent)
        self.boundary_observer = boundary_observer

    async def propose(self, source: SkillCurationSourceView) -> SkillCuratorDraft:
        if not isinstance(source, SkillCurationSourceView):
            raise TypeError("source must be a SkillCurationSourceView")
        if self.boundary_observer is not None:
            self.boundary_observer("curator_source", source)
        initial_draft = await self._adaptive_draft(
            self.drafting_curator.agent,
            source.model_dump_json(),
            "Pantheon adaptive curator returned an invalid draft",
        )
        if self.boundary_observer is not None:
            self.boundary_observer("curator_initial_adaptive_draft", initial_draft)
        audit_request = json.dumps(
            {
                "source": source.model_dump(mode="json"),
                "draft": initial_draft.model_dump(mode="json"),
            },
            sort_keys=True,
        )
        try:
            response = await self.audit_agent.run(audit_request)
            content = getattr(response, "content", response)
            if isinstance(content, SkillCuratorAudit):
                audit = content
            elif isinstance(content, str):
                audit = SkillCuratorAudit.model_validate_json(content)
            elif isinstance(content, Mapping):
                audit = SkillCuratorAudit.model_validate(content)
            else:
                raise TypeError("Unsupported curator audit response value")
        except (ValidationError, ValueError, TypeError) as exc:
            raise SkillCuratorError(
                "Pantheon curator auditor returned an invalid SkillCuratorAudit"
            ) from exc
        if self.boundary_observer is not None:
            self.boundary_observer("curator_audit", audit)
        revision_request = json.dumps(
            {
                "source": source.model_dump(mode="json"),
                "draft": initial_draft.model_dump(mode="json"),
                "audit": audit.model_dump(mode="json"),
            },
            sort_keys=True,
        )
        revised_draft = await self._adaptive_draft(
            self.revision_curator.agent,
            revision_request,
            "Pantheon adaptive curator reviser returned an invalid draft",
        )
        if self.boundary_observer is not None:
            self.boundary_observer("curator_revised_adaptive_draft", revised_draft)
        return revised_draft.to_curator_draft()

    @staticmethod
    async def _adaptive_draft(
        agent: Agent,
        request: str,
        error_message: str,
    ) -> SkillAdaptiveCuratorDraft:
        try:
            response = await agent.run(request)
            content = getattr(response, "content", response)
            if isinstance(content, SkillAdaptiveCuratorDraft):
                return content
            if isinstance(content, str):
                return SkillAdaptiveCuratorDraft.model_validate_json(content)
            if isinstance(content, Mapping):
                return SkillAdaptiveCuratorDraft.model_validate(content)
            raise TypeError("Unsupported adaptive curator response value")
        except (ValidationError, ValueError, TypeError) as exc:
            raise SkillCuratorError(error_message) from exc
