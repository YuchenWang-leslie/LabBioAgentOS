"""Actual Native delegation reads originals without lending the parent read credit.

Providers are scripted engineering fixtures, not scientific capability evaluations.
"""

from dataclasses import replace
import json
from uuid import uuid4

import pytest

from labbioagentos import (
    CapabilityProfile, PromptProfile, RuntimeStageInput, RuntimeStageResult,
    RuntimeWorkflowControlView, RuntimeWorkspaceIdentifiers, WorkflowStage,
)
from labbioagentos.kernel import FinishReason, ModelTurn, TokenUsage, ToolCall
from labbioagentos.runtime.assembly import RuntimeAgentCapabilitySpec, RuntimeStageAssemblySpec
from labbioagentos.runtime.native_stage import NativeKernelStageInvoker, NativeRuntimeFactory
from labbioagentos.runtime.observations import ObservationStore
from labbioagentos.runtime.contracts import RuntimePriorResultView, runtime_stage_result_format
from labbioagentos.runtime.context import stage_context_projection
from labbioagentos.runtime.context import attach_handoff_context, delegation_directory
from labbioagentos.runtime.handoff import finalization_evidence
from labbioagentos.runtime.tooling import LabBioRuntimeToolSet
from labbioagentos.runtime.contracts import CapabilityEvidenceBundle
from labbioagentos.kernel import DelegationResult
from test_execution_inspection import inspection, _submit  # noqa: F401
from test_runtime_milestone_b import boundary, _toolset  # noqa: F401
from test_runtime_milestone_c2 import _catalog
from test_native_stage_application import WorkflowProvider, _body


def turn(content=None, name=None, arguments=None):
    return ModelTurn(content=content,
        tool_calls=(ToolCall(call_id="fixture-call", name=name,
            arguments_json=json.dumps(arguments or {})),) if name else (),
        finish_reason=FinishReason.TOOL_CALLS if name else FinishReason.STOP,
        usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2))


@pytest.mark.asyncio
@pytest.mark.parametrize("needs_check", (True, False))
async def test_reviewer_reads_original_parent_rereads_before_handoff_and_returns_to_execute(
    tmp_path, inspection, boundary, needs_check,
):
    original = "# ORIGINAL_ENGINEERING_PROGRAM\npass\n"
    receipt = await _submit(inspection, boundary, original)
    service, executor, run_id = inspection
    tools = _toolset(boundary, WorkflowStage.VALIDATE, (), execution_submission=service,
                     observation_store=ObservationStore(tmp_path / "observations"))
    grants = ("execution_inspect", "evidence_read", "prepare_handoff")
    stage = RuntimeStageInput(run_id=run_id, stage_id=WorkflowStage.VALIDATE,
        instruction="Check a synthetic zero-exit result before accepting it.",
        workspace=RuntimeWorkspaceIdentifiers(**tools.binding.workspace.model_dump()),
        allowed_capabilities=grants,
        workflow_control=RuntimeWorkflowControlView(current_stage=WorkflowStage.VALIDATE,
            transition_targets=(WorkflowStage.EXECUTE, WorkflowStage.INTERPRET),
            request_user_input_available=False, retry_available=False, finish_available=False))
    target = WorkflowStage.EXECUTE if needs_check else WorkflowStage.INTERPRET
    action = {"action": "transition", "target_stage": target.value,
              "reason": "New evidence is needed." if needs_check else "The requested check is supported."}
    calls, shared, boundaries = [], {}, []

    class Leader:
        binding = WorkflowProvider.binding
        count = 0

        async def generate(self, request):
            calls.append(request)
            assert request.messages[0].content == "ROOT_HANDOFF_PROTOCOL"
            self.count += 1
            if self.count == 1:
                assert {"delegate", "list_specialists"}.issubset(t.name for t in request.tools)
                return turn(name="delegate", arguments={"target_agent_id": "checker",
                    "instruction": f"Inspect original execution {receipt.execution_id}; propose needed checks."})
            if self.count == 2:
                delegated = json.loads(request.messages[-1].content)["content"]
                assert delegated["status"] == "SUCCEEDED"
                assert delegated["content_authority"] == "AGENT_PROSE"
                assert original not in "\n".join(m.content or "" for m in request.messages)
                return turn(name="prepare_handoff", arguments={"observation_ids": [shared["observation"]],
                    "summary": "Check needed.", "next_action": action})
            if self.count == 3:
                rejected = json.loads(request.messages[-1].content)["content"]
                assert not rejected["success"], "Child reading must not become parent reading"
                return turn(name="evidence_read", arguments={"observation_id": shared["observation"]})
            if self.count == 4:
                page = json.loads(request.messages[-1].content)["content"]
                assert page["success"] and page["data"]["next_offset"] is None
                assert json.loads(page["data"]["text"])["data"]["submitted_program"]["source"] == original
                return turn(name="prepare_handoff", arguments={"observation_ids": [shared["observation"]],
                    "summary": "The source was actually inspected; obtain the proposed new check.",
                    "limitations": ["Synthetic check is still unexecuted."], "next_action": action})
            assert json.loads(request.messages[-1].content)["content"]["data"]["status"] == "READY"
            return turn("Original inspected; unresolved check handed back to the execution owner.")

    class Checker:
        binding = WorkflowProvider.binding
        count = 0

        async def generate(self, request):
            calls.append(request)
            assert request.messages[0].content == "READ_ONLY_INDEPENDENT_REVIEW"
            assert not {"execution_submit", "prepare_handoff", "report_submit"}.intersection(
                t.name for t in request.tools)
            self.count += 1
            if self.count == 1:
                assert "ROOT_HANDOFF_PROTOCOL" not in "\n".join(m.content for m in request.messages)
                return turn(name="execution_inspect", arguments={"execution_id": str(receipt.execution_id)})
            response = json.loads(request.messages[-1].content)["content"]
            assert response["data"]["submitted_program"]["source"] == original
            shared["observation"] = response["capability_invocation_id"]
            return turn("A further check is needed; original observation " + shared["observation"])

    class Finalizer:
        binding = WorkflowProvider.binding

        async def generate(self, request):
            calls.append(request)
            assert not request.tools
            result = RuntimeStageResult(stage_id=stage.stage_id, summary="Return for new evidence.",
                body=_body(stage.stage_id), next_action=action)
            return turn(result.model_dump_json(exclude={"result_id"}))

    catalog = _catalog()
    catalog.prompts["runtime-generic"] = PromptProfile(
        template_id="runtime-generic", version="1", template_text="{protocol}")
    catalog.capabilities["coordinator-capabilities"] = CapabilityProfile(
        profile_key="coordinator-capabilities", version="1", capability_allowlist=grants)
    catalog.agents["checker"] = catalog.agents["coordinator"].model_copy(update={
        "profile_key": "checker", "agent_name": "Checker", "capability_profile_key": "checker-caps"})
    catalog.capabilities["checker-caps"] = CapabilityProfile(
        profile_key="checker-caps", version="1", capability_allowlist=("execution_inspect",))
    providers = iter((Leader(), Checker(), Finalizer()))
    invoker = NativeKernelStageInvoker(assembly=RuntimeStageAssemblySpec(
        stage_id=stage.stage_id, root_profile_key="coordinator", prompt_template_key="runtime-generic",
        capability_allowlist=grants, capability_prompt_values={"protocol": "ROOT_HANDOFF_PROTOCOL"},
        finalization_prompt_values={"protocol": "ROOT_FINALIZATION_PROTOCOL"},
        capability_peer_specs=(RuntimeAgentCapabilitySpec("checker", ("execution_inspect",),
            {"protocol": "READ_ONLY_INDEPENDENT_REVIEW"}),)),
        factory=NativeRuntimeFactory(catalog, lambda _model: next(providers)),
        principal=tools.binding.principal, workspace=tools.binding.workspace, services=tools.services,
        boundary_observer=lambda kind, value: boundaries.append((kind, value)))
    result = await invoker.invoke(stage)
    assert result.next_action.target_stage is target
    assert result.handoff_context.observations[0].observation_id.hex == shared["observation"].replace("-", "")
    assert result.handoff_context.limitations == ("Synthetic check is still unexecuted.",)
    assert len(executor.plans) == 1, "Read-only review must not run a hidden check"
    assert len(calls) == 8
    assert result.delegations[0].content == "A further check is needed; original observation " + shared["observation"]
    assert result.delegations[0].content_authority == "AGENT_PROSE"
    persisted = RuntimeStageResult.model_validate_json(result.model_dump_json())
    view = RuntimePriorResultView.from_result(persisted)
    assert view.delegations == result.delegations
    projected = stage_context_projection(stage.model_copy(update={
        "allowed_capabilities": ("stage_context_read",), "prior_results": (view,)}))
    assert result.delegations[0].content not in json.dumps(projected)
    assert projected["prior_delegations"][0]["source_result_id"] == str(result.result_id)
    assert projected["prior_delegations"][0]["child_invocation_id"] == str(result.delegations[0].child_invocation_id)
    bundle = next(value for kind, value in boundaries if kind == "capability_evidence")
    assert bundle.delegations == result.delegations
    assert "A further check is needed" not in json.dumps(finalization_evidence(bundle))
    assert "delegations" not in runtime_stage_result_format(stage.stage_id).model_json_schema()["properties"]
    reader = LabBioRuntimeToolSet(replace(tools.binding, run_id=run_id, invocation_id=uuid4(),
        capability_allowlist=("stage_context_read",), prior_results=(view,)), tools.services)
    page = await reader.stage_context_read(str(result.result_id))
    assert page["success"] and page["data"]["next_offset"] is None
    assert json.loads(page["data"]["text"])["delegations"][0]["content"] == result.delegations[0].content
    assert page["data"]["source_read_credit"] is False


@pytest.mark.parametrize("status", ("SUCCEEDED", "FAILED", "STOPPED", "CANCELLED"))
def test_original_peer_outcome_survives_without_a_prepared_handoff(status):
    from test_runtime_milestone_c2 import _stage_input
    stage = _stage_input()
    payload = dict(task_id=uuid4(), parent_invocation_id=stage.invocation_id,
        child_invocation_id=uuid4(), target_agent_id="checker", status=status,
        content_authority="AGENT_PROSE" if status == "SUCCEEDED" else "CONTROL_STATE")
    if status == "SUCCEEDED":
        payload["content"] = "Original public peer assessment."
    else:
        payload.update(error_code="TEST_STOP", termination="TEST_STOP")
    if status == "CANCELLED":
        payload["cancellation_reason"] = "TEST_CANCEL"
    outcome = DelegationResult(**payload)
    bundle = CapabilityEvidenceBundle(run_id=stage.run_id, stage_id=stage.stage_id,
        invocation_id=stage.invocation_id, delegations=(outcome,))
    result = RuntimeStageResult(stage_id=stage.stage_id, summary="Synthetic handoff.",
        body=_body(stage.stage_id), next_action={"action": "finish"})
    saved = attach_handoff_context(result, stage, bundle)
    assert saved.handoff_context is None
    assert RuntimeStageResult.model_validate_json(saved.model_dump_json()).delegations == (outcome,)
    assert delegation_directory(saved.delegations)[0]["status"] == status
    assert "content" not in delegation_directory(saved.delegations)[0]
