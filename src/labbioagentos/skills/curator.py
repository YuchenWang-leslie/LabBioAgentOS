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
    SkillGuidanceDraft,
)
from .source import SkillSourceProjector


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
Review draft as a concise capability guide: what it helps with, when it applies,
and a useful approach that a future Agent can adapt. This is advisory feedback
for the author and human approver, not scientific certification or an execution
contract. Do not demand exact parameters, API mappings, artifact schemas,
exhaustive evidence tables or a rigid step sequence.
Reference material provides context, not text authored by this draft. Do not
attribute source statements to the draft or treat absent preview information as
proof of absent content. Flag misleading factual claims or unsafe advice without
inventing missing facts. Plans can inform suggestions without proving execution.
Return SkillCuratorAudit with a short summary and only useful concerns in
findings; use [] when none. Detailed checks are optional. Do not write the guide
for its author. Final approval belongs to the human, not this review.
"""


SKILL_CURATOR_REVISION_INSTRUCTIONS = """\
Consider the review's advice and revise the supplied Agent draft where useful.
The advice may be mistaken; compare it with the current draft and source rather
than obeying it blindly. Parameters and interface/schema mappings are not required.
Keep it a concise, useful, adaptable reference guide, not an evidence-audit report.
Preserve the practical workflow and supported guidance. Optional sections may be
empty; no exhaustive historical coverage or per-field evidence table is needed.
Use current task needs as the future authority for methods, parameters and code.
Do not invent historical facts, causal explanations or replacements for missing
evidence. Omit unnecessary incident narratives instead of turning them into
unreliable general rules. Return only the required draft; ownership, version,
lineage and approval are managed by the host, not by you.
"""


SKILL_CURATOR_ADAPTIVE_INSTRUCTIONS = """\
Write a WF+Skills reference Skill for a future compatible task using the
supplied safe source as context. This is guidance, not a rigid pipeline,
scientific certification, or a transcript of the completed run.

Structure the Skill as workflow guidance + per-step direction hints:

1. Give a short task-oriented name, description, and applicability.

2. Write workflow_guidance as a SEQUENCE OF DISTINCT STEPS (one string per
   step, not one paragraph). Each step should state:
   - What to accomplish at this stage
   - Which tools/methods/approaches to consider (direction, not exact code)
   - What evidence or output to produce
   - What to check before moving on
   Keep each step to 2-4 sentences. 4-8 steps is typical.

3. Write parameter_guidance as practical hints for key decisions the future
   Agent will face: which parameters matter, what ranges are reasonable,
   what tradeoffs to consider. State conditions, not fixed values.

4. Write reusable_principles as short, memorable rules that apply beyond
   this exact task (e.g. "always check mitochondrial fraction before
   filtering", "cluster resolution should match expected cell-type diversity").

5. Write adaptation_points for decisions that MUST be made fresh each time:
   what evidence to gather, what options exist, how to validate the choice.

6. Include validation_expectations (what good output looks like) and
   known_failure_modes (common pitfalls to watch for).

Tags and artifact types are optional catalog information. Do not enumerate
historical artifacts or fill out an audit. Exact parameters, tool interfaces
and artifact schema mappings are not required; choices and order belong to
the future task and user preferences.
Source plans can inform clearly advisory steps without proving each step ran.
Prefer reusable guidance over cataloguing historical failures or numeric results.
If including a historical claim or lesson, do not invent its cause or overstate
the available evidence. An observed value is not a universal quality threshold.
Do not recommend model access to RAW scripts/data/process streams, invent exact
contract identifiers, copy source UUIDs into prose, or supply ownership/approval.
Keep the scope of the current source task, not the task of curating Skills.
Use the language of the source user-facing context where evident.
Return only the required draft envelope.
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
        if agent.response_format not in (SkillAdaptiveCuratorDraft, SkillGuidanceDraft):
            raise ValueError(
                "Adaptive curator must use a supported Skill draft response_format"
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
            draft_type = self.agent.response_format
            if isinstance(content, draft_type):
                adaptive = content
            elif isinstance(content, str):
                adaptive = draft_type.model_validate_json(content)
            elif isinstance(content, Mapping):
                adaptive = draft_type.model_validate_json(json.dumps(content))
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
    """Agent-authored guidance and one revision, with advisory human-review notes."""

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
            raise ValueError("Pantheon Skill auditor must use SkillCuratorAudit response_format")
        self.audit_agent = audit_agent
        self.revision_curator = PantheonAdaptiveSkillCurator(revision_agent)
        self.boundary_observer = boundary_observer

    async def propose(self, source: SkillCurationSourceView) -> SkillCuratorDraft:
        if not isinstance(source, SkillCurationSourceView):
            raise TypeError("source must be a SkillCurationSourceView")
        self._observe("curator_source", source)
        context = SkillSourceProjector.guidance_view(source)
        review_source = SkillSourceProjector.review_view(source)
        self._observe("curator_writing_source", context)
        initial = await self._adaptive_draft(
            self.drafting_curator.agent, json.dumps(context, ensure_ascii=False),
            "Pantheon adaptive curator returned an invalid draft",
        )
        self._observe("curator_initial_adaptive_draft", initial)
        audit = await self._audit(review_source, initial, "curator_audit")
        if not audit.findings:
            return self._reviewed_draft(initial, audit, "initial")
        request = json.dumps({
            "source": context, "draft": initial.model_dump(mode="json"),
            "audit": audit.model_dump(mode="json"),
        }, ensure_ascii=False)
        revised = await self._adaptive_draft(
            self.revision_curator.agent, request,
            "Pantheon adaptive curator reviser returned an invalid draft",
        )
        self._observe("curator_revised_adaptive_draft", revised)
        final = await self._audit(review_source, revised, "curator_final_audit")
        return self._reviewed_draft(revised, final, "revised")

    def _reviewed_draft(self, draft, audit, revision):
        notes = (audit.summary, *(text for finding in audit.findings
                                 for text in (finding.statement, finding.rationale)))
        self._observe("curator_guidance_ready_for_user_review", {"draft": revision})
        return draft.to_curator_draft().model_copy(update={"review_notes": notes})

    def _observe(self, kind, value):
        if self.boundary_observer is not None:
            self.boundary_observer(kind, value)

    async def _audit(self, source, draft, boundary_kind):
        payload = {"draft": draft.model_dump(mode="json"), "reference_material": source}
        self._observe(f"{boundary_kind}_request", payload)
        request = json.dumps(payload, ensure_ascii=False)

        try:
            response = await self.audit_agent.run(request)
            content = getattr(response, "content", response)
            if isinstance(content, SkillCuratorAudit):
                audit = content
            elif isinstance(content, str):
                audit = SkillCuratorAudit.model_validate_json(content)
            elif isinstance(content, Mapping):
                audit = SkillCuratorAudit.model_validate_json(json.dumps(content))
            else:
                raise TypeError("Unsupported curator audit response value")
        except (ValidationError, ValueError, TypeError) as exc:
            self._observe("curator_audit_invalid", {
                "audit_boundary": boundary_kind, "reason_code": "INVALID_REVIEW_RESPONSE",
            })
            raise SkillCuratorError("Pantheon curator returned an invalid review") from exc
        self._observe(boundary_kind, audit)
        return audit

    @staticmethod
    async def _adaptive_draft(
        agent: Agent, request: str, error_message: str,
    ) -> SkillAdaptiveCuratorDraft | SkillGuidanceDraft:
        try:
            response = await agent.run(request)
            content = getattr(response, "content", response)
            draft_type = agent.response_format
            if isinstance(content, draft_type):
                return content
            if isinstance(content, str):
                return draft_type.model_validate_json(content)
            if isinstance(content, Mapping):
                return draft_type.model_validate_json(json.dumps(content))
            raise TypeError("Unsupported adaptive curator response value")
        except (ValidationError, ValueError, TypeError) as exc:
            raise SkillCuratorError(error_message) from exc
