"""Phase 8 acceptance tests for deterministic scope and Memory governance."""

from __future__ import annotations

import json
from types import MethodType
from uuid import uuid4

import pytest
from pantheon.agent import Agent, AgentResponse
from pantheon.team import PantheonTeam
from pydantic import ValidationError

from labbioagentos import (
    AccessAction,
    AccessService,
    ArtifactConsumer,
    ArtifactExposureClass,
    ArtifactExposureService,
    ArtifactQuery,
    ArtifactRepresentation,
    ArtifactViewType,
    AuthorizationDenied,
    AuthorizationPolicy,
    GoldSkillService,
    InMemoryMemoryStore,
    InMemoryProjectStore,
    InMemorySkillStore,
    InMemoryTraceSink,
    LocalArtifactStore,
    MemoryDecision,
    MemoryGovernanceService,
    MemoryKind,
    MemoryScope,
    MemoryUpdateProposal,
    PantheonStageAdapter,
    Principal,
    PrincipalRole,
    Project,
    RunStatus,
    RunTraceRecorder,
    SkillProcedure,
    SkillProposal,
    SkillScope,
    SkillSearchContext,
    SkillSourceBundle,
    SkillSourceProjector,
    SkillUserDecision,
    StageContext,
    TraceEventType,
    WorkflowRun,
    WorkflowStage,
    WorkspaceArea,
    WorkspaceContext,
    WorkspaceResolver,
)
from labbioagentos.artifacts import ExposurePolicy


@pytest.fixture
def governance():
    sink = InMemoryTraceSink()
    recorder = RunTraceRecorder(sink)
    projects = InMemoryProjectStore()
    projects.register(
        Project(
            project_id="project-a",
            lab_id="lab-a",
            owner_user_id="user-a",
            read_only_collaborators=frozenset({"user-c"}),
        )
    )
    access = AccessService(
        projects,
        AuthorizationPolicy(),
        trace_recorder=recorder,
    )
    principals = {
        "owner": Principal(user_id="user-a", lab_id="lab-a"),
        "outsider": Principal(user_id="user-b", lab_id="lab-a"),
        "collaborator": Principal(user_id="user-c", lab_id="lab-a"),
        "admin": Principal(
            user_id="admin-a",
            lab_id="lab-a",
            roles=frozenset({PrincipalRole.MEMBER, PrincipalRole.LAB_ADMIN}),
        ),
        "other_lab": Principal(user_id="user-z", lab_id="lab-z"),
    }
    return sink, recorder, projects, access, principals


def _memory_service(governance):
    sink, recorder, _, access, principals = governance
    store = InMemoryMemoryStore()
    service = MemoryGovernanceService(store, access, trace_recorder=recorder)
    return sink, store, service, principals


def _memory_proposal(
    scope,
    *,
    run_id=None,
    target_memory_id=None,
    target_version=None,
    evidence_artifact_ids=(),
):
    source_run_id = run_id or uuid4()
    kwargs = {
        "target_scope": scope,
        "lab_id": "lab-a",
        "proposed_kind": MemoryKind.OPERATING_NOTE,
        "proposed_content": "Synthetic bounded persistent note.",
        "reason": "Runtime supplied a synthetic update reason.",
        "evidence_run_ids": (source_run_id,),
        "evidence_artifact_ids": evidence_artifact_ids,
        "source_run_id": source_run_id,
        "target_memory_id": target_memory_id,
        "target_version": target_version,
    }
    if scope is MemoryScope.PERSONAL:
        kwargs["owner_user_id"] = "user-a"
    elif scope is MemoryScope.PROJECT:
        kwargs["owner_user_id"] = "user-a"
        kwargs["project_id"] = "project-a"
    return MemoryUpdateProposal(**kwargs)


def _decision(proposal, user, approved=True):
    return MemoryDecision(
        proposal_id=proposal.proposal_id,
        gate_id=proposal.approval_gate_id,
        approved=approved,
        decided_by=user,
    )


def _seed_skill(
    store,
    *,
    scope,
    owner_user_id=None,
    project_id=None,
    lab_id="lab-a",
):
    run_id = uuid4()
    bundle = SkillSourceBundle(
        source_run_id=run_id,
        final_status=RunStatus.COMPLETED,
        workflow_stage_path=(WorkflowStage.PLAN,),
        trace_event_ids=(uuid4(),),
    )
    store.save_source_bundle(bundle)
    proposal = SkillProposal(
        source_bundle_id=bundle.bundle_id,
        source_run_id=run_id,
        proposed_name=f"{scope.value} synthetic skill",
        description="Synthetic governed procedural memory.",
        scope=scope,
        owner_user_id=owner_user_id,
        project_id=project_id,
        lab_id=lab_id,
        procedure=SkillProcedure(
            applicability="Runtime evaluates current-task relevance.",
            workflow_outline=("Use as planning context only.",),
        ),
    )
    store.save_proposal(proposal)
    gold = store.decide_proposal(
        proposal.proposal_id,
        SkillUserDecision(
            subject_id=proposal.proposal_id,
            gate_id=proposal.approval_gate_id,
            approved=True,
            decided_by=owner_user_id or "trusted-seed",
        ),
    )
    assert gold is not None
    return gold, bundle, proposal


def test_owner_collaborator_outsider_and_lab_admin_project_policy(governance):
    sink, _, _, access, principals = governance
    run_id = uuid4()
    assert access.require_project(
        principals["owner"], "project-a", AccessAction.READ_PROJECT, run_id=run_id
    ).project_id == "project-a"
    assert access.require_project(
        principals["collaborator"],
        "project-a",
        AccessAction.READ_PROJECT,
        run_id=run_id,
    ).project_id == "project-a"
    with pytest.raises(AuthorizationDenied):
        access.require_project(
            principals["collaborator"],
            "project-a",
            AccessAction.WRITE_PROJECT,
            run_id=run_id,
        )
    with pytest.raises(AuthorizationDenied):
        access.require_project(
            principals["outsider"],
            "project-a",
            AccessAction.WRITE_PROJECT,
            run_id=run_id,
        )
    assert access.require_project(
        principals["admin"],
        "project-a",
        AccessAction.WRITE_PROJECT,
        run_id=run_id,
    ).project_id == "project-a"
    assert TraceEventType.PROJECT_ACCESS_DENIED in {
        event.event_type for event in sink.read(run_id)
    }


@pytest.mark.asyncio
async def test_workflow_workspace_identity_is_frozen_and_never_shared_with_pantheon():
    run = WorkflowRun(
        owner_user_id="user-a",
        project_id="project-a",
        lab_id="lab-a",
        current_stage=WorkflowStage.PLAN,
    )
    observed = {}
    agent = Agent(name="mock", instructions="Mock only.", model="openai/mock")

    async def mock_run(self, message, **kwargs):
        observed.update(kwargs)
        return AgentResponse(
            agent_name=self.name,
            content={"stage": "PLAN", "summary": "Mock result.", "payload": {}},
            details=None,
        )

    agent.run = MethodType(mock_run, agent)
    team = PantheonTeam(agents=[agent])
    await PantheonStageAdapter(team).run_stage(
        StageContext(
            run_id=run.run_id,
            stage=WorkflowStage.PLAN,
            instruction="Return synthetic structure.",
        )
    )
    with pytest.raises(ValidationError):
        run.owner_user_id = "user-b"
    assert "owner_user_id" not in json.dumps(observed, default=str)
    assert all(value is not run for value in vars(team).values())


def test_known_artifact_uuid_cannot_bypass_authorization_or_reach_exposure_policy(
    tmp_path,
    governance,
):
    sink, recorder, _, access, principals = governance
    store = LocalArtifactStore(tmp_path / "artifacts", trace_recorder=recorder)
    ref = store.register(
        artifact_type="synthetic-derived",
        exposure_class=ArtifactExposureClass.DERIVED,
        representation=ArtifactRepresentation(summary={"count": 1}),
        owner_user_id="user-a",
        project_id="project-a",
        lab_id="lab-a",
        run_id=uuid4(),
    )

    class CountingExposurePolicy(ExposurePolicy):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def decide(self, ref, query, consumer):
            self.calls += 1
            return super().decide(ref, query, consumer)

    policy = CountingExposurePolicy()
    service = ArtifactExposureService(
        store,
        policy,
        access_service=access,
        trace_recorder=recorder,
    )
    with pytest.raises(AuthorizationDenied):
        service.artifact_ref(ref.artifact_id, principal=principals["outsider"])
    with pytest.raises(AuthorizationDenied):
        service.artifact_query(
            ref.artifact_id,
            ArtifactQuery(view_type=ArtifactViewType.SUMMARY),
            ArtifactConsumer.USER,
            principal=principals["outsider"],
        )
    assert policy.calls == 0
    view = service.artifact_query(
        ref.artifact_id,
        ArtifactQuery(view_type=ArtifactViewType.SUMMARY),
        ArtifactConsumer.USER,
        principal=principals["owner"],
    )
    assert view.summary == {"count": 1}
    assert view.provenance.project_id == "project-a"
    events = sink.read(ref.run_id)
    assert TraceEventType.AUTHORIZATION_DENIED in {
        event.event_type for event in events
    }
    encoded = json.dumps([event.model_dump(mode="json") for event in events])
    assert '"count": 1' not in encoded


def test_gold_skill_personal_project_and_lab_visibility(governance):
    _, _, _, access, principals = governance
    store = InMemorySkillStore()
    service = GoldSkillService(
        store,
        SkillSourceProjector(),
        access_service=access,
    )
    personal, _, _ = _seed_skill(
        store,
        scope=SkillScope.PERSONAL,
        owner_user_id="user-a",
    )
    project, _, _ = _seed_skill(
        store,
        scope=SkillScope.PROJECT,
        owner_user_id="user-a",
        project_id="project-a",
    )
    lab, _, _ = _seed_skill(store, scope=SkillScope.LAB)

    owner_visible = service.search(
        SkillSearchContext(project_id="project-a"),
        principal=principals["owner"],
    )
    assert {item.skill_id for item in owner_visible} == {
        personal.skill_id,
        project.skill_id,
        lab.skill_id,
    }
    collaborator_visible = service.search(
        SkillSearchContext(project_id="project-a"),
        principal=principals["collaborator"],
    )
    assert {item.skill_id for item in collaborator_visible} == {
        project.skill_id,
        lab.skill_id,
    }
    outsider_visible = service.search(
        SkillSearchContext(project_id="project-a"),
        principal=principals["outsider"],
    )
    assert outsider_visible == (lab,)
    other_lab_visible = service.search(
        SkillSearchContext(),
        principal=principals["other_lab"],
    )
    assert other_lab_visible == ()
    with pytest.raises(AuthorizationDenied):
        service.get_gold(
            personal.skill_id,
            personal.version,
            principal=principals["outsider"],
        )


def test_lab_gold_promotion_requires_lab_admin(governance):
    _, recorder, _, access, principals = governance
    store = InMemorySkillStore()
    service = GoldSkillService(
        store,
        SkillSourceProjector(),
        access_service=access,
        trace_recorder=recorder,
    )
    _, _, seeded_proposal = _seed_skill(store, scope=SkillScope.LAB)
    proposal = seeded_proposal.model_copy(
        update={
            "proposal_id": uuid4(),
            "approval_gate_id": f"skill-proposal:{uuid4()}",
            "proposed_name": "Pending Lab Gold",
        }
    )
    store.save_proposal(proposal)
    member_decision = SkillUserDecision(
        subject_id=proposal.proposal_id,
        gate_id=proposal.approval_gate_id,
        approved=True,
        decided_by=principals["owner"].user_id,
    )
    with pytest.raises(AuthorizationDenied):
        service.decide_proposal(
            proposal.proposal_id,
            member_decision,
            principal=principals["owner"],
        )
    admin_decision = member_decision.model_copy(
        update={
            "decision_id": uuid4(),
            "decided_by": principals["admin"].user_id,
        }
    )
    approved = service.decide_proposal(
        proposal.proposal_id,
        admin_decision,
        principal=principals["admin"],
    )
    assert approved is not None
    assert approved.lab_id == "lab-a"


def test_agent_has_no_direct_persistent_memory_write_api(governance):
    _, store, service, _ = _memory_service(governance)
    assert not hasattr(store, "write")
    assert not hasattr(store, "save_entry")
    assert not hasattr(service, "write")
    assert not hasattr(service, "update")


def test_personal_memory_requires_owner_approval_and_rejection_creates_nothing(
    governance,
):
    sink, store, service, principals = _memory_service(governance)
    rejected_run_id = uuid4()
    rejected = _memory_proposal(MemoryScope.PERSONAL, run_id=rejected_run_id)
    service.submit_proposal(principals["owner"], rejected)
    assert service.decide(
        principals["owner"],
        rejected.proposal_id,
        _decision(rejected, "user-a", approved=False),
    ) is None
    assert store.entries() == ()
    assert TraceEventType.MEMORY_PROPOSAL_REJECTED in {
        event.event_type for event in sink.read(rejected_run_id)
    }

    proposal = _memory_proposal(MemoryScope.PERSONAL)
    service.submit_proposal(principals["owner"], proposal)
    with pytest.raises(AuthorizationDenied):
        service.decide(
            principals["outsider"],
            proposal.proposal_id,
            _decision(proposal, "user-b"),
        )
    entry = service.decide(
        principals["owner"], proposal.proposal_id, _decision(proposal, "user-a")
    )
    assert entry is not None
    assert entry.version == 1
    with pytest.raises(ValidationError):
        entry.content = "mutated"


def test_project_memory_requires_owner_or_admin_approval(governance):
    _, _, service, principals = _memory_service(governance)
    proposal = _memory_proposal(MemoryScope.PROJECT)
    with pytest.raises(AuthorizationDenied):
        service.submit_proposal(principals["collaborator"], proposal)
    service.submit_proposal(principals["owner"], proposal)
    entry = service.decide(
        principals["owner"], proposal.proposal_id, _decision(proposal, "user-a")
    )
    assert entry is not None
    assert entry.project_id == "project-a"


def test_lab_memory_requires_lab_admin_approval(governance):
    _, _, service, principals = _memory_service(governance)
    proposal = _memory_proposal(MemoryScope.LAB)
    with pytest.raises(AuthorizationDenied):
        service.submit_proposal(principals["owner"], proposal)
    service.submit_proposal(principals["admin"], proposal)
    entry = service.decide(
        principals["admin"],
        proposal.proposal_id,
        _decision(proposal, "admin-a"),
    )
    assert entry is not None
    assert entry.scope is MemoryScope.LAB


def test_memory_update_creates_v2_preserves_v1_and_reconstructs_lineage(governance):
    _, store, service, principals = _memory_service(governance)
    first = _memory_proposal(MemoryScope.PERSONAL)
    service.submit_proposal(principals["owner"], first)
    v1 = service.decide(
        principals["owner"], first.proposal_id, _decision(first, "user-a")
    )
    assert v1 is not None
    snapshot = v1.model_dump_json()

    update = _memory_proposal(
        MemoryScope.PERSONAL,
        target_memory_id=v1.memory_id,
        target_version=v1.version,
    ).model_copy(update={"proposed_content": "Approved synthetic version two."})
    service.submit_proposal(principals["owner"], update)
    v2 = service.decide(
        principals["owner"], update.proposal_id, _decision(update, "user-a")
    )
    assert v2 is not None
    assert v2.memory_id == v1.memory_id
    assert v2.version == 2
    assert v2.previous_version == 1
    assert store.get(v1.memory_id, 1).model_dump_json() == snapshot
    assert service.lineage(principals["owner"], v1.memory_id) == (v1, v2)


def test_memory_evidence_is_reference_only_and_trace_omits_content(
    tmp_path, governance
):
    sink, recorder, _, access, principals = governance
    artifact_store = LocalArtifactStore(tmp_path / "artifacts", trace_recorder=recorder)
    memory_store = InMemoryMemoryStore()
    service = MemoryGovernanceService(
        memory_store,
        access,
        trace_recorder=recorder,
        artifact_store=artifact_store,
    )
    run_id = uuid4()
    evidence = artifact_store.register(
        artifact_type="synthetic-derived",
        exposure_class=ArtifactExposureClass.DERIVED,
        representation=ArtifactRepresentation(summary={"count": 1}),
        owner_user_id="user-a",
        project_id="project-a",
        lab_id="lab-a",
        run_id=run_id,
    )
    proposal = _memory_proposal(
        MemoryScope.PERSONAL,
        run_id=run_id,
        evidence_artifact_ids=(evidence.artifact_id,),
    )
    raw_token = "RAW_ARTIFACT_CONTENT_MUST_NOT_APPEAR"
    raw_representation = ArtifactRepresentation(stored_content=raw_token)
    assert raw_token in raw_representation.model_dump_json()
    assert raw_token not in proposal.model_dump_json()
    service.submit_proposal(
        principals["owner"],
        proposal,
        workspace=WorkspaceContext(
            user_id="user-a", project_id="project-a", lab_id="lab-a"
        ),
    )
    entry = service.decide(
        principals["owner"], proposal.proposal_id, _decision(proposal, "user-a")
    )
    assert entry is not None
    assert entry.evidence_artifact_ids == proposal.evidence_artifact_ids
    assert raw_token not in entry.model_dump_json()
    events = sink.read(run_id)
    event_types = {event.event_type for event in events}
    assert TraceEventType.AUTHORIZATION_ALLOWED in event_types
    assert TraceEventType.MEMORY_PROPOSAL_CREATED in event_types
    assert TraceEventType.MEMORY_PROPOSAL_APPROVED in event_types
    assert TraceEventType.MEMORY_VERSION_CREATED in event_types
    encoded = json.dumps([event.model_dump(mode="json") for event in events])
    assert proposal.proposed_content not in encoded
    assert proposal.reason not in encoded


def test_workspace_resolver_rejects_cross_user_and_path_traversal(
    tmp_path,
    governance,
):
    _, _, _, access, principals = governance
    resolver = WorkspaceResolver(tmp_path / "workspace", access)
    owner_context = WorkspaceContext(
        user_id="user-a", project_id="project-a", lab_id="lab-a"
    )
    owner_path = resolver.resolve(
        principals["owner"], owner_context, WorkspaceArea.PROJECT_ARTIFACTS
    )
    assert owner_path == (
        tmp_path
        / "workspace"
        / "users"
        / "user-a"
        / "projects"
        / "project-a"
        / "artifacts"
    ).resolve()
    with pytest.raises(AuthorizationDenied):
        resolver.resolve(
            principals["outsider"],
            owner_context,
            WorkspaceArea.PROJECT_ARTIFACTS,
        )
    with pytest.raises(ValidationError):
        WorkspaceContext(
            user_id="../user-a",
            project_id="project-a",
            lab_id="lab-a",
        )
