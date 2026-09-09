"""Opt-in isolated finalization diagnostic; never resumes the source workflow."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from labbioagentos import (
    CapabilityEvidenceBundle, PantheonRuntimeFactory, PantheonTypedStageInvoker,
    ResponseSchemaRef, RuntimeInvocationMode, RuntimeStageInput,
)
from labbioagentos.local_config import build_application, load_settings
from labbioagentos.local_workspace_cli import scoped_settings


def _packet(rows):
    stage = RuntimeStageInput.model_validate_json(json.dumps(next(
        row["payload"] for row in reversed(rows) if row["kind"] == "stage_input")))
    evidence = CapabilityEvidenceBundle.model_validate_json(json.dumps(next(
        row["payload"] for row in reversed(rows) if row["kind"] == "capability_evidence"
        and row["payload"]["invocation_id"] == str(stage.invocation_id))))
    return stage, evidence


def test_replay_packet_uses_strict_json_roundtrip():
    from test_execution_result_grounding import _input, _bundle
    stage = _input()
    evidence = _bundle(stage)
    rows = json.loads(json.dumps([
        {"kind": "stage_input", "payload": stage.model_dump(mode="json")},
        {"kind": "capability_evidence", "payload": evidence.model_dump(mode="json")},
    ]))
    assert _packet(rows) == (stage, evidence)


@pytest.mark.skipif(os.environ.get("LABBIO_FINALIZATION_REPLAY_LIVE") != "1",
                    reason="Explicit bounded finalization diagnostic required")
@pytest.mark.asyncio
async def test_isolated_last_finalization_packet(monkeypatch):
    from pantheon.utils.adapters.openai_adapter import OpenAIAdapter
    from pantheon.utils.log import logger
    logger.remove()
    source = Path(os.environ["LABBIO_FINALIZATION_REPLAY_SOURCE"])
    output = Path(os.environ["LABBIO_FINALIZATION_REPLAY_OUTPUT"])
    assert source.resolve() != output.resolve()
    rows = [json.loads(line) for line in (source / "model-boundaries.jsonl").read_text().splitlines()]
    stage, evidence = _packet(rows)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    settings = load_settings(Path(os.environ["LABBIO_FINALIZATION_REPLAY_CONFIG"]))
    settings = scoped_settings(SimpleNamespace(command="status", run_dir=source,
        workspace_root=None, user=stage.workspace.user_id, project=stage.workspace.project_id,
        credential_file=None), settings)
    app = build_application(settings, output / "runtime")
    result, failure = None, None
    wire_limits = []
    make_client = OpenAIAdapter._make_client

    def observe_client(self, *args, **kwargs):
        client = make_client(self, *args, **kwargs)
        create = client.chat.completions.create

        async def observe_request(**request):
            wire_limits.append({key: request.get(key) for key in
                                ("max_tokens", "max_completion_tokens")})
            assert len(wire_limits) == 1, "Diagnostic permits one finalization request"
            assert not request.get("tools"), "Finalization diagnostic cannot call tools"
            assert any(value == settings.provider.max_output_tokens
                       for value in wire_limits[-1].values())
            return await create(**request)

        monkeypatch.setattr(client.chat.completions, "create", observe_request)
        return client

    monkeypatch.setattr(OpenAIAdapter, "_make_client", observe_client)
    try:
        assembly = next(spec for spec in app.configuration.stage_assemblies if spec.stage_id == stage.stage_id)
        catalog = app.configuration.profile_catalog
        key = assembly.root_profile_key
        team, prompts = await PantheonRuntimeFactory(catalog).create_team(
            (key,), prompt_values={key: assembly.finalization_prompt_values},
            invocation_mode=RuntimeInvocationMode.FINALIZE,
            finalization_stage=stage.stage_id, workflow_control=stage.workflow_control,
        )
        result = await PantheonTypedStageInvoker(team, profile=catalog.agents[key],
            prompt=prompts[key], response_schema=ResponseSchemaRef(),
            trace_recorder=app.trace_recorder).invoke(stage, capability_evidence=evidence)
    except Exception as exc:
        failure = {"type": type(exc).__name__,
            "code": getattr(exc, "error_code", None),
            "field_paths": list(getattr(exc, "validation_error_field_paths", ())),
            "error_types": list(getattr(exc, "validation_error_types", ()))}
    finally:
        app.run_state_store.close()
        if app.configuration.skill_service is not None:
            from labbioagentos.local_gold import close_personal_gold
            close_personal_gold(app.configuration.skill_service)
        (output / "AUDIT.json").write_text(json.dumps({
            "scope": "ISOLATED_FINALIZATION_DIAGNOSTIC_NOT_WORKFLOW_ACCEPTANCE",
            "source_run_id": str(stage.run_id), "source_invocation_id": str(stage.invocation_id),
            "runtime_revision": app.configuration.runtime_revision,
            "max_output_tokens": settings.provider.max_output_tokens,
            "wire_output_limits": wire_limits,
            "failure": failure, "result": result.model_dump(mode="json") if result else None,
        }, indent=2) + "\n")
    assert failure is None
    assert result.stage_id == stage.stage_id
