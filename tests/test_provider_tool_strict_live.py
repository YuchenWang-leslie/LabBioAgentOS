"""Opt-in wire compatibility check; synthetic protocol data, no scientific run."""

import copy
from dataclasses import replace
import json
import os
from pathlib import Path

import pytest

from labbioagentos import LabBioRuntimeToolSet, RuntimeInvocationMode, WorkflowStage
from labbioagentos.local_config import build_application, load_settings
from labbioagentos.runtime.pantheon import PantheonRuntimeFactory
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401


def _execution_toolset(binding, services):
    return LabBioRuntimeToolSet(replace(
        binding, stage_id=WorkflowStage.EXECUTE,
        actor_profile_key="execution", actor_agent_name="ExecutionAgent",
        capability_allowlist=("artifact_query", "execution_submit"),
    ), services)


@pytest.mark.asyncio
async def test_protocol_fixture_exposes_both_real_tools(artifact_query_boundary):
    _, binding, ref, original = artifact_query_boundary
    toolset = _execution_toolset(binding, original.services)
    assert {"artifact_query", "execution_submit"}.issubset(toolset.functions)
    assert toolset.binding.stage_id is WorkflowStage.EXECUTE
    assert toolset.services.execution_submission is None
    assert (await toolset.artifact_query(str(ref.artifact_id), "SUMMARY"))["success"]
    assert toolset.evidence_items()[-1].capability_name == "artifact_query"


@pytest.mark.skipif(os.environ.get("LABBIO_TOOL_STRICT_LIVE") != "1",
                    reason="Explicit bounded provider protocol smoke required")
@pytest.mark.asyncio
async def test_real_provider_uses_unmodified_strict_artifact_schema(
    artifact_query_boundary, monkeypatch,
):
    from pantheon.utils.adapters.openai_adapter import OpenAIAdapter
    from pantheon.utils.log import logger

    logger.remove()  # Provider bodies are not test diagnostics.
    sink, binding, ref, toolset = artifact_query_boundary
    # Include the real execution schema for compatibility validation, without an
    # executor service. The smoke authorizes only inspection of synthetic data.
    toolset = _execution_toolset(binding, toolset.services)
    output = Path(os.environ["LABBIO_TOOL_STRICT_OUTPUT"])
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    settings = load_settings(Path(os.environ["LABBIO_TOOL_STRICT_CONFIG"]))
    assert settings.provider.provider_tool_schema_strict is True
    assert settings.provider.thinking_enabled is False
    application = build_application(settings, output / "runtime")
    make_client = OpenAIAdapter._make_client
    schemas = []

    def observe_client(self, *args, **kwargs):
        client = make_client(self, *args, **kwargs)
        create = client.chat.completions.create

        async def observe_request(**request):
            schemas.append(copy.deepcopy(request.get("tools", [])))
            return await create(**request)

        monkeypatch.setattr(client.chat.completions, "create", observe_request)
        return client

    monkeypatch.setattr(OpenAIAdapter, "_make_client", observe_client)
    failure_type = None
    try:
        agent, _ = await PantheonRuntimeFactory(
            application.configuration.profile_catalog,
        ).create_agent(
            "execution", toolset=toolset,
            invocation_mode=RuntimeInvocationMode.CAPABILITY,
            prompt_values={"protocol": "Inspect governed references using available capabilities."},
        )
        await agent.run(
            "Inspect this governed Artifact and summarize its contents: "
            + json.dumps({"kind": "ARTIFACT", "reference_id": str(ref.artifact_id)}),
            max_turns=3,
        )
    except Exception as exc:
        failure_type = type(exc).__name__  # Never persist provider exception text.
    finally:
        application.run_state_store.close()
        audit = [event.model_dump(mode="json") for event in sink.read(binding.run_id)]
        (output / "AUDIT.json").write_text(json.dumps({
            "scope": "SYNTHETIC_TOOL_PROTOCOL_NOT_SCIENTIFIC_ACCEPTANCE",
            "runtime_revision": application.configuration.runtime_revision,
            "failure_type": failure_type, "provider_tool_schemas": schemas,
            "trace": audit,
        }, indent=2) + "\n")
    assert failure_type is None
    assert schemas
    for tools in schemas:
        assert any(tool["function"]["name"].endswith("__execution_submit") for tool in tools)
        for tool in tools:
            function = tool["function"]
            assert function["name"] != "background_task"
            assert function["strict"] is True
            assert function["parameters"]["additionalProperties"] is False
            assert "_background" not in function["parameters"]["properties"]
            assert "_background" not in function["parameters"].get("required", [])
            if function["name"].endswith("__artifact_query"):
                assert function["parameters"]["properties"]["limit"]["anyOf"] == [
                    {"type": "integer", "minimum": 1}, {"type": "null"},
                ]
    evidence = toolset.evidence_items()
    assert evidence and all(item.status.value == "COMPLETED" for item in evidence)
    assert all(item.capability_name == "artifact_query" for item in evidence)
