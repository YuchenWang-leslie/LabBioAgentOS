"""Exact reporting facts traverse the Native host; model prose stays context.

These are scripted transport fixtures, not evidence that a live model checks
every report sentence. No scientific program or report is supplied here.
"""

from dataclasses import replace
from hashlib import sha256
import json
from uuid import UUID, uuid4

import pytest

from labbioagentos import (
    NextAction, NextActionProposal, ReportStageBody, RuntimePriorResultView,
    RuntimeStageAssemblySpec, RuntimeStageInput, RuntimeStageResult,
    RuntimeWorkspaceIdentifiers, WorkflowStage,
)
from labbioagentos.kernel import FinishReason, ModelTurn, ProviderBinding, ToolCall
from labbioagentos.runtime.native_stage import NativeKernelStageInvoker, NativeRuntimeFactory
from labbioagentos.runtime.observations import ObservationStore
from test_execution_inspection import _submit, inspection  # noqa: F401
from test_runtime_milestone_b import _toolset, boundary  # noqa: F401
from test_runtime_milestone_c2 import _catalog


class CheckingProvider:
    binding = ProviderBinding(
        provider="fixture", model="fact-chain-fixture",
        endpoint="https://provider.invalid/v1", transport="OPENAI_COMPATIBLE_CHAT",
        thinking_mode="DISABLED",
    )

    def __init__(self, respond):
        self.respond = respond
        self.requests = []

    async def generate(self, request):
        self.requests.append(request)
        try:
            return self.respond(request, len(self.requests))
        except Exception as exc:
            pytest.fail(f"Scripted provider assertion failed: {type(exc).__name__}: {exc}")


@pytest.mark.asyncio
@pytest.mark.parametrize("select_program", [True, False])
async def test_report_rereads_exact_execution_and_error_without_promoting_stale_plan(
    tmp_path, inspection, boundary, select_program,
):
    # Harmless engineering-only source, stored by the real submission service
    # with a mock executor. The old model claim intentionally contradicts it.
    source = "# engineering implementation-beta\npass\n"
    receipt = await _submit(inspection, boundary, source)
    service, executor, run_id = inspection
    capabilities = ("execution_inspect", "evidence_read", "prepare_handoff")
    tools = _toolset(boundary, WorkflowStage.REPORT, capabilities,
        execution_submission=service, observation_store=ObservationStore(tmp_path / "observations"))
    stage_input = RuntimeStageInput(
        run_id=run_id, invocation_id=tools.binding.invocation_id,
        stage_id=WorkflowStage.REPORT,
        instruction="Review engineering execution facts; unsupported assertions remain unverified.",
        workspace=RuntimeWorkspaceIdentifiers(
            user_id=tools.binding.workspace.user_id,
            project_id=tools.binding.workspace.project_id,
            lab_id=tools.binding.workspace.lab_id,
        ),
        allowed_capabilities=capabilities,
        prior_results=(RuntimePriorResultView(
            result_id=uuid4(), stage_id=WorkflowStage.PLAN, body_kind="PLAN",
            model_summary="implementation-alpha executed; the error contained claimed_response_field.",
            model_body={"proposed_implementation": "implementation-alpha",
                        "claimed_response_field": "not-an-observed-field"},
        ),),
    )
    seen = {}

    def capability(request, turn):
        def call(name, arguments):
            return ModelTurn(tool_calls=(ToolCall(call_id=f"fixture-{turn}", name=name,
                arguments_json=json.dumps(arguments)),), finish_reason=FinishReason.TOOL_CALLS)

        if turn == 1:
            context = json.loads(request.messages[-1].content)
            assert context["prior_results"][0]["authority"] == "MODEL_CONTEXT"
            assert context["evidence_grounding"]["prior_results"] == "MODEL_CONTEXT"
            assert source not in request.messages[-1].content
            return call("execution_inspect", {"execution_id": str(uuid4())})
        feedback = json.loads(request.messages[-1].content)["content"]
        if turn == 2:
            assert feedback["success"] is False
            assert "claimed_response_field" not in json.dumps(feedback)
            seen["failure"] = feedback
            return call("execution_inspect", {"execution_id": str(receipt.execution_id)})
        assert feedback["success"], feedback
        if turn == 3:
            assert feedback["data"]["submitted_program"]["source"] == source
            assert feedback["data"]["receipt"] == receipt.model_dump(mode="json")
            seen["inspection"] = feedback
            return call("evidence_read", {"observation_id": seen["failure"]["capability_invocation_id"]})
        if turn == 4:
            assert feedback["data"]["complete_observation_delivered"]
            assert json.loads(feedback["data"]["text"]) == seen["failure"]
            selected = [seen["failure"]["capability_invocation_id"]]
            if select_program:
                selected.append(seen["inspection"]["capability_invocation_id"])
            return call("prepare_handoff", {"observation_ids": selected,
                "summary": "Agent assessment, not a replacement for the selected originals.",
                "limitations": [] if select_program else ["Original program not selected."]})
        assert turn == 5 and feedback["data"]["status"] == "READY"
        return ModelTurn(content="Engineering handoff ready; stale plan remains unverified.",
                         finish_reason=FinishReason.STOP)

    def finalize(request, turn):
        assert turn == 1 and request.tools == ()
        payload = json.loads(request.messages[-1].content)
        control = payload["prepared_handoff_control"]
        assert control["summary_authority"] == "MODEL_CONTEXT"
        assert "Other archived bodies are unavailable" in control["scope"]
        assert payload["explicit_completion_control"]["authority"] == "MODEL_CONTEXT"
        prior = payload["stage_input"]["prior_results"][0]
        assert prior["authority"] == "MODEL_CONTEXT"
        assert "implementation-alpha" in prior["model_summary"]
        handoff = next(json.loads(message.content) for message in request.messages
            if message.content and message.content.startswith('{"summary":'))
        delivered = {item["observation_id"]: item["public_response"] for item in handoff["observations"]}
        assert delivered[seen["failure"]["capability_invocation_id"]] == seen["failure"]
        if select_program:
            actual = delivered[seen["inspection"]["capability_invocation_id"]]["data"]
            assert actual["submitted_program"]["authority"] == "MODEL_CONTEXT"
            assert actual["submitted_program"]["source"] == source
            assert actual["receipt"]["script_hash"] == sha256(source.encode()).hexdigest()
        else:
            assert seen["inspection"]["capability_invocation_id"] not in delivered
            assert "implementation-beta" not in json.dumps([m.content for m in request.messages])
        items = payload["capability_evidence"]["items"]
        failed = next(item for item in items if item["capability_invocation_id"] == seen["failure"]["capability_invocation_id"])
        assert failed["status"] == "FAILED" and failed["error_code"] == seen["failure"]["error"]["error_code"]
        result = RuntimeStageResult(stage_id=WorkflowStage.REPORT,
            summary="Synthetic engineering handoff fixture.",
            body=ReportStageBody(report_summary="No scientific report authored by this fixture."),
            next_action=NextActionProposal(action=NextAction.TRANSITION, target_stage=WorkflowStage.LEARN))
        return ModelTurn(content=result.model_dump_json(exclude={"result_id"}), finish_reason=FinishReason.STOP)

    providers = [CheckingProvider(capability), CheckingProvider(finalize)]
    catalog = _catalog()
    configured = catalog.capabilities["coordinator-capabilities"]
    catalog.capabilities["coordinator-capabilities"] = configured.model_copy(update={"capability_allowlist": capabilities})
    constructed = iter(providers)
    evidence = []
    invoker = NativeKernelStageInvoker(
        assembly=RuntimeStageAssemblySpec(stage_id=WorkflowStage.REPORT,
            root_profile_key="coordinator", prompt_template_key="runtime-generic",
            capability_allowlist=capabilities, max_capability_turns=6,
            preserve_capability_completion=True),
        factory=NativeRuntimeFactory(catalog, lambda _model: next(constructed)),
        principal=tools.binding.principal, workspace=tools.binding.workspace,
        services=tools.services, trace_recorder=tools.services.trace_recorder,
        boundary_observer=lambda kind, value: evidence.append(value) if kind == "capability_evidence" else None,
    )
    result = await invoker.invoke(stage_input)
    assert result.stage_id is WorkflowStage.REPORT
    assert [len(provider.requests) for provider in providers] == [5, 1]
    assert len(executor.plans) == 1, "Reporting must not execute or revise the original program"
    saved = tools.services.observation_store.load(replace(tools.binding, run_id=run_id),
        UUID(seen["failure"]["capability_invocation_id"]))
    assert saved.public_response == seen["failure"]
    assert saved.item.status.value == "FAILED"
    assert evidence[0].prepared_handoff is not None
