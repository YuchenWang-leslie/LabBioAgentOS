"""A controlled rejection is not permission to read the rejected data view."""

from dataclasses import replace
import json
from uuid import UUID, uuid4

import pytest

from labbioagentos import (
    ArtifactExposureClass, ArtifactRepresentation, LabBioRuntimeToolSet, WorkflowStage,
)
from labbioagentos.governance import AuthorizationDenied
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401
from test_runtime_working_notes import note_tools


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", ["artifact_query", "artifact_aggregate"])
@pytest.mark.parametrize("next_stage", [False, True])
async def test_denied_raw_diagnostic_survives_note_history_and_handoff_without_read_credit(
    tmp_path, artifact_query_boundary, capability, next_stage,
):
    original, _ = note_tools(tmp_path, artifact_query_boundary)
    tools = LabBioRuntimeToolSet(replace(original.binding, capability_allowlist=(
        *original.binding.capability_allowlist, "artifact_aggregate", "evidence_list",
    )), original.services)
    raw = tools.services.artifact_store.register(
        artifact_type="private-log", exposure_class=ArtifactExposureClass.RAW,
        representation=ArtifactRepresentation(stored_content="PRIVATE_RAW_CANARY"),
        owner_user_id=tools.binding.principal.user_id,
        project_id=tools.binding.workspace.project_id, lab_id=tools.binding.workspace.lab_id,
        run_id=tools.binding.run_id,
    )
    fail = (lambda host: host.artifact_query(str(raw.artifact_id), "METADATA")) if capability == "artifact_query" else (
        lambda host: host.artifact_aggregate(str(raw.artifact_id), "SUM", "value"))
    failure = await fail(tools)
    assert failure["error"]["error_code"] == "ARTIFACT_EXPOSURE_DENIED"
    assert failure["data"] is None and failure.get("artifact_read_receipt") is None
    assert "PRIVATE_RAW_CANARY" not in json.dumps(failure)
    identity = failure["capability_invocation_id"]
    archived = tools.services.observation_store.root / f"{identity}.json"
    original_bytes = archived.read_bytes()
    # An undispatched or not-yet-consumed result remains ineligible for handoff.
    assert not (await tools.prepare_handoff([identity], "Rejected view."))["success"]
    tools.mark_feedback_delivered((failure,))
    note = await tools.working_note_write("View denied; no data read.", [identity])
    assert note["success"], note
    if next_stage:
        tools = LabBioRuntimeToolSet(replace(tools.binding,
            invocation_id=uuid4(), stage_id=WorkflowStage.INTERPRET,
            capability_allowlist=tuple(n for n in tools.binding.capability_allowlist if n != "report_check")),
            tools.services)
    history = await tools.evidence_list(capability)
    assert any(row["observation_id"] == identity for row in history["data"]["items"])
    page = await tools.evidence_read(identity)
    assert page["success"] and json.loads(page["data"]["text"]) == failure
    tools.mark_feedback_delivered((page,))
    read = await tools.working_note_read(note["capability_invocation_id"])
    assert read["success"] and read["information_authority"] == "MODEL_CONTEXT"
    tools.mark_feedback_delivered((read,))
    selected = await tools.prepare_handoff([identity, read["capability_invocation_id"]], "Still denied.")
    assert selected["success"] and selected["data"]["status"] == "READY"
    assert tools.prepared_handoff.observations[0].public_response == failure
    assert tools._delivered_observations[UUID(identity)].artifact_read_receipt is None
    checked = await tools.report_check(evidence_references=[{
        "artifact_id": str(raw.artifact_id), "observation_ids": [identity],
    }])
    if next_stage:
        assert not checked["success"]  # A new stage does not acquire report permission.
    else:
        assert not checked["data"]["ready"]
    assert not (await fail(tools))["success"]
    assert archived.read_bytes() == original_bytes


@pytest.mark.asyncio
@pytest.mark.parametrize("revocation", ["capability", "source", "run", "project", "user"])
async def test_diagnostic_reuse_still_checks_capability_source_and_scope(
    tmp_path, artifact_query_boundary, monkeypatch, revocation,
):
    tools, ref = note_tools(tmp_path, artifact_query_boundary)
    failure = await tools.artifact_query(str(ref.artifact_id), "TOP_N", offset=-1)
    tools.mark_feedback_delivered((failure,))
    note = await tools.working_note_write("Invalid query, not a scientific result.",
        [failure["capability_invocation_id"]])
    assert note["success"]
    changes = {"invocation_id": uuid4()}
    if revocation == "capability":
        changes["capability_allowlist"] = tuple(n for n in tools.binding.capability_allowlist if n != "artifact_query")
    elif revocation == "source":
        def revoked(_):
            raise AuthorizationDenied("Source authorization revoked")
        monkeypatch.setattr(tools, "_authorized_report_source", revoked)
    elif revocation == "run":
        changes["run_id"] = uuid4()
    elif revocation == "project":
        changes["workspace"] = tools.binding.workspace.model_copy(update={"project_id": "foreign"})
    else:
        changes["principal"] = tools.binding.principal.model_copy(update={"user_id": "foreign"})
    reader = tools if revocation == "source" else LabBioRuntimeToolSet(replace(tools.binding, **changes), tools.services)
    for identity in (failure["capability_invocation_id"], note["capability_invocation_id"]):
        response = await reader.evidence_read(identity)
        assert not response["success"] and response["data"] is None
    assert not (await reader.working_note_read(note["capability_invocation_id"]))["success"]


@pytest.mark.asyncio
async def test_failed_query_with_source_payload_is_not_a_value_free_diagnostic(tmp_path, artifact_query_boundary):
    tools, ref = note_tools(tmp_path, artifact_query_boundary)
    failed = await tools.artifact_query(str(ref.artifact_id), "SUMMARY", offset=-1)
    item = tools.evidence_items()[-1].model_copy(update={
        "capability_invocation_id": uuid4(), "safe_result": {"private": "PAYLOAD_CANARY"},
    })
    tools._record_observation(item, {**failed,
        "capability_invocation_id": str(item.capability_invocation_id), "data": item.safe_result})
    reader = LabBioRuntimeToolSet(replace(tools.binding, invocation_id=uuid4()), tools.services)
    response = await reader.evidence_read(str(item.capability_invocation_id))
    assert not response["success"] and "PAYLOAD_CANARY" not in json.dumps(response)
