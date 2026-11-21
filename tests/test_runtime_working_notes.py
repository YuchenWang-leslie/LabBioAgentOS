"""Agent-authored working state never inherits the authority of its sources."""

from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from labbioagentos import LabBioRuntimeToolSet, WorkflowStage
from labbioagentos.runtime.native_tools import build_labbio_tool_registry
from labbioagentos.governance import AuthorizationDenied
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401
from test_runtime_evidence_handoff import tools_with_store


def note_tools(tmp_path, boundary):
    tools, ref = tools_with_store(tmp_path, boundary)
    return LabBioRuntimeToolSet(replace(tools.binding, capability_allowlist=(
        *tools.binding.capability_allowlist, "working_note_write", "working_note_read",
    )), tools.services), ref


@pytest.mark.asyncio
async def test_note_survives_fresh_host_without_granting_source_read_credit(
    tmp_path, artifact_query_boundary,
):
    tools, ref = note_tools(tmp_path, artifact_query_boundary)
    source = await tools.artifact_query(str(ref.artifact_id), "TOP_N", 2)
    source_id = source["capability_invocation_id"]
    denied = await tools.working_note_write("An unresolved Agent assessment.", [source_id])
    assert not denied["success"]
    tools.mark_feedback_delivered((source,))
    saved = await tools.working_note_write("An unresolved Agent assessment.", [source_id])
    assert saved["success"] and saved["data"]["revision"] == 1
    note_id = saved["capability_invocation_id"]
    fresh = LabBioRuntimeToolSet(replace(tools.binding, invocation_id=uuid4()), tools.services)
    read = await fresh.working_note_read(note_id)
    assert read["success"] and read["information_authority"] == "MODEL_CONTEXT"
    assert read["data"]["text"] == "An unresolved Agent assessment."
    assert read["data"]["source_observation_ids"] == [source_id]
    fresh.mark_feedback_delivered((read,))
    assert UUID(source_id) not in fresh._delivered_observations
    check = await fresh.report_check(evidence_references=[{
        "artifact_id": str(ref.artifact_id), "observation_ids": [source_id],
    }])
    assert not check["data"]["ready"]
    note_check = await fresh.report_check(evidence_references=[{
        "artifact_id": str(ref.artifact_id), "observation_ids": [read["capability_invocation_id"]],
    }])
    assert not note_check["data"]["ready"]


@pytest.mark.asyncio
async def test_agent_authors_immutable_revision_and_can_read_it_across_stages(
    tmp_path, artifact_query_boundary,
):
    tools, _ = note_tools(tmp_path, artifact_query_boundary)
    first = await tools.working_note_write("First investigation; not evidence.")
    first_id = first["capability_invocation_id"]
    next_stage = LabBioRuntimeToolSet(replace(
        tools.binding, stage_id=WorkflowStage.INTERPRET, invocation_id=uuid4(),
        capability_allowlist=tuple(name for name in tools.binding.capability_allowlist
                                   if name != "report_check"),
    ), tools.services)
    second = await next_stage.working_note_write("Agent revised its assessment.", previous_note_id=first_id)
    assert second["success"] and second["data"]["revision"] == 2
    assert second["data"]["previous_note_id"] == first_id
    original = await next_stage.working_note_read(first_id)
    assert original["data"]["text"] == "First investigation; not evidence."
    assert original["data"]["revision"] == 1
    other = LabBioRuntimeToolSet(replace(next_stage.binding, actor_profile_key="another-agent"), tools.services)
    assert (await other.working_note_read(first_id))["success"]
    assert not (await other.working_note_write("Cannot revise another Agent.", previous_note_id=first_id))["success"]


@pytest.mark.asyncio
async def test_note_read_checks_scope_and_kind_without_disclosing_content(
    tmp_path, artifact_query_boundary,
):
    tools, ref = note_tools(tmp_path, artifact_query_boundary)
    note = await tools.working_note_write("NOTE_CONTENT_CANARY")
    foreign = LabBioRuntimeToolSet(replace(tools.binding, run_id=uuid4()), tools.services)
    denied = await foreign.working_note_read(note["capability_invocation_id"])
    unknown = await foreign.working_note_read(str(uuid4()))
    assert denied["error"]["error_code"] == unknown["error"]["error_code"]
    assert denied["error"]["safe_message"] == unknown["error"]["safe_message"]
    assert "NOTE_CONTENT_CANARY" not in str(denied)
    source = await tools.artifact_query(str(ref.artifact_id), "SUMMARY")
    assert not (await tools.working_note_read(source["capability_invocation_id"]))["success"]


@pytest.mark.asyncio
async def test_notes_require_explicit_capability_and_a_durable_archive(
    tmp_path, artifact_query_boundary,
):
    tools, _ = tools_with_store(tmp_path, artifact_query_boundary)
    assert not (await tools.working_note_write("Not granted."))["success"]
    tools, _ = note_tools(tmp_path, artifact_query_boundary)
    transient = LabBioRuntimeToolSet(tools.binding, replace(tools.services, observation_store=None))
    assert not (await transient.working_note_write("Must not claim persistence."))["success"]


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "x" * 8001, True, 23])
async def test_host_and_wire_reject_invalid_note_text(tmp_path, artifact_query_boundary, text):
    tools, _ = note_tools(tmp_path, artifact_query_boundary)
    assert not (await tools.working_note_write(text))["success"]


def test_working_note_schema_describes_authority_and_revision():
    class Host:
        async def invoke(self, *args):
            raise AssertionError("Schema inspection must not call the host")

    registry = build_labbio_tool_registry(Host())
    assert "MODEL_CONTEXT" in registry.definition("working_note_write").spec.description
    assert "read credit" in registry.definition("working_note_read").spec.description


@pytest.mark.asyncio
@pytest.mark.parametrize("release", ["read", "archive", "handoff"])
@pytest.mark.parametrize("dependency", ["previous", "read_response"])
async def test_note_dependencies_are_reauthorized_at_every_release(
    tmp_path, artifact_query_boundary, monkeypatch, release, dependency,
):
    tools, ref = note_tools(tmp_path, artifact_query_boundary)
    source = await tools.artifact_query(str(ref.artifact_id), "SUMMARY")
    tools.mark_feedback_delivered((source,))
    first = await tools.working_note_write("Prior approved assessment.", [source["capability_invocation_id"]])
    first_id = first["capability_invocation_id"]
    if dependency == "previous":
        saved = await tools.working_note_write("Assessment carried forward.", previous_note_id=first_id)
    else:
        read = await tools.working_note_read(first_id)
        tools.mark_feedback_delivered((read,))
        saved = await tools.working_note_write("Assessment carried forward.", [read["capability_invocation_id"]])
    assert saved["success"]
    tools.mark_feedback_delivered((saved,))

    def revoked(_item):
        raise AuthorizationDenied("The synthetic source is no longer authorized.")

    monkeypatch.setattr(tools, "_validate_report_observation", revoked)
    note_id = saved["capability_invocation_id"]
    if release == "read":
        response = await tools.working_note_read(note_id)
    elif release == "archive":
        response = await tools.evidence_read(note_id)
    else:
        response = await tools.prepare_handoff([note_id], "Agent-selected handoff.")
    assert not response["success"], "A linked note bypassed current source authorization"
    assert "Assessment carried forward." not in str(response)
