"""A corrected report and imported analyses have different producers."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from labbioagentos import WorkflowStage
from labbioagentos.local_gold_source import source_outcome_evidence
from labbioagentos.local_gold_source import source_artifact_views
from labbioagentos.runtime.contracts import ReportStageBody, RuntimeReference
from labbioagentos.runtime.reporting import ReportSubmissionService
from labbioagentos.skills.models import SkillSourceUserRequest
from test_delivery_selection import producer
from test_gold_evolution_curation import source  # noqa: F401
from test_local_gold import principal, service  # noqa: F401
from test_local_delivery import local_run  # noqa: F401


def test_partial_request_preserves_exact_unicode_spans_and_withheld_gaps():
    text = "Which outcome is reproducible?\n\nRead /media/private/test.\n\n供者的结果？"
    result = SkillSourceUserRequest.from_record(text)
    assert result.status == 'WITHHELD' and result.text is None
    assert result.sha256 == hashlib.sha256(text.encode()).hexdigest()
    assert result.total_characters == len(text)
    assert [p.text.strip() for p in result.passages] == ['Which outcome is reproducible?', '供者的结果？']
    assert all(p.text == text[p.start_offset:p.end_offset] for p in result.passages)
    assert result.passages[0].end_offset < result.passages[1].start_offset
    assert '/media/private/' not in result.model_dump_json()


def test_request_does_not_release_cross_paragraph_secrets_or_clipped_text():
    text = "A public question.\n\n-----BEGIN PRIVATE KEY-----\n\nopaque private data\n\n"
    assert SkillSourceUserRequest.from_record(text).passages == ()
    long = SkillSourceUserRequest.from_record("Q" * 32001)
    assert long.status == "LENGTH_LIMIT" and long.text is None and not long.passages
    assert long.total_characters == 32001


@pytest.mark.parametrize('case', ['mixed', 'tamper', 'foreign', 'unselected', 'legacy'])
def test_curation_uses_selected_current_report_and_verified_mixed_producers(local_run, tmp_path, case):
    app, handle, principal, workspace = local_run
    current = handle.run_id
    other = uuid4()
    old, old_program = producer((app, SimpleNamespace(run_id=other), principal, workspace), tmp_path, 'old')
    audit, audit_program = producer(local_run, tmp_path, 'audit')
    imported = (old.artifact_id, old_program.artifact_id)
    service = ReportSubmissionService(app.artifact_store, app.access_service, trace_recorder=app.trace_recorder)
    report = service.submit(title='Correction', report_text='The prior measured result was checked.',
        evidence_artifact_ids=(), delivery_artifact_ids=None if case == 'legacy' else (old.artifact_id, audit.artifact_id),
        delivery_imported_artifact_ids=imported, principal=principal, workspace=workspace,
        run_id=current, stage_id=WorkflowStage.REPORT, invocation_id=uuid4())
    body = ReportStageBody(report_summary='Corrected conclusion', report_reference=RuntimeReference(
        kind='ARTIFACT', reference_id=str(report.report_artifact_id)))
    record = SimpleNamespace(run_id=current, owner_user_id=principal.user_id, project_id=workspace.project_id,
        lab_id=principal.lab_id, runtime_results=() if case=='unselected' else (SimpleNamespace(body=body),),
        workflow_run=SimpleNamespace(clarifications=()), input_artifact_ids=() if case=='foreign' else imported,
        context_artifact_ids=())
    if case == 'tamper':
        Path(old.storage_locator).write_bytes(b'changed after report selection')
    if case in {'tamper','foreign'}:
        with pytest.raises((ValueError, PermissionError)):
            source_outcome_evidence(app, record, current, principal, workspace)
        return
    outcome=source_outcome_evidence(app, record, current, principal, workspace)
    if case in {'unselected','legacy'}:
        assert outcome.selected_delivery is None
        assert (outcome.selected_report is None) == (case=='unselected')
        return
    assert outcome.selected_report.artifact_id == report.report_artifact_id
    assert outcome.selected_report.excerpt.text == 'The prior measured result was checked.'
    assert [x.producer_run_id for x in outcome.selected_delivery.outputs] == [other,current]
    assert [x.artifact_id for x in outcome.selected_delivery.programs] == [old_program.artifact_id,audit_program.artifact_id]
    assert str(tmp_path) not in outcome.model_dump_json()
    assert 'print(' not in outcome.model_dump_json()
    assert not source_artifact_views(app, current, principal, workspace, selected_delivery=outcome.selected_delivery)


def test_explicitly_withheld_request_spans_cannot_gain_full_visibility():
    original = SkillSourceUserRequest.from_record('A safe question.\n\nRead /tmp/private/path.\n\nAnswer briefly.')
    raw = original.model_dump()
    for change in ({'status':'AVAILABLE', 'text':'invented'}, {'sha256':None}, {'total_characters':3},
                   {'passages':tuple(reversed(original.passages))}):
        with pytest.raises(ValueError):
            SkillSourceUserRequest.model_validate({**raw, **change})


@pytest.mark.asyncio
async def test_plain_propose_includes_selected_terminal_outcome_and_preserves_source_runtime(
        source, monkeypatch):
    from labbioagentos import local_gold
    from labbioagentos.skills.models import SkillOutcomeEvidence, SkillSelectedReport
    from test_local_gold import _FixtureCurator
    app, rid, owner, workspace = source
    report_text = 'Checked the selected result.'
    report = SkillSelectedReport.from_prefix(uuid4(), hashlib.sha256(report_text.encode()).hexdigest(),
                                            len(report_text), report_text)
    monkeypatch.setattr('labbioagentos.local_gold_source.source_outcome_evidence',
                        lambda *args: SkillOutcomeEvidence(selected_report=report))
    seen = []
    app.configuration.boundary_observer = lambda k, v: seen.append((k,v))
    curator = _FixtureCurator()
    proposal = await local_gold.propose_from_completed_run(app, rid, owner, workspace, curator)
    assert proposal.source_run_id == curator.source.source_run_id == rid
    assert curator.source.outcome_evidence.selected_report == report
    assert app.run_state_store.get(rid).runtime_revision == 'source'
    assert ('curation_runtime', {'source_run_id': str(rid), 'source_runtime_revision':'source',
                                 'curation_runtime_revision':'candidate'}) in seen
