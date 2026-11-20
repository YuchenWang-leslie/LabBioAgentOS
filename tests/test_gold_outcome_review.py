"""The evolution reviewer must compare the authenticated goal with the selected delivery."""
from hashlib import sha256
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from labbioagentos.artifacts import ArtifactExposureService, ExposurePolicy
from labbioagentos.contracts import ClarificationQuestion, ClarificationRecord, RunStatus, WorkflowStage
from labbioagentos.local_gold_source import source_outcome_evidence
from labbioagentos.runtime.contracts import ReportStageBody, RuntimeReference, RuntimeReferenceKind
from labbioagentos.runtime.reporting import ReportSubmissionService
from labbioagentos.skills.curator import SkillCurationChange, SkillCuratorError, TypedAuditedAdaptiveSkillCurator
from labbioagentos.skills.models import (
    SkillCurationSourceView, SkillCuratorDraft, SkillOutcomeEvidence, SkillOutcomeReview, SkillOutcomeSelection,
    SkillProcedureDraft, SkillSelectedReport, SkillSourceText, SkillSourceUserRequest,
)
from test_gold_evolution_curation import source
from test_local_gold import principal, service


@pytest.mark.parametrize("length,expected_length,partial", [(22, 22, False), (8401, 8401, False),
                                                           (33000, 32000, True)])
def test_only_selected_run_report_and_authenticated_answers_are_frozen(source, length, expected_length, partial):
    app, run_id, owner, workspace = source
    app.artifact_exposure = ArtifactExposureService(app.artifact_store, ExposurePolicy(),
                                                  access_service=app.access_service)
    submit = ReportSubmissionService(app.artifact_store, app.access_service)
    def report(text, rid):
        receipt = submit.submit(title="Result", report_text=text, evidence_artifact_ids=(),
                                principal=owner, workspace=workspace, run_id=rid,
                                stage_id=WorkflowStage.REPORT, invocation_id=uuid4())
        return SimpleNamespace(body=ReportStageBody(report_summary="summary",
            report_reference=RuntimeReference(kind=RuntimeReferenceKind.ARTIFACT,
                                              reference_id=str(receipt.report_artifact_id))))
    def answer(who, text):
        return ClarificationRecord(question_id="question", source_stage=WorkflowStage.UNDERSTAND,
            question=ClarificationQuestion(issue_key="scope", prompt="Which period?", why_needed="The target varies."),
            status="ANSWERED", answered_by=who, answer_text=text)
    text = "A" * length
    earlier, selected = report("Previous draft", run_id), report(text, run_id)
    record = SimpleNamespace(run_id=run_id, owner_user_id=owner.user_id, project_id=workspace.project_id,
        lab_id=owner.lab_id, input_artifact_ids=(), context_artifact_ids=(),
        runtime_results=(earlier, selected), workflow_run=SimpleNamespace(
        clarifications=(answer(owner.user_id, "The full period."), answer("someone-else", "Foreign reply.")),
        gate_decisions=("Approved the skill using a different criterion.",)))
    result = source_outcome_evidence(app, record, run_id, owner, workspace)
    assert str(result.selected_report.artifact_id) == selected.body.report_reference.reference_id
    assert result.selected_report.sha256 == sha256(text.encode()).hexdigest()
    assert result.selected_report.excerpt.text == text[:expected_length]
    assert result.selected_report.total_characters == length and result.selected_report.truncated == partial
    assert len(result.answered_clarifications) == 1
    assert result.answered_clarifications[0].user_answer.text == "The full period."
    assert "Approved the skill" not in result.model_dump_json()
    record.runtime_results = (report("Do not expose this.", uuid4()),)
    with pytest.raises(PermissionError):
        source_outcome_evidence(app, record, run_id, owner, workspace)
    record.runtime_results = (report("Output path: /media/private/file", run_id),)
    withheld = source_outcome_evidence(app, record, run_id, owner, workspace).selected_report
    assert withheld.excerpt.status == "WITHHELD" and withheld.excerpt.text is None


@pytest.mark.asyncio
@pytest.mark.parametrize("question,choice", [(None,"first"), ("Requested B", None),
                                           ("Requested B", "Invented")], ids=["no-intent","no-anchor","fabricated"])
async def test_invalid_outcome_review_cannot_reach_the_action_decision(question, choice):
    view = SkillCurationSourceView(source_run_id=uuid4(), source_bundle_id=uuid4(), final_status=RunStatus.COMPLETED,
        workflow_stage_path=(), source_user_request=SkillSourceUserRequest.from_record(question),
        outcome_evidence=SkillOutcomeEvidence(selected_report=SkillSelectedReport.from_prefix(
            uuid4(), sha256(b"Reported A.").hexdigest(), 11, "Reported A.")))
    parent = SkillCuratorDraft(proposed_name="Original", description="Conditional guide",
                               procedure=SkillProcedureDraft(applicability="Research task", workflow_outline=("Check.",)))
    responses = []
    class Invoker:
        async def invoke(self, *, instructions, request, response_model):
            responses.append(response_model)
            assert response_model is SkillOutcomeSelection
            evidence = json.loads(request)["outcome_evidence"]["selected_report"]["anchors"]
            anchor = evidence[0]["id"] if choice == "first" else choice
            return SkillOutcomeSelection(research_question="Requested B", primary_result="Reported A", report_anchor_id=anchor,
                                      assessment="MISALIGNED", explanation="The target is different.")
    with pytest.raises(SkillCuratorError):
        await TypedAuditedAdaptiveSkillCurator(Invoker()).recommend_evolution(
            view, change=SkillCurationChange(evolution_kind="AUTO", parent_guidance=parent))
    assert responses == [SkillOutcomeSelection, SkillOutcomeSelection]


def test_mixed_report_keeps_only_exact_contiguous_safe_passages(source):
    from labbioagentos.skills.models import SkillSelectedReport
    public = "The published outcome is A.\n\n"
    withheld = "Source file: /media/private/workspace/file\n\n"
    tail = "Secondary result: B."
    text = public + withheld + tail
    report = SkillSelectedReport.from_prefix(uuid4(), sha256(text.encode()).hexdigest(), len(text), text)
    assert report.excerpt.text is None and report.truncated
    assert [text[p.start_offset:p.end_offset] for p in report.passages] == [public, tail]
    assert report.contains_quote(public.strip()) and report.contains_quote(tail)
    assert not report.contains_quote(public + tail)
    assert "/media/private" not in report.model_dump_json()
    mixed_secret = "Safe first.\n\npassword=\n\nfixture-CONTINUATION\n\nSafe end."
    sealed = SkillSelectedReport.from_prefix(uuid4(), sha256(mixed_secret.encode()).hexdigest(),
                                             len(mixed_secret), mixed_secret)
    assert sealed.excerpt.status == "WITHHELD" and not sealed.passages
    fenced = "Header.\n\n~~~sh\ncat /media/private/extract\n\nfixture-fenced-body\n~~~\n\nDiscussion."
    protected = SkillSelectedReport.from_prefix(uuid4(), sha256(fenced.encode()).hexdigest(), len(fenced), fenced)
    assert protected.contains_quote("Discussion.") and "fixture-fenced-body" not in protected.model_dump_json()


def test_partial_report_preserves_complete_safe_lines_and_exact_offsets():
    first = "方法：先汇总，再做 CPM。\n"
    hidden = "混合口径 /private/fixture\n"
    last = "program_5 的方向向下。\n两个结果均为探索性。"
    text = first + hidden + last
    digest = sha256(text.encode()).hexdigest()
    report = SkillSelectedReport.from_prefix(uuid4(), digest, len(text), text)
    assert (report.excerpt.status, report.sha256, report.total_characters, report.truncated) == (
        "WITHHELD", digest, len(text), True)
    assert [(p.start_offset, p.end_offset, p.text) for p in report.passages] == [
        (0, len(first), first), (len(first + hidden), len(text), last)]
    assert not report.contains_quote(first + last)


def test_report_releases_slash_connected_prose_but_withholds_attached_paths():
    answer = "只有需要幅度或因果/可推广结论时才值得重做，仍无法排除批次/lane 效应。"
    text = "结论摘要\n" + answer + "\n读取路径/var/private/result，配置/.env。"
    digest = sha256(text.encode()).hexdigest()

    report = SkillSelectedReport.from_prefix(uuid4(), digest, len(text), text)

    assert report.contains_quote(answer)
    assert report.sha256 == digest
    assert "/var/private/result" not in report.model_dump_json()
    assert "/.env" not in report.model_dump_json()


@pytest.mark.parametrize("content", [
    "Bearer\nCREDENTIALSENTINEL", "api_key=\nCREDENTIALSENTINEL",
    "-----BEGIN PRIVATE KEY-----\nCREDENTIALSENTINEL\n-----END PRIVATE KEY-----",
    "```text\n/local/CREDENTIALSENTINEL\nCREDENTIALSENTINEL\n```",
    "See /local/CREDENTIALSENTINEL", "storage_locator CREDENTIALSENTINEL",
    "storage_locator /tmp/source\nCREDENTIALSENTINEL",
])
def test_partial_report_never_releases_restricted_lines_or_cross_line_values(content):
    text = "Public preface.\n" + content + "\nPublic conclusion."
    report = SkillSelectedReport.from_prefix(uuid4(), sha256(text.encode()).hexdigest(), len(text), text)
    assert report.excerpt.status == "WITHHELD"
    assert "CREDENTIALSENTINEL" not in report.model_dump_json()


def test_partial_report_caps_intervals_and_excludes_final_clipped_paragraph():
    body = "".join(f"Safe result {i}.\nRead /private/fixture\n" for i in range(70))
    text = body + "\nClipped paragraph.\nThe source continues"
    report = SkillSelectedReport.from_prefix(uuid4(), sha256(text.encode()).hexdigest(), len(text), text[:-3])
    assert len(report.passages) == 64 and report.contains_quote("Safe result 63.")
    assert not report.contains_quote("Safe result 64.") and not report.contains_quote("Clipped paragraph.")


@pytest.mark.asyncio
async def test_model_selects_a_report_anchor_without_returning_any_quote():
    report_text = "A different answer.\n\nPath /media/private/fixture\n\n**Main finding:** β = 0.52."
    report_id, run_id = uuid4(), uuid4()
    digest = sha256(report_text.encode()).hexdigest()
    report = SkillSelectedReport.from_prefix(report_id, digest, len(report_text), report_text)
    view = SkillCurationSourceView(source_run_id=run_id, source_bundle_id=uuid4(),
        final_status=RunStatus.COMPLETED, workflow_stage_path=(),
        source_user_request=SkillSourceUserRequest.from_record("What is the effect?"),
        outcome_evidence=SkillOutcomeEvidence(selected_report=report))
    class Invoker:
        async def invoke(self, *, instructions, request, response_model):
            assert "report_quote" not in response_model.model_fields
            sent = json.loads(request)["outcome_evidence"]["selected_report"]
            assert sent["coverage"]["last_released_offset"] == len(report_text)
            assert sent["coverage"]["released_characters"] < len(report_text)
            assert "/media/private" not in request
            anchor = next(a for a in sent["anchors"] if "Main finding" in a["text"])
            return response_model(research_question="Effect?", primary_result="Estimate is 0.52",
                report_anchor_id=anchor["id"], assessment="ALIGNED", explanation="A direct answer.")
    result = await TypedAuditedAdaptiveSkillCurator(Invoker()).assess_outcome(view)
    locator = result.report_anchor
    assert (locator.run_id, locator.artifact_id, locator.sha256) == (run_id, report_id, digest)
    assert result.report_quote == report_text[locator.start_offset:locator.end_offset] == "**Main finding:** β = 0.52."
    # Historical stored reviews remain readable without an anchor.
    old = result.model_dump(exclude={"report_anchor"})
    assert SkillOutcomeReview.model_validate(old).report_anchor is None


def test_anchor_ids_and_offsets_bind_the_exact_run_and_safe_source_ranges():
    from labbioagentos.skills.curator import _report_anchors
    text = "Line A.\n\nFile /media/private/file\n\n" + ("β\n" * 12000)
    report = SkillSelectedReport.from_prefix(uuid4(), sha256(text.encode()).hexdigest(), len(text), text)
    anchors = _report_anchors(uuid4(), report)
    other = _report_anchors(uuid4(), report)
    assert anchors and len(anchors) < 256
    assert {a["id"] for a in anchors}.isdisjoint(a["id"] for a in other)
    for a in anchors:
        assert a["text"] == text[a["start_offset"]:a["end_offset"]]
        assert any(p.start_offset <= a["start_offset"] < a["end_offset"] <= p.end_offset for p in report.passages)


@pytest.mark.asyncio
async def test_outcome_quote_correction_can_report_insufficient_without_overriding_the_agent(source):
    invocations, observations = [], []
    class Invoker:
        async def invoke(self, *, instructions, request, response_model):
            invocations.append(json.loads(request))
            if len(invocations) == 1:
                return SkillOutcomeSelection(research_question="What happened?", primary_result="A",
                    report_anchor_id="Imagined", assessment="ALIGNED", explanation="A")
            return SkillOutcomeSelection(research_question="What happened?", primary_result="Unknown",
                report_anchor_id=None, assessment="INSUFFICIENT_EVIDENCE", explanation="No released report text.")
    view = SkillCurationSourceView(source_run_id=uuid4(), source_bundle_id=uuid4(),
        final_status=RunStatus.COMPLETED, workflow_stage_path=(),
        source_user_request=SkillSourceUserRequest.from_record("What happened?"))
    reviewer = TypedAuditedAdaptiveSkillCurator(Invoker(), boundary_observer=lambda k, v: observations.append((k,v)))
    outcome = await reviewer.assess_outcome(view)
    assert outcome.assessment == "INSUFFICIENT_EVIDENCE" and len(invocations) == 2
    assert invocations[1]["correction"]["code"] == "UNKNOWN_REPORT_ANCHOR"
    assert any(k == "curator_outcome_review_rejected" for k, _ in observations)
