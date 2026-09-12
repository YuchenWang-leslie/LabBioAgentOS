"""Outer personal Gold persistence does not bypass the approved lifecycle."""

import os
from types import SimpleNamespace
from uuid import uuid4

import pytest

from labbioagentos import (
    AuthorizationDenied, InMemoryTraceSink, LocalArtifactStore, Principal, Project,
    RunStatus, RunTraceRecorder, SkillApprovalRequiredError, SkillCuratorDraft,
    SkillCuratorPort, SkillDecisionError, SkillProcedureDraft, SkillProposalContext,
    SkillScope, SkillSearchContext, SkillSourceBundle, SkillStoreError, SkillUseMode,
    SkillUseProposal, SkillUserDecision, SQLiteSkillStore, TraceEventType, WorkflowStage,
    WorkspaceContext,
)
from labbioagentos.local_gold import (
    build_personal_gold_service, close_personal_gold, decide_personal_gold,
    propose_from_run, review_personal_gold,
    completed_gold_source_record, propose_from_completed_run,
)
from labbioagentos.run_state import RunRecoveryState


@pytest.fixture
def service(tmp_path):
    value = build_personal_gold_service(tmp_path / "TEST1" / "GoldSkills", "TEST1")
    yield value
    close_personal_gold(value)


@pytest.fixture
def principal():
    return Principal(user_id="TEST1", lab_id="lab-test")


def _draft():
    return SkillCuratorDraft(
        proposed_name="Synthetic fixture guidance",
        description="Fixture-only procedural content, not a real Gold Skill.",
        procedure=SkillProcedureDraft(
            applicability="The current Agent evaluates applicability.",
            workflow_outline=("Use current task evidence.",),
        ),
    )


def _proposal(service, principal, **context):
    bundle = SkillSourceBundle(
        source_run_id=uuid4(), final_status=RunStatus.COMPLETED,
        workflow_stage_path=(WorkflowStage.LEARN,), trace_event_ids=(),
    )
    service.store.save_source_bundle(bundle)
    values = {"scope": SkillScope.PERSONAL, "owner_user_id": principal.user_id,
              "lab_id": principal.lab_id}
    return service.create_proposal(
        bundle.bundle_id, _draft(), SkillProposalContext(**(values | context)),
    )


def _gold(service, principal):
    proposal = _proposal(service, principal)
    gold = decide_personal_gold(service, proposal.proposal_id, proposal.approval_gate_id,
                                True, principal)
    return proposal, gold


def test_personal_gold_shared_across_own_projects_and_restart(tmp_path, principal):
    root = tmp_path / "TEST1" / "GoldSkills"
    service = build_personal_gold_service(root, principal.user_id)
    proposal, gold = _gold(service, principal)
    close_personal_gold(service)
    reopened = build_personal_gold_service(root, principal.user_id)
    try:
        for project in ("PRJ1", "PRJ2"):
            assert reopened.search(SkillSearchContext(
                user_id=principal.user_id, lab_id=principal.lab_id, project_id=project,
            ), principal=principal) == (gold,)
        assert review_personal_gold(reopened, proposal.proposal_id, principal) == proposal
        assert reopened.get_gold(gold.skill_id, gold.version, principal=principal) == gold
        assert (root / "skills.sqlite").stat().st_mode & 0o077 == 0
        assert not tuple(root.glob("*.md"))
    finally:
        close_personal_gold(reopened)
    with pytest.raises(AuthorizationDenied):
        build_personal_gold_service(root, "TEST2")


@pytest.mark.parametrize("changes", [
    {"owner_user_id": "TEST2"},
    {"scope": SkillScope.PROJECT, "project_id": "PRJ1"},
    {"scope": SkillScope.LAB},
])
def test_only_exact_personal_candidates_can_be_stored(service, principal, changes):
    with pytest.raises(AuthorizationDenied):
        _proposal(service, principal, **changes)
    assert service.search(SkillSearchContext(user_id=principal.user_id,
                          lab_id=principal.lab_id), principal=principal) == ()


def test_foreign_user_cannot_search_view_or_approve(service, principal):
    proposal, gold = _gold(service, principal)
    other = Principal(user_id="TEST2", lab_id=principal.lab_id)
    with pytest.raises(AuthorizationDenied):
        service.search(SkillSearchContext(user_id=other.user_id,
                       lab_id=other.lab_id), principal=other)
    with pytest.raises(AuthorizationDenied):
        service.get_gold(gold.skill_id, gold.version, principal=other)
    with pytest.raises(AuthorizationDenied):
        review_personal_gold(service, proposal.proposal_id, other)
    with pytest.raises(AuthorizationDenied):
        decide_personal_gold(service, proposal.proposal_id, proposal.approval_gate_id,
                             True, other)


def test_candidate_is_not_gold_and_exact_decision_is_required(service, principal):
    proposal = _proposal(service, principal)
    context = SkillSearchContext(user_id=principal.user_id, lab_id=principal.lab_id)
    assert service.search(context, principal=principal) == ()
    with pytest.raises(SkillDecisionError):
        decide_personal_gold(service, proposal.proposal_id, "wrong-gate", True, principal)
    assert service.search(context, principal=principal) == ()
    assert decide_personal_gold(service, proposal.proposal_id, proposal.approval_gate_id,
                                False, principal) is None
    assert service.search(context, principal=principal) == ()


def test_populated_legacy_store_and_markdown_are_not_imported(tmp_path):
    root = tmp_path / "GoldSkills"
    root.mkdir()
    (root / "candidate.md").write_text("This file is not approval authority.")
    legacy = SQLiteSkillStore(root / "skills.sqlite")
    bundle = SkillSourceBundle(source_run_id=uuid4(), final_status=RunStatus.COMPLETED,
                               workflow_stage_path=(), trace_event_ids=())
    legacy.save_source_bundle(bundle)
    legacy.close()
    with pytest.raises(AuthorizationDenied, match="no local owner binding"):
        build_personal_gold_service(root, "TEST1")
    reopened = SQLiteSkillStore(root / "skills.sqlite")
    assert reopened.get_source_bundle(bundle.bundle_id) == bundle
    reopened.close()
    clean_root = tmp_path / "clean" / "GoldSkills"
    clean_root.mkdir(parents=True)
    (clean_root / "candidate.md").write_text("This file is still not approval authority.")
    service = build_personal_gold_service(clean_root, "TEST1")
    try:
        assert service.store.search(SkillSearchContext(user_id="TEST1")) == ()
    finally:
        close_personal_gold(service)


def test_gold_context_requires_exact_approved_run_and_project(service, principal):
    _, gold = _gold(service, principal)
    proposal = SkillUseProposal(
        run_id=uuid4(), requesting_user_id=principal.user_id, project_id="PRJ1",
        lab_id=principal.lab_id, skill_id=gold.skill_id, skill_version=gold.version,
        proposed_mode=SkillUseMode.REFERENCE, reason="Synthetic test context.",
    )
    service.submit_use_proposal(proposal, principal=principal)
    assert service.store.get_authorization_for_proposal(proposal.proposal_id) is None
    authorization = service.decide_use(proposal.proposal_id, SkillUserDecision(
        subject_id=proposal.proposal_id, gate_id=proposal.approval_gate_id,
        approved=True, decided_by=principal.user_id,
    ), principal=principal)
    for run_id, project_id, actor in (
        (uuid4(), "PRJ1", principal),
        (proposal.run_id, "PRJ2", principal),
        (proposal.run_id, "PRJ1", Principal(user_id="TEST2", lab_id=principal.lab_id)),
    ):
        with pytest.raises(SkillApprovalRequiredError):
            service.get_authorized_context(authorization.authorization_id, run_id=run_id,
                                           project_id=project_id, principal=actor)
    assert service.get_authorized_context(
        authorization.authorization_id, run_id=proposal.run_id,
        project_id="PRJ1", principal=principal,
    ) == gold
    rejected = proposal.model_copy(update={"proposal_id": uuid4(), "run_id": uuid4()})
    service.submit_use_proposal(rejected, principal=principal)
    rejection = service.decide_use(rejected.proposal_id, SkillUserDecision(
        subject_id=rejected.proposal_id, gate_id=rejected.approval_gate_id,
        approved=False, decided_by=principal.user_id,
    ), principal=principal)
    with pytest.raises(SkillApprovalRequiredError):
        service.get_authorized_context(rejection.authorization_id, run_id=rejected.run_id,
                                       project_id="PRJ1", principal=principal)


@pytest.mark.parametrize("name", ["skills.sqlite", "skills.sqlite-wal", "skills.sqlite-shm",
                                  "skills.sqlite-journal"])
@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_aliased_database_targets_rejected_before_open(tmp_path, name, alias):
    root = tmp_path / "GoldSkills"
    root.mkdir()
    source = tmp_path / "other-owner-file"
    source.write_bytes(b"untouched")
    if alias == "symlink":
        (root / name).symlink_to(source)
    else:
        os.link(source, root / name)
    with pytest.raises(ValueError):
        build_personal_gold_service(root, "TEST1")
    assert source.read_bytes() == b"untouched"


def test_symlinked_gold_ancestor_rejected(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    (tmp_path / "alias").symlink_to(actual, target_is_directory=True)
    with pytest.raises(ValueError):
        build_personal_gold_service(tmp_path / "alias" / "GoldSkills", "TEST1")
    assert not (actual / "GoldSkills").exists()


class _FixtureCurator(SkillCuratorPort):
    def __init__(self):
        self.source = None

    async def propose(self, source):
        self.source = source
        return _draft()


@pytest.mark.asyncio
async def test_propose_from_run_uses_safe_source_and_agent_draft(service, principal, tmp_path):
    workspace = WorkspaceContext(user_id=principal.user_id, project_id="PRJ1",
                                 lab_id=principal.lab_id)
    service.access_service.projects.register(Project(project_id="PRJ1",
        owner_user_id=principal.user_id, lab_id=principal.lab_id))
    recorder = RunTraceRecorder(InMemoryTraceSink())
    run_id = uuid4()
    for event, status in ((TraceEventType.RUN_CREATED, "CREATED"),
                          (TraceEventType.RUN_STARTED, "RUNNING"),
                          (TraceEventType.RUN_COMPLETED, "COMPLETED")):
        recorder.emit(run_id, event, status=status)
    application = SimpleNamespace(
        configuration=SimpleNamespace(skill_service=service, boundary_observer=None,
                                      runtime_revision="new-curation-runtime"),
        result=lambda handle: SimpleNamespace(run_id=run_id, status=RunStatus.COMPLETED),
        run_state_store=SimpleNamespace(get=lambda identifier: SimpleNamespace(
            owner_user_id=principal.user_id, project_id="PRJ1", lab_id=principal.lab_id,
            runtime_results=(), workflow_run=SimpleNamespace(status=RunStatus.COMPLETED),
            recovery_state=RunRecoveryState.STABLE, runtime_revision="frozen-source-runtime")),
        access_service=service.access_service,
        artifact_store=LocalArtifactStore(tmp_path / "artifacts"),
        trace_events=lambda handle: recorder.events(run_id),
        trace_recorder=recorder,
    )
    curator = _FixtureCurator()
    proposal = await propose_from_run(application, run_id, principal, workspace, curator)
    assert curator.source is not None
    assert curator.source.final_status is RunStatus.COMPLETED
    assert proposal.source_run_id == run_id
    assert proposal.scope is SkillScope.PERSONAL
    assert proposal.owner_user_id == principal.user_id
    assert proposal.project_id is None
    assert proposal.proposed_name == _draft().proposed_name
    historical = await propose_from_completed_run(
        application, run_id, principal, workspace, _FixtureCurator(),
    )
    assert historical.source_run_id == run_id
    assert application.run_state_store.get(run_id).runtime_revision == "frozen-source-runtime"
    assert service.search(SkillSearchContext(user_id=principal.user_id,
                          lab_id=principal.lab_id), principal=principal) == ()
    old_get = application.run_state_store.get
    incomplete = old_get(run_id)
    incomplete.workflow_run.status = RunStatus.RUNNING
    application.run_state_store.get = lambda identifier: incomplete
    with pytest.raises(SkillStoreError):
        await propose_from_run(application, run_id, principal, workspace, _FixtureCurator())
    with pytest.raises(AuthorizationDenied):
        await propose_from_run(application, run_id,
                               Principal(user_id="TEST2", lab_id=principal.lab_id),
                               workspace, _FixtureCurator())


@pytest.mark.parametrize("status,recovery", [
    (RunStatus.RUNNING, RunRecoveryState.STABLE),
    (RunStatus.FAILED, RunRecoveryState.STABLE),
    (RunStatus.COMPLETED, RunRecoveryState.STAGE_IN_FLIGHT),
])
def test_historical_curation_requires_stable_completion(service, principal, status, recovery):
    workspace = WorkspaceContext(user_id=principal.user_id, project_id="PRJ1", lab_id=principal.lab_id)
    service.access_service.projects.register(Project(project_id="PRJ1",
        owner_user_id=principal.user_id, lab_id=principal.lab_id))
    record = SimpleNamespace(owner_user_id=principal.user_id, project_id="PRJ1",
        lab_id=principal.lab_id, workflow_run=SimpleNamespace(status=status), recovery_state=recovery)
    app = SimpleNamespace(configuration=SimpleNamespace(skill_service=service),
        access_service=service.access_service, run_state_store=SimpleNamespace(get=lambda _: record))
    with pytest.raises(SkillStoreError, match="stable completed"):
        completed_gold_source_record(app, uuid4(), principal, workspace)
    with pytest.raises(AuthorizationDenied):
        completed_gold_source_record(app, uuid4(), principal,
            WorkspaceContext(user_id=principal.user_id, project_id="PRJ2", lab_id=principal.lab_id))
