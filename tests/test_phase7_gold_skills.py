"""Phase 7 acceptance tests for user-approved immutable Gold Skills."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from labbioagentos import (
    ArtifactExposureClass,
    ArtifactRepresentation,
    GoldSkillService,
    InMemorySkillStore,
    InMemoryTraceSink,
    InstructionKind,
    InstructionRecord,
    LocalArtifactStore,
    RunTraceRecorder,
    SkillApprovalRequiredError,
    SkillCuratorPort,
    SkillDecisionError,
    SkillProcedure,
    SkillProposal,
    SkillScope,
    SkillSearchContext,
    SkillSourceProjectionError,
    SkillSourceProjector,
    SkillUsageOutcome,
    SkillUseMode,
    SkillUseProposal,
    SkillUserDecision,
    TraceEventType,
    WorkflowStage,
)


class MockSkillCurator(SkillCuratorPort):
    """Test-only curator returning caller-supplied synthetic procedural content."""

    def __init__(
        self,
        *,
        name="Synthetic procedural memory",
        scope=SkillScope.PERSONAL,
        owner_user_id="user-a",
        project_id=None,
        tags=frozenset({"synthetic"}),
        artifact_types=frozenset({"synthetic-result"}),
        parent_skill_id=None,
        parent_version=None,
        source_usage_record_id=None,
    ):
        self.name = name
        self.scope = scope
        self.owner_user_id = owner_user_id
        self.project_id = project_id
        self.tags = tags
        self.artifact_types = artifact_types
        self.parent_skill_id = parent_skill_id
        self.parent_version = parent_version
        self.source_usage_record_id = source_usage_record_id

    def propose(self, source):
        return SkillProposal(
            source_bundle_id=source.bundle_id,
            source_run_id=source.source_run_id,
            proposed_name=self.name,
            description="Mock curator-provided description.",
            scope=self.scope,
            owner_user_id=self.owner_user_id,
            project_id=self.project_id,
            parent_skill_id=self.parent_skill_id,
            parent_version=self.parent_version,
            source_usage_record_id=self.source_usage_record_id,
            procedure=SkillProcedure(
                applicability="Runtime model evaluates applicability for the current task.",
                workflow_outline=(
                    "Review the referenced successful workflow evidence.",
                    "Produce a current-task plan through the normal runtime.",
                ),
                agent_collaboration_guidance=(
                    "Treat prior delegation as guidance, not a fixed sequence.",
                ),
                important_instruction_ids=tuple(
                    item.instruction_id for item in source.instruction_refs
                ),
                execution_guidance=(
                    "Use referenced script hashes as evidence only.",
                ),
                output_contract_ids=("generic.scalar.records.v1",),
                validation_expectations=("Apply current-task technical validation.",),
                known_failure_modes=("Prior technical failure references may be reviewed.",),
                known_limitations=("No scientific applicability is asserted.",),
                script_artifact_ids=tuple(
                    item.script_artifact_id
                    for item in source.execution_refs
                    if item.script_artifact_id is not None
                ),
                source_trace_event_ids=source.trace_event_ids,
                tags=self.tags,
                artifact_types=self.artifact_types,
            ),
        )


def _trace_environment(tmp_path):
    sink = InMemoryTraceSink()
    recorder = RunTraceRecorder(sink)
    artifact_store = LocalArtifactStore(tmp_path / "artifacts", trace_recorder=recorder)
    skill_store = InMemorySkillStore()
    service = GoldSkillService(
        skill_store,
        SkillSourceProjector(artifact_store),
        trace_recorder=recorder,
    )
    return sink, recorder, artifact_store, skill_store, service


def _successful_trace(
    recorder,
    artifact_store,
    *,
    run_id=None,
    raw_token="RAW_ARTIFACT_PAYLOAD_MUST_NOT_ENTER_SKILLS",
):
    run_id = run_id or uuid4()
    root_invocation_id = uuid4()
    child_invocation_id = uuid4()
    recorder.emit(run_id, TraceEventType.RUN_CREATED, status="CREATED")
    recorder.emit(run_id, TraceEventType.RUN_STARTED, status="RUNNING")
    recorder.emit(
        run_id,
        TraceEventType.STAGE_ENTERED,
        stage_id=WorkflowStage.INTAKE,
        status="RUNNING",
    )
    recorder.emit(
        run_id,
        TraceEventType.STAGE_ENTERED,
        stage_id=WorkflowStage.PLAN,
        status="RUNNING",
    )
    recorder.emit(
        run_id,
        TraceEventType.AGENT_STARTED,
        stage_id=WorkflowStage.PLAN,
        invocation_id=root_invocation_id,
        agent_name="planner",
        status="STARTED",
    )
    recorder.emit(
        run_id,
        TraceEventType.DELEGATION_STARTED,
        stage_id=WorkflowStage.PLAN,
        invocation_id=child_invocation_id,
        parent_invocation_id=root_invocation_id,
        caller="planner",
        target="specialist",
        execution_context_id="synthetic|d1|specialist|0001",
        parent_tool_call_id="call-specialist",
        chain_path=("planner", "specialist:call-specialist"),
        status="STARTED",
    )
    recorder.emit(
        run_id,
        TraceEventType.DELEGATION_COMPLETED,
        stage_id=WorkflowStage.PLAN,
        invocation_id=child_invocation_id,
        parent_invocation_id=root_invocation_id,
        caller="planner",
        target="specialist",
        execution_context_id="synthetic|d1|specialist|0001",
        parent_tool_call_id="call-specialist",
        chain_path=("planner", "specialist:call-specialist"),
        status="SUCCEEDED",
    )
    recorder.emit(
        run_id,
        TraceEventType.AGENT_COMPLETED,
        stage_id=WorkflowStage.PLAN,
        invocation_id=root_invocation_id,
        agent_name="planner",
        status="SUCCEEDED",
    )
    reusable_instruction = InstructionRecord(
        run_id=run_id,
        stage_id=WorkflowStage.PLAN,
        invocation_id=root_invocation_id,
        kind=InstructionKind.PLANNING,
        template_id="synthetic-plan-template",
        template_version="1",
        template_hash="synthetic-template-hash",
        sanitized_rendered_instruction="Use explicit synthetic procedural evidence.",
        procedural_reuse_relevant=True,
    )
    recorder.record_instruction(reusable_instruction)
    recorder.record_instruction(
        InstructionRecord(
            run_id=run_id,
            stage_id=WorkflowStage.PLAN,
            kind=InstructionKind.OTHER,
            sanitized_rendered_instruction="Not marked for procedural reuse.",
        )
    )
    recorder.emit(
        run_id,
        TraceEventType.STAGE_ENTERED,
        stage_id=WorkflowStage.EXECUTE,
        status="RUNNING",
    )
    artifact_ref = artifact_store.register(
        artifact_type="synthetic-result",
        exposure_class=ArtifactExposureClass.RAW,
        representation=ArtifactRepresentation(
            stored_content={"private_fixture": raw_token}
        ),
        run_id=run_id,
        stage_id=WorkflowStage.EXECUTE,
        producer_invocation_id=root_invocation_id,
        metadata={"schema_id": "synthetic.raw.v1"},
    )
    execution_id = uuid4()
    recorder.emit(
        run_id,
        TraceEventType.EXECUTION_PLANNED,
        stage_id=WorkflowStage.EXECUTE,
        invocation_id=root_invocation_id,
        status="PLANNED",
        payload={
            "execution_id": str(execution_id),
            "image_key": "python-analysis",
            "resolved_image": "local/python-fixture:3.11",
            "script_hash": "a" * 64,
            "script_artifact_id": str(artifact_ref.artifact_id),
            "input_artifact_ids": [str(artifact_ref.artifact_id)],
        },
    )
    recorder.emit(
        run_id,
        TraceEventType.EXECUTION_COMPLETED,
        stage_id=WorkflowStage.EXECUTE,
        invocation_id=root_invocation_id,
        status="SUCCEEDED",
        payload={
            "execution_id": str(execution_id),
            "exit_code": 0,
            "output_artifact_ids": [str(artifact_ref.artifact_id)],
        },
    )
    recorder.emit(
        run_id,
        TraceEventType.STAGE_ENTERED,
        stage_id=WorkflowStage.VALIDATE,
        status="RUNNING",
    )
    recorder.emit(
        run_id,
        TraceEventType.STAGE_COMPLETED,
        stage_id=WorkflowStage.VALIDATE,
        status="SUCCEEDED",
    )
    recorder.emit(run_id, TraceEventType.RUN_COMPLETED, status="COMPLETED")
    return run_id, artifact_ref, reusable_instruction


def _approve_proposal(service, proposal, *, user="user-a"):
    return service.decide_proposal(
        proposal.proposal_id,
        SkillUserDecision(
            subject_id=proposal.proposal_id,
            gate_id=proposal.approval_gate_id,
            approved=True,
            decided_by=user,
        ),
    )


def _gold_fixture(tmp_path, *, curator=None):
    sink, recorder, artifacts, skill_store, service = _trace_environment(tmp_path)
    run_id, _, _ = _successful_trace(recorder, artifacts)
    bundle = service.create_source_bundle(sink.read(run_id), run_id=run_id)
    proposal = service.create_proposal(
        bundle.bundle_id,
        curator or MockSkillCurator(),
    )
    gold = _approve_proposal(service, proposal)
    return sink, recorder, artifacts, skill_store, service, bundle, proposal, gold


def test_only_successful_completed_trace_can_create_source_bundle(tmp_path):
    sink, recorder, artifacts, _, service = _trace_environment(tmp_path)
    failed_run_id = uuid4()
    recorder.emit(failed_run_id, TraceEventType.RUN_CREATED, status="CREATED")
    recorder.emit(failed_run_id, TraceEventType.RUN_FAILED, status="FAILED")

    with pytest.raises(SkillSourceProjectionError, match="successfully completed"):
        service.create_source_bundle(
            sink.read(failed_run_id),
            run_id=failed_run_id,
        )

    successful_run_id, _, _ = _successful_trace(recorder, artifacts)
    bundle = service.create_source_bundle(
        sink.read(successful_run_id),
        run_id=successful_run_id,
    )
    assert bundle.final_status.value == "COMPLETED"


def test_source_bundle_preserves_path_delegation_instruction_and_safe_refs(tmp_path):
    sink, recorder, artifacts, _, service = _trace_environment(tmp_path)
    run_id, artifact_ref, instruction = _successful_trace(recorder, artifacts)
    bundle = service.create_source_bundle(
        sink.read(run_id),
        run_id=run_id,
        task_reference="synthetic-task-ref",
    )

    assert bundle.workflow_stage_path == (
        WorkflowStage.INTAKE,
        WorkflowStage.PLAN,
        WorkflowStage.EXECUTE,
        WorkflowStage.VALIDATE,
    )
    assert bundle.invocations[0].agent_name == "planner"
    assert bundle.delegations[0].caller == "planner"
    assert bundle.delegations[0].target == "specialist"
    assert bundle.instruction_refs[0].instruction_id == instruction.instruction_id
    assert len(bundle.instruction_refs) == 1
    assert bundle.execution_refs[0].script_hash == "a" * 64
    assert artifact_ref.artifact_id in bundle.artifact_ids
    assert bundle.artifact_refs[0].artifact_id == artifact_ref.artifact_id
    assert bundle.validation_refs


def test_source_bundle_and_gold_store_do_not_copy_raw_artifact_payload(tmp_path):
    raw_token = "RAW_MATRIX_SENTINEL_99172"
    sink, recorder, artifacts, _, service = _trace_environment(tmp_path)
    run_id, _, _ = _successful_trace(
        recorder,
        artifacts,
        raw_token=raw_token,
    )
    bundle = service.create_source_bundle(sink.read(run_id), run_id=run_id)
    proposal = service.create_proposal(bundle.bundle_id, MockSkillCurator())
    gold = _approve_proposal(service, proposal)

    assert raw_token not in bundle.model_dump_json()
    assert raw_token not in gold.model_dump_json()
    assert not any(
        hasattr(gold, method) for method in ("execute", "run", "apply", "execute_workflow")
    )


def test_mock_curator_returns_typed_proposal_but_cannot_auto_promote(tmp_path):
    sink, recorder, artifacts, skill_store, service = _trace_environment(tmp_path)
    run_id, _, _ = _successful_trace(recorder, artifacts)
    bundle = service.create_source_bundle(sink.read(run_id), run_id=run_id)
    proposal = service.create_proposal(bundle.bundle_id, MockSkillCurator())

    assert isinstance(proposal, SkillProposal)
    assert skill_store.search(SkillSearchContext(user_id="user-a")) == ()
    with pytest.raises(SkillDecisionError, match="does not match"):
        service.decide_proposal(
            proposal.proposal_id,
            SkillUserDecision(
                subject_id=proposal.proposal_id,
                gate_id="wrong-gate",
                approved=True,
                decided_by="user-a",
            ),
        )


def test_user_approval_creates_v1_and_rejection_creates_no_gold(tmp_path):
    _, _, _, skill_store, service, bundle, proposal, gold = _gold_fixture(tmp_path)
    assert gold.version == 1
    assert gold.source_run_id == bundle.source_run_id
    assert gold.source_proposal_id == proposal.proposal_id
    assert skill_store.get_gold(gold.skill_id, 1) == gold

    rejected = service.create_proposal(
        bundle.bundle_id,
        MockSkillCurator(name="Rejected synthetic proposal"),
    )
    result = service.decide_proposal(
        rejected.proposal_id,
        SkillUserDecision(
            subject_id=rejected.proposal_id,
            gate_id=rejected.approval_gate_id,
            approved=False,
            decided_by="user-a",
        ),
    )
    assert result is None
    assert all(
        candidate.source_proposal_id != rejected.proposal_id
        for candidate in skill_store.search(SkillSearchContext(user_id="user-a"))
    )


def test_gold_versions_are_immutable(tmp_path):
    *_, gold = _gold_fixture(tmp_path)
    with pytest.raises(ValidationError):
        gold.version = 99
    with pytest.raises(ValidationError):
        gold.procedure.workflow_outline = ("mutated",)


def test_search_filters_scope_tags_and_metadata_without_reuse_decision(tmp_path):
    _, _, _, _, service, bundle, _, personal = _gold_fixture(tmp_path)
    project_proposal = service.create_proposal(
        bundle.bundle_id,
        MockSkillCurator(
            name="Project candidate",
            scope=SkillScope.PROJECT,
            owner_user_id=None,
            project_id="project-a",
            tags=frozenset({"synthetic", "project"}),
        ),
    )
    project = _approve_proposal(service, project_proposal, user="project-owner")
    lab_proposal = service.create_proposal(
        bundle.bundle_id,
        MockSkillCurator(
            name="Lab candidate",
            scope=SkillScope.LAB,
            owner_user_id=None,
            tags=frozenset({"synthetic", "lab"}),
        ),
    )
    lab = _approve_proposal(service, lab_proposal, user="lab-owner")

    visible = service.search(
        SkillSearchContext(
            user_id="user-a",
            project_id="project-a",
            required_tags=frozenset({"synthetic"}),
            artifact_types=frozenset({"synthetic-result"}),
        )
    )
    assert {item.skill_id for item in visible} == {
        personal.skill_id,
        project.skill_id,
        lab.skill_id,
    }
    outsider = service.search(
        SkillSearchContext(
            user_id="user-b",
            project_id="project-b",
            query_text="candidate",
        )
    )
    assert outsider == (lab,)
    assert all(not hasattr(item, "similarity_score") for item in visible)
    assert all(not hasattr(item, "proposed_mode") for item in visible)


@pytest.mark.parametrize(
    "mode",
    (SkillUseMode.REUSE, SkillUseMode.ADAPT, SkillUseMode.REFERENCE),
)
def test_each_runtime_selected_use_mode_requires_user_approval(tmp_path, mode):
    *_, service, _, _, gold = _gold_fixture(tmp_path)
    use = SkillUseProposal(
        run_id=uuid4(),
        skill_id=gold.skill_id,
        skill_version=gold.version,
        proposed_mode=mode,
        reason="Runtime-provided synthetic use reason.",
        proposed_deviations=("Runtime-provided synthetic deviation.",),
    )
    service.submit_use_proposal(use)
    rejected = service.decide_use(
        use.proposal_id,
        SkillUserDecision(
            subject_id=use.proposal_id,
            gate_id=use.approval_gate_id,
            approved=False,
            decided_by="user-a",
        ),
    )
    with pytest.raises(SkillApprovalRequiredError, match="Rejected Skill use"):
        service.record_usage(rejected.authorization_id, SkillUsageOutcome.SUCCEEDED)


def test_approved_reference_usage_records_exact_version_without_mutating_gold(tmp_path):
    *_, skill_store, service, _, _, gold = _gold_fixture(tmp_path)
    original = gold.model_dump_json()
    run_id = uuid4()
    use = SkillUseProposal(
        run_id=run_id,
        skill_id=gold.skill_id,
        skill_version=gold.version,
        proposed_mode=SkillUseMode.REFERENCE,
        reason="Use as non-executable planning context.",
    )
    service.submit_use_proposal(use)
    authorization = service.decide_use(
        use.proposal_id,
        SkillUserDecision(
            subject_id=use.proposal_id,
            gate_id=use.approval_gate_id,
            approved=True,
            decided_by="user-a",
        ),
    )
    usage = service.record_usage(
        authorization.authorization_id,
        SkillUsageOutcome.SUCCEEDED,
    )

    assert usage.run_id == run_id
    assert usage.skill_id == gold.skill_id
    assert usage.skill_version == 1
    assert usage.proposed_mode is SkillUseMode.REFERENCE
    assert skill_store.get_gold(gold.skill_id, 1).model_dump_json() == original


def test_successful_adapted_run_can_create_v2_without_overwriting_v1(tmp_path):
    sink, recorder, artifacts, skill_store, service, _, _, v1 = _gold_fixture(tmp_path)
    v1_snapshot = v1.model_dump_json()
    adapted_run_id, _, _ = _successful_trace(
        recorder,
        artifacts,
        run_id=uuid4(),
        raw_token="ADAPTED_RUN_RAW_PAYLOAD",
    )
    adapted_bundle = service.create_source_bundle(
        sink.read(adapted_run_id),
        run_id=adapted_run_id,
    )
    use = SkillUseProposal(
        run_id=adapted_run_id,
        skill_id=v1.skill_id,
        skill_version=v1.version,
        proposed_mode=SkillUseMode.ADAPT,
        reason="Runtime proposes adaptation for this synthetic task.",
        proposed_deviations=("Runtime-provided adaptation summary.",),
    )
    service.submit_use_proposal(use)
    authorization = service.decide_use(
        use.proposal_id,
        SkillUserDecision(
            subject_id=use.proposal_id,
            gate_id=use.approval_gate_id,
            approved=True,
            decided_by="user-a",
        ),
    )
    usage = service.record_usage(
        authorization.authorization_id,
        SkillUsageOutcome.SUCCEEDED,
    )
    v2_proposal = service.create_proposal(
        adapted_bundle.bundle_id,
        MockSkillCurator(
            name="Synthetic procedural memory v2",
            parent_skill_id=v1.skill_id,
            parent_version=v1.version,
            source_usage_record_id=usage.usage_id,
        ),
    )
    v2 = _approve_proposal(service, v2_proposal)

    assert v2.skill_id == v1.skill_id
    assert v2.version == 2
    assert v2.parent_skill_id == v1.skill_id
    assert v2.parent_version == 1
    assert v2.source_usage_record_id == usage.usage_id
    assert skill_store.get_gold(v1.skill_id, 1).model_dump_json() == v1_snapshot
    assert skill_store.lineage(v1.skill_id) == (v1, v2)


def test_skill_lifecycle_trace_contains_ids_not_skill_content(tmp_path):
    sink, _, _, _, service, bundle, proposal, gold = _gold_fixture(tmp_path)
    use = SkillUseProposal(
        run_id=bundle.source_run_id,
        skill_id=gold.skill_id,
        skill_version=gold.version,
        proposed_mode=SkillUseMode.REUSE,
        reason="TRACE_SECRET_USE_REASON",
    )
    service.submit_use_proposal(use)
    authorization = service.decide_use(
        use.proposal_id,
        SkillUserDecision(
            subject_id=use.proposal_id,
            gate_id=use.approval_gate_id,
            approved=True,
            decided_by="user-a",
        ),
    )
    usage = service.record_usage(
        authorization.authorization_id,
        SkillUsageOutcome.SUCCEEDED,
    )

    events = sink.read(bundle.source_run_id)
    event_types = {event.event_type for event in events}
    assert TraceEventType.SKILL_SOURCE_CREATED in event_types
    assert TraceEventType.SKILL_PROPOSAL_CREATED in event_types
    assert TraceEventType.SKILL_PROPOSAL_APPROVED in event_types
    assert TraceEventType.SKILL_USE_PROPOSED in event_types
    assert TraceEventType.SKILL_USE_APPROVED in event_types
    assert TraceEventType.SKILL_USAGE_RECORDED in event_types
    trace_json = json.dumps([event.model_dump(mode="json") for event in events])
    assert str(proposal.proposal_id) in trace_json
    assert str(gold.skill_id) in trace_json
    assert str(usage.usage_id) in trace_json
    assert "Mock curator-provided description" not in trace_json
    assert "TRACE_SECRET_USE_REASON" not in trace_json
    assert "Review the referenced successful workflow evidence" not in trace_json
