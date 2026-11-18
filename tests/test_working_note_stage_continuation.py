"""Deterministic cross-stage continuity through the real coordinator projection."""

import json
from dataclasses import replace
from uuid import UUID

import pytest

from labbioagentos import (
    ArtifactExposureClass, ArtifactReleaseBasis, ArtifactRepresentation,
    RuntimeCoordinatorService, RuntimeEvidenceReference, RuntimeEvidenceRole,
    RuntimeReferenceKind, RuntimeStageResult, StageRuntimeRegistry,
    StageRuntimeSpec, WorkflowEngine, WorkflowStage,
)
from labbioagentos.contracts import WorkflowDefinition, WorkflowTransition
from labbioagentos.governance import AuthorizationDenied
from labbioagentos.kernel import FinishReason, ModelTurn, PublicMessage
from labbioagentos.runtime.observations import ObservationStore
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401
from test_runtime_milestone_a import trusted_boundary  # noqa: F401
from test_runtime_working_notes import note_tools
from test_working_note_continuation import (
    ContextCheckingProvider, NOTE_TEXT, call, latest_result, new_loop,
)


class NativeFixtureStage:
    """Run scripted model decisions, not a hand-authored next-stage input."""

    def __init__(self, tools, respond):
        self.tools = tools
        self.provider = ContextCheckingProvider(respond)

    async def invoke(self, stage_input):
        self.stage_input = stage_input
        self.binding = replace(
            self.tools.binding, run_id=stage_input.run_id,
            stage_id=stage_input.stage_id, invocation_id=stage_input.invocation_id,
            capability_allowlist=stage_input.allowed_capabilities,
        )
        services = replace(self.tools.services, observation_store=ObservationStore(
            self.tools.services.observation_store.root,
        ))
        self.loop, self.host = new_loop(self.binding, services, self.provider)
        self.outcome = await self.loop.run(
            messages=(PublicMessage(role="USER", content=stage_input.model_dump_json()),),
            deadline_monotonic=10_000_000_000.0,
        )
        assert self.outcome.termination == "MODEL_COMPLETED"
        return RuntimeStageResult.model_validate_json(self.outcome.output)


@pytest.mark.asyncio
@pytest.mark.parametrize("source_revoked", [False, True])
async def test_compacted_note_crosses_coordinator_stage_boundary_with_current_authority(
    tmp_path, artifact_query_boundary, trusted_boundary, monkeypatch, source_revoked,
):
    tools, _ = note_tools(tmp_path, artifact_query_boundary)
    seen = {}

    def author(request, turn):
        if turn == 1:
            context = json.loads(request.messages[0].content)
            source = context["authoritative_evidence_references"][0]
            seen["artifact_id"] = source["reference_id"]
            return call("artifact_query", {
                "artifact_id": source["reference_id"], "view_type": "TOP_N", "limit": 2,
            }, turn)
        result = latest_result(request)
        assert result["success"], result
        if turn == 2:
            seen["source_id"] = result["capability_invocation_id"]
            return call("working_note_write", {
                "text": NOTE_TEXT, "source_observation_ids": [seen["source_id"]],
            }, turn)
        if turn == 3:
            seen["note_id"] = result["capability_invocation_id"]
        if turn in (3, 4, 5):
            return call("working_note_write", {"text": f"Other working note {turn}. " * 280}, turn)
        return ModelTurn(content=json.dumps({
            "stage_id": "INTERPRET", "summary": "Continue the unresolved working assessment.",
            "body": {"kind": "INTERPRET", "findings": ["Assessment is not source evidence."]},
            "references": [
                {"reference_id": seen["note_id"], "kind": "OTHER",
                 "label": "Agent-selected working note"},
                {"reference_id": seen["artifact_id"], "kind": "ARTIFACT",
                 "label": "Source associated with this assessment"},
            ],
            "next_action": {"action": "transition", "target_stage": "REPORT"},
        }), finish_reason=FinishReason.STOP)

    def continue_from_prior_result(request, turn):
        if turn == 1:
            context = json.loads(request.messages[0].content)
            assert context["stage_id"] == "REPORT"
            prior = context["prior_results"]
            assert len(prior) == 1 and prior[0]["stage_id"] == "INTERPRET"
            assert prior[0]["authority"] == "MODEL_CONTEXT"
            selected = next(item for item in prior[0]["model_references"] if item["kind"] == "OTHER")
            assert selected["kind"] == "OTHER"
            seen["continued_artifact_id"] = next(
                item["reference_id"] for item in prior[0]["model_references"] if item["kind"] == "ARTIFACT"
            )
            assert context["authoritative_evidence_references"] == []
            assert "AGENT_CONTEXT_CANARY" not in request.messages[0].content
            seen["continued_note_id"] = selected["reference_id"]
            return call("working_note_read", {"note_id": selected["reference_id"]}, turn)
        result = latest_result(request)
        if source_revoked:
            assert not result["success"]
            assert "AGENT_CONTEXT_CANARY" not in json.dumps(result)
        elif turn == 2:
            assert result["success"] and result["information_authority"] == "MODEL_CONTEXT"
            assert result["data"]["text"] == NOTE_TEXT
            assert result["data"]["source_read_credit"] is False
            seen["reread_source_id"] = result["data"]["source_observation_ids"][0]
            return call("report_check", {"delivery_artifact_ids": [], "evidence_references": [{
                "artifact_id": seen["continued_artifact_id"],
                "observation_ids": [seen["reread_source_id"]],
            }]}, turn)
        elif turn == 3:
            assert not result["data"]["ready"], "A working note granted source-read credit"
            return call("evidence_read", {"observation_id": seen["reread_source_id"]}, turn)
        elif turn == 4:
            assert result["success"] and result["data"]["complete_observation_delivered"]
            assert json.loads(result["data"]["text"])["data"]["records"] == [{"row": 0}, {"row": 1}]
            return call("report_check", {"delivery_artifact_ids": [], "evidence_references": [{
                "artifact_id": seen["continued_artifact_id"],
                "observation_ids": [seen["reread_source_id"]],
            }]}, turn)
        else:
            assert result["data"]["ready"], "Actually reread source evidence was not credited"
        return ModelTurn(content=json.dumps({
            "stage_id": "REPORT", "summary": "Current-authority continuity check finished.",
            "body": {"kind": "REPORT", "report_summary": (
                "Source revoked; note content was not released." if source_revoked else
                "Exact working note continued; original evidence separately reread."
            )},
            "next_action": {"action": "finish"},
        }), finish_reason=FinishReason.STOP)

    first = NativeFixtureStage(tools, author)
    second = NativeFixtureStage(tools, continue_from_prior_result)
    registry = StageRuntimeRegistry((
        StageRuntimeSpec(
            stage_id=WorkflowStage.INTERPRET, profile_key="reviewer",
            prompt_template_key="note-author", capability_allowlist=(
                "artifact_query", "evidence_read", "working_note_write", "working_note_read",
            ), invoker=first,
        ),
        StageRuntimeSpec(
            stage_id=WorkflowStage.REPORT, profile_key="reviewer",
            prompt_template_key="note-continue", capability_allowlist=(
                "working_note_read", "evidence_read", "report_check",
            ), invoker=second,
        ),
    ))
    engine = WorkflowEngine(WorkflowDefinition(
        workflow_id="note-stage-continuity-fixture",
        nodes=frozenset({WorkflowStage.INTERPRET, WorkflowStage.REPORT}),
        initial_stage=WorkflowStage.INTERPRET,
        terminal_stages=frozenset({WorkflowStage.REPORT}),
        allowed_transitions=frozenset({WorkflowTransition(
            source=WorkflowStage.INTERPRET, target=WorkflowStage.REPORT,
        )}),
    ))
    coordinator = RuntimeCoordinatorService(engine, registry)
    access, principal, workspace = trusted_boundary
    run = coordinator.create_run(principal=principal, workspace=workspace, access_service=access)
    engine.start(run)
    artifact = tools.services.artifact_store.register(
        artifact_type="synthetic-derived-table", exposure_class=ArtifactExposureClass.DERIVED,
        release_basis=ArtifactReleaseBasis.TRUSTED_EXECUTION_DECLASSIFICATION,
        representation=ArtifactRepresentation(
            records=tuple({"row": index} for index in range(15)), record_count=15,
        ), owner_user_id=principal.user_id, project_id=workspace.project_id,
        lab_id=workspace.lab_id, run_id=run.run_id,
    )
    result = await coordinator.run_current_stage(
        run, instruction="Retain and continue the Agent's unresolved assessment.",
        artifact_references=(RuntimeEvidenceReference(
            reference_id=str(artifact.artifact_id), kind=RuntimeReferenceKind.ARTIFACT,
            evidence_role=RuntimeEvidenceRole.CURRENT_ATTEMPT_EVIDENCE,
        ),),
    )
    assert run.current_stage is WorkflowStage.REPORT
    assert result.references[0].reference_id == seen["note_id"]
    assert any(event.numeric_facts.get("compacted_batches", 0) > 0 for event in first.outcome.events)
    replacements = [json.loads(message.content) for message in first.provider.requests[-1].messages
                    if message.role == "USER" and "COMPLETED_TOOL_BATCH" in message.content]
    replaced_ids = {item["observation_id"] for marker in replacements for item in marker["observations"]}
    assert {seen["source_id"], seen["note_id"]}.issubset(replaced_ids)
    assert all(NOTE_TEXT not in (message.content or "") for message in first.provider.requests[-1].messages)
    archive = ObservationStore(tools.services.observation_store.root)
    archived_bytes = {path: path.read_bytes() for path in archive.root.glob("*.json")}
    originals = {identity: archive.load(first.binding, UUID(identity))
                 for identity in (seen["source_id"], seen["note_id"])}
    assert originals[seen["note_id"]].public_response["data"]["text"] == NOTE_TEXT

    if source_revoked:
        def deny_current_source(_host, _item):
            raise AuthorizationDenied("The synthetic source authority was revoked.")
        monkeypatch.setattr(type(first.host._host), "_validate_report_observation", deny_current_source)
    await coordinator.run_current_stage(
        run, instruction="Retain and continue the Agent's unresolved assessment.",
    )
    assert second.stage_input.prior_results[0].result_id == result.result_id
    assert second.stage_input.prior_results[0].model_references == result.references
    assert seen["continued_note_id"] == seen["note_id"]
    assert first.binding.invocation_id != second.binding.invocation_id
    assert first.host._host.services.observation_store is not second.host._host.services.observation_store
    assert len(second.provider.requests) == (2 if source_revoked else 5)
    if source_revoked:
        assert UUID(seen["source_id"]) not in second.host._host._delivered_observations
        assert all("AGENT_CONTEXT_CANARY" not in (message.content or "")
                   for request in second.provider.requests for message in request.messages)
    for identity, original in originals.items():
        assert archive.load(second.binding, UUID(identity)) == original
    assert all(path.read_bytes() == original for path, original in archived_bytes.items())
    assert all(len(request.model_dump_json().encode()) <= 28_000
               for stage in (first, second) for request in stage.provider.requests)
