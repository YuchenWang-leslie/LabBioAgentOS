"""Report citations identify actual observations, not an unread whole table."""

from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from labbioagentos import ArtifactRepresentation, LabBioRuntimeToolSet, WorkflowStage
from labbioagentos.governance import AccessService, InMemoryProjectStore, Project
from labbioagentos.runtime.reporting import ReportSubmissionService
from native_test_provider import LocalProvider
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401


def report_tools(boundary, *, real_service=False):
    _, binding, original_ref, original = boundary
    calls = []

    def submit(**kwargs):
        calls.append(kwargs)
        return {"status": "REGISTERED"}

    projects = InMemoryProjectStore()
    projects.register(Project(project_id=binding.workspace.project_id,
        lab_id=binding.workspace.lab_id, owner_user_id=binding.principal.user_id))
    service = (ReportSubmissionService(
        original.services.artifact_store,
        AccessService(projects),
    ) if real_service else SimpleNamespace(submit=submit))
    tools = LabBioRuntimeToolSet(
        replace(binding, stage_id=WorkflowStage.REPORT, invocation_id=uuid4(),
                capability_allowlist=("artifact_query", "artifact_aggregate",
                                      "report_check", "report_submit")),
        replace(original.services, report_submission=service),
    )
    ref = tools.services.artifact_store.register(
        artifact_type=original_ref.artifact_type,
        exposure_class=original_ref.exposure_class,
        release_basis=original_ref.release_basis,
        representation=ArtifactRepresentation(
            records=tuple({"row": n, "group": n % 2} for n in range(6401)),
            record_count=6401,
        ),
        owner_user_id=binding.principal.user_id,
        project_id=binding.workspace.project_id,
        lab_id=binding.workspace.lab_id,
        run_id=binding.run_id,
    )
    return tools, ref, calls


def reference(ref, *responses):
    return [{"artifact_id": str(ref.artifact_id),
             "observation_ids": [r["capability_invocation_id"] for r in responses]}]


@pytest.mark.asyncio
async def test_6401_records_can_be_cited_as_exact_page_not_whole_table(artifact_query_boundary):
    tools, ref, calls = report_tools(artifact_query_boundary)
    page = await tools.artifact_query(str(ref.artifact_id), "TOP_N", 10, offset=200)
    tools.mark_feedback_delivered((page,))
    evidence = reference(ref, page)
    checked = await tools.report_check(evidence_references=evidence)
    assert checked["success"] and checked["data"]["ready"]
    item = checked["data"]["items"][0]
    assert item["citation_mode"] == "OBSERVED_SCOPE"
    assert item["read_receipts"][0]["scope"] == {
        "kind": "TABLE_PAGE", "offset": 200, "end_offset": 210,
        "record_count": 6401,
    }
    assert checked["data"]["scientific_validation"] == "NOT_PERFORMED"
    response = await tools.report_submit("Synthetic", "These ten rows only.",
                                         evidence_references=evidence)
    assert response["success"] and len(calls) == 1
    assert len(calls[0]["evidence_read_receipts"]) == 1
    # The new scoped citation does not grant legacy whole-table read credit.
    legacy = await tools.report_check([str(ref.artifact_id)])
    assert legacy["data"]["items"][0]["status"] == "REPORT_EVIDENCE_INCOMPLETE"
    assert len(tools.evidence_items()) < 64


@pytest.mark.asyncio
async def test_aggregate_and_summary_are_explicit_scopes_and_persist(artifact_query_boundary):
    tools, ref, _ = report_tools(artifact_query_boundary, real_service=True)
    total = await tools.artifact_aggregate(str(ref.artifact_id), "COUNT",
                                          match_field="group", match_value=1)
    summary = await tools.artifact_query(str(ref.artifact_id), "SUMMARY")
    tools.mark_feedback_delivered((total, summary))
    response = await tools.report_submit("Synthetic", "A mechanical count.",
                                         evidence_references=reference(ref, total, summary))
    assert response["success"], response
    stored = tools.services.artifact_store.load_for_view(response["data"]["report_artifact_id"])
    citations = stored.representation.summary["evidence_read_receipts"]
    assert stored.representation.summary["evidence_contract"] == "OBSERVED_SCOPE_V1"
    assert [item["scope"]["kind"] for item in citations] == [
        "TABLE_AGGREGATE", "ARTIFACT_SUMMARY"]
    assert citations[0]["scope"]["query"]["match_value"] == 1
    assert citations[0]["scope"]["matched_count"] == 3200
    assert citations[0]["observation_sha256"] == total["artifact_read_receipt"]["observation_sha256"]
    assert citations[0]["source_sha256"] == summary["artifact_read_receipt"]["source_sha256"]


@pytest.mark.asyncio
async def test_report_rejects_unknown_wrong_source_and_duplicate_observations(artifact_query_boundary):
    tools, ref, calls = report_tools(artifact_query_boundary)
    page = await tools.artifact_query(str(ref.artifact_id), "TOP_N", 10)
    tools.mark_feedback_delivered((page,))
    for references, status in (
        ([{"artifact_id": str(ref.artifact_id), "observation_ids": [str(uuid4())]}],
         "REPORT_OBSERVATION_NOT_DELIVERED"),
        ([{"artifact_id": str(artifact_query_boundary[2].artifact_id),
           "observation_ids": [page["capability_invocation_id"]]}],
         "REPORT_OBSERVATION_SOURCE_MISMATCH"),
        (reference(ref, page, page), "DUPLICATE_REFERENCE"),
    ):
        result = await tools.report_submit("Synthetic", "Synthetic", evidence_references=references)
        assert result["error"]["error_code"] == status
    assert not calls


@pytest.mark.asyncio
async def test_scope_cannot_be_model_forged_and_formats_cannot_mix(artifact_query_boundary):
    tools, ref, calls = report_tools(artifact_query_boundary)
    page = await tools.artifact_query(str(ref.artifact_id), "TOP_N", 10)
    tools.mark_feedback_delivered((page,))
    references = reference(ref, page)
    result = await tools.report_submit("Synthetic", "Synthetic",
        evidence_artifact_ids=[], evidence_references=references)
    assert not result["success"] and not calls
    from pydantic import ValidationError
    provider = LocalProvider(tools)
    with pytest.raises(ValidationError):
        await provider.call_tool("report_submit", {
            "title": "Synthetic", "report_text": "Synthetic",
            "evidence_references": [{**references[0], "scope": "WHOLE_TABLE"}],
        })
    result = await provider.call_tool("report_submit", {
        "title": "Synthetic", "report_text": "Synthetic",
        "evidence_references": references,
    })
    assert result["success"] and len(calls) == 1


@pytest.mark.asyncio
async def test_prior_observation_requires_explicit_redelivery_and_current_access(artifact_query_boundary):
    tools, ref, calls = report_tools(artifact_query_boundary)
    page = await tools.artifact_query(str(ref.artifact_id), "TOP_N", 10)
    prior = tools.evidence_items()[-1]
    fresh = LabBioRuntimeToolSet(replace(tools.binding, invocation_id=uuid4()), tools.services)
    result = await fresh.report_check(evidence_references=reference(ref, page))
    assert result["data"]["items"][0]["status"] == "REPORT_OBSERVATION_NOT_DELIVERED"
    fresh.record_delivered_observation(prior)
    result = await fresh.report_submit("Synthetic", "Only the redelivered rows.",
                                       evidence_references=reference(ref, page))
    assert result["success"] and len(calls) == 1
    assert calls[0]["evidence_read_receipts"][0].delivered_invocation_id == fresh.binding.invocation_id
    foreign = LabBioRuntimeToolSet(replace(fresh.binding,
        workspace=fresh.binding.workspace.model_copy(update={"project_id": "foreign"})), fresh.services)
    from labbioagentos.governance import AuthorizationDenied
    with pytest.raises(AuthorizationDenied):
        foreign.record_delivered_observation(prior)


@pytest.mark.asyncio
async def test_failed_observation_and_report_registration_are_not_read_receipts(artifact_query_boundary):
    tools, ref, _ = report_tools(artifact_query_boundary)
    failed = await tools.artifact_query(str(ref.artifact_id), "SUMMARY", 2)
    assert not failed["success"]
    result = await tools.report_check(evidence_references=reference(ref, failed))
    assert result["data"]["items"][0]["status"] == "REPORT_OBSERVATION_NOT_DELIVERED"


@pytest.mark.asyncio
async def test_changed_source_and_tampered_receipt_fail_closed(artifact_query_boundary, monkeypatch):
    tools, ref, calls = report_tools(artifact_query_boundary)
    page = await tools.artifact_query(str(ref.artifact_id), "TOP_N", 10)
    tools.mark_feedback_delivered((page,))
    prior = tools.evidence_items()[-1]
    fresh = LabBioRuntimeToolSet(replace(tools.binding, invocation_id=uuid4()), tools.services)
    tampered = prior.model_copy(update={"artifact_read_receipt":
        prior.artifact_read_receipt.model_copy(update={"scope": {"kind": "WHOLE_TABLE"}})})
    with pytest.raises(ValueError, match="scope"):
        fresh.record_delivered_observation(tampered)
    assert not fresh._delivered_observations
    store = tools.services.artifact_store
    original_load = store.load_for_view

    def changed_load(artifact_id):
        stored = original_load(artifact_id)
        if str(artifact_id) == str(ref.artifact_id):
            return stored.model_copy(update={"representation": ArtifactRepresentation(
                records=({"row": "changed"},), record_count=6401)})
        return stored

    monkeypatch.setattr(store, "load_for_view", changed_load)
    result = await tools.report_submit("Synthetic", "Synthetic", evidence_references=reference(ref, page))
    assert result["error"]["error_code"] == "REPORT_OBSERVATION_SOURCE_CHANGED"
    assert not calls


@pytest.mark.asyncio
async def test_scoped_check_reveals_no_foreign_source_structure(artifact_query_boundary):
    tools, _, calls = report_tools(artifact_query_boundary)
    original = artifact_query_boundary[2]
    foreign = tools.services.artifact_store.register(
        artifact_type="foreign", exposure_class=original.exposure_class,
        release_basis=original.release_basis,
        representation=ArtifactRepresentation(records=({"private": "PRIVATE_VALUE"},), record_count=9401),
        owner_user_id="foreign-user", project_id="foreign-project", lab_id="lab-a",
    )
    result = await tools.report_check(evidence_references=[
        {"artifact_id": str(foreign.artifact_id), "observation_ids": [str(uuid4())]}])
    item = result["data"]["items"][0]
    assert item["status"] == "AUTHORIZATION_DENIED"
    assert not item["read_receipts"] and item["record_count"] is None
    assert "PRIVATE_VALUE" not in str(result) and "9401" not in str(result)
    assert not calls


@pytest.mark.asyncio
async def test_scoped_reference_limit_is_shared_by_host_and_native_schema(artifact_query_boundary):
    tools, ref, calls = report_tools(artifact_query_boundary)
    from pydantic import ValidationError
    references = [
        {"artifact_id": str(ref.artifact_id), "observation_ids": [str(uuid4())] * 33},
        {"artifact_id": str(ref.artifact_id), "observation_ids": [str(uuid4())] * 32},
    ]
    payload = {"title": "Synthetic", "report_text": "Synthetic", "evidence_references": references}
    with pytest.raises(ValidationError):
        await LocalProvider(tools).call_tool("report_submit", payload)
    direct = await tools.report_submit(**payload)
    assert not direct["success"] and not calls
