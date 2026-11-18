"""Typed procedural-memory contracts with no executable Gold Skill behavior."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
import re
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StrictStr,
    StringConstraints,
    field_validator,
    model_validator,
)

from labbioagentos.artifacts import ArtifactExposureClass, ArtifactRef, ArtifactView
from labbioagentos.contracts import InformationAuthority, RunStatus, WorkflowStage
from labbioagentos.execution.error_context import redact_process_text
from labbioagentos.execution.models import (
    ExecutionDiagnostic, ExecutionFailureClass, OutputContractFailureCode,
)
from labbioagentos.model_safety import validate_model_visible_json
from labbioagentos.trace import DelegationProjection, InvocationProjection, InstructionKind


BoundedText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=4000),
]
ShortText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=256),
]


def _text_values(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _text_values(item)
    elif isinstance(value, (tuple, list, set, frozenset)):
        for item in value:
            yield from _text_values(item)


_UNSAFE_REMOTE_TEXT_PATTERNS = (
    r"(?:^|\s)/(?:home|media|mnt|tmp|var|run)/",
    r"\bstorage[_ -]?locator\b",
    r"\breasoning[_ -]?content\b",
    r"\bprovider[_ -]?(?:request|response|raw)[_ -]?body\b",
    r"\b(?:api[_ -]?key|authorization[_ -]?secret)\b",
    r"\bbearer\s+[A-Za-z0-9._~-]{8,}",
    r"-----BEGIN [A-Z ]+ PRIVATE KEY-----",
)


def _reject_unsafe_remote_text(value, *, label: str) -> None:
    for text in _text_values(value):
        if any(
            re.search(pattern, text, flags=re.IGNORECASE)
            for pattern in _UNSAFE_REMOTE_TEXT_PATTERNS
        ):
            raise ValueError(f"{label} contains prohibited unsafe text")


class SkillScope(StrEnum):
    PERSONAL = "PERSONAL"
    PROJECT = "PROJECT"
    LAB = "LAB"


class SkillStatus(StrEnum):
    GOLD = "GOLD"


class SkillUseMode(StrEnum):
    REUSE = "REUSE"
    ADAPT = "ADAPT"
    REFERENCE = "REFERENCE"


class SkillCuratorAuditCategory(StrEnum):
    """Generic defects an Agent auditor can identify before user review."""

    UNSUPPORTED_BY_SOURCE = "UNSUPPORTED_BY_SOURCE"
    SOURCE_FACT_AS_FUTURE_DEFAULT = "SOURCE_FACT_AS_FUTURE_DEFAULT"
    PRESCRIPTIVE_FUTURE_CHOICE = "PRESCRIPTIVE_FUTURE_CHOICE"
    IDENTIFIER_KIND_MISUSE = "IDENTIFIER_KIND_MISUSE"
    RAW_CONTENT_ACCESS = "RAW_CONTENT_ACCESS"
    HIDDEN_FALLBACK = "HIDDEN_FALLBACK"
    OVERSTATED_FAILURE_CAUSE = "OVERSTATED_FAILURE_CAUSE"


class SkillUsageOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class SkillTraceRef(BaseModel):
    """Reference to one trace fact without copying its payload."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    event_id: UUID
    sequence: int = Field(ge=0)
    event_type: StrictStr = Field(min_length=1)
    stage_id: WorkflowStage | None = None
    invocation_id: UUID | None = None
    status: StrictStr | None = Field(default=None, min_length=1)


class SkillInstructionRef(BaseModel):
    """Explicitly marked, sanitized instruction evidence; never hidden reasoning."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    instruction_id: UUID
    trace_event_id: UUID
    stage_id: WorkflowStage
    invocation_id: UUID | None = None
    kind: InstructionKind
    template_id: StrictStr | None = Field(default=None, min_length=1)
    template_version: StrictStr | None = Field(default=None, min_length=1)
    template_hash: StrictStr | None = Field(default=None, min_length=1)
    sanitized_instruction: StrictStr = Field(min_length=1, max_length=16_000)


class SkillExecutionRef(BaseModel):
    """Safe execution lineage reconstructed from execution trace metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    execution_id: UUID
    revision_of: UUID | None = None
    planned_event_id: UUID | None = None
    terminal_event_id: UUID | None = None
    terminal_event_ids: tuple[UUID, ...] = Field(default=(), max_length=128)
    image_key: StrictStr | None = Field(default=None, min_length=1)
    resolved_image: StrictStr | None = Field(default=None, min_length=1)
    script_hash: StrictStr | None = Field(default=None, min_length=1)
    script_artifact_id: UUID | None = None
    input_artifact_ids: tuple[UUID, ...] = ()
    output_artifact_ids: tuple[UUID, ...] = ()
    status: StrictStr
    exit_code: int | None = None
    issue_codes: tuple[ExecutionFailureClass, ...] = Field(default=(), max_length=32)
    issue_detail_codes: tuple[OutputContractFailureCode, ...] = Field(default=(), max_length=32)
    diagnostics: tuple[ExecutionDiagnostic, ...] = Field(default=(), max_length=8)


class SkillInvocationSummary(BaseModel):
    """Bounded model-facing invocation fact without transport payloads."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    invocation_id: UUID
    parent_invocation_id: UUID | None = None
    agent_name: ShortText | None = None
    stage_id: WorkflowStage | None = None
    status: ShortText


class SkillDelegationSummary(BaseModel):
    """Bounded model-facing delegation fact without conversation content."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    invocation_id: UUID
    parent_invocation_id: UUID | None = None
    caller: ShortText
    target: ShortText
    stage_id: WorkflowStage | None = None
    status: ShortText


class SkillArtifactDescriptor(BaseModel):
    """Whitelist-only Artifact identity and shape; no locator or metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    artifact_id: UUID
    artifact_type: ShortText
    exposure_class: ArtifactExposureClass
    run_id: UUID | None = None
    stage_id: WorkflowStage | None = None
    producer_invocation_id: UUID | None = None
    shape: tuple[int, ...] | None = Field(default=None, max_length=16)
    column_count: int = Field(default=0, ge=0)
    dtype_field_count: int = Field(default=0, ge=0)


class SkillCapabilityUsageRef(BaseModel):
    """Safe actor-attributed capability fact with references only."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    capability_invocation_id: UUID
    actor_profile_key: ShortText
    actor_agent_name: ShortText
    capability_name: ShortText
    status: ShortText
    reference_ids: tuple[UUID, ...] = Field(default=(), max_length=128)


class SkillStageContext(BaseModel):
    """Historical Agent statements, not proof that a plan was executed."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    result_id: UUID
    invocation_id: UUID | None = None
    stage_id: WorkflowStage
    authority: Literal[InformationAuthority.MODEL_CONTEXT] = (
        InformationAuthority.MODEL_CONTEXT
    )
    model_summary: StrictStr = Field(min_length=1, max_length=8000)
    model_body: JsonValue

    @model_validator(mode="after")
    def validate_safe_context(self) -> "SkillStageContext":
        if not isinstance(self.model_body, dict):
            raise ValueError("Skill stage context model_body must be an object")
        validate_model_visible_json(
            {"model_summary": self.model_summary, "model_body": self.model_body},
            reject_absolute_paths=True,
        )
        _reject_unsafe_remote_text(
            self.model_dump(mode="python"), label="Skill stage context"
        )
        return self


MAX_CURATION_SOURCE_BYTES = 512_000


def _scan_source_text(text):
    # Recognize a credential or host path immediately following a non-ASCII word.
    return re.sub(r"(?<=[^\x00-\x7f])(?=[A-Za-z~/])", " ", text)


class SkillSourceText(BaseModel):
    """Bounded source text with explicit unavailable/withheld states."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: Literal["AVAILABLE", "NOT_RECORDED", "WITHHELD", "LENGTH_LIMIT"] = "NOT_RECORDED"
    text: StrictStr | None = Field(default=None, min_length=1, max_length=32_000)

    @classmethod
    def from_record(cls, text: str | None):
        if not text or not text.strip():
            return cls()
        if len(text) > 32_000:
            return cls(status="LENGTH_LIMIT")
        try:
            return cls(status="AVAILABLE", text=text)
        except ValueError:
            # Preserve the existing run's evidence without relaying rejected text
            # or replacing the researcher's wording with a redacted instruction.
            return cls(status="WITHHELD")

    @model_validator(mode="after")
    def validate_exposure(self):
        if (self.status == "AVAILABLE") != (self.text is not None):
            raise ValueError("Only an available source request may contain text")
        if self.text is not None:
            validate_model_visible_json(self.text, reject_absolute_paths=True,
                                        max_string_length=32_000, max_serialized_bytes=192_002)
            _reject_unsafe_remote_text(self.text, label="Skill source user request")
            scan = _scan_source_text(self.text)
            if redact_process_text(scan) != scan:
                raise ValueError("Source request includes restricted content")
        return self


class SkillReportPassage(BaseModel):
    """Exact original text at a half-open character interval in the selected report."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    text: StrictStr = Field(min_length=1, max_length=32_000)

    @model_validator(mode="after")
    def validate_passage(self):
        if self.end_offset - self.start_offset != len(self.text):
            raise ValueError("Report passage length differs from its source interval")
        SkillSourceText(status="AVAILABLE", text=self.text)
        return self


def _safe_report_passages(text: str, *, prefix_only: bool) -> tuple[SkillReportPassage, ...]:
    """Preserve verbatim safe intervals without splitting a cross-boundary redaction."""
    paragraphs = list(re.finditer(r".+?(?:\n[ \t\r]*\n|\Z)", text, flags=re.DOTALL))
    if "PRIVATE KEY-----" in text or "-----BEGIN " in text:
        return ()
    local_scan = "".join(redact_process_text(_scan_source_text(m.group())) for m in paragraphs)
    if redact_process_text(_scan_source_text(text)) != local_scan:
        return ()
    spans = []
    open_fence = None
    for match in paragraphs:
        paragraph = match.group()
        fenced = open_fence is not None
        for line in paragraph.splitlines():
            fence = re.match(r"^\s*(`{3,}|~{3,})(.*)$", line)
            if fence:
                fenced = True
                mark, rest = fence.groups()
                if open_fence is None:
                    open_fence = mark
                elif mark.startswith(open_fence) and not rest.strip():
                    open_fence = None
        if fenced or (prefix_only and match.end() == len(text)):
            continue
        if SkillSourceText.from_record(paragraph).status == "AVAILABLE":
            intervals = [match.span()]
        else:
            # Only split on original newlines. Compare redactions solely to detect
            # a cross-line value; replacement text never enters a passage.
            lines = list(re.finditer(r"[^\n]*\n|[^\n]+\Z", paragraph))
            redacted_lines = [redact_process_text(_scan_source_text(line.group())) for line in lines]
            if redact_process_text(_scan_source_text(paragraph)) != "".join(redacted_lines):
                continue
            intervals = []
            for line, redacted in zip(lines, redacted_lines):
                body = line.group()
                exposure = SkillSourceText.from_record(body)
                if exposure.status == "AVAILABLE":
                    intervals.append((match.start() + line.start(), match.start() + line.end()))
                elif exposure.status == "WITHHELD" and SkillSourceText.from_record(redacted).status == "WITHHELD":
                    # A restricted marker outside the diagnostic redactor may
                    # introduce a value on the next line. Keep its paragraph closed.
                    intervals = []
                    break
        for start, end in intervals:
            if spans and start == spans[-1][1]:
                spans[-1] = (spans[-1][0], end)
            else:
                spans.append((start, end))
    return tuple(SkillReportPassage(start_offset=a, end_offset=b, text=text[a:b])
                 for a, b in spans[:64] if SkillSourceText.from_record(text[a:b]).status == "AVAILABLE")


class SkillSelectedReport(BaseModel):
    """Governed prefix of the selected report. The prose is an Agent claim."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    artifact_id: UUID
    authority: Literal[InformationAuthority.MODEL_CONTEXT] = InformationAuthority.MODEL_CONTEXT
    sha256: StrictStr | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    total_characters: int | None = Field(default=None, ge=0)
    truncated: bool | None = None
    excerpt: SkillSourceText
    passages: tuple[SkillReportPassage, ...] = Field(default=(), max_length=64,
        description="When the contiguous excerpt is withheld, these disjoint verbatim spans remain readable. Gaps are withheld.")

    @model_validator(mode="after")
    def validate_partial_coverage(self):
        if self.passages:
            if (self.excerpt.status != "WITHHELD" or self.truncated is not True
                    or self.sha256 is None or self.total_characters is None):
                raise ValueError("Partial report passages require the original identity and explicit omission")
            previous_end = 0
            for passage in self.passages:
                if passage.start_offset < previous_end or passage.end_offset > self.total_characters:
                    raise ValueError("Report passage intervals must be ordered within the original report")
                previous_end = passage.end_offset
        return self

    @classmethod
    def from_prefix(cls, artifact_id: UUID, digest: str, total: int, text: str):
        excerpt = SkillSourceText.from_record(text)
        truncated = len(text) < total
        passages = _safe_report_passages(text, prefix_only=truncated) if excerpt.status == "WITHHELD" else ()
        return cls(artifact_id=artifact_id, sha256=digest, total_characters=total,
                   truncated=truncated or excerpt.text is None, excerpt=excerpt, passages=passages)

    def contains_quote(self, quote: str) -> bool:
        return bool(quote) and (quote in (self.excerpt.text or "") or any(quote in p.text for p in self.passages))


class SkillSourceUserRequest(SkillSourceText):
    """Original researcher wording. Gaps between safe verbatim spans remain withheld."""

    authority: Literal[InformationAuthority.USER_ASSERTION] = InformationAuthority.USER_ASSERTION
    sha256: StrictStr | None = Field(default=None, pattern="^[0-9a-f]{64}$")
    total_characters: int | None = Field(default=None, ge=0)
    passages: tuple[SkillReportPassage, ...] = Field(default=(), max_length=64)

    @classmethod
    def from_record(cls, text: str | None):
        full = SkillSourceText.from_record(text)
        if full.status == "NOT_RECORDED":
            return cls()
        return cls(**full.model_dump(), sha256=sha256(text.encode("utf-8")).hexdigest(),
                   total_characters=len(text),
                   passages=_safe_report_passages(text, prefix_only=False) if full.status == "WITHHELD" else ())

    @model_validator(mode="after")
    def validate_source_identity(self):
        if self.passages:
            if self.status != "WITHHELD" or self.sha256 is None or self.total_characters is None:
                raise ValueError("Partial user wording requires the identity of the withheld original")
            previous_end = 0
            for span in self.passages:
                if span.start_offset < previous_end or span.end_offset > self.total_characters:
                    raise ValueError("User wording spans must be ordered within the original request")
                previous_end = span.end_offset
        if self.text is not None and self.sha256 is not None and (
                self.sha256 != sha256(self.text.encode("utf-8")).hexdigest()
                or self.total_characters != len(self.text)):
            raise ValueError("User wording does not match its original identity")
        return self


class SkillSourceClarification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    agent_question: SkillSourceText
    user_answer: SkillSourceUserRequest


class SkillDeliveryFile(BaseModel):
    """Host-verified identity only. Compare producer_run_id to source_run_id to identify imports."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    artifact_id: UUID
    producer_run_id: UUID
    execution_id: UUID
    sha256: StrictStr = Field(pattern="^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)


class SkillSelectedDelivery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    outputs: tuple[SkillDeliveryFile, ...] = Field(max_length=64)
    programs: tuple[SkillDeliveryFile, ...] = Field(max_length=64)


class SkillOutcomeEvidence(BaseModel):
    """Evidence specific to the user-facing result, frozen with the source."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    selected_report: SkillSelectedReport | None = None
    selected_delivery: SkillSelectedDelivery | None = None
    answered_clarifications: tuple[SkillSourceClarification, ...] = Field(default=(), max_length=3)
    projection_limited: bool = Field(default=False,
        description="Some originally available outcome passages were omitted to fit the curator budget.")


class SkillReportAnchor(BaseModel):
    """Location selected by the reviewer in this run's immutable report."""
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    id: ShortText
    run_id: UUID
    artifact_id: UUID
    sha256: StrictStr = Field(pattern="^[0-9a-f]{64}$")
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)


class _SkillOutcomeJudgment(BaseModel):
    """The Agent's comparison of the requested and actually delivered answer."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    research_question: BoundedText
    primary_result: BoundedText
    assessment: Literal["ALIGNED", "PARTIAL", "MISALIGNED", "INSUFFICIENT_EVIDENCE"]
    explanation: BoundedText

    @model_validator(mode="after")
    def validate_safe_text(self):
        _reject_unsafe_remote_text(self.model_dump(mode="python"), label="Skill outcome review")
        validate_model_visible_json(self.model_dump(mode="json"), reject_absolute_paths=True)
        return self


class SkillOutcomeSelection(_SkillOutcomeJudgment):
    report_anchor_id: ShortText | None = Field(description="ID of one supplied anchor supporting the primary answer, or null if unavailable.")


class SkillOutcomeReview(_SkillOutcomeJudgment):
    report_quote: BoundedText | None
    report_anchor: SkillReportAnchor | None = None


class SkillCurationSourceView(BaseModel):
    """The only Skill source representation permitted at a curator boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_bundle_id: UUID
    source_run_id: UUID
    task_reference: StrictStr | None = Field(default=None, min_length=1, max_length=2000)
    source_user_request: SkillSourceUserRequest = Field(default_factory=SkillSourceUserRequest)
    outcome_evidence: SkillOutcomeEvidence | None = None
    final_status: RunStatus
    workflow_stage_path: tuple[WorkflowStage, ...] = Field(max_length=64)
    stage_context: tuple[SkillStageContext, ...] = Field(default=(), max_length=64)
    invocations: tuple[SkillInvocationSummary, ...] = Field(default=(), max_length=128)
    delegations: tuple[SkillDelegationSummary, ...] = Field(default=(), max_length=128)
    instruction_refs: tuple[SkillInstructionRef, ...] = Field(default=(), max_length=128)
    execution_refs: tuple[SkillExecutionRef, ...] = Field(default=(), max_length=128)
    artifact_descriptors: tuple[SkillArtifactDescriptor, ...] = Field(
        default=(), max_length=256
    )
    artifact_evidence_views: tuple[ArtifactView, ...] = Field(
        default=(),
        max_length=32,
        description=(
            "Bounded policy-controlled views of terminal run outputs; source-run "
            "facts are evidence for abstraction, not fixed future-task choices."
        ),
    )
    failure_refs: tuple[SkillTraceRef, ...] = Field(default=(), max_length=128)
    retry_refs: tuple[SkillTraceRef, ...] = Field(default=(), max_length=128)
    validation_refs: tuple[SkillTraceRef, ...] = Field(default=(), max_length=128)
    capability_usage_refs: tuple[SkillCapabilityUsageRef, ...] = Field(
        default=(), max_length=256
    )

    @model_validator(mode="after")
    def reject_explicit_unsafe_text(self) -> "SkillCurationSourceView":
        _reject_unsafe_remote_text(
            self.model_dump(mode="python"), label="Skill curation source view"
        )
        if len(self.model_dump_json().encode("utf-8")) > MAX_CURATION_SOURCE_BYTES:
            raise ValueError("Skill curation source view exceeds the size bound")
        return self


class SkillSourceBundle(BaseModel):
    """Deterministic evidence projection; it is not a curated or executable Skill."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    bundle_id: UUID = Field(default_factory=uuid4)
    source_run_id: UUID
    task_reference: StrictStr | None = Field(default=None, min_length=1, max_length=2000)
    source_user_request: SkillSourceUserRequest = Field(default_factory=SkillSourceUserRequest)
    outcome_evidence: SkillOutcomeEvidence | None = None
    final_status: RunStatus
    workflow_stage_path: tuple[WorkflowStage, ...]
    stage_context: tuple[SkillStageContext, ...] = Field(default=(), max_length=64)
    invocations: tuple[InvocationProjection, ...] = ()
    delegations: tuple[DelegationProjection, ...] = ()
    instruction_refs: tuple[SkillInstructionRef, ...] = ()
    execution_refs: tuple[SkillExecutionRef, ...] = ()
    artifact_ids: tuple[UUID, ...] = ()
    artifact_refs: tuple[ArtifactRef, ...] = ()
    artifact_evidence_views: tuple[ArtifactView, ...] = Field(
        default=(), max_length=32
    )
    failure_refs: tuple[SkillTraceRef, ...] = ()
    retry_refs: tuple[SkillTraceRef, ...] = ()
    validation_refs: tuple[SkillTraceRef, ...] = ()
    capability_usage_refs: tuple[SkillCapabilityUsageRef, ...] = ()
    trace_event_ids: tuple[UUID, ...]
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("created_at")
    @classmethod
    def require_utc_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("SkillSourceBundle created_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class SkillAdaptationPoint(BaseModel):
    """A future-task choice that Gold deliberately leaves unresolved."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    decision: BoundedText = Field(
        description="Decision the future task must make; never a fixed answer or value."
    )
    evidence_requirements: tuple[BoundedText, ...] = Field(
        min_length=1,
        max_length=16,
        description="Current-task evidence needed before making the decision.",
    )
    selection_considerations: tuple[BoundedText, ...] = Field(
        min_length=1,
        max_length=16,
        description=(
            "Criteria and source-grounded options for the future Agent to evaluate; "
            "a reference choice is not a required choice."
        ),
    )
    revalidation_requirements: tuple[BoundedText, ...] = Field(
        min_length=1,
        max_length=16,
        description="Checks required after the future Agent makes its choice.",
    )
    modifiable: Literal[True] = True


class SkillProcedureDraft(BaseModel):
    """Untrusted curator-authored procedure content without evidence authority."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    applicability: BoundedText
    workflow_outline: tuple[BoundedText, ...] = Field(min_length=1, max_length=100)
    agent_collaboration_guidance: tuple[BoundedText, ...] = Field(
        default_factory=tuple,
        max_length=100,
    )
    execution_guidance: tuple[BoundedText, ...] = Field(
        default_factory=tuple,
        max_length=100,
    )
    parameter_guidance: tuple[BoundedText, ...] = Field(
        default_factory=tuple,
        max_length=100,
        description=(
            "Future-task decision considerations, including source-grounded parameter "
            "choices with conditions and revalidation; historical values are not "
            "unconditional future defaults."
        ),
    )
    input_contract_ids: tuple[ShortText, ...] = Field(
        default_factory=tuple,
        max_length=100,
        description=(
            "Exact reusable input contract identifiers explicitly present in the "
            "safe source view; Artifact UUIDs are not contract identifiers."
        ),
    )
    output_contract_ids: tuple[ShortText, ...] = Field(
        default_factory=tuple,
        max_length=100,
        description=(
            "Exact reusable output contract identifiers explicitly present in the "
            "safe source view; Artifact UUIDs are not contract identifiers."
        ),
    )
    validation_expectations: tuple[BoundedText, ...] = Field(
        default_factory=tuple,
        max_length=100,
    )
    known_failure_modes: tuple[BoundedText, ...] = Field(
        default_factory=tuple,
        max_length=100,
    )
    debug_lessons: tuple[BoundedText, ...] = Field(
        default_factory=tuple,
        max_length=100,
    )
    known_limitations: tuple[BoundedText, ...] = Field(
        default_factory=tuple,
        max_length=100,
    )
    tags: frozenset[ShortText] = Field(default_factory=frozenset, max_length=100)
    artifact_types: frozenset[ShortText] = Field(
        default_factory=frozenset,
        max_length=100,
    )
    reusable_principles: tuple[BoundedText, ...] = Field(
        default_factory=tuple,
        max_length=32,
    )
    adaptation_points: tuple[SkillAdaptationPoint, ...] = Field(
        default_factory=tuple,
        max_length=32,
    )


class SkillProcedure(SkillProcedureDraft):
    """Approved procedure plus host-assembled source evidence references."""

    important_instruction_ids: tuple[UUID, ...] = Field(default=(), max_length=128)
    script_artifact_ids: tuple[UUID, ...] = Field(default=(), max_length=128)
    # Legacy inline references remain readable. New records resolve the complete
    # trace through their proposal/Gold source_bundle_id, not this bounded copy.
    source_trace_event_ids: tuple[UUID, ...] = Field(default=(), max_length=512)


class SkillCuratorDraft(BaseModel):
    """Strict remote-curator output; it cannot set trusted proposal fields."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    proposed_name: ShortText
    description: BoundedText
    procedure: SkillProcedureDraft
    review_notes: tuple[BoundedText, ...] = Field(default=(), max_length=129)

    @model_validator(mode="after")
    def reject_explicit_unsafe_text(self) -> "SkillCuratorDraft":
        _reject_unsafe_remote_text(
            self.model_dump(mode="python"), label="Skill curator draft"
        )
        return self


class SkillGuidanceDraft(BaseModel):
    """Minimal Agent envelope; the reference guide itself is ordinary prose."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    proposed_name: ShortText
    description: BoundedText
    applicability: BoundedText
    guidance: BoundedText = Field(
        description="Readable reference workflow and adaptation advice; Markdown is allowed."
    )
    tags: frozenset[ShortText] = Field(default_factory=frozenset, max_length=32)
    artifact_types: frozenset[ShortText] = Field(default_factory=frozenset, max_length=32)

    @model_validator(mode="after")
    def reject_explicit_unsafe_text(self) -> "SkillGuidanceDraft":
        _reject_unsafe_remote_text(self.model_dump(mode="python"), label="Skill guidance draft")
        return self

    def to_curator_draft(self) -> SkillCuratorDraft:
        """Store the Agent's prose verbatim, without synthesizing procedural choices."""
        return SkillCuratorDraft(
            proposed_name=self.proposed_name, description=self.description,
            procedure=SkillProcedureDraft(
                applicability=self.applicability, workflow_outline=(self.guidance,),
                tags=self.tags, artifact_types=self.artifact_types,
            ),
        )


class SkillAdaptiveProcedureDraft(BaseModel):
    """Agent-authored procedure that separates stable guidance from choices."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    applicability: BoundedText
    workflow_guidance: tuple[BoundedText, ...] = Field(
        min_length=1,
        max_length=32,
        description=(
            "Source-grounded reference steps, including supported methods, tools, "
            "inputs, outputs and checks where available. State applicability; "
            "the future Agent may adapt the sequence."
        ),
    )
    agent_collaboration_guidance: tuple[BoundedText, ...] = Field(
        default=(), max_length=32
    )
    execution_guidance: tuple[BoundedText, ...] = Field(default=(), max_length=32)
    parameter_guidance: tuple[BoundedText, ...] = Field(
        default=(),
        max_length=32,
        description=(
            "Source-grounded reference choices and conditions, never unconditional "
            "defaults; identify what current evidence would justify adaptation."
        ),
    )
    reusable_principles: tuple[BoundedText, ...] = Field(min_length=1, max_length=32)
    adaptation_points: tuple[SkillAdaptationPoint, ...] = Field(
        min_length=1,
        max_length=32,
    )
    validation_expectations: tuple[BoundedText, ...] = Field(
        default=(), max_length=32
    )
    known_failure_modes: tuple[BoundedText, ...] = Field(default=(), max_length=32)
    debug_lessons: tuple[BoundedText, ...] = Field(default=(), max_length=32)
    known_limitations: tuple[BoundedText, ...] = Field(default=(), max_length=32)
    tags: frozenset[ShortText] = Field(default_factory=frozenset, max_length=32)
    artifact_types: frozenset[ShortText] = Field(
        default_factory=frozenset,
        max_length=32,
    )


class SkillAdaptiveCuratorDraft(BaseModel):
    """Strict Agent output for an adaptable, non-executable Gold proposal."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    proposed_name: ShortText
    description: BoundedText
    procedure: SkillAdaptiveProcedureDraft

    @model_validator(mode="after")
    def reject_explicit_unsafe_text(self) -> "SkillAdaptiveCuratorDraft":
        _reject_unsafe_remote_text(
            self.model_dump(mode="python"), label="Adaptive Skill curator draft"
        )
        return self

    def to_curator_draft(self) -> SkillCuratorDraft:
        """Mechanically preserve Agent content in the durable procedure model."""

        procedure = self.procedure
        return SkillCuratorDraft(
            proposed_name=self.proposed_name,
            description=self.description,
            procedure=SkillProcedureDraft(
                applicability=procedure.applicability,
                workflow_outline=procedure.workflow_guidance,
                agent_collaboration_guidance=procedure.agent_collaboration_guidance,
                execution_guidance=procedure.execution_guidance,
                parameter_guidance=procedure.parameter_guidance,
                validation_expectations=procedure.validation_expectations,
                known_failure_modes=procedure.known_failure_modes,
                debug_lessons=procedure.debug_lessons,
                known_limitations=procedure.known_limitations,
                tags=procedure.tags,
                artifact_types=procedure.artifact_types,
                reusable_principles=procedure.reusable_principles,
                adaptation_points=procedure.adaptation_points,
            ),
        )


class SkillCuratorAuditFinding(BaseModel):
    """One advisory Agent opinion; not a deterministic verdict on the guide."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    category: SkillCuratorAuditCategory
    draft_field: ShortText
    statement: BoundedText
    rationale: BoundedText


class SkillCuratorEvidenceCheck(BaseModel):
    """Agent-owned cross-check; citations can be verified, semantics require review."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    draft_field: ShortText
    evidence_ids: tuple[ShortText, ...] = Field(
        max_length=16, description="IDs from the supplied evidence catalog; empty if unsupported."
    )
    rationale: BoundedText


class SkillCuratorAudit(BaseModel):
    """Untrusted Agent advice retained for explicit human review."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    findings: tuple[SkillCuratorAuditFinding, ...] = Field(max_length=64)
    summary: BoundedText
    checks: tuple[SkillCuratorEvidenceCheck, ...] = Field(
        default=(), max_length=32,
        description="Optional detailed cross-checks; not required for advisory guidance review.",
    )

    @model_validator(mode="after")
    def reject_explicit_unsafe_text(self) -> "SkillCuratorAudit":
        _reject_unsafe_remote_text(
            self.model_dump(mode="python"), label="Skill curator audit"
        )
        return self


class _SkillProposalLineage(BaseModel):
    """Scope, evidence of previous use, and the human decision requested."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    scope: SkillScope
    owner_user_id: StrictStr | None = Field(default=None, min_length=1)
    project_id: StrictStr | None = Field(default=None, min_length=1)
    lab_id: StrictStr = Field(default="local-lab", min_length=1)
    parent_skill_id: UUID | None = None
    parent_version: int | None = Field(default=None, ge=1)
    source_usage_record_id: UUID | None = None
    evolution_kind: Literal["UPDATE", "FORK"] = "UPDATE"
    revises_proposal_id: UUID | None = None
    review_feedback: BoundedText | None = None
    outcome_review: SkillOutcomeReview | None = None

    @model_validator(mode="after")
    def validate_scope_and_parent(self) -> "_SkillProposalLineage":
        if self.scope is SkillScope.PERSONAL and self.owner_user_id is None:
            raise ValueError("PERSONAL Skill proposals require owner_user_id")
        if self.scope is SkillScope.PROJECT and self.project_id is None:
            raise ValueError("PROJECT Skill proposals require project_id")
        if (self.parent_skill_id is None) != (self.parent_version is None):
            raise ValueError("parent_skill_id and parent_version must be set together")
        if self.source_usage_record_id is not None and self.parent_skill_id is None:
            raise ValueError("A source usage record requires parent Skill lineage")
        if self.evolution_kind == "FORK" and self.parent_skill_id is None:
            raise ValueError("A fork requires an exact parent Skill version")
        if self.revises_proposal_id is not None and self.review_feedback is None:
            raise ValueError("A revised candidate requires reviewer feedback")
        return self


class SkillProposalContext(_SkillProposalLineage):
    """Trusted host facts used to assemble one immutable Skill proposal."""


class SkillProposal(_SkillProposalLineage):
    """Runtime-curator proposal that remains non-Gold until user approval."""

    proposal_id: UUID = Field(default_factory=uuid4)
    approval_gate_id: StrictStr = Field(
        default_factory=lambda: f"skill-proposal:{uuid4()}",
        min_length=1,
    )
    source_bundle_id: UUID
    source_run_id: UUID
    proposed_name: ShortText
    description: BoundedText
    procedure: SkillProcedure
    review_notes: tuple[BoundedText, ...] = Field(default=(), max_length=129)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("created_at")
    @classmethod
    def require_utc_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("SkillProposal created_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class SkillUserDecision(BaseModel):
    """Explicit LabBio-owned approval/rejection matching one pending gate."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    decision_id: UUID = Field(default_factory=uuid4)
    subject_id: UUID
    gate_id: StrictStr = Field(min_length=1)
    approved: bool
    decided_by: StrictStr = Field(min_length=1)
    decided_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("decided_at")
    @classmethod
    def require_utc_decided_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("SkillUserDecision decided_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class GoldSkill(BaseModel):
    """Immutable approved procedural memory; deliberately has no run/apply API."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    skill_id: UUID
    version: int = Field(ge=1)
    status: SkillStatus = SkillStatus.GOLD
    name: StrictStr = Field(min_length=1, max_length=256)
    description: StrictStr = Field(min_length=1, max_length=4000)
    scope: SkillScope
    source_run_id: UUID
    source_bundle_id: UUID
    source_proposal_id: UUID
    evolution_kind: Literal["UPDATE", "FORK"] = "UPDATE"
    parent_skill_id: UUID | None = None
    parent_version: int | None = Field(default=None, ge=1)
    source_usage_record_id: UUID | None = None
    owner_user_id: StrictStr | None = Field(default=None, min_length=1)
    project_id: StrictStr | None = Field(default=None, min_length=1)
    lab_id: StrictStr = Field(default="local-lab", min_length=1)
    procedure: SkillProcedure
    approved_by: StrictStr = Field(min_length=1)
    approved_at: datetime

    @model_validator(mode="after")
    def validate_scope_and_lineage(self) -> "GoldSkill":
        if self.scope is SkillScope.PERSONAL and self.owner_user_id is None:
            raise ValueError("PERSONAL Gold Skills require owner_user_id")
        if self.scope is SkillScope.PROJECT and self.project_id is None:
            raise ValueError("PROJECT Gold Skills require project_id")
        if (self.parent_skill_id is None) != (self.parent_version is None):
            raise ValueError("parent_skill_id and parent_version must be set together")
        if self.evolution_kind == "FORK":
            if self.version != 1 or self.parent_skill_id is None or self.parent_skill_id == self.skill_id:
                raise ValueError("A fork must start a new Skill at v1 with its source version")
        elif self.version == 1 and self.parent_skill_id is not None:
            raise ValueError("An original Gold Skill v1 cannot have parent version lineage")
        if self.version > 1:
            if self.parent_skill_id != self.skill_id:
                raise ValueError("A later version must preserve its parent skill_id")
            if self.parent_version != self.version - 1:
                raise ValueError("A later version must reference the preceding version")
        return self

    @field_validator("approved_at")
    @classmethod
    def require_utc_approved_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("GoldSkill approved_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class SkillSearchContext(BaseModel):
    """Deterministic eligibility filters, never a scientific similarity query."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    user_id: StrictStr | None = Field(default=None, min_length=1)
    project_id: StrictStr | None = Field(default=None, min_length=1)
    lab_id: StrictStr | None = Field(default=None, min_length=1)
    include_lab: bool = True
    query_text: StrictStr | None = Field(default=None, min_length=1, max_length=500)
    required_tags: frozenset[ShortText] = Field(default_factory=frozenset)
    artifact_types: frozenset[ShortText] = Field(default_factory=frozenset)


class SkillUseProposal(BaseModel):
    """Runtime-selected use mode; the Skill service never generates this choice."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    proposal_id: UUID = Field(default_factory=uuid4)
    approval_gate_id: StrictStr = Field(
        default_factory=lambda: f"skill-use:{uuid4()}",
        min_length=1,
    )
    run_id: UUID
    requesting_user_id: StrictStr = Field(min_length=1)
    project_id: StrictStr = Field(min_length=1)
    lab_id: StrictStr = Field(min_length=1)
    skill_id: UUID
    skill_version: int = Field(ge=1)
    proposed_mode: SkillUseMode
    latest_user_answer_id: ShortText | None = Field(default=None, description="Host-bound last authenticated clarification when this request was made.")
    reason: BoundedText
    proposed_deviations: tuple[BoundedText, ...] = Field(
        default_factory=tuple,
        max_length=100,
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("created_at")
    @classmethod
    def require_utc_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("SkillUseProposal created_at must be timezone-aware")
        return value.astimezone(timezone.utc)

    def decision_scope(self) -> tuple:
        """Changing a model-written reason cannot reset the researcher's decision."""
        return (self.run_id, self.requesting_user_id, self.project_id, self.lab_id,
                self.skill_id, self.skill_version, self.proposed_mode, self.latest_user_answer_id)


class SkillUseAuthorization(BaseModel):
    """Recorded user decision; only approved records authorize contextual use."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    authorization_id: UUID = Field(default_factory=uuid4)
    use_proposal_id: UUID
    approval_gate_id: StrictStr = Field(min_length=1)
    decision_id: UUID
    run_id: UUID
    authorized_user_id: StrictStr = Field(min_length=1)
    project_id: StrictStr = Field(min_length=1)
    lab_id: StrictStr = Field(min_length=1)
    skill_id: UUID
    skill_version: int = Field(ge=1)
    approved: bool
    decided_by: StrictStr = Field(min_length=1)
    decided_at: datetime

    @field_validator("decided_at")
    @classmethod
    def require_utc_decided_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("SkillUseAuthorization decided_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class SkillContextAccess(BaseModel):
    """Proof that an approved full procedure reached its exact authorized run."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    access_id: UUID = Field(default_factory=uuid4)
    authorization_id: UUID
    run_id: UUID
    skill_id: UUID
    skill_version: int = Field(ge=1)
    accessed_by: StrictStr = Field(min_length=1)
    accessed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("accessed_at")
    @classmethod
    def require_utc_accessed_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("SkillContextAccess accessed_at must be timezone-aware")
        return value.astimezone(timezone.utc)


class SkillUsageRecord(BaseModel):
    """Evidence linking one run to the exact approved Skill version used."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    usage_id: UUID = Field(default_factory=uuid4)
    authorization_id: UUID
    run_id: UUID
    skill_id: UUID
    skill_version: int = Field(ge=1)
    proposed_mode: SkillUseMode
    user_approved: bool
    runtime_provided_deviations: tuple[BoundedText, ...] = Field(
        default_factory=tuple,
        max_length=100,
    )
    outcome: SkillUsageOutcome
    resulting_proposal_id: UUID | None = None
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("recorded_at")
    @classmethod
    def require_utc_recorded_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("SkillUsageRecord recorded_at must be timezone-aware")
        return value.astimezone(timezone.utc)
