"""Complete evidence is reachable without unbounded model responses."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from labbioagentos import ArtifactRepresentation, LabBioRuntimeToolSet, WorkflowStage
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401


@pytest.mark.asyncio
async def test_authored_claim_is_a_stored_record_not_cross_file_verification(artifact_query_boundary):
    _, binding, original, tools = artifact_query_boundary
    record = {"statement": "All labels in a different file were corrected", "verified": True}
    ref = tools.services.artifact_store.register(
        artifact_type="generic-table", exposure_class=original.exposure_class,
        release_basis=original.release_basis,
        representation=ArtifactRepresentation(records=(record,), record_count=1),
        owner_user_id=binding.principal.user_id, project_id=binding.workspace.project_id,
        lab_id=binding.workspace.lab_id, run_id=binding.run_id)
    response = await tools.artifact_query(str(ref.artifact_id), "TOP_N", 1)
    assert response["success"]
    assert response["data"]["evidence_scope"] == "REGISTERED_ARTIFACT_CONTENT"
    assert response["data"]["records"] == [record]
    evidence = tools.evidence_items()[-1]
    assert evidence.safe_result["evidence_scope"] == "REGISTERED_ARTIFACT_CONTENT"
    assert set(evidence.reference_ids) == {str(ref.artifact_id), str(binding.run_id)}
    assert "verified" not in response["data"]


@pytest.mark.asyncio
async def test_all_170_records_are_reachable_and_citation_coverage_is_exact(artifact_query_boundary):
    _, _, original, tools = artifact_query_boundary
    ref = tools.services.artifact_store.register(
        artifact_type=original.artifact_type, exposure_class=original.exposure_class,
        release_basis=original.release_basis,
        representation=ArtifactRepresentation(records=tuple({"row": n} for n in range(170)), record_count=170),
        owner_user_id=original.owner_user_id, project_id=original.project_id,
        lab_id=original.lab_id, run_id=original.run_id,
    )
    first = await tools.artifact_query(str(ref.artifact_id), "TOP_N", 170)
    assert first["success"]
    assert first["data"]["returned_count"] == 100
    assert first["data"]["next_offset"] == 100
    with pytest.raises(ValueError, match="incomplete"):
        tools._require_report_evidence_coverage((ref.artifact_id,))
    # A last page is not sufficient when a middle page was skipped.
    last = await tools.artifact_query(str(ref.artifact_id), "TOP_N", 20, offset=150)
    assert last["success"]
    with pytest.raises(ValueError, match="incomplete"):
        tools._require_report_evidence_coverage((ref.artifact_id,))
    rest = await tools.artifact_query(str(ref.artifact_id), "TOP_N", 100, offset=100)
    assert rest["success"] and rest["data"]["next_offset"] is None
    assert [x["row"] for x in first["data"]["records"] + rest["data"]["records"]] == list(range(170))
    tools._require_report_evidence_coverage((ref.artifact_id,))
    assert tools.evidence_items()[-1].artifact_query_request.offset == 100
    restarted = LabBioRuntimeToolSet(replace(tools.binding), tools.services)
    with pytest.raises(ValueError, match="incomplete"):
        restarted._require_report_evidence_coverage((ref.artifact_id,))


@pytest.mark.asyncio
async def test_offset_cannot_page_raw_or_nonrecord_views(artifact_query_boundary):
    _, _, ref, tools = artifact_query_boundary
    for view in ("METADATA", "SCHEMA", "SUMMARY"):
        response = await tools.artifact_query(str(ref.artifact_id), view, offset=1)
        assert not response["success"]
    response = await tools.artifact_query(str(ref.artifact_id), "TOP_N", offset=-1)
    assert not response["success"]


@pytest.mark.asyncio
async def test_report_submit_rejects_unread_table_without_registering_then_accepts_pages(artifact_query_boundary):
    _, binding, ref, original = artifact_query_boundary
    calls = []
    def submit(**kwargs):
        calls.append(kwargs)
        return {"status": "REGISTERED"}
    tools = LabBioRuntimeToolSet(replace(binding, stage_id=WorkflowStage.REPORT,
        capability_allowlist=("artifact_query", "report_submit")),
        replace(original.services, report_submission=SimpleNamespace(submit=submit)))
    response = await tools.report_submit("Synthetic", "Synthetic report", [str(ref.artifact_id)])
    assert response["error"]["error_code"] == "REPORT_EVIDENCE_INCOMPLETE"
    assert not calls
    await tools.artifact_query(str(ref.artifact_id), "TOP_N", 10)
    await tools.artifact_query(str(ref.artifact_id), "TOP_N", 10, offset=10)
    response = await tools.report_submit("Synthetic", "Synthetic report", [str(ref.artifact_id)])
    assert response["success"] and len(calls) == 1


@pytest.mark.asyncio
async def test_raw_records_remain_denied_at_nonzero_offset(artifact_query_boundary):
    from labbioagentos import ArtifactExposureClass
    _, binding, _, tools = artifact_query_boundary
    ref = tools.services.artifact_store.register(
        artifact_type="private", exposure_class=ArtifactExposureClass.RAW,
        representation=ArtifactRepresentation(records=({"secret": "PRIVATE_SENTINEL"},), record_count=1),
        owner_user_id=binding.principal.user_id, project_id=binding.workspace.project_id,
        lab_id=binding.workspace.lab_id)
    response = await tools.artifact_query(str(ref.artifact_id), "TOP_N", 1, offset=1)
    assert response["error"]["error_code"] == "ARTIFACT_EXPOSURE_DENIED"
    assert "PRIVATE_SENTINEL" not in str(response)
