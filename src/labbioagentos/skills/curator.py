"""Strict remote-runtime boundary for procedural Skill curation."""

from __future__ import annotations

from abc import ABC, abstractmethod
from hashlib import sha256
import json
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from .models import (
    BoundedText,
    SkillAdaptiveCuratorDraft,
    SkillCurationSourceView,
    SkillCuratorAudit,
    SkillCuratorDraft,
    SkillGuidanceDraft,
    SkillOutcomeReview,
    SkillOutcomeSelection,
    SkillReportAnchor,
    SkillSourceText,
    SkillUseProposal,
    _reject_unsafe_remote_text,
)
from .source import SkillSourceProjector
from .updates import SkillUpdatePatch


SOURCE_REQUEST_GUIDANCE = """
When available, source_user_request is the source researcher's own request. Use it
to understand their objective and language preferences. It is user intent, not proof
that a step was executed or a result is valid. Source bookkeeping (including an
absent reference or unavailable request) does not belong in a Skill's applicability.
"""


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
RAW content. Separate reusable procedure from decisions that need fresh evidence.
State each lesson once in its most relevant section rather than repeating it as
a workflow step, failure mode, validation rule and limitation. Checks serve a
current decision or claim, not exhaustive repetition of the historical run.
Return only the required SkillCuratorDraft.
""" + SOURCE_REQUEST_GUIDANCE


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
for its author. Flag redundant sections or historical checks presented as
unconditional future work when they obscure what can be reused and what must be
reconsidered. Do not demand brevity at the expense of a meaningful limitation.
Final approval belongs to the human, not this review.
""" + SOURCE_REQUEST_GUIDANCE


SKILL_CURATOR_REVISION_INSTRUCTIONS = """\
Consider the review's advice and revise the supplied Agent draft where useful.
The advice may be mistaken; compare it with the current draft and source rather
than obeying it blindly. Parameters and interface/schema mappings are not required.
Keep it a concise, useful, adaptable reference guide, not an evidence-audit report.
Preserve the practical workflow and supported guidance. Optional sections may be
empty; no exhaustive historical coverage or per-field evidence table is needed.
Remove repeated lessons across sections while preserving unique conditions and
limitations. Keep reusable guidance distinct from choices requiring new evidence.
Use current task needs as the future authority for methods, parameters and code.
Do not invent historical facts, causal explanations or replacements for missing
evidence. Omit unnecessary incident narratives instead of turning them into
unreliable general rules. Return only the required draft; ownership, version,
lineage and approval are managed by the host, not by you.
""" + SOURCE_REQUEST_GUIDANCE


SKILL_FEEDBACK_EDIT_INSTRUCTIONS = """
The current requested_change.review_feedback is an editorial request about the
existing guide, not source evidence. During a revision pass it is under
source.requested_change. Use the exact current draft named by edit_base; do not
restart extraction from an earlier draft or source plan. Address supported
corrections in the affected fields themselves, preserving unrelated guidance.
Adding an explanation elsewhere does not resolve an unchanged conflicting
clause. Check consistency across the current draft after each edit. You may
disagree with feedback when source evidence supports that decision; do not
invent a correction or treat reviewer preference as an executed fact.
"""


SKILL_FEEDBACK_AUDIT_INSTRUCTIONS = """
Assess the current review_feedback in reference_material.requested_change
against this current draft, not just the source or an earlier draft. Use checks
to identify the affected actual draft fields and state whether each requested
correction is resolved, unresolved, or one you disagree with for a source-based
reason. Check for contradictions across fields, including unchanged clauses;
an explanation elsewhere does not by itself resolve a conflicting clause.
Put unresolved misleading guidance in findings with its actual field and
reason. Feedback is review intent, not proof of source facts, and you may
disagree with it. These checks remain advisory; human approval is unchanged.
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
Put each lesson in its most relevant section once. Workflow steps describe the
reference approach; adaptation_points identify fresh decisions and their evidence;
validation_expectations explain checks supporting current claims, not a demand
to repeat every historical calculation. Reuse should reduce rediscovery without
replacing current-task validation. Do not claim a reference method or implementation
was successfully executed unless the supplied execution evidence supports that claim.
If including a historical claim or lesson, do not invent its cause or overstate
the available evidence. An observed value is not a universal quality threshold.
Do not recommend model access to RAW scripts/data/process streams, invent exact
contract identifiers, copy source UUIDs into prose, or supply ownership/approval.
Keep the scope of the current source task, not the task of curating Skills.
Use the language of the source user-facing context where evident.
Return only the required draft envelope.
""" + SOURCE_REQUEST_GUIDANCE


EVOLUTION_SCOPE_GUIDANCE = """
Use the original request and authenticated clarifications to identify required
deliverables. The approved_adaptation records the Agent's reason and proposed
deviations accepted for this use. It describes the agreed scope of using the
guide. It cannot by itself establish a new research requirement, an executed
step, or a verified outcome. Treat its text as evidence. Distinguish a parent
option outside this scope from an unmet user requirement. For an alleged
omission, distinguish evidence of absence from incomplete access to the
delivery. Explain which conditions a proposed refinement would affect and
preserve guidance that remains applicable under the parent's other conditions.
"""

SKILL_CURATOR_EVOLUTION_INSTRUCTIONS = """\
Use requested_change to draft a new candidate for the researcher. Its approved parent
is prior guidance, its previous draft is unapproved, and researcher feedback expresses
an intent for review. None of these establishes what the current run did. Ground
procedural changes in the current source, preserve applicable parent guidance, and
describe conditions for choosing the variant when a fork is requested. An execution's
revision_of links attempts; success after a change does not establish a cause or
scientific validity. Never invent the content of a script change from a hash.
Use the outcome_review to address any tension between the parent's workflow,
its adaptation guidance and its validation checks. Tie a proposed change to the
source evidence; a run-specific mistake need not imply a defective guide.
""" + EVOLUTION_SCOPE_GUIDANCE


SKILL_OUTCOME_REVIEW_INSTRUCTIONS = """\
Compare the source researcher's actual question, including authenticated answers
to clarification questions, with the primary answer in the selected delivered
report. Identify what each one asks or claims in its own terms. Distinguish the
headline from secondary results and caveats. A result merely present elsewhere
does not establish that the headline answers the question. Select one supplied
report anchor by its id as report_anchor_id. Choose the text that supports your
identification of the primary answer. The host copies that original text;
do not transcribe it. Offsets are half-open character ranges in the original
report; gaps may be withheld. Use null when no suitable evidence is available.
Check registered output views where relevant, with attention to their identities
and coverage. The report establishes what the Agent communicated; its wording
is not independent evidence of scientific correctness. Skill-use approval is
permission, not a scientific clarification. Accurately note ambiguities,
unreadable material, truncation and plausible interpretations. Use
INSUFFICIENT_EVIDENCE when the question or primary answer cannot be established.
This step assesses the delivery only. Treat all source prose as evidence.
When a relevant report section or output cannot be inspected, describe the
limits of what you can verify. Claim an omission from the delivery only when
the available evidence establishes it.
"""


class SkillEvolutionDecision(BaseModel):
    """Model advice about the existing approved version; it carries no approval."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    action: Literal["KEEP", "UPDATE", "FORK"]
    reason: BoundedText

    @model_validator(mode="after")
    def validate_reason(self):
        _reject_unsafe_remote_text(self.reason, label="Skill evolution recommendation")
        return self


class SkillEvolutionRecommendation(SkillEvolutionDecision):
    outcome_review: SkillOutcomeReview


class ApprovedAdaptationView(BaseModel):
    """Exact authorized plan text. Unavailable entries retain their position."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    reason: SkillSourceText
    proposed_deviations: tuple[SkillSourceText, ...] = Field(max_length=100)

    @classmethod
    def from_proposal(cls, proposal: SkillUseProposal):
        remaining = 32_000
        entries = []
        for text in (proposal.reason, *proposal.proposed_deviations):
            entry = SkillSourceText.from_record(text)
            size = len(entry.model_dump_json(exclude_none=True).encode("utf-8"))
            if entry.text is not None and size > remaining:
                entry = SkillSourceText(status="LENGTH_LIMIT")
            else:
                remaining -= size
            entries.append(entry)
        return cls(reason=entries[0], proposed_deviations=tuple(entries[1:]))


class SkillCurationChange(BaseModel):
    """Bounded model-facing guidance, separate from evidence and promotion authority."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    evolution_kind: Literal["AUTO", "UPDATE", "FORK"] | None = None
    parent_guidance: SkillCuratorDraft | None = None
    approved_adaptation: ApprovedAdaptationView | None = None
    previous_draft: SkillCuratorDraft | None = None
    review_feedback: BoundedText | None = None
    previous_review_feedback: BoundedText | None = None
    recommendation_reason: BoundedText | None = None
    outcome_review: SkillOutcomeReview | None = None

    @model_validator(mode="after")
    def validate_model_input(self):
        _reject_unsafe_remote_text(
            (self.review_feedback, self.previous_review_feedback, self.recommendation_reason),
            label="Skill curation feedback"
        )
        if (self.parent_guidance is None) != (self.evolution_kind is None):
            raise ValueError("A Skill evolution requires its approved parent guidance and kind")
        if self.approved_adaptation is not None and self.parent_guidance is None:
            raise ValueError("An approved adaptation requires its parent guidance")
        if len(self.model_dump_json(exclude_none=True).encode("utf-8")) > 512_000:
            raise ValueError("Skill evolution context exceeds the bounded curation input size")
        return self


class SkillCuratorError(RuntimeError):
    """A curator failed to return the strict untrusted draft contract."""


class SkillCuratorPort(ABC):
    """Runtime intelligence receives only a safe view and returns only a draft."""

    @abstractmethod
    async def propose(self, source: SkillCurationSourceView) -> SkillCuratorDraft:
        """Return procedural content without trusted proposal fields."""

    async def propose_with_context(
        self, source: SkillCurationSourceView, *, change: SkillCurationChange
    ) -> SkillCuratorDraft:
        """A legacy curator cannot silently ignore a requested change."""
        raise SkillCuratorError("This curator does not support changes to Skill guidance")

    async def recommend_evolution(
        self, source: SkillCurationSourceView, *, change: SkillCurationChange
    ) -> SkillEvolutionRecommendation:
        raise SkillCuratorError("This curator does not support a Skill evolution recommendation")

    async def assess_outcome(self, source: SkillCurationSourceView) -> SkillOutcomeReview:
        raise SkillCuratorError("This curator does not support research-question review")


class TypedModelInvoker(Protocol):
    """Generic typed model call; curation owns no runtime implementation."""

    async def invoke(
        self,
        *,
        instructions: str,
        request: str,
        response_model: type[BaseModel],
    ) -> BaseModel: ...


class TypedSkillCurator(SkillCuratorPort):
    """Curate one strict draft through a backend-neutral typed invocation port."""

    def __init__(self, invoker: TypedModelInvoker, *, boundary_observer=None):
        self.invoker = invoker
        self.boundary_observer = boundary_observer

    async def propose(self, source: SkillCurationSourceView) -> SkillCuratorDraft:
        if not isinstance(source, SkillCurationSourceView):
            raise TypeError("source must be a SkillCurationSourceView")
        if self.boundary_observer is not None:
            self.boundary_observer("curator_source", source)
        try:
            draft = await self.invoker.invoke(
                instructions=SKILL_CURATOR_INSTRUCTIONS,
                request=json.dumps(SkillSourceProjector.model_view(source), ensure_ascii=False),
                response_model=SkillCuratorDraft,
            )
            value = SkillCuratorDraft.model_validate(draft)
        except (ValidationError, ValueError, TypeError) as exc:
            raise SkillCuratorError("Curator returned an invalid SkillCuratorDraft") from exc
        if self.boundary_observer is not None:
            self.boundary_observer("curator_draft", value)
        return value


def _report_anchors(run_id, report):
    """Offer every released range, without ranking or choosing scientific content."""
    if report is None or report.sha256 is None:
        return []
    spans = ([(0, report.excerpt.text)] if report.excerpt.text else
             [(p.start_offset, p.text) for p in report.passages])
    scope = sha256(f"{run_id}:{report.artifact_id}:{report.sha256}".encode()).hexdigest()[:12]
    # For extremely short lines, grouping preserves the full evidence without
    # expanding a bounded report into thousands of JSON descriptors.
    use_lines = sum(len(text.splitlines()) for _, text in spans) <= 192
    anchors = []
    for start, text in spans:
        for line in text.splitlines(keepends=True) if use_lines else (text,):
            for offset in range(0, len(line), 1000):
                part = line[offset:offset + 1000]
                quoted = part.strip()
                if quoted and SkillSourceText.from_record(quoted).status == "AVAILABLE":
                    begin = start + offset + len(part) - len(part.lstrip())
                    anchors.append({"id": f"{scope}.{len(anchors)+1}", "start_offset": begin,
                                    "end_offset": begin + len(quoted), "text": quoted})
            start += len(line)
    return anchors


class TypedAuditedAdaptiveSkillCurator(SkillCuratorPort):
    """Draft, review, and optionally revise through generic typed invocations."""

    def __init__(self, invoker: TypedModelInvoker, *, boundary_observer=None):
        self.invoker = invoker
        self.boundary_observer = boundary_observer

    async def propose(self, source: SkillCurationSourceView) -> SkillCuratorDraft:
        return await self._compose(source, None)

    async def propose_with_context(
        self, source: SkillCurationSourceView, *, change: SkillCurationChange
    ) -> SkillCuratorDraft:
        return await self._compose(source, change)

    async def assess_outcome(self, source: SkillCurationSourceView) -> SkillOutcomeReview:
        evidence = SkillSourceProjector.model_view(source, include={
            "source_bundle_id", "source_run_id", "source_user_request", "outcome_evidence", "artifact_evidence_views",
        })
        outcome = source.outcome_evidence
        report = outcome.selected_report if outcome is not None else None
        anchors = _report_anchors(source.source_run_id, report)
        by_id = {a["id"]: a for a in anchors}
        if report is not None:
            evidence["outcome_evidence"]["selected_report"] = {
                **report.model_dump(mode="json", exclude={"excerpt", "passages"}),
                "excerpt_status": report.excerpt.status, "anchors": anchors,
            }
        self._observe("curator_outcome_review_request", evidence)
        has_intent = source.source_user_request.text or source.source_user_request.passages or (
            outcome and any(x.user_answer.text or x.user_answer.passages for x in outcome.answered_clarifications))
        request = evidence
        for attempt in (1, 2):
            try:
                selection = await self._invoke(SKILL_OUTCOME_REVIEW_INSTRUCTIONS, json.dumps(request, ensure_ascii=False),
                                               SkillOutcomeSelection)
            except SkillCuratorError:
                code, hint = "INVALID_TYPED_REVIEW", "Return the required structured review with safe, bounded fields."
            else:
                anchor = by_id.get(selection.report_anchor_id)
                if selection.report_anchor_id and anchor is None:
                    code, hint = "UNKNOWN_REPORT_ANCHOR", (
                        "Select one id from this request's selected_report.anchors. "
                        "When it is unavailable, use null and INSUFFICIENT_EVIDENCE.")
                elif selection.assessment != "INSUFFICIENT_EVIDENCE" and not (has_intent and anchor):
                    code, hint = "MISSING_OBJECTIVE_OR_ANCHOR", (
                        "A substantive judgment needs an available researcher objective and a supporting report anchor. "
                        "If either is unavailable, explain the gap and use INSUFFICIENT_EVIDENCE.")
                else:
                    reference = (SkillReportAnchor(id=anchor["id"], run_id=source.source_run_id,
                        artifact_id=report.artifact_id, sha256=report.sha256,
                        start_offset=anchor["start_offset"], end_offset=anchor["end_offset"])
                        if anchor is not None else None)
                    review = SkillOutcomeReview(**selection.model_dump(exclude={"report_anchor_id"}),
                                                report_quote=anchor["text"] if anchor else None, report_anchor=reference)
                    self._observe("curator_outcome_review", {"source_bundle_id": str(source.source_bundle_id),
                                                             **review.model_dump(mode="json")})
                    return review
            self._observe("curator_outcome_review_rejected", {
                "source_bundle_id": str(source.source_bundle_id), "attempt": attempt, "code": code,
            })
            request = {**evidence, "correction": {"code": code, "instruction": hint}}
        raise SkillCuratorError(f"Outcome review rejected after one correction: {code}")

    async def recommend_evolution(
        self, source: SkillCurationSourceView, *, change: SkillCurationChange
    ) -> SkillEvolutionRecommendation:
        if not isinstance(source, SkillCurationSourceView) or change.evolution_kind != "AUTO":
            raise TypeError("Skill evolution recommendations require a source and its parent guidance")
        review = await self.assess_outcome(source)
        request = {
            "source": SkillSourceProjector.model_view(source),
            "outcome_review": review.model_dump(mode="json"),
            "requested_change": change.model_dump(mode="json", exclude_none=True),
        }
        self._observe("curator_evolution_request", request)
        decision = await self._invoke(
            "Review the approved parent guidance against the supplied completed run. "
            "Recommend KEEP if available evidence does not justify a durable change, UPDATE "
            "for a correction or useful refinement within the same intended scope, or FORK "
            "if distinct conditions support a separate guide and the parent remains applicable. "
            "Use the completed outcome_review to judge whether the parent guidance contributed "
            "to, failed to prevent, or was unrelated to any mismatch between the question and the primary answer. "
            "Explain that relationship and remaining uncertainty in your reason. "
            "A run completion or a researcher's preference alone does not establish a repair, "
            "scientific validity or improved performance. Your recommendation authorizes no changes."
            + EVOLUTION_SCOPE_GUIDANCE,
            json.dumps(request, ensure_ascii=False),
            SkillEvolutionDecision,
        )
        recommendation = SkillEvolutionRecommendation(**decision.model_dump(), outcome_review=review)
        self._observe("curator_evolution_recommendation", {
            "source_bundle_id": str(source.source_bundle_id),
            **recommendation.model_dump(mode="json"),
        })
        return recommendation

    async def _compose(self, source, change):
        if not isinstance(source, SkillCurationSourceView):
            raise TypeError("source must be a SkillCurationSourceView")
        self._observe("curator_source", source)
        context = SkillSourceProjector.guidance_view(source)
        review_source = SkillSourceProjector.review_view(source)
        instructions = SKILL_CURATOR_ADAPTIVE_INSTRUCTIONS
        if change is not None:
            requested = change.model_dump(mode="json", exclude_none=True)
            context["requested_change"] = requested
            review_source["requested_change"] = requested
            self._observe("curator_requested_change", requested)
            instructions += SKILL_CURATOR_EVOLUTION_INSTRUCTIONS
        if change is not None and (change.previous_draft is not None or change.evolution_kind == "UPDATE"):
            return await self._compose_update(context, review_source, change)
        initial = await self._invoke(
            instructions,
            json.dumps(context, ensure_ascii=False),
            SkillAdaptiveCuratorDraft,
        )
        self._observe("curator_initial_adaptive_draft", initial)
        audit = await self._audit(review_source, initial, "curator_audit")
        if not audit.findings:
            return self._reviewed(initial.to_curator_draft(), audit, "initial")
        revised = await self._invoke(
            SKILL_CURATOR_REVISION_INSTRUCTIONS,
            json.dumps({
                "source": context,
                "draft": initial.model_dump(mode="json"),
                "audit": audit.model_dump(mode="json"),
            }, ensure_ascii=False),
            SkillAdaptiveCuratorDraft,
        )
        self._observe("curator_revised_adaptive_draft", revised)
        final = await self._audit(review_source, revised, "curator_final_audit")
        return self._reviewed(revised.to_curator_draft(), final, "revised")

    async def _compose_update(self, context, review_source, change):
        instructions = (
            "Return edits to the named fields of the existing Skill. Each edit replaces one "
            "entire field with its supplied value. Omit all unaffected fields. Preserve still "
            "applicable entries inside an edited list. An empty list explicitly removes that "
            "section; use it only when that removal is intended. Use exact input/output "
            "contract identifiers only if explicitly supported. Do not supply host authority, "
            "approval or provenance. The host validates and applies these edits before review."
        )
        if change.review_feedback:
            instructions += SKILL_FEEDBACK_EDIT_INSTRUCTIONS
        base = change.previous_draft or change.parent_guidance
        initial = await self._edit(
            base, SKILL_CURATOR_EVOLUTION_INSTRUCTIONS + instructions,
            {**context, "edit_base": "previous_draft" if change.previous_draft else "parent_guidance"},
        )
        self._observe("curator_initial_update_draft", initial)
        audit = await self._audit(review_source, initial, "curator_audit")
        if not audit.findings:
            return self._reviewed(initial, audit, "initial")
        revised = await self._edit(initial, SKILL_CURATOR_REVISION_INSTRUCTIONS + instructions, {
            "source": context, "draft": initial.model_dump(mode="json"),
            "audit": audit.model_dump(mode="json"), "edit_base": "draft",
        })
        self._observe("curator_revised_update_draft", revised)
        final = await self._audit(review_source, revised, "curator_final_audit")
        return self._reviewed(revised, final, "revised")

    async def _edit(self, base, instructions, payload):
        patch = await self._invoke(instructions, json.dumps(payload, ensure_ascii=False), SkillUpdatePatch)
        try:
            result = patch.apply(base)
        except ValidationError as exc:
            raise SkillCuratorError("Curator returned an invalid Skill update") from exc
        self._observe("curator_update_fields", {"fields": [edit.field for edit in patch.edits]})
        return result

    async def _audit(self, source, draft, kind):
        payload = {"draft": draft.model_dump(mode="json"), "reference_material": source}
        self._observe(f"{kind}_request", payload)
        instructions = SKILL_CURATOR_AUDIT_INSTRUCTIONS
        if source.get("requested_change", {}).get("review_feedback"):
            instructions += SKILL_FEEDBACK_AUDIT_INSTRUCTIONS
        audit = await self._invoke(
            instructions,
            json.dumps(payload, ensure_ascii=False),
            SkillCuratorAudit,
        )
        self._observe(kind, audit)
        return audit

    async def _invoke(self, instructions, request, response_model):
        try:
            value = await self.invoker.invoke(
                instructions=instructions,
                request=request,
                response_model=response_model,
            )
            return response_model.model_validate(value)
        except (ValidationError, ValueError, TypeError) as exc:
            raise SkillCuratorError("Curator returned an invalid typed response") from exc

    def _reviewed(self, draft, audit, revision):
        notes = (audit.summary, *(text for finding in audit.findings
                                 for text in (finding.statement, finding.rationale)))
        self._observe("curator_guidance_ready_for_user_review", {"draft": revision})
        return draft.model_copy(update={"review_notes": notes})

    def _observe(self, kind, value):
        if self.boundary_observer is not None:
            self.boundary_observer(kind, value)
