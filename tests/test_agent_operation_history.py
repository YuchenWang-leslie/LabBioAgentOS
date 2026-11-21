"""A scripted Agent must consult original operations, not inherit its own prose."""

from dataclasses import replace
import json
from uuid import UUID, uuid4

import pytest

from labbioagentos import LabBioRuntimeToolSet, WorkflowStage
from labbioagentos.kernel import FinishReason, ModelTurn, PublicMessage
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401
from test_runtime_working_notes import note_tools
from test_working_note_continuation import (
    ContextCheckingProvider, call, latest_result, new_loop,
)


@pytest.mark.asyncio
async def test_agent_checks_original_operation_history_instead_of_lying_context(
    tmp_path, artifact_query_boundary,
):
    author, _ = note_tools(tmp_path, artifact_query_boundary)
    first = await author.working_note_write("First successful working note.")
    second = await author.working_note_write(
        "Second successful revision.", previous_note_id=first["capability_invocation_id"],
    )
    reviewer = LabBioRuntimeToolSet(replace(author.binding,
        stage_id=WorkflowStage.VALIDATE, invocation_id=uuid4(), actor_profile_key="bridge-reviewer",
        capability_allowlist=tuple(name for name in author.binding.capability_allowlist
                                   if name != "report_check")),
        author.services)
    failure = await reviewer.working_note_write(
        "Cannot revise a different author's chain.",
        previous_note_id=second["capability_invocation_id"],
    )
    assert failure["error"]["error_code"] == "WORKING_NOTE_AUTHOR_MISMATCH"
    wrong_id = str(uuid4())
    misleading = await reviewer.working_note_write(
        f"All previous writes failed; the diagnostic {wrong_id} cannot be reread.",
    )
    original = {path: path.read_bytes() for path in author.services.observation_store.root.glob('*.json')}
    seen = {}

    def investigate(request, turn):
        if turn == 1:
            # Initial context includes only the selected note, not repaired facts.
            assert failure["capability_invocation_id"] not in request.messages[0].content
            return call("working_note_read", {"note_id": misleading["capability_invocation_id"]}, turn)
        result = latest_result(request)
        if turn == 2:
            assert result["success"] and result["information_authority"] == "MODEL_CONTEXT"
            assert "All previous writes failed" in result["data"]["text"]
            assert result["data"]["source_read_credit"] is False
            return call("evidence_read", {"observation_id": wrong_id}, turn)
        if turn == 3:
            assert not result["success"]
            assert result["error"]["error_code"] == "OBSERVATION_NOT_AVAILABLE"
            assert failure["capability_invocation_id"] not in json.dumps(result)
            return call("evidence_list", {"capability_name": "working_note_write"}, turn)
        if turn == 4:
            assert result["success"], result
            assert result["information_authority"] == "CONTROL_STATE"
            assert result["data"]["source_read_credit"] is False
            rows = result["data"]["items"]
            assert {first["capability_invocation_id"], second["capability_invocation_id"]}.issubset(
                row["observation_id"] for row in rows if row["status"] == "COMPLETED")
            failed = [row for row in rows if row["status"] == "FAILED"]
            assert len(failed) == 1
            seen["original_id"] = failed[0]["observation_id"]
            assert seen["original_id"] == failure["capability_invocation_id"]
            assert UUID(seen["original_id"]) not in host._host._delivered_observations
            return call("evidence_read", {"observation_id": seen["original_id"], "limit": 32}, turn)
        if turn == 5:
            assert result["success"] and not result["data"]["complete_observation_delivered"]
            seen["first_page"] = result["data"]["text"]
            return call("prepare_handoff", {
                "observation_ids": [seen["original_id"]], "summary": "Premature selection.",
            }, turn)
        if turn == 6:
            assert not result["success"]
            assert result["error"]["error_code"] == "HANDOFF_OBSERVATION_NOT_DELIVERED"
            return call("evidence_read", {
                "observation_id": seen["original_id"], "offset": 32, "limit": 8000,
            }, turn)
        if turn == 7:
            assert result["success"] and result["data"]["complete_observation_delivered"]
            assert json.loads(seen["first_page"] + result["data"]["text"]) == failure
            return call("prepare_handoff", {
                "observation_ids": [seen["original_id"]],
                "summary": "Original host rejection checked; an unavailable ID is not a global capability failure.",
                "limitations": ["This diagnostic is not scientific validation."],
            }, turn)
        if turn == 8:
            assert result["success"] and result["data"]["status"] == "READY"
            assert host._host.prepared_handoff.observations[0].public_response == failure
            assert host._host._delivered_observations[UUID(seen["original_id"])].artifact_read_receipt is None
            return call("working_note_write", {
                "text": "Two original writes succeeded; the distinct author-mismatch attempt failed."
                " The earlier lookup used a different identity; general rereadability was not disproven.",
                "source_observation_ids": [seen["original_id"]],
            }, turn)
        assert result["success"] and result["data"]["revision"] == 1
        assert result["data"]["source_observation_ids"] == [failure["capability_invocation_id"]]
        assert result["information_authority"] == "MODEL_CONTEXT"
        return ModelTurn(content="Original operation checked; prior prose was not evidence.",
                         finish_reason=FinishReason.STOP)

    binding = replace(reviewer.binding, stage_id=WorkflowStage.REPORT,
        invocation_id=uuid4(), actor_profile_key="coordinator",
        capability_allowlist=(*reviewer.binding.capability_allowlist, "evidence_list"))
    provider = ContextCheckingProvider(investigate)
    loop, host = new_loop(binding, reviewer.services, provider, max_request_bytes=80_000)
    completed = await loop.run(messages=(PublicMessage(role="USER", content=(
        "Review this unverified prior working state: " + misleading["capability_invocation_id"])),),
        deadline_monotonic=10_000_000_000.0)
    assert completed.termination == "MODEL_COMPLETED"
    assert len(provider.requests) == 9
    assert all(path.read_bytes() == content for path, content in original.items())
