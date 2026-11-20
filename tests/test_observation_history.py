"""History discovery preserves exact identities, scope and independent read credit."""

from dataclasses import replace
import json
from uuid import UUID, uuid4

import pytest

from labbioagentos import LabBioRuntimeToolSet, WorkflowStage
from labbioagentos.runtime.native_tools import build_labbio_tool_registry
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401
from test_runtime_working_notes import note_tools
from test_aggregate_failure_audit import stored_tools
from test_report_failure_audit import mixed_references


def fresh(tools, **changes):
    binding = replace(tools.binding, invocation_id=uuid4(), **changes)
    return LabBioRuntimeToolSet(binding, tools.services)


def add_history(tools):
    return LabBioRuntimeToolSet(replace(tools.binding, capability_allowlist=(
        *tools.binding.capability_allowlist, "evidence_list",
    )), tools.services)


@pytest.mark.asyncio
@pytest.mark.parametrize("new_invocation", [False, True])
async def test_original_author_diagnostic_exact_replay(tmp_path, artifact_query_boundary, new_invocation):
    tools, _ = note_tools(tmp_path, artifact_query_boundary)
    note = await tools.working_note_write("Original note.")
    other = fresh(tools, actor_profile_key="other-profile")
    failure = await other.working_note_write("Rejected.", previous_note_id=note["capability_invocation_id"])
    assert failure["error"]["error_code"] == "WORKING_NOTE_AUTHOR_MISMATCH"
    reader = fresh(other) if new_invocation else other
    page = await reader.evidence_read(failure["capability_invocation_id"])
    assert page["success"], page
    assert json.loads(page["data"]["text"]) == failure


@pytest.mark.asyncio
async def test_author_failure_discovered_then_exactly_reread_in_next_stage(tmp_path, artifact_query_boundary):
    tools, _ = note_tools(tmp_path, artifact_query_boundary)
    tools = add_history(tools)
    note = await tools.working_note_write("PRIVATE_NOTE_TEXT")
    reviewer = fresh(tools, actor_profile_key="other-profile")
    failure = await reviewer.working_note_write("Not saved.", previous_note_id=note["capability_invocation_id"])
    assert failure["error"]["error_code"] == "WORKING_NOTE_AUTHOR_MISMATCH"
    report = fresh(reviewer, stage_id=WorkflowStage.REPORT)
    wrong = await report.evidence_read(str(uuid4()))
    assert wrong["error"]["error_code"] == "OBSERVATION_NOT_AVAILABLE"
    history = await report.evidence_list("working_note_write")
    assert history["success"], history
    items = {item["observation_id"]: item for item in history["data"]["items"]}
    identity = failure["capability_invocation_id"]
    assert items[identity] == {
        "observation_id": identity, "stage_id": "REPORT",
        "invocation_id": str(reviewer.binding.invocation_id),
        "actor_profile_key": "other-profile", "status": "FAILED",
        "information_authority": "CONTROL_STATE", "error_code": "WORKING_NOTE_AUTHOR_MISMATCH",
        "response_delivered_in_current_invocation": False,
    }
    assert note["capability_invocation_id"] in items
    assert "PRIVATE_NOTE_TEXT" not in json.dumps(history)
    assert history["data"]["source_read_credit"] is False
    assert UUID(identity) not in report._delivered_observations
    page = await report.evidence_read(identity)
    assert page["success"] and json.loads(page["data"]["text"]) == failure
    assert UUID(identity) not in report._delivered_observations
    report.mark_feedback_delivered((page,))
    assert UUID(identity) in report._delivered_observations
    assert report._delivered_observations[UUID(identity)].artifact_read_receipt is None


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["run", "user", "project", "capability"])
async def test_history_and_replay_do_not_widen_authority(tmp_path, artifact_query_boundary, scope):
    tools, _ = note_tools(tmp_path, artifact_query_boundary)
    tools = add_history(tools)
    saved = await tools.working_note_write("PRIVATE_SCOPE_CANARY")
    if scope == "run":
        other = fresh(tools, run_id=uuid4())
    elif scope == "user":
        other = fresh(tools, principal=tools.binding.principal.model_copy(update={"user_id": "other-user"}))
    elif scope == "project":
        other = fresh(tools, workspace=tools.binding.workspace.model_copy(update={"project_id": "other-project"}))
    else:
        other = fresh(tools, capability_allowlist=("evidence_list", "evidence_read"))
    listed = await other.evidence_list("working_note_write")
    assert not listed["success"] or listed["data"]["items"] == []
    replay = await other.evidence_read(saved["capability_invocation_id"])
    assert not replay["success"]
    assert "PRIVATE_SCOPE_CANARY" not in json.dumps((listed, replay))


@pytest.mark.asyncio
async def test_history_uses_keyset_pages_and_never_implicit_latest(tmp_path, artifact_query_boundary):
    tools, _ = note_tools(tmp_path, artifact_query_boundary)
    tools = add_history(tools)
    ids = [(await tools.working_note_write(f"Revision {index}."))["capability_invocation_id"] for index in range(5)]
    report = fresh(tools)
    collected, cursor = [], None
    while True:
        page = await report.evidence_list("working_note_write", after_id=cursor, limit=2)
        assert page["success"], page
        collected.extend(item["observation_id"] for item in page["data"]["items"])
        cursor = page["data"]["next_after_id"]
        if cursor is None:
            break
    assert collected == sorted(ids)
    assert not report._delivered_observations


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 17, True, "2"])
async def test_history_host_and_schema_reject_invalid_limits(tmp_path, artifact_query_boundary, limit):
    from labbioagentos.runtime.native_tools import EvidenceListArguments
    tools, _ = note_tools(tmp_path, artifact_query_boundary)
    tools = add_history(tools)
    assert not (await tools.evidence_list("working_note_write", limit=limit))["success"]
    with pytest.raises(ValueError):
        EvidenceListArguments.model_validate_json(json.dumps({"capability_name": "working_note_write", "limit": limit}))


@pytest.mark.asyncio
async def test_failed_aggregate_replay_and_linked_notes_recheck_source_access(tmp_path, artifact_query_boundary, monkeypatch):
    tools, identifier = stored_tools(tmp_path, artifact_query_boundary, [{"n": "PRIVATE_VALUE"}])
    tools = LabBioRuntimeToolSet(replace(tools.binding, capability_allowlist=(
        "artifact_aggregate", "evidence_read", "evidence_list", "working_note_write", "working_note_read",
    )), tools.services)
    failed = await tools.artifact_aggregate(identifier, "SUM", "n")
    assert failed["error"]["error_code"] == "AGGREGATE_NON_NUMERIC_VALUE"
    tools.mark_feedback_delivered((failed,))
    note = await tools.working_note_write("Linked failure.", [failed["capability_invocation_id"]])
    report = fresh(tools)
    original = await report.evidence_read(failed["capability_invocation_id"])
    assert original["success"] and json.loads(original["data"]["text"]) == failed
    # A fully delivered wrapper still has the original dependency, not a new grant.
    report.mark_feedback_delivered((original,))
    wrapped = await report.working_note_write("Wrapper.", [original["capability_invocation_id"]])
    decision = tools.services.artifact_exposure.policy.decide
    monkeypatch.setattr(type(tools.services.artifact_exposure.policy), "decide",
        lambda self, ref, query, consumer: decision(ref, query, consumer).model_copy(
            update={"allowed": False}) if query.view_type.value == "TOP_N"
        else decision(ref, query, consumer))
    for identity in (failed["capability_invocation_id"], note["capability_invocation_id"], wrapped["capability_invocation_id"]):
        replay = await report.evidence_read(identity)
        assert not replay["success"] and replay["data"] is None
    listed = await report.evidence_list("artifact_aggregate")
    assert listed["success"] and listed["data"]["items"] == []
    assert not (await report.working_note_read(note["capability_invocation_id"]))["success"]


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", ["artifact_query", "artifact_aggregate"])
async def test_original_view_denial_is_readable_as_metadata_without_authorizing_view(tmp_path, artifact_query_boundary, monkeypatch, capability):
    tools, ref = note_tools(tmp_path, artifact_query_boundary)
    tools = LabBioRuntimeToolSet(replace(tools.binding, capability_allowlist=(
        *tools.binding.capability_allowlist, "artifact_aggregate", "evidence_list",
    )), tools.services)
    decide = tools.services.artifact_exposure.policy.decide
    monkeypatch.setattr(type(tools.services.artifact_exposure.policy), "decide",
        lambda self, source, query, consumer: decide(source, query, consumer).model_copy(
            update={"allowed": False}) if query.view_type.value == "TOP_N"
        else decide(source, query, consumer))
    failed = (await tools.artifact_query(str(ref.artifact_id), "TOP_N", 1)
              if capability == "artifact_query" else
              await tools.artifact_aggregate(str(ref.artifact_id), "SUM", "row"))
    assert failed["error"]["error_code"] == "ARTIFACT_EXPOSURE_DENIED"
    tools.mark_feedback_delivered((failed,))
    note = await tools.working_note_write("The requested view was denied.", [failed["capability_invocation_id"]])
    assert note["success"], note
    for report in (tools, fresh(tools)):
        page = await report.evidence_read(failed["capability_invocation_id"])
        assert page["success"] and json.loads(page["data"]["text"]) == failed
        assert (await report.working_note_read(note["capability_invocation_id"]))["success"]
        history = await report.evidence_list(capability)
        assert history["success"] and len(history["data"]["items"]) == 1
    # Releasing the diagnostic did not grant the denied action.
    denied = await tools.artifact_query(str(ref.artifact_id), "TOP_N", 1)
    assert not denied["success"]


@pytest.mark.asyncio
@pytest.mark.parametrize("view", ["METADATA", "SCHEMA", "SUMMARY", "TOP_N"])
async def test_value_free_failure_is_control_state_not_a_metadata_view(tmp_path, artifact_query_boundary, monkeypatch, view):
    tools, ref = note_tools(tmp_path, artifact_query_boundary)
    tools = add_history(tools)
    # Invalid offset is an actual failure, with a valid original source and view.
    failed = await tools.artifact_query(str(ref.artifact_id), view, offset=-1)
    assert not failed["success"]
    report = fresh(tools)
    assert (await report.evidence_read(failed["capability_invocation_id"]))["success"]
    decide = tools.services.artifact_exposure.policy.decide
    seen = []
    def revoke(self, source, query, consumer):
        seen.append(query.view_type.value)
        result = decide(source, query, consumer)
        return result.model_copy(update={"allowed": False})
    monkeypatch.setattr(type(tools.services.artifact_exposure.policy), "decide", revoke)
    replay = await report.evidence_read(failed["capability_invocation_id"])
    assert replay["success"] and json.loads(replay["data"]["text"]) == failed
    assert seen == []  # No source values/names were released by this rejection.
    assert len((await report.evidence_list("artifact_query"))["data"]["items"]) == 1
    denied = await report.artifact_query(str(ref.artifact_id), view, limit=1 if view == "TOP_N" else None)
    assert not denied["success"] and view in seen


@pytest.mark.asyncio
async def test_unknown_source_audits_and_failure_payloads_are_not_cross_stage_replayed(tmp_path, artifact_query_boundary):
    tools, ref = note_tools(tmp_path, artifact_query_boundary)
    tools = add_history(tools)
    unknown_source = await tools.artifact_query(str(uuid4()), "SUMMARY")
    invalid_view = await tools.artifact_query(str(ref.artifact_id), "UNKNOWN_VIEW")
    original = await tools.working_note_write("Original.")
    other = fresh(tools, actor_profile_key="other-profile")
    failed = await other.working_note_write("Rejected.", previous_note_id=original["capability_invocation_id"])
    item = other.evidence_items()[-1].model_copy(update={
        "capability_invocation_id": uuid4(), "safe_result": {"unresolved_source": "CANARY"}})
    payload_failure = {**failed, "capability_invocation_id": str(item.capability_invocation_id),
                       "data": item.safe_result}
    other._record_observation(item, payload_failure)
    report = fresh(other)
    assert (await report.evidence_read(invalid_view["capability_invocation_id"]))["success"]
    for identity in (unknown_source["capability_invocation_id"], str(item.capability_invocation_id)):
        assert not (await report.evidence_read(identity))["success"]
    history = await report.evidence_list("working_note_write")
    assert str(item.capability_invocation_id) not in json.dumps(history)
    assert "CANARY" not in json.dumps(history)


@pytest.mark.asyncio
async def test_readonly_note_permission_allows_discovery_but_not_writing(tmp_path, artifact_query_boundary):
    tools, _ = note_tools(tmp_path, artifact_query_boundary)
    note = await tools.working_note_write("Prior note.")
    other = fresh(tools, actor_profile_key="other-profile")
    failed = await other.working_note_write("Rejected.", previous_note_id=note["capability_invocation_id"])
    report = fresh(tools, capability_allowlist=("evidence_read", "evidence_list", "working_note_read"))
    history = await report.evidence_list("working_note_write")
    assert history["success"] and len(history["data"]["items"]) == 1
    assert history["data"]["items"][0]["observation_id"] == note["capability_invocation_id"]
    assert (await report.evidence_read(note["capability_invocation_id"]))["success"]
    assert not (await report.evidence_read(failed["capability_invocation_id"]))["success"]
    assert not (await report.working_note_write("Not granted."))["success"]


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper", [False, True])
@pytest.mark.parametrize("release", ["note", "archive"])
async def test_revoked_origin_capability_cannot_be_laundered_through_notes(tmp_path, artifact_query_boundary, wrapper, release):
    tools, _ = note_tools(tmp_path, artifact_query_boundary)
    original = await tools.working_note_write("Original author.")
    other = fresh(tools, actor_profile_key="other-profile")
    failure = await other.working_note_write("Rejected.", previous_note_id=original["capability_invocation_id"])
    other.mark_feedback_delivered((failure,))
    source_id = failure["capability_invocation_id"]
    if wrapper:
        page = await other.evidence_read(source_id)
        other.mark_feedback_delivered((page,))
        source_id = page["capability_invocation_id"]
    note = await other.working_note_write("SOURCE_DEPENDENCY_CANARY", [source_id])
    assert note["success"], note
    # A current origin grant is sufficient even across a fresh invocation.
    allowed = fresh(other)
    assert (await allowed.working_note_read(note["capability_invocation_id"]))["success"]
    denied = fresh(other, capability_allowlist=("evidence_read", "working_note_read", "prepare_handoff"))
    assert not (await denied.evidence_read(failure["capability_invocation_id"]))["success"]
    result = (await denied.working_note_read(note["capability_invocation_id"]) if release == "note"
              else await denied.evidence_read(note["capability_invocation_id"]))
    assert not result["success"] and result["data"] is None
    assert "SOURCE_DEPENDENCY_CANARY" not in json.dumps(result)
    assert not (await denied.prepare_handoff([note["capability_invocation_id"]], "No unread content."))["success"]


@pytest.mark.asyncio
async def test_current_invocation_rich_report_failure_remains_readable_and_handoff_eligible(tmp_path, artifact_query_boundary):
    original, _registered, aggregate, valid, invalid = await mixed_references(tmp_path, artifact_query_boundary)
    tools = LabBioRuntimeToolSet(replace(original.binding, capability_allowlist=(
        *original.binding.capability_allowlist, "evidence_read", "prepare_handoff",
        "working_note_write", "working_note_read",
    )), original.services)
    page = await tools.evidence_read(aggregate["capability_invocation_id"])
    tools.mark_feedback_delivered((page,))
    failed = await tools.report_submit("Synthetic", "Synthetic.", evidence_references=[valid, invalid])
    assert not failed["success"] and failed["data"]["report_reference_selections"]
    assert failed["error"]["report_evidence"] is not None
    tools.mark_feedback_delivered((failed,))
    assert (await tools.evidence_read(failed["capability_invocation_id"]))["success"]
    note = await tools.working_note_write("Current diagnostic.", [failed["capability_invocation_id"]])
    assert note["success"]
    selected = await tools.prepare_handoff([failed["capability_invocation_id"]], "Original report rejection.")
    assert selected["success"] and selected["data"]["status"] == "READY"
    assert tools.prepared_handoff.observations[0].public_response == failed
    # This payload has no general cross-invocation source-reauthorization contract.
    later = fresh(tools)
    assert not (await later.evidence_read(failed["capability_invocation_id"]))["success"]
    assert not (await later.working_note_read(note["capability_invocation_id"]))["success"]


@pytest.mark.asyncio
async def test_partial_failure_pages_do_not_grant_original_delivery(tmp_path, artifact_query_boundary):
    tools, _ = note_tools(tmp_path, artifact_query_boundary)
    note = await tools.working_note_write("Original.")
    other = fresh(tools, actor_profile_key="other-profile")
    failure = await other.working_note_write("Rejected.", previous_note_id=note["capability_invocation_id"])
    report = fresh(other)
    identity = UUID(failure["capability_invocation_id"])
    first = await report.evidence_read(identity, limit=20)
    report.mark_feedback_delivered((first,))
    assert identity not in report._delivered_observations
    assert not first["data"]["complete_observation_delivered"]
    rest = await report.evidence_read(identity, offset=20)
    assert rest["data"]["complete_observation_delivered"]
    assert identity not in report._delivered_observations
    report.mark_feedback_delivered((rest,))
    assert identity in report._delivered_observations


@pytest.mark.asyncio
async def test_polling_delivered_observation_tail_does_not_resend_it(
    tmp_path, artifact_query_boundary,
):
    author, _ = note_tools(tmp_path, artifact_query_boundary)
    note = await author.working_note_write("界" * 7_950)
    identity = UUID(note["capability_invocation_id"])
    reader = fresh(author)
    first = await reader.evidence_read(identity)
    reader.mark_feedback_delivered((first,))
    offset = first["data"]["next_offset"]
    assert offset == 8_000
    tail = await reader.evidence_read(identity, offset)
    assert tail["data"]["complete_observation_delivered"]
    assert identity not in reader._delivered_observations

    # Before provider delivery the page is still needed, even for the same request.
    pending = await reader.evidence_read(identity, offset)
    assert pending["data"] == tail["data"]
    reader.mark_feedback_delivered((tail,))
    assert identity in reader._delivered_observations

    for _ in range(3):
        repeated = await reader.evidence_read(identity, offset)
        assert repeated["success"], repeated
        data = repeated["data"]
        assert data["format"] == "OBSERVATION_PAGE_ALREADY_DELIVERED"
        assert "text" not in data
        assert data["complete_observation_delivered"]
        assert data["next_offset"] is None
        assert repeated["retained_metadata"]["kind"] == "OBSERVATION_PAGE_ALREADY_DELIVERED"
        assert reader.services.observation_store.load(
            reader.binding, UUID(repeated["capability_invocation_id"])
        ).public_response == repeated
        reader.mark_feedback_delivered((repeated,))

    reread = await reader.evidence_read(identity, offset, reread=True)
    assert reread["data"] == tail["data"]
    assert json.loads(first["data"]["text"] + reread["data"]["text"]) == note
    next_reader = fresh(reader)
    assert (await next_reader.evidence_read(identity, offset))["data"]["text"] == tail["data"]["text"]


@pytest.mark.asyncio
async def test_successful_control_observations_do_not_gain_cross_stage_replay(tmp_path, artifact_query_boundary):
    tools, _ = note_tools(tmp_path, artifact_query_boundary)
    tools = add_history(tools)
    response = await tools.handoff_status()
    report = fresh(tools)
    assert not (await report.evidence_read(response["capability_invocation_id"]))["success"]


@pytest.mark.asyncio
async def test_corrupt_store_fails_closed_without_partial_history(tmp_path, artifact_query_boundary):
    tools, _ = note_tools(tmp_path, artifact_query_boundary)
    tools = add_history(tools)
    await tools.working_note_write("Invisible on corrupt store.")
    # Simulate corrupt persisted input, not an editable runtime artifact.
    path = tools.services.observation_store.root / f"{uuid4()}.json"
    path.touch(mode=0o600)
    result = await tools.evidence_list("working_note_write", limit=1)
    assert not result["success"] and result["data"] is None


def test_history_tool_has_strict_required_filter_and_no_scientific_credit():
    from labbioagentos.runtime.native_tools import EvidenceListArguments
    class Host:
        async def invoke(self, *args):
            raise AssertionError("No runtime call")
    definition = build_labbio_tool_registry(Host()).definition("evidence_list")
    assert "metadata" in definition.spec.description
    assert "read credit" in definition.spec.description
    with pytest.raises(ValueError):
        EvidenceListArguments.model_validate_json("{}")
