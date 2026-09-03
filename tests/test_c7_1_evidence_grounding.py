"""Deterministic C7.1 regressions for evidence authority and completeness."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from labbioagentos import (
    ArtifactConsumer,
    ArtifactExposureClass,
    ArtifactExposureService,
    ArtifactQuery,
    ArtifactRepresentation,
    ArtifactReleaseBasis,
    ArtifactViewType,
    ExposurePolicy,
    InformationAuthority,
    LocalArtifactStore,
    NextAction,
    NextActionProposal,
    RuntimeEvidenceReference,
    RuntimeEvidenceRole,
    RuntimeInputBody,
    RuntimePriorResultView,
    RuntimeReference,
    RuntimeReferenceKind,
    RuntimeStageInput,
    RuntimeStageResult,
    RuntimeWorkspaceIdentifiers,
    UnderstandStageBody,
    WorkflowStage,
)


def _prior_result(*, reference: RuntimeReference | None = None) -> RuntimeStageResult:
    references = (reference,) if reference is not None else ()
    return RuntimeStageResult(
        stage_id=WorkflowStage.UNDERSTAND,
        summary="A prior model claimed the measured value was 123.456.",
        body=UnderstandStageBody(
            requirements=("Continue from the prior proposal.",),
            assumptions=("The measured value was 123.456.",),
            evidence_references=references,
        ),
        references=references,
        next_action=NextActionProposal(
            action=NextAction.TRANSITION,
            target_stage=WorkflowStage.PLAN,
        ),
    )


def _stage_input(
    prior: RuntimePriorResultView,
    *,
    authoritative_reference: RuntimeEvidenceReference | None = None,
) -> RuntimeStageInput:
    return RuntimeStageInput(
        run_id=uuid4(),
        stage_id=WorkflowStage.PLAN,
        instruction="Assess the request within the current stage.",
        workspace=RuntimeWorkspaceIdentifiers(
            user_id="user-grounding",
            project_id="project-grounding",
            lab_id="lab-grounding",
        ),
        prior_results=(prior,),
        authoritative_evidence_references=(
            (authoritative_reference,) if authoritative_reference is not None else ()
        ),
        body=RuntimeInputBody(),
    )


def _derived_service(tmp_path, record_count: int):
    store = LocalArtifactStore(tmp_path / "artifacts")
    records = tuple(
        {"record_type": "measurement", "ordinal": index, "value": index / 10}
        for index in range(record_count)
    )
    ref = store.register(
        artifact_type="generic-measurements",
        exposure_class=ArtifactExposureClass.DERIVED,
        release_basis=ArtifactReleaseBasis.TRUSTED_EXECUTION_DECLASSIFICATION,
        representation=ArtifactRepresentation(
            records=records,
            record_count=len(records),
        ),
    )
    return ref, records, ArtifactExposureService(store, ExposurePolicy())


def test_g1_prior_model_claim_is_explicit_model_context_not_evidence():
    view = RuntimePriorResultView.from_result(_prior_result())

    assert view.authority is InformationAuthority.MODEL_CONTEXT
    assert "123.456" in view.model_summary
    assert "123.456" in json.dumps(view.model_body)
    assert "summary" not in RuntimePriorResultView.model_fields
    assert "structured_body" not in RuntimePriorResultView.model_fields
    stage_input = _stage_input(view)
    assert (
        stage_input.evidence_grounding.prior_results
        is InformationAuthority.MODEL_CONTEXT
    )


def test_g2_authoritative_artifact_reference_survives_independently_of_prose():
    artifact_reference = RuntimeEvidenceReference(
        reference_id=str(uuid4()),
        kind=RuntimeReferenceKind.ARTIFACT,
        label="DERIVED governed Artifact",
        evidence_role=RuntimeEvidenceRole.INPUT_EVIDENCE,
    )
    prior = RuntimePriorResultView.from_result(
        _prior_result(reference=artifact_reference)
    )
    stage_input = _stage_input(
        prior,
        authoritative_reference=artifact_reference,
    )

    assert prior.model_references == (artifact_reference,)
    assert stage_input.authoritative_evidence_references == (artifact_reference,)
    authoritative_json = json.dumps(
        [
            item.model_dump(mode="json")
            for item in stage_input.authoritative_evidence_references
        ]
    )
    assert "123.456" not in authoritative_json


def test_g3_default_top_n_explicitly_reports_partial_collection(tmp_path):
    ref, records, service = _derived_service(tmp_path, 14)

    view = service.artifact_query(
        ref.artifact_id,
        ArtifactQuery(view_type=ArtifactViewType.TOP_N),
        ArtifactConsumer.REMOTE_LLM,
    )

    assert view.authority is InformationAuthority.AUTHORITATIVE_EVIDENCE
    assert view.records == records[:10]
    assert view.returned_count == 10
    assert view.available_count == 14
    assert view.effective_limit == 10
    assert view.truncated is True


def test_g4_explicit_bounded_query_reports_complete_collection(tmp_path):
    ref, records, service = _derived_service(tmp_path, 14)

    view = service.artifact_query(
        ref.artifact_id,
        ArtifactQuery(view_type=ArtifactViewType.TOP_N, limit=14),
        ArtifactConsumer.REMOTE_LLM,
    )

    assert view.records == records
    assert view.returned_count == 14
    assert view.available_count == 14
    assert view.effective_limit == 14
    assert view.truncated is False


def test_g5_collection_larger_than_policy_maximum_remains_partial(tmp_path):
    ref, records, service = _derived_service(tmp_path, 150)

    view = service.artifact_query(
        ref.artifact_id,
        ArtifactQuery(view_type=ArtifactViewType.TOP_N, limit=1_000),
        ArtifactConsumer.REMOTE_LLM,
    )

    assert view.records == records[:100]
    assert view.returned_count == 100
    assert view.available_count == 150
    assert view.effective_limit == 100
    assert view.truncated is True


def test_g6_model_context_cannot_be_promoted_by_reprojection():
    view = RuntimePriorResultView.from_result(_prior_result())
    for _ in range(3):
        view = RuntimePriorResultView.model_validate(view.model_dump(mode="json"))
        assert view.authority is InformationAuthority.MODEL_CONTEXT

    forged = view.model_dump(mode="json")
    forged["authority"] = InformationAuthority.AUTHORITATIVE_EVIDENCE.value
    with pytest.raises(ValidationError):
        RuntimePriorResultView.model_validate(forged)


def test_g7_completeness_metadata_preserves_the_leak_boundary(tmp_path):
    ref, _, service = _derived_service(tmp_path, 14)
    view = service.artifact_query(
        ref.artifact_id,
        ArtifactQuery(view_type=ArtifactViewType.TOP_N),
        ArtifactConsumer.REMOTE_LLM,
    )
    dumped = view.model_dump(mode="json", by_alias=True)
    encoded = json.dumps(dumped).lower()

    assert set(dumped) == {
        "artifact_id",
        "artifact_type",
        "view_type",
        "exposure_class",
        "release_basis",
        "authority",
        "metadata",
        "schema",
        "columns",
        "summary",
        "records",
        "returned_count",
        "available_count",
        "effective_limit",
        "truncated",
        "provenance",
    }
    for forbidden in (
        "storage_locator",
        "stored_content",
        "provider_raw_body",
        "reasoning_content",
        "file_contents",
        "raw_matrix",
    ):
        assert forbidden not in encoded
