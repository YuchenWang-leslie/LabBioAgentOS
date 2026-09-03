"""C12 cross-user, project, and lab authorization attacks."""

from __future__ import annotations

from uuid import uuid4

import pytest

from labbioagentos import (
    AccessService,
    ApplicationRunRecord,
    ArtifactConsumer,
    ArtifactExposureClass,
    ArtifactExposureService,
    ArtifactReleaseBasis,
    ArtifactRepresentation,
    AuthorizationDenied,
    AuthorizationPolicy,
    ExecutionPlanDraft,
    ExecutionRuntime,
    ExecutionSubmissionService,
    GoldSkillService,
    InMemoryMemoryStore,
    InMemoryProjectStore,
    InMemorySkillStore,
    LabBioApplication,
    LabBioRuntimeToolSet,
    LocalArtifactStore,
    MemoryDecision,
    MemoryGovernanceService,
    MemoryKind,
    MemoryScope,
    MemoryUpdateProposal,
    Principal,
    Project,
    ReportSubmissionService,
    RunStatus,
    RuntimeCapabilityContext,
    RuntimeCapabilityServices,
    SkillCuratorDraft,
    SkillProcedureDraft,
    SkillProposalContext,
    SkillScope,
    SkillSearchContext,
    SkillSourceBundle,
    SkillSourceProjector,
    SkillUserDecision,
    WorkflowRun,
    WorkflowStage,
    WorkspaceContext,
)
from labbioagentos.artifacts import ExposurePolicy


class _ExecutorMustNotRun:
    calls = 0

    def execute(self, _plan):
        self.calls += 1
        raise AssertionError("authorization must fail before execution")


def _boundary(tmp_path):
    projects = InMemoryProjectStore()
    projects.register(
        Project(project_id="project-a", lab_id="lab-a", owner_user_id="user-a")
    )
    projects.register(
        Project(project_id="project-b", lab_id="lab-b", owner_user_id="user-b")
    )
    access = AccessService(projects, AuthorizationPolicy())
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    exposure = ArtifactExposureService(
        artifacts, ExposurePolicy(), access_service=access
    )
    principal_a = Principal(user_id="user-a", lab_id="lab-a")
    workspace_a = WorkspaceContext(
        user_id="user-a", project_id="project-a", lab_id="lab-a"
    )
    principal_b = Principal(user_id="user-b", lab_id="lab-b")
    workspace_b = WorkspaceContext(
        user_id="user-b", project_id="project-b", lab_id="lab-b"
    )
    return (
        projects,
        access,
        artifacts,
        exposure,
        principal_a,
        workspace_a,
        principal_b,
        workspace_b,
    )


def _approve_personal_gold(service, store, principal):
    source = SkillSourceBundle(
        source_run_id=uuid4(),
        final_status=RunStatus.COMPLETED,
        workflow_stage_path=(WorkflowStage.PLAN, WorkflowStage.REPORT),
        trace_event_ids=(),
    )
    store.save_source_bundle(source)
    proposal = service.create_proposal(
        source.bundle_id,
        SkillCuratorDraft(
            proposed_name="Private project procedure",
            description="Approved only for its owner.",
            procedure=SkillProcedureDraft(
                applicability="A private project context.",
                workflow_outline=("Inspect governed inputs.",),
            ),
        ),
        SkillProposalContext(
            scope=SkillScope.PERSONAL,
            owner_user_id=principal.user_id,
            lab_id=principal.lab_id,
        ),
    )
    return service.decide_proposal(
        proposal.proposal_id,
        SkillUserDecision(
            subject_id=proposal.proposal_id,
            gate_id=proposal.approval_gate_id,
            approved=True,
            decided_by=principal.user_id,
        ),
        principal=principal,
    )


@pytest.mark.asyncio
async def test_known_foreign_artifact_uuid_cannot_be_listed_queried_mounted_or_cited(
    tmp_path,
):
    (
        _,
        access,
        artifacts,
        exposure,
        principal_a,
        workspace_a,
        _,
        _,
    ) = _boundary(tmp_path)
    foreign = artifacts.register(
        artifact_type="foreign-result",
        exposure_class=ArtifactExposureClass.DERIVED,
        release_basis=ArtifactReleaseBasis.TRUSTED_EXECUTION_DECLASSIFICATION,
        representation=ArtifactRepresentation(summary={"count": 4}),
        owner_user_id="user-b",
        project_id="project-b",
        lab_id="lab-b",
    )
    executor = _ExecutorMustNotRun()
    submission = ExecutionSubmissionService(
        artifact_store=artifacts,
        access_service=access,
        executor=executor,
    )
    reports = ReportSubmissionService(artifacts, access)
    binding = RuntimeCapabilityContext(
        principal=principal_a,
        workspace=workspace_a,
        run_id=uuid4(),
        stage_id=WorkflowStage.INTAKE,
        invocation_id=uuid4(),
        actor_profile_key="coordinator",
        actor_agent_name="CoordinatorAgent",
        capability_allowlist=("artifact_list", "artifact_query"),
    )
    toolset = LabBioRuntimeToolSet(
        binding,
        RuntimeCapabilityServices(
            artifact_store=artifacts,
            artifact_exposure=exposure,
        ),
    )

    listed = await toolset.artifact_list()
    queried = await toolset.artifact_query(str(foreign.artifact_id), "SUMMARY")
    assert listed["success"] is True
    assert listed["data"] == []
    assert queried["error"]["error_code"] == "AUTHORIZATION_DENIED"

    with pytest.raises(AuthorizationDenied):
        await submission.submit(
            ExecutionPlanDraft(
                runtime=ExecutionRuntime.PYTHON,
                image_key="python-c12",
                script_content="print('must not execute')\n",
                input_artifact_ids=(foreign.artifact_id,),
            ),
            principal=principal_a,
            workspace=workspace_a,
            run_id=binding.run_id,
            stage_id=WorkflowStage.EXECUTE,
            invocation_id=uuid4(),
        )
    assert executor.calls == 0

    with pytest.raises(AuthorizationDenied):
        reports.submit(
            title="Foreign evidence attempt",
            report_text="This must not be registered.",
            evidence_artifact_ids=(foreign.artifact_id,),
            principal=principal_a,
            workspace=workspace_a,
            run_id=binding.run_id,
            stage_id=WorkflowStage.REPORT,
            invocation_id=uuid4(),
        )

    memory = MemoryGovernanceService(
        InMemoryMemoryStore(), access, artifact_store=artifacts
    )
    proposal = MemoryUpdateProposal(
        target_scope=MemoryScope.PROJECT,
        project_id=workspace_a.project_id,
        lab_id=workspace_a.lab_id,
        proposed_kind=MemoryKind.PROJECT_FACT,
        proposed_content="A proposed local project fact.",
        reason="Attempt to cite a foreign Artifact.",
        evidence_artifact_ids=(foreign.artifact_id,),
        source_run_id=binding.run_id,
    )
    with pytest.raises(AuthorizationDenied):
        memory.submit_proposal(
            principal_a, proposal, workspace=workspace_a
        )


def test_foreign_gold_memory_and_run_uuid_remain_inaccessible(tmp_path):
    (
        _,
        access,
        artifacts,
        _,
        principal_a,
        workspace_a,
        principal_b,
        _,
    ) = _boundary(tmp_path)
    skill_store = InMemorySkillStore()
    skills = GoldSkillService(
        skill_store,
        SkillSourceProjector(artifacts),
        access_service=access,
    )
    gold = _approve_personal_gold(skills, skill_store, principal_b)
    assert gold is not None

    visible = skills.search(
        SkillSearchContext(
            user_id=principal_a.user_id,
            project_id=workspace_a.project_id,
            lab_id=workspace_a.lab_id,
        ),
        principal=principal_a,
    )
    assert visible == ()
    with pytest.raises(AuthorizationDenied):
        skills.get_gold(gold.skill_id, gold.version, principal=principal_a)

    memory_store = InMemoryMemoryStore()
    memory = MemoryGovernanceService(memory_store, access)
    proposal = MemoryUpdateProposal(
        target_scope=MemoryScope.PERSONAL,
        owner_user_id=principal_b.user_id,
        lab_id=principal_b.lab_id,
        proposed_kind=MemoryKind.OPERATING_NOTE,
        proposed_content="Private owner context.",
        reason="Persist private owner context.",
        source_run_id=uuid4(),
    )
    memory.submit_proposal(principal_b, proposal)
    entry = memory.decide(
        principal_b,
        proposal.proposal_id,
        MemoryDecision(
            proposal_id=proposal.proposal_id,
            gate_id=proposal.approval_gate_id,
            approved=True,
            decided_by=principal_b.user_id,
        ),
    )
    assert entry is not None
    assert memory.list_candidates(principal_a) == ()
    with pytest.raises(AuthorizationDenied):
        memory.get(principal_a, entry.memory_id, entry.version)

    run = WorkflowRun(
        workflow_id="c12-test",
        owner_user_id=principal_b.user_id,
        project_id="project-b",
        lab_id=principal_b.lab_id,
    )
    record = ApplicationRunRecord(
        run_id=run.run_id,
        task_text="Private run.",
        owner_user_id=run.owner_user_id,
        project_id=run.project_id,
        lab_id=run.lab_id,
        workflow_run=run,
        runtime_revision="c12-test",
    )
    uninitialized_application = object.__new__(LabBioApplication)
    with pytest.raises(AuthorizationDenied, match="durable run scope"):
        uninitialized_application._reauthorize_recovery(
            record, principal_a, workspace_a
        )
