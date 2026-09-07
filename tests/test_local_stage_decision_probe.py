"""Explicit decision-only probes; never recover/apply an old run or execute tools.

Opt in only for an individually authorized probe with LABBIO_LIVE_STAGE_DECISION_PROBE=1,
LABBIO_PROBE_CONFIG, LABBIO_PROBE_CAPTURE, LABBIO_PROBE_INVOCATION and a fresh
LABBIO_PROBE_OUTPUT directory. A result is a diagnostic proposal, not acceptance.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from uuid import UUID, uuid4

import pytest

from labbioagentos import (
    ArtifactRef, CapabilityEvidenceBundle, PantheonTypedStageInvoker,
    RuntimeInvocationMode, RuntimeStageInput, RuntimeWorkspaceIdentifiers,
    WorkflowStage,
)
from labbioagentos.artifacts.exposure import ArtifactExposureDenied
from labbioagentos.cli import _run_directory, _write_json
from labbioagentos.governance import AccessAction
from labbioagentos.local_config import build_application, load_settings, runtime_manifest
from labbioagentos.model_safety import validate_model_visible_json
from labbioagentos.runtime.contracts import (
    CapabilityErrorDetails, RuntimeInputArtifactUsage, RuntimeWorkflowControlView,
)
from labbioagentos.runtime.assembly import PerInvocationPantheonStageInvoker
from labbioagentos.runtime.tooling import LabBioRuntimeToolSet
from labbioagentos.run_state import ApplicationRunRecord, RunRecoveryState


def _capture(path: Path, invocation_id: UUID):
    """Select an exact recorded pair, never a stage_result or guessed latest turn."""
    if path.stat().st_size > 16_777_216:
        raise ValueError("Probe capture exceeds its bound")
    found = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        record = json.loads(line)
        kind, payload = record.get("kind"), record.get("payload", {})
        if kind not in {"stage_input", "capability_evidence"}:
            continue
        if payload.get("invocation_id") != str(invocation_id):
            continue
        if kind in found:
            raise ValueError("Probe capture is ambiguous")
        found[kind] = (line_number, payload)
    if set(found) != {"stage_input", "capability_evidence"}:
        raise ValueError("Probe requires one exact input/evidence pair")
    stage_input = RuntimeStageInput.model_validate_json(json.dumps(found["stage_input"][1]))
    evidence = CapabilityEvidenceBundle.model_validate_json(json.dumps(found["capability_evidence"][1]))
    if (stage_input.run_id, stage_input.stage_id, stage_input.invocation_id) != (
        evidence.run_id, evidence.stage_id, evidence.invocation_id
    ):
        raise ValueError("Probe evidence identity does not match")
    if stage_input.stage_id not in {WorkflowStage.UNDERSTAND, WorkflowStage.VALIDATE}:
        raise ValueError("This diagnostic supports UNDERSTAND and VALIDATE only")
    # Explicit unsafe fields are rejected, never stripped to make a capture usable.
    for value in (stage_input, evidence):
        validate_model_visible_json(
            value.model_dump(mode="json"), max_depth=16, max_nodes=65_536,
            max_serialized_bytes=1_048_576, reject_absolute_paths=True,
        )
    return stage_input, evidence, {kind: item[0] for kind, item in found.items()}


def _current_error_details(evidence):
    """Explicit code-catalog migration; do not infer arbitrary historical errors."""
    items, migrated = [], []
    for item in evidence.items:
        if item.error_code == "ARTIFACT_EXPOSURE_DENIED" and item.error_details is None:
            error = LabBioRuntimeToolSet._safe_error(ArtifactExposureDenied())
            item = item.model_copy(update={"error_details": CapabilityErrorDetails(
                safe_message=error.safe_message, retryable=error.retryable,
                denied_operation=error.denied_operation,
            )})
            migrated.append(str(item.capability_invocation_id))
        items.append(item)
    return evidence.model_copy(update={"items": tuple(items)}), migrated


def _current_input_usage(application, settings, source_root, stage_input):
    """Read canonical local metadata, then use the current production projection."""
    database = source_root / "state.sqlite"
    if database.is_symlink() or not database.is_file():
        raise ValueError("Probe requires an existing regular run-state database")
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT payload FROM application_run_state WHERE run_id=?", (str(stage_input.run_id),)
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise ValueError("Captured run has no durable scope")
    record = ApplicationRunRecord.model_validate_json(row[0])
    expected = (settings.workspace.user_id, settings.workspace.project_id, settings.workspace.lab_id)
    if expected != (record.owner_user_id, record.project_id, record.lab_id) or expected != (
        stage_input.workspace.user_id, stage_input.workspace.project_id, stage_input.workspace.lab_id
    ):
        raise ValueError("Current identity does not match captured run scope")
    if record.recovery_state is not RunRecoveryState.STABLE:
        raise ValueError("Probe source must be a stable immutable snapshot")
    inputs = record.input_artifact_ids
    context = record.context_artifact_ids
    usage = []
    for source, ids in (("RUN_INPUT", inputs), ("RUN_CONTEXT", context)):
        for artifact_id in ids:
            path = source_root / "artifacts" / f"{artifact_id}.json"
            if any(part.is_symlink() for part in (path, *path.parents)):
                raise ValueError("Probe Artifact metadata cannot traverse symlinks")
            ref = ArtifactRef.model_validate_json(json.dumps(json.loads(path.read_text(encoding="utf-8"))["ref"]))
            if ref.artifact_id != artifact_id or (ref.owner_user_id, ref.project_id, ref.lab_id) != expected:
                raise ValueError("Probe Artifact scope does not match")
            application.access_service.require_artifact(settings.principal, ref, AccessAction.READ_ARTIFACT)
            usage.append(RuntimeInputArtifactUsage.from_authorized_artifact(
                ref, source=source, exposure_policy=application.configuration.exposure_policy,
                mountable_input_artifact_ids=inputs if application.execution_capability is not None else (),
            ))
    return tuple(usage)


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get("LABBIO_LIVE_STAGE_DECISION_PROBE") != "1",
                    reason="Explicit authorization required for one live decision-only probe")
async def test_live_captured_stage_decision_only():
    from pantheon.utils.log import logger

    logger.remove()
    logger.add(lambda message: print(json.dumps({
        "event": "runtime_diagnostic", "level": message.record["level"].name,
    }), file=sys.stderr), level="WARNING", backtrace=False, diagnose=False)
    try:
        await _probe_once()
    except Exception as exc:
        # pytest must not print a provider/Pydantic exception containing raw input.
        pytest.fail(f"Decision-only probe failed: {type(exc).__name__}", pytrace=False)


async def _probe_once():
    settings = load_settings(Path(os.environ["LABBIO_PROBE_CONFIG"]))
    capture = Path(os.environ["LABBIO_PROBE_CAPTURE"]).resolve(strict=True)
    original, evidence, selections = _capture(capture, UUID(os.environ["LABBIO_PROBE_INVOCATION"]))
    requested = Path(os.environ["LABBIO_PROBE_OUTPUT"]).expanduser()
    if requested.resolve().is_relative_to(capture.parent):
        raise ValueError("Probe must not write inside its captured source run")
    output = _run_directory(settings.result_root, requested, create=True)
    application = build_application(settings, output)
    try:
        usage = _current_input_usage(application, settings, capture.parent, original)
        current = original.model_copy(update={"input_artifact_usage": usage})
        current_evidence, migrated = _current_error_details(evidence)
        assembly = next(spec for spec in application.configuration.stage_assemblies if spec.stage_id is current.stage_id)
        PerInvocationPantheonStageInvoker(
            assembly=assembly, factory=application.runtime_factory,
            principal=settings.principal, workspace=settings.workspace,
            services=application.capability_services, input_usage_provider=lambda: usage,
        )._validate_input_binding(current)
        key = assembly.root_profile_key
        team, prompts = await application.runtime_factory.create_team(
            (key,), prompt_values={key: dict(assembly.finalization_prompt_values)},
            invocation_mode=RuntimeInvocationMode.FINALIZE, finalization_stage=current.stage_id,
            workflow_control=current.workflow_control,
        )
        assert not any(agent.providers for agent in team.team_agents)
        profile = application.runtime_factory.catalog.agents[key]
        invoker = PantheonTypedStageInvoker(
            team, profile=profile, prompt=prompts[key],
            response_schema=application.runtime_factory.catalog.schemas[profile.response_schema_key],
            trace_recorder=application.trace_recorder,
        )
        _write_json(output / "PROBE_INPUT.json", {
            "diagnostic_only_not_applied": True,
            "capture_sha256": hashlib.sha256(capture.read_bytes()).hexdigest(),
            "capture_line_numbers": selections,
            "current_runtime": runtime_manifest(settings),
            "original_stage_input": original.model_dump(mode="json"),
            "original_capability_evidence": evidence.model_dump(mode="json"),
            "current_stage_input": current.model_dump(mode="json"),
            "current_capability_evidence": current_evidence.model_dump(mode="json"),
            "current_projection_sources": {
                "input_artifact_usage": "canonical source run metadata and current ExposurePolicy",
                "error_details": "current _safe_error catalog for ARTIFACT_EXPOSURE_DENIED only",
                "migrated_capability_invocation_ids": migrated,
            },
            "finalization_instruction": prompts[key].sanitized_text,
        })
        result = await invoker.invoke(current, capability_evidence=current_evidence)
        _write_json(output / "PROBE_RESULT.json", {
            "diagnostic_only_not_applied": True, "result": result.model_dump(mode="json"),
        })
        assert application.run_state_store.list() == ()
        assert not tuple((output / "executions").iterdir())
        print(json.dumps({"probe_output": str(output), "stage": current.stage_id.value,
                          "proposed_action": result.next_action.action.value}))
    finally:
        application.run_state_store.close()


def _write_capture(path, *, mismatch=False, duplicate=False):
    invocation, run_id = uuid4(), uuid4()
    stage_input = RuntimeStageInput(
        run_id=run_id, invocation_id=invocation, stage_id=WorkflowStage.UNDERSTAND,
        instruction="Inspect supplied data under its actual contract.",
        workspace=RuntimeWorkspaceIdentifiers(user_id="user", project_id="project", lab_id="lab"),
        workflow_control=RuntimeWorkflowControlView(
            current_stage=WorkflowStage.UNDERSTAND,
            transition_targets=(WorkflowStage.PLAN,), request_user_input_available=True,
            retry_available=False, finish_available=False,
        ),
    )
    evidence = CapabilityEvidenceBundle(
        run_id=uuid4() if mismatch else run_id, invocation_id=invocation,
        stage_id=WorkflowStage.UNDERSTAND,
    )
    records = [{"kind": "stage_input", "payload": stage_input.model_dump(mode="json")},
               {"kind": "capability_evidence", "payload": evidence.model_dump(mode="json")},
               {"kind": "stage_result", "payload": {"next_action": "DO_NOT_REPLAY_EXPECTED_ACTION"}}]
    if duplicate:
        records.append(records[0])
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
    return invocation


def test_capture_selects_exact_pair_not_original_decision(tmp_path):
    path = tmp_path / "capture.jsonl"
    invocation = _write_capture(path)
    stage_input, evidence, lines = _capture(path, invocation)
    assert stage_input.invocation_id == evidence.invocation_id == invocation
    assert lines == {"stage_input": 1, "capability_evidence": 2}
    assert stage_input.workflow_control.transition_targets == (WorkflowStage.PLAN,)
    assert "DO_NOT_REPLAY" not in stage_input.model_dump_json() + evidence.model_dump_json()


@pytest.mark.parametrize("kwargs", ({"mismatch": True}, {"duplicate": True}))
def test_capture_rejects_mismatched_or_ambiguous_evidence(tmp_path, kwargs):
    path = tmp_path / "capture.jsonl"
    invocation = _write_capture(path, **kwargs)
    with pytest.raises(ValueError):
        _capture(path, invocation)


def test_error_migration_is_explicit_and_never_guesses_unknown_failures():
    evidence = CapabilityEvidenceBundle(
        run_id=uuid4(), invocation_id=uuid4(), stage_id=WorkflowStage.UNDERSTAND,
        items=tuple({
            "actor_profile_key": "test", "actor_agent_name": "TestAgent",
            "capability_name": "artifact_query", "information_authority": "AUTHORITATIVE_EVIDENCE",
            "status": "FAILED", "error_code": code,
        } for code in ("ARTIFACT_EXPOSURE_DENIED", "CAPABILITY_FAILED")),
    )
    projected, migrations = _current_error_details(evidence)
    assert evidence.items[0].error_details is None
    assert projected.items[0].error_details.denied_operation == "REMOTE_ARTIFACT_VIEW"
    assert projected.items[1].error_details is None
    assert migrations == [str(evidence.items[0].capability_invocation_id)]


def test_capture_rejects_unsafe_evidence_instead_of_silently_stripping_it(tmp_path):
    path = tmp_path / "capture.jsonl"
    invocation = _write_capture(path)
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    records[1]["payload"]["items"] = [{
        "actor_profile_key": "test", "actor_agent_name": "TestAgent",
        "capability_name": "artifact_query", "information_authority": "AUTHORITATIVE_EVIDENCE",
        "status": "COMPLETED", "safe_result": {"raw_data": "PRIVATE_SENTINEL"},
    }]
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
    with pytest.raises(ValueError):
        _capture(path, invocation)
