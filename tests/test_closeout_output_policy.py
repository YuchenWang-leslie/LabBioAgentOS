"""Closing a bounded investigation must not inherit its code-generation allowance."""
import json

import pytest

from labbioagentos import (
    ArtifactExposureClass, ArtifactReleaseBasis, ArtifactRepresentation, CapabilityProfile,
    RuntimeStageInput, RuntimeStageResult, RuntimeWorkflowControlView,
    RuntimeWorkspaceIdentifiers, WorkflowStage,
)
from labbioagentos.kernel import KernelDeadlineExceededError
from labbioagentos.kernel.control import completion_capacity
from labbioagentos.runtime.assembly import RuntimeStageAssemblySpec
from labbioagentos.runtime.native_stage import NativeKernelStageInvoker, NativeRuntimeFactory
from labbioagentos.runtime.observations import ObservationStore
from test_runtime_milestone_b import boundary, _toolset  # noqa: F401
from test_runtime_milestone_c2 import _catalog
from test_native_stage_application import WorkflowProvider, _body
from test_review_evidence_loop import turn


@pytest.mark.parametrize("model_cap, expected", [(32768, 8192), (8192, 8192), (4096, 4096)])
def test_published_completion_policy_keeps_original_reserves_and_smaller_model_ceiling(model_cap, expected):
    from labbioagentos.kernel import BudgetLimits
    limits = BudgetLimits(provider_turns=16, tokens=2031616, wall_time_ms=900000,
        tool_calls=24, evidence_entries=24, evidence_bytes=600000,
        delegations=0, delegation_depth=0, finalization_attempts=0)
    policy = completion_capacity(limits, max_output_tokens=model_cap)
    assert policy["max_output_tokens"] == expected
    assert policy["model_time_ms"] == 300000
    assert policy["tokens"] == 2031616 // 3
    assert policy["provider_turns"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("work_cap", [32768, 4096])
async def test_native_timeout_publishes_and_sends_same_independent_closeout_cap(
        tmp_path, boundary, monkeypatch, work_cap):
    from labbioagentos.runtime import native_stage
    from labbioagentos.kernel import loop
    now = [1000.0]
    for module in (native_stage, loop):
        monkeypatch.setattr(module, "monotonic", lambda: now[0])
    grants = ("artifact_query", "handoff_status", "prepare_handoff")
    tools = _toolset(boundary, WorkflowStage.VALIDATE, grants,
        observation_store=ObservationStore(tmp_path / "observations"))
    ref = tools.services.artifact_store.register(artifact_type="synthetic-table",
        exposure_class=ArtifactExposureClass.DERIVED,
        release_basis=ArtifactReleaseBasis.TRUSTED_EXECUTION_DECLASSIFICATION,
        representation=ArtifactRepresentation(records=({"record_type": "engineering"},), record_count=1),
        owner_user_id=tools.binding.principal.user_id, project_id=tools.binding.workspace.project_id,
        lab_id=tools.binding.workspace.lab_id, run_id=tools.binding.run_id)
    stage = RuntimeStageInput(run_id=tools.binding.run_id, stage_id=WorkflowStage.VALIDATE,
        instruction="Inspect a synthetic record and select your legal next action.",
        workspace=RuntimeWorkspaceIdentifiers(**tools.binding.workspace.model_dump()),
        allowed_capabilities=grants, workflow_control=RuntimeWorkflowControlView(
            current_stage=WorkflowStage.VALIDATE, transition_targets=(WorkflowStage.INTERPRET,),
            request_user_input_available=False, retry_available=False, finish_available=False))
    action = {"action": "transition", "target_stage": "INTERPRET", "reason": "Synthetic record inspected."}
    seen = {"requests": [], "policies": [], "source": None}
    class Leader:
        binding = WorkflowProvider.binding
        async def generate(self, request):
            seen["requests"].append(request)
            seen["policies"].append(json.loads(request.messages[1].content)["completion_capacity"])
            for message in request.messages:
                if message.role == "TOOL":
                    data = json.loads(message.content).get("content") or {}
                    if (data.get("data") or {}).get("status") == "READY":
                        return turn("Agent-authored handoff complete.")
                    if data.get("success") and data.get("capability_invocation_id"):
                        seen["source"] = data["capability_invocation_id"]
            if len(seen["requests"]) == 1:
                now[0] += 1
                return turn(name="artifact_query", arguments={
                    "artifact_id": str(ref.artifact_id), "view_type": "SUMMARY"})
            if len(seen["requests"]) == 2:
                # A response interrupted at the existing work deadline executes
                # no tools and has unknown usage; no provider-speed assumption.
                now[0] = request.deadline_monotonic + .001
                raise KernelDeadlineExceededError()
            assert {t.name for t in request.tools} == {"handoff_status", "prepare_handoff"}
            now[0] += 1
            return turn(name="prepare_handoff", arguments={"observation_ids": [seen["source"]],
                "summary": "Actually inspected one synthetic record.", "next_action": action})
    class Finalizer:
        binding = WorkflowProvider.binding
        async def generate(self, request):
            assert not request.tools
            return turn(RuntimeStageResult(stage_id=stage.stage_id, summary="Agent handoff.",
                body=_body(stage.stage_id), next_action=action).model_dump_json(exclude={"result_id"}))
    catalog = _catalog()
    catalog.models["runtime-default"] = catalog.models["runtime-default"].model_copy(
        update={"max_output_tokens": work_cap})
    catalog.capabilities["coordinator-capabilities"] = CapabilityProfile(
        profile_key="coordinator-capabilities", version="1", capability_allowlist=grants)
    providers = iter((Leader(), Finalizer()))
    invoker = NativeKernelStageInvoker(assembly=RuntimeStageAssemblySpec(stage_id=stage.stage_id,
        root_profile_key="coordinator", prompt_template_key="runtime-generic", capability_allowlist=grants,
        max_capability_turns=16, max_no_progress_seconds=900),
        factory=NativeRuntimeFactory(catalog, lambda _: next(providers)),
        principal=tools.binding.principal, workspace=tools.binding.workspace, services=tools.services)
    result = await invoker.invoke(stage)
    assert result.next_action.model_dump(mode="json") == action
    assert result.handoff_context is not None
    assert [r.max_output_tokens for r in seen["requests"][:2]] == [work_cap, work_cap]
    expected = min(8192, work_cap)
    assert all(r.max_output_tokens == expected for r in seen["requests"][2:])
    for policy in seen["policies"]:
        assert policy["investigation_max_output_tokens"] == work_cap
        assert policy["reserved_within_shared_limits"]["max_output_tokens"] == expected
        assert policy["total_model_time_ms"] == 900000
        assert policy["reserved_within_shared_limits"]["model_time_ms"] == 300000
        assert policy["total_provider_turns"] == 16
    assert now[0] - 1000 < 900
