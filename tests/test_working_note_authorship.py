"""Readable cross-profile notes do not become editable revision chains."""

from dataclasses import replace
import json
from time import monotonic
from uuid import UUID, uuid4

import pytest

from labbioagentos import LabBioRuntimeToolSet, TraceEventType
from labbioagentos.governance import AuthorizationDenied
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401
from test_runtime_working_notes import note_tools


async def author_and_reader(tmp_path, boundary):
    author, ref = note_tools(tmp_path, boundary)
    saved = await author.working_note_write("AUTHOR_CONTEXT_CANARY")
    reader = LabBioRuntimeToolSet(replace(author.binding,
        actor_profile_key="different-profile", invocation_id=uuid4()), author.services)
    return author, reader, ref, saved["capability_invocation_id"]


@pytest.mark.asyncio
async def test_other_profile_can_read_author_identity_but_cannot_revise_its_chain(
    tmp_path, artifact_query_boundary,
):
    author, reader, _ref, note_id = await author_and_reader(tmp_path, artifact_query_boundary)
    original = author.services.observation_store.load(author.binding, UUID(note_id))
    read = await reader.working_note_read(note_id)
    assert read["success"] and read["data"]["text"] == "AUTHOR_CONTEXT_CANARY"
    rejected = await reader.working_note_write("REJECTED_NOTE_CANARY", previous_note_id=note_id)
    assert rejected["error"]["error_code"] == "WORKING_NOTE_AUTHOR_MISMATCH"
    assert read["data"]["author_profile_key"] == author.binding.actor_profile_key
    assert read["data"]["same_author_profile"] is False
    assert read["data"]["source_read_credit"] is False
    assert "previous_note_id" in rejected["error"]["safe_message"]
    assert "No note was saved" in rejected["error"]["safe_message"]
    failed = reader.evidence_items()[-1]
    archived = reader.services.observation_store.load(reader.binding, failed.capability_invocation_id)
    assert archived.item == failed and archived.public_response == rejected
    assert "REJECTED_NOTE_CANARY" not in archived.model_dump_json()
    assert author.services.observation_store.load(author.binding, UUID(note_id)) == original
    terminal = artifact_query_boundary[0].read(reader.binding.run_id)[-1]
    assert terminal.event_type is TraceEventType.CAPABILITY_FAILED
    assert terminal.payload["error_code"] == "WORKING_NOTE_AUTHOR_MISMATCH"
    assert terminal.payload["previous_note_id"] == note_id


@pytest.mark.asyncio
async def test_same_author_profile_in_fresh_invocation_can_append_immutable_revision(
    tmp_path, artifact_query_boundary,
):
    author, _reader, _ref, note_id = await author_and_reader(tmp_path, artifact_query_boundary)
    fresh = LabBioRuntimeToolSet(replace(author.binding, invocation_id=uuid4()), author.services)
    read = await fresh.working_note_read(note_id)
    assert read["data"]["author_profile_key"] == author.binding.actor_profile_key
    assert read["data"]["same_author_profile"] is True
    revised = await fresh.working_note_write("Current author's new assessment.", previous_note_id=note_id)
    assert revised["success"] and revised["data"]["revision"] == 2
    assert revised["data"]["previous_note_id"] == note_id
    assert (await author.working_note_read(note_id))["data"]["revision"] == 1


@pytest.mark.asyncio
async def test_same_author_fact_does_not_grant_missing_write_capability(
    tmp_path, artifact_query_boundary,
):
    author, _reader, _ref, note_id = await author_and_reader(tmp_path, artifact_query_boundary)
    readonly = LabBioRuntimeToolSet(replace(author.binding,
        capability_allowlist=("working_note_read",), invocation_id=uuid4()), author.services)
    read = await readonly.working_note_read(note_id)
    assert read["success"] and read["data"]["same_author_profile"] is True
    denied = await readonly.working_note_write("Not granted.", previous_note_id=note_id)
    assert not denied["success"] and denied["error"]["error_code"] != "WORKING_NOTE_AUTHOR_MISMATCH"


@pytest.mark.asyncio
async def test_unknown_and_foreign_scope_notes_do_not_disclose_author(
    tmp_path, artifact_query_boundary,
):
    _author, reader, _ref, note_id = await author_and_reader(tmp_path, artifact_query_boundary)
    foreign = LabBioRuntimeToolSet(replace(reader.binding, run_id=uuid4()), reader.services)
    for operation in ("read", "write"):
        async def invoke(identity):
            return (await foreign.working_note_read(identity) if operation == "read" else
                    await foreign.working_note_write("Rejected.", previous_note_id=identity))
        known = await invoke(note_id)
        unknown = await invoke(str(uuid4()))
        assert known["error"]["error_code"] == unknown["error"]["error_code"] == "OBSERVATION_NOT_AVAILABLE"
        assert known["error"]["safe_message"] == unknown["error"]["safe_message"]
        assert known["data"] is None and unknown["data"] is None
        assert "author_profile_key" not in known["error"]["safe_message"]
        assert "AUTHOR_CONTEXT_CANARY" not in json.dumps(known)


@pytest.mark.asyncio
async def test_revoked_note_dependency_precedes_author_mismatch_and_read_metadata(
    tmp_path, artifact_query_boundary, monkeypatch,
):
    author, reader, ref, _note_id = await author_and_reader(tmp_path, artifact_query_boundary)
    source = await author.artifact_query(str(ref.artifact_id), "SUMMARY")
    author.mark_feedback_delivered((source,))
    saved = await author.working_note_write("REVOKED_NOTE_CANARY", [source["capability_invocation_id"]])

    def revoked(_item):
        raise AuthorizationDenied("PRIVATE_AUTHORITY_CANARY")

    monkeypatch.setattr(reader, "_validate_report_observation", revoked)
    read = await reader.working_note_read(saved["capability_invocation_id"])
    write = await reader.working_note_write("Rejected.", previous_note_id=saved["capability_invocation_id"])
    for response in (read, write):
        assert not response["success"] and response["data"] is None
        assert response["error"]["error_code"] == "AUTHORIZATION_DENIED"
        assert "AUTHOR_MISMATCH" not in json.dumps(response)
        assert "REVOKED_NOTE_CANARY" not in json.dumps(response)
        assert "PRIVATE_AUTHORITY_CANARY" not in json.dumps(response)


@pytest.mark.asyncio
async def test_undelivered_sources_are_rejected_before_previous_note_is_loaded(
    tmp_path, artifact_query_boundary, monkeypatch,
):
    _author, reader, _ref, note_id = await author_and_reader(tmp_path, artifact_query_boundary)

    def should_not_load(_identity):
        raise AssertionError("Previous-note existence must not be probed before source validation")

    monkeypatch.setattr(reader, "_working_note", should_not_load)
    for previous_id in (note_id, str(uuid4())):
        rejected = await reader.working_note_write("Rejected.", [str(uuid4())], previous_id)
        assert rejected["error"]["error_code"] == "WORKING_NOTE_SOURCE_NOT_DELIVERED"
        assert rejected["data"] is None


def test_note_tool_schema_explains_same_profile_limit_without_granting_write_authority():
    from labbioagentos.runtime.native_tools import WorkingNoteWriteArguments, build_labbio_tool_registry
    description = WorkingNoteWriteArguments.model_json_schema()["properties"]["previous_note_id"]["description"]
    assert "author_profile_key" in description
    class Host:
        async def invoke(self, *_args):
            raise AssertionError("Schema inspection must not invoke a capability")
    registry = build_labbio_tool_registry(Host())
    assert "same_author_profile" in registry.definition("working_note_read").spec.description


@pytest.mark.asyncio
async def test_native_next_turn_gets_author_facts_and_mismatch_then_agent_selects_new_chain(
    tmp_path, artifact_query_boundary,
):
    """Scripted Native transport, not a live-model behavior claim."""
    from labbioagentos.kernel import (
        AgentInvocation, AgentLoop, AgentMode, BudgetLedger, FakeProvider, FinishReason,
        ModelResultProjector, ModelTurn, PublicMessage, ToolCall, ToolDispatcher,
    )
    from labbioagentos.runtime.native_tools import NativeRuntimeCapabilityHost, build_labbio_tool_registry
    from native_kernel.test_agent_loop import _binding, _identity, _limits

    _author, reader, _ref, note_id = await author_and_reader(tmp_path, artifact_query_boundary)
    host = NativeRuntimeCapabilityHost(reader.binding, reader.services)
    identity = _identity()
    host.begin_model_invocation(identity)
    calls = (("working_note_read", {"note_id": note_id}),
             ("working_note_write", {"text": "Not this author's revision.", "previous_note_id": note_id}),
             ("working_note_write", {"text": "The current Agent's independent assessment."}))
    turns = tuple(ModelTurn(tool_calls=(ToolCall(call_id=f"note-{index}", name=name,
        arguments_json=json.dumps(arguments)),), finish_reason=FinishReason.TOOL_CALLS)
        for index, (name, arguments) in enumerate(calls))
    provider = FakeProvider(binding=_binding(), turns=(*turns,
        ModelTurn(content="Fixture completed.", finish_reason=FinishReason.STOP)))
    budget = BudgetLedger(_limits())
    loop = AgentLoop(provider=provider, invocation=AgentInvocation(identity=identity,
        agent_id="leader", mode=AgentMode.CAPABILITY, tool_names=("working_note_read", "working_note_write")),
        budget_ledger=budget, dispatcher=ToolDispatcher(build_labbio_tool_registry(host),
            ModelResultProjector(max_bytes=16_000), budget_ledger=budget,
            batch_observer=host.mark_feedback_delivered))
    await loop.run(messages=(PublicMessage(role="USER", content="Synthetic author-boundary test."),),
                   deadline_monotonic=monotonic() + 60)
    read = json.loads(provider.requests[1].messages[-1].content)["content"]
    assert read["data"]["same_author_profile"] is False
    rejected = json.loads(provider.requests[2].messages[-1].content)["content"]
    assert rejected["error"]["error_code"] == "WORKING_NOTE_AUTHOR_MISMATCH"
    accepted = json.loads(provider.requests[3].messages[-1].content)["content"]
    assert accepted["success"] and accepted["data"]["revision"] == 1
    assert accepted["data"]["previous_note_id"] is None
    archived = reader.services.observation_store.load(reader.binding, UUID(rejected["capability_invocation_id"]))
    assert archived.public_response == rejected
