"""C11 governed Memory proposal, retrieval, and provider-schema tests."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest
from pantheon.providers import LocalProvider
from pydantic import ValidationError

from labbioagentos import (
    AccessService,
    ArtifactConsumer,
    ArtifactExposureClass,
    ArtifactExposureService,
    ArtifactRepresentation,
    AuthorizationPolicy,
    CAPABILITY_INFORMATION_AUTHORITY,
    InMemoryMemoryStore,
    InMemoryProjectStore,
    InMemoryTraceSink,
    InformationAuthority,
    LabBioRuntimeToolSet,
    LocalArtifactStore,
    MemoryDecision,
    MemoryGovernanceService,
    MemoryKind,
    MemoryProposalAction,
    MemoryScope,
    MemoryStatus,
    MemoryUpdateProposal,
    Principal,
    PrincipalRole,
    Project,
    ProjectStatus,
    RunTraceRecorder,
    RuntimeCapabilityContext,
    RuntimeCapabilityServices,
    WorkflowStage,
    WorkspaceContext,
)
from labbioagentos.artifacts import ExposurePolicy


@pytest.fixture
def boundary(tmp_path):
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
    projects.register(
        Project(project_id="project-b", lab_id="lab-a", owner_user_id="user-b")
    )
    projects.register(
        Project(project_id="project-z", lab_id="lab-z", owner_user_id="user-z")
    )
    access = AccessService(projects, AuthorizationPolicy(), trace_recorder=recorder)
    principal = Principal(user_id="user-a", lab_id="lab-a")
    workspace = WorkspaceContext(
        user_id="user-a", project_id="project-a", lab_id="lab-a"
    )
    artifacts = LocalArtifactStore(tmp_path / "artifacts", trace_recorder=recorder)
    exposure = ArtifactExposureService(
        artifacts,
        ExposurePolicy(),
        access_service=access,
        trace_recorder=recorder,
    )
    memory_store = InMemoryMemoryStore()
    memory = MemoryGovernanceService(
        memory_store,
        access,
        trace_recorder=recorder,
        artifact_store=artifacts,
    )
    binding = RuntimeCapabilityContext(
        principal=principal,
        workspace=workspace,
        run_id=uuid4(),
        stage_id=WorkflowStage.LEARN,
        invocation_id=uuid4(),
        actor_profile_key="learner",
        actor_agent_name="LearnerAgent",
        capability_allowlist=("memory_search", "memory_view", "memory_propose_update"),
        consumer=ArtifactConsumer.REMOTE_LLM,
    )
    toolset = LabBioRuntimeToolSet(
        binding,
        RuntimeCapabilityServices(
            artifact_store=artifacts,
            artifact_exposure=exposure,
            memory_service=memory,
            trace_recorder=recorder,
        ),
    )
    return {
        "sink": sink,
        "recorder": recorder,
        "projects": projects,
        "access": access,
        "principal": principal,
        "workspace": workspace,
        "artifacts": artifacts,
        "memory_store": memory_store,
        "memory": memory,
        "binding": binding,
        "toolset": toolset,
    }


def _proposal(boundary, *, kind=MemoryKind.OPERATING_NOTE, content="Context note."):
    proposal = MemoryUpdateProposal(
        target_scope=MemoryScope.PERSONAL,
        owner_user_id="user-a",
        lab_id="lab-a",
        proposed_kind=kind,
        proposed_content=content,
        reason="The user explicitly requested durable context.",
        evidence_run_ids=(boundary["binding"].run_id,),
        source_run_id=boundary["binding"].run_id,
    )
    boundary["memory"].submit_proposal(
        boundary["principal"], proposal, workspace=boundary["workspace"]
    )
    return proposal


def _approve(boundary, proposal, actor=None):
    principal = actor or boundary["principal"]
    return boundary["memory"].decide(
        principal,
        proposal.proposal_id,
        MemoryDecision(
            proposal_id=proposal.proposal_id,
            gate_id=proposal.approval_gate_id,
            approved=True,
            decided_by=principal.user_id,
        ),
    )


@pytest.mark.asyncio
async def test_m14_m15_m16_m17_trusted_identity_and_evidence_boundary(boundary):
    toolset = boundary["toolset"]
    with pytest.raises(TypeError):
        await toolset.memory_propose_update(
            target_scope="PERSONAL",
            proposed_kind="PREFERENCE",
            proposed_content="Bounded preference.",
            reason="Explicit user request.",
            owner_user_id="intruder",
        )

    missing = await toolset.memory_propose_update(
        target_scope="PERSONAL",
        proposed_kind="PREFERENCE",
        proposed_content="Bounded preference.",
        reason="Explicit user request.",
        evidence_artifact_ids=[str(uuid4())],
    )
    assert missing["error"]["error_code"] == "ARTIFACT_NOT_FOUND"

    cross = boundary["artifacts"].register(
        artifact_type="cross-project-derived",
        exposure_class=ArtifactExposureClass.DERIVED,
        representation=ArtifactRepresentation(summary={"count": 1}),
        owner_user_id="user-b",
        project_id="project-b",
        lab_id="lab-a",
    )
    denied = await toolset.memory_propose_update(
        target_scope="PERSONAL",
        proposed_kind="PREFERENCE",
        proposed_content="Bounded preference.",
        reason="Explicit user request.",
        evidence_artifact_ids=[str(cross.artifact_id)],
    )
    assert denied["error"]["error_code"] == "AUTHORIZATION_DENIED"

    raw = boundary["artifacts"].register(
        artifact_type="raw-input",
        exposure_class=ArtifactExposureClass.RAW,
        representation=ArtifactRepresentation(stored_content="not projected"),
        owner_user_id="user-a",
        project_id="project-a",
        lab_id="lab-a",
    )
    unsafe = await toolset.memory_propose_update(
        target_scope="PERSONAL",
        proposed_kind="PREFERENCE",
        proposed_content="Bounded preference.",
        reason="Explicit user request.",
        evidence_artifact_ids=[str(raw.artifact_id)],
    )
    assert unsafe["error"]["error_code"] == "INVALID_MEMORY_PROVENANCE"

    derived = boundary["artifacts"].register(
        artifact_type="derived-summary",
        exposure_class=ArtifactExposureClass.DERIVED,
        representation=ArtifactRepresentation(summary={"count": 1}),
        owner_user_id="user-a",
        project_id="project-a",
        lab_id="lab-a",
    )
    accepted = await toolset.memory_propose_update(
        target_scope="PROJECT",
        proposed_kind="PROJECT_FACT",
        proposed_content="A bounded contextual project fact.",
        reason="Retain this user-approved project context.",
        evidence_artifact_ids=[str(derived.artifact_id)],
    )
    assert accepted["success"] is True
    proposal = boundary["memory_store"].get_proposal(
        UUID(accepted["data"]["proposal_id"])
    )
    assert proposal.owner_user_id == "user-a"
    assert proposal.project_id == "project-a"
    assert proposal.lab_id == "lab-a"
    assert proposal.source_run_id == boundary["binding"].run_id
    assert proposal.proposing_invocation_id == boundary["binding"].invocation_id
    assert proposal.evidence_run_ids == (boundary["binding"].run_id,)


@pytest.mark.asyncio
async def test_m8_m9_m18_m19_m20_m21_m22_m23_catalog_and_authority(boundary):
    first = _approve(boundary, _proposal(boundary, content="First context."))
    second = _approve(
        boundary,
        _proposal(
            boundary,
            kind=MemoryKind.BIOLOGICAL_EVIDENCE,
            content="Historical biological context, not current evidence.",
        ),
    )
    assert first is not None and second is not None
    update = MemoryUpdateProposal(
        target_scope=MemoryScope.PERSONAL,
        owner_user_id="user-a",
        lab_id="lab-a",
        target_memory_id=first.memory_id,
        target_version=1,
        proposed_kind=MemoryKind.OPERATING_NOTE,
        proposed_content="Current first context.",
        reason="The user approved a revision.",
        source_run_id=boundary["binding"].run_id,
    )
    boundary["memory"].submit_proposal(
        boundary["principal"], update, workspace=boundary["workspace"]
    )
    current_first = _approve(boundary, update)
    assert current_first is not None

    page1 = await boundary["toolset"].memory_search(offset=0, limit=1)
    page2 = await boundary["toolset"].memory_search(
        offset=page1["data"]["next_offset"], limit=1
    )
    assert page1["information_authority"] == "MODEL_CONTEXT"
    assert page1["data"]["returned_count"] == 1
    assert page1["data"]["available_count"] == 2
    assert page1["data"]["truncated"] is True
    assert page2["data"]["truncated"] is False
    items = page1["data"]["items"] + page2["data"]["items"]
    by_id = {item["memory_id"]: item for item in items}
    assert by_id[str(first.memory_id)]["version"] == 2
    assert all(item["version"] != 1 or item["memory_id"] != str(first.memory_id) for item in items)

    biological = await boundary["toolset"].memory_search(
        kind="BIOLOGICAL_EVIDENCE"
    )
    assert biological["information_authority"] == "MODEL_CONTEXT"
    assert biological["data"]["items"][0]["memory_id"] == str(second.memory_id)
    detail = await boundary["toolset"].memory_view(str(second.memory_id), 1)
    assert detail["information_authority"] == "MODEL_CONTEXT"
    assert detail["data"]["evidence_run_count"] == 1
    encoded = json.dumps(detail)
    assert "evidence_run_ids" not in encoded
    assert "evidence_artifact_ids" not in encoded
    assert str(boundary["binding"].run_id) not in encoded
    assert boundary["memory"].get(boundary["principal"], first.memory_id, 1) == first


@pytest.mark.asyncio
async def test_m24_m31_m32_control_state_and_typed_system_separation(boundary):
    functions_before = tuple(sorted(boundary["toolset"].functions))
    receipt = await boundary["toolset"].memory_propose_update(
        target_scope="PERSONAL",
        proposed_kind="OPERATING_NOTE",
        proposed_content="Always use method X.",
        reason="Store only as optional context.",
    )
    assert receipt["information_authority"] == "CONTROL_STATE"
    assert set(receipt["data"]) == {
        "proposal_id",
        "approval_gate_id",
        "domain_reference_id",
        "status",
    }
    assert boundary["memory_store"].entries() == ()
    assert tuple(sorted(boundary["toolset"].functions)) == functions_before
    assert CAPABILITY_INFORMATION_AUTHORITY["memory_view"] is InformationAuthority.MODEL_CONTEXT
    assert "skill_search" not in boundary["toolset"].functions


@pytest.mark.asyncio
async def test_m33_m34_m35_retired_memory_is_hidden_but_auditable(boundary):
    v1 = _approve(boundary, _proposal(boundary))
    assert v1 is not None
    retire = MemoryUpdateProposal(
        action=MemoryProposalAction.RETIRE,
        target_scope=MemoryScope.PERSONAL,
        owner_user_id="user-a",
        lab_id="lab-a",
        target_memory_id=v1.memory_id,
        target_version=1,
        reason="The user retired stale contextual Memory.",
        source_run_id=boundary["binding"].run_id,
    )
    boundary["memory"].submit_proposal(
        boundary["principal"], retire, workspace=boundary["workspace"]
    )
    v2 = _approve(boundary, retire)
    assert v2 is not None and v2.status is MemoryStatus.RETIRED
    page = await boundary["toolset"].memory_search()
    assert page["data"]["items"] == []
    assert boundary["memory"].lineage(boundary["principal"], v1.memory_id) == (v1, v2)


def test_m10_m11_m12_m13_scope_write_gate_is_completable(boundary):
    collaborator = Principal(user_id="user-c", lab_id="lab-a")
    admin = Principal(
        user_id="admin-a",
        lab_id="lab-a",
        roles=frozenset({PrincipalRole.MEMBER, PrincipalRole.LAB_ADMIN}),
    )
    other_lab = Principal(user_id="user-z", lab_id="lab-z")
    source_run_id = boundary["binding"].run_id

    project = MemoryUpdateProposal(
        target_scope=MemoryScope.PROJECT,
        owner_user_id="user-a",
        project_id="project-a",
        lab_id="lab-a",
        proposed_kind=MemoryKind.PROJECT_FACT,
        proposed_content="Project context.",
        reason="Explicit project context request.",
        source_run_id=source_run_id,
    )
    with pytest.raises(PermissionError):
        boundary["memory"].submit_proposal(
            collaborator, project, workspace=boundary["workspace"]
        )
    boundary["memory"].submit_proposal(
        boundary["principal"], project, workspace=boundary["workspace"]
    )
    approved_project = _approve(boundary, project)
    assert approved_project is not None
    assert boundary["memory"].get(collaborator, approved_project.memory_id, 1)

    lab = MemoryUpdateProposal(
        target_scope=MemoryScope.LAB,
        lab_id="lab-a",
        proposed_kind=MemoryKind.OPERATING_NOTE,
        proposed_content="Lab context.",
        reason="Explicit lab context request.",
        source_run_id=source_run_id,
    )
    with pytest.raises(PermissionError):
        boundary["memory"].submit_proposal(
            boundary["principal"], lab, workspace=boundary["workspace"]
        )
    boundary["memory"].submit_proposal(
        admin, lab, workspace=WorkspaceContext(
            user_id="admin-a", project_id="project-a", lab_id="lab-a"
        )
    )
    approved_lab = _approve(boundary, lab, actor=admin)
    assert approved_lab is not None
    assert boundary["memory"].get(boundary["principal"], approved_lab.memory_id, 1)
    with pytest.raises(PermissionError):
        boundary["memory"].get(other_lab, approved_lab.memory_id, 1)


def test_archived_project_and_unsafe_persistent_text_are_rejected(boundary):
    boundary["projects"].register(
        Project(
            project_id="archived",
            lab_id="lab-a",
            owner_user_id="user-a",
            status=ProjectStatus.ARCHIVED,
        )
    )
    proposal = MemoryUpdateProposal(
        target_scope=MemoryScope.PROJECT,
        owner_user_id="user-a",
        project_id="archived",
        lab_id="lab-a",
        proposed_kind=MemoryKind.OPERATING_NOTE,
        proposed_content="Safe context.",
        reason="Explicit request.",
        source_run_id=boundary["binding"].run_id,
    )
    with pytest.raises(PermissionError):
        boundary["memory"].submit_proposal(
            boundary["principal"],
            proposal,
            workspace=WorkspaceContext(
                user_id="user-a", project_id="archived", lab_id="lab-a"
            ),
        )
    with pytest.raises(ValidationError):
        proposal.model_copy(
            update={"proposed_content": "Read /media/private/secret.txt"}
        ).__class__.model_validate(
            {
                **proposal.model_dump(),
                "proposed_content": "Read /media/private/secret.txt",
            }
        )


@pytest.mark.asyncio
async def test_provider_schema_is_typed_and_excludes_trusted_fields(boundary):
    provider = LocalProvider(boundary["toolset"])
    await provider.initialize()
    tools = {item.name: item.inputSchema["parameters"] for item in await provider.list_tools()}
    search = tools["memory_search"]
    assert "query_text" not in search["properties"]
    assert search["properties"]["offset"]["type"] == "integer"
    assert search["properties"]["limit"]["type"] == "integer"
    assert search["properties"]["scope"]["anyOf"][0] == {
        "enum": ["PERSONAL", "PROJECT", "LAB"],
        "type": "string",
    }
    assert search["properties"]["kind"]["anyOf"][0]["enum"] == [
        "PREFERENCE",
        "PROJECT_FACT",
        "BIOLOGICAL_EVIDENCE",
        "HYPOTHESIS",
        "OPERATING_NOTE",
    ]
    proposal = tools["memory_propose_update"]
    assert proposal["additionalProperties"] is False
    assert proposal["properties"]["target_scope"]["enum"] == [
        "PERSONAL", "PROJECT", "LAB"
    ]
    assert proposal["properties"]["action"]["enum"] == ["UPSERT", "RETIRE"]
    assert proposal["properties"]["target_version"]["anyOf"] == [
        {"type": "integer"}, {"type": "null"}
    ]
    for trusted in (
        "owner_user_id",
        "project_id",
        "lab_id",
        "source_run_id",
        "proposing_invocation_id",
        "evidence_run_ids",
    ):
        assert trusted not in proposal["properties"]
