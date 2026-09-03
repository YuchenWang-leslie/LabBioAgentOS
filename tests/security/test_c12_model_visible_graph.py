"""Recursive C12 regression for every representative model-visible DTO."""

from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import BaseModel, ValidationError

from labbioagentos import (
    ArtifactConsumer,
    ArtifactExposureClass,
    ArtifactExposureService,
    ArtifactListItem,
    ArtifactQuery,
    ArtifactReleaseBasis,
    ArtifactRepresentation,
    ArtifactViewType,
    CapabilityEvidenceBundle,
    CapabilityEvidenceItem,
    CapabilityEvidenceStatus,
    ExecutionReceipt,
    ExecutionStatus,
    InformationAuthority,
    LocalArtifactStore,
    MemoryCandidatePage,
    MemoryCandidateView,
    MemoryDetailView,
    MemoryKind,
    MemoryScope,
    RuntimeGateDecisionView,
    RuntimeStageInput,
    RuntimeWorkspaceIdentifiers,
    SkillCandidatePage,
    SkillCandidateView,
    SkillDetailView,
    TraceEvent,
    TraceEventType,
    WorkflowStage,
)
from labbioagentos.artifacts import ExposurePolicy


FORBIDDEN_KEYS = {
    "storage_locator",
    "stored_content",
    "script_body",
    "script_content",
    "stdout_body",
    "stderr_body",
    "provider_request_body",
    "provider_response_body",
    "provider_raw_body",
    "reasoning_content",
    "credentials",
    "authorization_secret",
}
FORBIDDEN_TEXT = (
    "PRIVATE_C12_RAW_SENTINEL",
    "docker.sock",
    "-----BEGIN PRIVATE KEY-----",
)


def _normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _scan_model_graph(value, *, path="root"):
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", by_alias=True)
    if isinstance(value, Path):
        raise AssertionError(f"Path object at {path}")
    if isinstance(value, dict):
        for key, item in value.items():
            assert isinstance(key, str), f"non-string key at {path}"
            assert _normalized(key) not in FORBIDDEN_KEYS, f"unsafe key {path}.{key}"
            _scan_model_graph(item, path=f"{path}.{key}")
        return
    if isinstance(value, (tuple, list, set, frozenset)):
        for index, item in enumerate(value):
            _scan_model_graph(item, path=f"{path}[{index}]")
        return
    if isinstance(value, str):
        assert not value.startswith("/"), f"absolute host path at {path}"
        assert not re.match(r"^[A-Za-z]:[\\/]", value), f"host path at {path}"
        for forbidden in FORBIDDEN_TEXT:
            assert forbidden not in value, f"forbidden content at {path}"
        return
    assert value is None or isinstance(value, (bool, int, float)), (
        f"internal object {type(value).__name__} at {path}"
    )


def test_recursive_model_visible_object_graph_contains_only_bounded_dtos(tmp_path):
    artifact_store = LocalArtifactStore(tmp_path / "artifacts")
    ref = artifact_store.register(
        artifact_type="safe-aggregate",
        exposure_class=ArtifactExposureClass.AGGREGATE,
        release_basis=ArtifactReleaseBasis.TRUSTED_AGGREGATE_INSPECTOR,
        representation=ArtifactRepresentation(summary={"observation_count": 4}),
    )
    artifact_view = ArtifactExposureService(
        artifact_store, ExposurePolicy()
    ).artifact_query(
        ref.artifact_id,
        ArtifactQuery(view_type=ArtifactViewType.SUMMARY),
        ArtifactConsumer.REMOTE_LLM,
    )
    artifact_list_item = ArtifactListItem(
        artifact_id=ref.artifact_id,
        artifact_type=ref.artifact_type,
        exposure_class=ref.exposure_class.value,
        release_basis=ref.release_basis.value,
        available_views=("METADATA", "SCHEMA", "SUMMARY"),
        owner_user_id=ref.owner_user_id,
        project_id=ref.project_id,
        lab_id=ref.lab_id,
    )
    execution_receipt = ExecutionReceipt(
        execution_id=uuid4(),
        status=ExecutionStatus.SUCCEEDED,
        image_key="python-c12",
        script_hash="a" * 64,
        exit_code=0,
        output_artifact_ids=(ref.artifact_id,),
    )
    skill_candidate = SkillCandidateView(
        skill_id=uuid4(),
        version=1,
        name="Generic validated procedure",
        description="Optional procedural context.",
        scope="PERSONAL",
        applicability_preview="Use only when currently applicable.",
    )
    skill_page = SkillCandidatePage(
        items=(skill_candidate,),
        returned_count=1,
        available_count=1,
        offset=0,
        effective_limit=20,
        truncated=False,
    )
    skill_detail = SkillDetailView(
        **skill_candidate.model_dump(),
        source_run_id=uuid4(),
        applicability="Use only when currently applicable.",
        workflow_outline=("Inspect current governed evidence.",),
    )
    memory_candidate = MemoryCandidateView(
        memory_id=uuid4(),
        version=1,
        scope=MemoryScope.PERSONAL,
        kind=MemoryKind.PREFERENCE,
        preview="Prefer concise summaries.",
    )
    memory_page = MemoryCandidatePage(
        items=(memory_candidate,),
        returned_count=1,
        available_count=1,
        offset=0,
        effective_limit=20,
        truncated=False,
    )
    memory_detail = MemoryDetailView(
        **memory_candidate.model_dump(),
        content="Prefer concise summaries.",
        evidence_run_count=0,
        evidence_artifact_count=0,
        has_evidence=False,
    )
    evidence = CapabilityEvidenceBundle(
        run_id=uuid4(),
        stage_id=WorkflowStage.VALIDATE,
        invocation_id=uuid4(),
        items=(
            CapabilityEvidenceItem(
                actor_profile_key="reviewer",
                actor_agent_name="ReviewerAgent",
                capability_name="artifact_query",
                information_authority=InformationAuthority.AUTHORITATIVE_EVIDENCE,
                status=CapabilityEvidenceStatus.COMPLETED,
                reference_ids=(str(ref.artifact_id),),
                safe_result=artifact_view.model_dump(mode="json", by_alias=True),
            ),
        ),
        explicit_completion="Inspection completed using governed evidence.",
    )
    stage_input = RuntimeStageInput(
        run_id=evidence.run_id,
        stage_id=WorkflowStage.VALIDATE,
        invocation_id=evidence.invocation_id,
        instruction="Validate the current governed result.",
        workspace=RuntimeWorkspaceIdentifiers(
            user_id="local-user",
            project_id="local-project",
            lab_id="local-lab",
        ),
        allowed_capabilities=("artifact_query",),
        gate_decisions=(
            RuntimeGateDecisionView(
                gate_id="gate-c12",
                source_stage=WorkflowStage.PLAN,
                approved=True,
                domain_reference_id="skill-use-c12",
                decision_reference_id="decision-c12",
            ),
        ),
    )

    representative_provider_inputs = {
        "runtime_stage_input": stage_input,
        "capability_evidence_bundle": evidence,
        "artifact_list_item": artifact_list_item,
        "artifact_view": artifact_view,
        "execution_receipt": execution_receipt,
        "skill_candidate_page": skill_page,
        "authorized_gold_view": skill_detail,
        "memory_candidate_page": memory_page,
        "memory_detail_view": memory_detail,
        "user_gate_context": stage_input.gate_decisions,
        "finalization_input": {
            "stage": stage_input,
            "evidence": evidence,
        },
    }
    _scan_model_graph(representative_provider_inputs)


@pytest.mark.parametrize(
    "unsafe",
    [
        {"storage_locator": "/private/input.h5ad"},
        {"nested": {"script_content": "print('secret')"}},
        {"summary": "PRIVATE_C12_RAW_SENTINEL"},
        {"socket": "/var/run/docker.sock"},
        {"internal": Path("/private/host")},
    ],
)
def test_recursive_scanner_detects_forbidden_surface(unsafe):
    with pytest.raises(AssertionError):
        _scan_model_graph(unsafe)


@pytest.mark.parametrize(
    "payload",
    [
        {"provider_response_body": "unsafe transport body"},
        {"error_message": "/private/host/execution.log"},
        {"nested": {"credentials": "unsafe secret"}},
    ],
)
def test_trace_rejects_transport_bodies_secrets_and_host_paths(payload):
    with pytest.raises(ValidationError, match="unsafe model content"):
        TraceEvent(
            run_id=uuid4(),
            sequence=0,
            event_type=TraceEventType.EXECUTION_FAILED,
            payload=payload,
        )
