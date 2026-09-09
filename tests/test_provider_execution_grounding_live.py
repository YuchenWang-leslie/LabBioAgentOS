"""Opt-in synthetic finalization protocol check; not scientific acceptance."""

import copy
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from labbioagentos import (
    CapabilityEvidenceBundle, CapabilityEvidenceItem, ExecutionReceipt,
    ExecutionStatus, InformationAuthority, PantheonRuntimeFactory,
    PantheonTypedStageInvoker, ResponseSchemaRef, RuntimeInvocationMode,
    RuntimeStageInput, RuntimeWorkspaceIdentifiers, WorkflowStage,
)
from labbioagentos.local_config import build_application, load_settings


def _packet():
    stage = RuntimeStageInput(
        run_id=uuid4(), stage_id=WorkflowStage.EXECUTE,
        instruction="Summarize the observed execution outcome.",
        workspace=RuntimeWorkspaceIdentifiers(user_id="fixture", project_id="fixture", lab_id="fixture"),
        allowed_capabilities=("execution_submit",),
    )
    receipt = ExecutionReceipt(execution_id=uuid4(), status=ExecutionStatus.FAILED,
                               image_key="synthetic", script_hash="a" * 64, exit_code=1)
    evidence = CapabilityEvidenceBundle(
        run_id=stage.run_id, stage_id=stage.stage_id, invocation_id=stage.invocation_id,
        items=(CapabilityEvidenceItem(actor_profile_key="execution", actor_agent_name="ExecutionAgent",
            capability_name="execution_submit", information_authority=InformationAuthority.AUTHORITATIVE_EVIDENCE,
            status="COMPLETED", safe_result=receipt.model_dump(mode="json")),),
    )
    return stage, receipt, evidence


def test_synthetic_finalization_packet_has_distinct_call_and_execution_status():
    stage, receipt, evidence = _packet()
    assert evidence.items[0].status.value == "COMPLETED"
    assert evidence.items[0].safe_result["status"] == receipt.status.value == "FAILED"
    assert evidence.run_id == stage.run_id


@pytest.mark.skipif(os.environ.get("LABBIO_EXECUTION_GROUNDING_LIVE") != "1",
                    reason="Explicit bounded provider protocol smoke required")
@pytest.mark.asyncio
async def test_real_finalizer_preserves_synthetic_failed_execution(monkeypatch):
    from pantheon.utils.adapters.openai_adapter import OpenAIAdapter
    from pantheon.utils.log import logger
    logger.remove()
    output = Path(os.environ["LABBIO_EXECUTION_GROUNDING_OUTPUT"])
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    settings = load_settings(Path(os.environ["LABBIO_EXECUTION_GROUNDING_CONFIG"]))
    app = build_application(settings, output / "runtime")
    stage, receipt, evidence = _packet()
    catalog = app.configuration.profile_catalog
    result, failure_type, schema = None, None, None
    schemas = []
    make_client = OpenAIAdapter._make_client

    def observe_client(self, *args, **kwargs):
        client = make_client(self, *args, **kwargs)
        create = client.chat.completions.create

        async def observe_request(**request):
            schemas.append(copy.deepcopy(request.get("response_format")))
            return await create(**request)

        monkeypatch.setattr(client.chat.completions, "create", observe_request)
        return client

    monkeypatch.setattr(OpenAIAdapter, "_make_client", observe_client)
    try:
        team, prompts = await PantheonRuntimeFactory(catalog).create_team(
            ("execution",), invocation_mode=RuntimeInvocationMode.FINALIZE,
            prompt_values={"execution": {"protocol": "Summarize the observed execution outcome."}},
        )
        result = await PantheonTypedStageInvoker(team, profile=catalog.agents["execution"],
            prompt=prompts["execution"], response_schema=ResponseSchemaRef(),
            trace_recorder=app.trace_recorder,
        ).invoke(stage, capability_evidence=evidence)
        schema = team.team_agents[0].response_format.model_json_schema()
    except Exception as exc:
        failure_type = type(exc).__name__
    finally:
        app.run_state_store.close()
        (output / "AUDIT.json").write_text(json.dumps({
            "scope": "SYNTHETIC_FINALIZATION_NOT_SCIENTIFIC_ACCEPTANCE",
            "runtime_revision": app.configuration.runtime_revision,
            "receipt": receipt.model_dump(mode="json"),
            "failure_type": failure_type, "response_schema": schema,
            "provider_response_schemas": schemas,
            "result": result.model_dump(mode="json") if result is not None else None,
        }, indent=2) + "\n")
    assert failure_type is None
    assert schemas and all(item and item["type"] == "json_schema" for item in schemas)
    assert result.body.execution_status == "FAILED"
    assert result.body.execution_reference.kind.value == "EXECUTION"
    assert result.body.execution_reference.reference_id == str(receipt.execution_id)
    assert result.body.output_artifact_references == ()
