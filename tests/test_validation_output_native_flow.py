"""Current-output read admission traverses real Native tools and finalization.

Scripted Agent fixtures check the contract, not a live model's scientific judgment.
"""

from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import socket
from types import SimpleNamespace
from uuid import UUID, uuid4

import jsonschema
import pytest

import labbioagentos
from labbioagentos import (
    ArtifactExposureClass, ArtifactReleaseBasis, ArtifactRepresentation,
    ExecutionRuntime, RequestedResources, RuntimeExecutionCapabilityView,
    RuntimePriorResultView, ValidateStageBody, WorkflowStage,
)
from labbioagentos.execution.models import ExecutionStatus
from labbioagentos.kernel import FinishReason, ModelTurn, TokenUsage, ToolCall
from labbioagentos.runtime import native_stage
from labbioagentos.runtime.assembly import RuntimeStageAssemblySpec
from labbioagentos.runtime.contracts import (
    CapabilityEvidenceStatus, RuntimeExecutionActivity, RuntimeExecutionActivityReceipt,
    RuntimeHandoffContext, RuntimeHandoffObservationReference, RuntimeWorkflowControlView,
)
from labbioagentos.runtime.native_stage import NativeKernelStageInvoker, NativeRuntimeFactory
from labbioagentos.runtime.native_tools import NativeRuntimeCapabilityHost
from labbioagentos.runtime.observations import ObservationStore, canonical_bytes
from labbioagentos.runtime.tooling import LabBioRuntimeToolSet
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401
from test_execution_result_grounding import _receipt
from test_native_stage_application import WorkflowProvider, _capability_catalog, _stage_input


FORWARD = {"action": "transition", "target_stage": "INTERPRET",
           "reason": "Agent selected forward review."}
REWORK = {"action": "transition", "target_stage": "EXECUTE",
          "reason": "Agent selected further execution work."}
FAIL = {"action": "fail", "reason": "Agent selected an unresolved outcome."}
RETRY_FORWARD = {"action": "retry", "target_stage": "INTERPRET",
                 "reason": "Agent selected a retry followed by forwarding."}
RETRY_CURRENT = {"action": "retry", "target_stage": "VALIDATE",
                 "reason": "Agent selected another validation attempt."}
RETRY_REWORK = {"action": "retry", "target_stage": "EXECUTE",
                "reason": "Agent selected retry with further execution work."}


@pytest.fixture(autouse=True)
def offline_and_explicit_package(monkeypatch):
    expected = os.environ.get("LABBIO_TEST_EXPECTED_PACKAGE_ROOT")
    if expected:
        assert Path(labbioagentos.__file__).resolve().parent == Path(expected).resolve()

    def forbidden(*_args, **_kwargs):
        pytest.fail("Validation contract fixtures must not use a network/provider")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


async def _native_case(tmp_path, monkeypatch, boundary, *, mode, final_action=FORWARD,
                       forward_action=FORWARD):
    sink, binding, old_ref, original = boundary
    current_ref = original.services.artifact_store.register(
        artifact_type="synthetic-current-output",
        exposure_class=ArtifactExposureClass.DERIVED,
        release_basis=ArtifactReleaseBasis.TRUSTED_EXECUTION_DECLASSIFICATION,
        representation=ArtifactRepresentation(summary={"record_count": 2},
            records=({"item": "current-B", "value": 0}, {"item": "current-B", "value": 3}),
            record_count=2),
        owner_user_id=binding.principal.user_id, project_id=binding.workspace.project_id,
        lab_id=binding.workspace.lab_id, run_id=binding.run_id, stage_id=WorkflowStage.EXECUTE,
        producer_invocation_id=uuid4(),
    )
    current = _receipt(status=ExecutionStatus.SUCCEEDED, outputs=(current_ref.artifact_id,))
    prior = _receipt(status=ExecutionStatus.SUCCEEDED, outputs=(old_ref.artifact_id,))
    inspect_calls = []

    def inspect(execution_id, **scope):
        assert scope["run_id"] == binding.run_id
        assert execution_id in {current.execution_id, prior.execution_id}
        inspect_calls.append(execution_id)
        receipt = current if execution_id == current.execution_id else prior
        return {"execution_id": str(execution_id), "producer_run_id": str(binding.run_id),
            "receipt": receipt.model_dump(mode="json"), "submitted_contract": None,
            "submitted_program": {"authority": "MODEL_CONTEXT", "script_hash": receipt.script_hash,
                "source_offset": 0, "source_end": 5, "total_characters": 5, "complete": True,
                "diagnostic_line_offsets": [], "diagnostic_source_lines": [],
                "diagnostic_source_lines_truncated": False, "source": "pass\n"}}

    services = replace(original.services, execution_submission=SimpleNamespace(inspect=inspect),
        observation_store=ObservationStore(tmp_path / "observations"))
    grants = ("artifact_query", "execution_inspect", "evidence_read", "stage_context_read",
              "prepare_handoff")
    control = RuntimeWorkflowControlView(current_stage=WorkflowStage.VALIDATE,
        transition_targets=(WorkflowStage.INTERPRET, WorkflowStage.EXECUTE),
        request_user_input_available=False, retry_available=True,
        retry_transition_targets=(WorkflowStage.VALIDATE, WorkflowStage.EXECUTE, WorkflowStage.INTERPRET),
        finish_available=False)
    stage = _stage_input(WorkflowStage.VALIDATE).model_copy(update={
        "run_id": binding.run_id, "invocation_id": binding.invocation_id,
        "workspace": binding.workspace, "allowed_capabilities": grants,
        "workflow_control": control,
        "execution_capability": RuntimeExecutionCapabilityView(runtime=ExecutionRuntime.PYTHON,
            image_key="fixture-image", resources=RequestedResources(), network_required=False,
            minimum_queryable_output_count=1),
        "last_execution_activity": RuntimeExecutionActivity(run_id=binding.run_id,
            invocation_id=uuid4(), evidence_id=uuid4(), termination_reason="MODEL_RETURNED",
            receipts=(RuntimeExecutionActivityReceipt(execution_id=current.execution_id,
                status=ExecutionStatus.SUCCEEDED),)),
    })
    carried = None
    if mode == "carried_current":
        source = LabBioRuntimeToolSet(replace(binding, stage_id=WorkflowStage.EXECUTE,
            invocation_id=uuid4(), capability_allowlist=("artifact_query",)), services)
        carried = await source.artifact_query(str(current_ref.artifact_id), "TOP_N", 2)
        assert carried["success"]
        # It is prior delivery, not current credit; the real Native context path
        # must reauthorize and confirm the exact carried public body again.
        source.mark_feedback_delivered((carried,))
        context = RuntimeHandoffContext(run_id=binding.run_id, stage_id=WorkflowStage.EXECUTE,
            source_invocation_id=source.binding.invocation_id, summary="Prior Agent context only.",
            termination_reason="MODEL_RETURNED", observations=(RuntimeHandoffObservationReference(
                observation_id=UUID(carried["capability_invocation_id"]),
                sha256=sha256(canonical_bytes(carried)).hexdigest()),))
        stage = stage.model_copy(update={"prior_results": (RuntimePriorResultView(
            result_id=uuid4(), stage_id=WorkflowStage.EXECUTE, body_kind="EXECUTE",
            model_summary="Old prose is not current content authority.",
            model_body={"kind": "EXECUTE", "execution_status": "SUCCEEDED"},
            handoff_context=context),)})

    hosts = []
    def capture_host(context, shared):
        host = NativeRuntimeCapabilityHost(context, shared)
        hosts.append(host)
        return host
    monkeypatch.setattr(native_stage, "NativeRuntimeCapabilityHost", capture_host)

    # Each list entry is one actual provider turn/tool batch. The scripted
    # Agent chooses every action; no callback chooses a replacement for it.
    steps = []
    if mode == "historical_then_current":
        steps += [[("artifact_query", {"artifact_id": str(old_ref.artifact_id), "view_type": "TOP_N", "limit": 2})],
                  [("execution_inspect", {"execution_id": str(prior.execution_id)})]]
    steps += [[("execution_inspect", {"execution_id": str(current.execution_id)})]]
    if mode == "metadata_only":
        steps += [[("artifact_query", {"artifact_id": str(current_ref.artifact_id), "view_type": "METADATA"})]]
    if mode == "same_batch_current":
        steps += [[("artifact_query", {"artifact_id": str(current_ref.artifact_id), "view_type": "TOP_N", "limit": 2}),
                   ("prepare_handoff", {"action": forward_action, "accepted": False})]]
        steps += [[("prepare_handoff", {"action": FORWARD, "accepted": True})]]
    elif mode in {"historical_then_current", "metadata_only"}:
        steps += [[("prepare_handoff", {"action": forward_action, "accepted": False})]]
        if mode == "historical_then_current":
            steps += [[("artifact_query", {"artifact_id": str(current_ref.artifact_id), "view_type": "TOP_N", "limit": 2})]]
        steps += [[("prepare_handoff", {"action": final_action, "accepted": True})]]
    else:
        steps += [[("prepare_handoff", {"action": final_action, "accepted": True})]]

    class Agent:
        binding = WorkflowProvider.binding
        index = 0
        previous = None
        prepared_responses = []
        responses = []
        requests = []
        finalizer_requests = []
        selected = []

        async def generate(self, request):
            try:
                return await self._respond(request)
            except Exception as exc:
                pytest.fail(f"Scripted Native assertion failed: {type(exc).__name__}: {exc}")

        async def _respond(self, request):
            if not request.tools:
                self.finalizer_requests.append(request)
                payload = json.loads(request.messages[-1].content)
                assert payload["stage_decision_control"]["next_action"] == final_action
                outcome = {"stage_id": "VALIDATE", "summary": "Scripted generic review.",
                    "body": ValidateStageBody(technical_status="FIXTURE_REVIEW",
                        runtime_assessment="Agent-selected review outcome, not scientific approval.").model_dump(mode="json"),
                    "next_action": final_action}
                jsonschema.validate(outcome, request.response_schema)
                changed = {**outcome, "next_action": FAIL if final_action != FAIL else FORWARD}
                with pytest.raises(jsonschema.ValidationError):
                    jsonschema.validate(changed, request.response_schema)
                return ModelTurn(content=json.dumps(outcome), finish_reason=FinishReason.STOP,
                    usage=TokenUsage(total_tokens=1))
            self.requests.append(request)
            if self.previous is not None:
                messages = [m for m in request.messages if m.role == "TOOL"][-len(self.previous):]
                for (name, expected), message in zip(self.previous, messages, strict=True):
                    outer = json.loads(message.content)
                    response = outer["content"]
                    assert response is not None, outer
                    self.responses.append((name, response))
                    if name == "prepare_handoff":
                        self.prepared_responses.append(response)
                        assert response["success"] is expected["accepted"], response
                        if not expected["accepted"]:
                            assert response["error"]["error_code"] == "HANDOFF_ACTION_NOT_CURRENT"
                            assert response["error"]["retryable"] is False
                            action_control = response["data"]["handoff_action_control"]
                            assert action_control["field"] == "next_action"
                            assert action_control["schema_status"] == "AVAILABLE"
                            assert action_control["action_schema_encoding"] == "JSON"
                            schema = json.loads(action_control["action_schema"])
                            with pytest.raises(jsonschema.ValidationError):
                                jsonschema.validate(FORWARD, schema)
                            with pytest.raises(jsonschema.ValidationError):
                                jsonschema.validate(forward_action, schema)
                            jsonschema.validate(REWORK, schema)
                            jsonschema.validate(FAIL, schema)
                            assert hosts[0].prepared_handoff is None
                        else:
                            assert response["data"]["status"] == "READY"
                    else:
                        assert response["success"], response
                        self.selected.append(response["capability_invocation_id"])
            if self.index == 0 and carried is not None:
                bodies = [json.loads(m.content)["observation"] for m in request.messages
                    if m.content and '"context_type":"CARRIED_OBSERVATION"' in m.content]
                assert bodies == [carried]
                assert UUID(carried["capability_invocation_id"]) not in hosts[0]._host._delivered_observations
                self.selected.append(carried["capability_invocation_id"])
            if self.index == len(steps):
                return ModelTurn(content="Own review action prepared.", finish_reason=FinishReason.STOP,
                    usage=TokenUsage(total_tokens=1))
            step = steps[self.index]
            self.index += 1
            self.previous = step
            calls = []
            for position, (name, arguments) in enumerate(step):
                if name == "prepare_handoff":
                    arguments = {"observation_ids": self.selected,
                        "summary": "Agent-selected generic review; exact observations retained.",
                        "next_action": arguments["action"]}
                calls.append(ToolCall(call_id=f"turn-{self.index}-{position}", name=name,
                    arguments_json=json.dumps(arguments)))
            return ModelTurn(tool_calls=tuple(calls), finish_reason=FinishReason.TOOL_CALLS,
                usage=TokenUsage(total_tokens=1))

    agent = Agent()
    catalog = _capability_catalog()
    catalog.capabilities["coordinator-capabilities"] = catalog.capabilities[
        "coordinator-capabilities"].model_copy(update={"capability_allowlist": grants})
    boundaries = []
    invoker = NativeKernelStageInvoker(assembly=RuntimeStageAssemblySpec(
        stage_id=WorkflowStage.VALIDATE, root_profile_key="coordinator",
        prompt_template_key="runtime-generic", capability_allowlist=grants,
        max_capability_turns=16),
        factory=NativeRuntimeFactory(catalog, lambda _model: agent),
        principal=binding.principal, workspace=binding.workspace, services=services,
        execution_capability=stage.execution_capability,
        boundary_observer=lambda kind, value: boundaries.append((kind, value)))
    result = await invoker.invoke(stage)
    assert result.next_action.model_dump(mode="json") == final_action
    assert len(agent.finalizer_requests) == 1
    evidence, = [value for kind, value in boundaries if kind == "capability_evidence"]
    assert evidence.prepared_handoff.stage_decision.next_action.model_dump(mode="json") == final_action
    for response in agent.prepared_responses:
        if response["success"]:
            continue
        identity = UUID(response["capability_invocation_id"])
        failed = next(item for item in evidence.items if item.capability_invocation_id == identity)
        assert failed.status is CapabilityEvidenceStatus.FAILED
        assert failed.error_code == "HANDOFF_ACTION_NOT_CURRENT"
        archived = services.observation_store.load(hosts[0]._host.binding, identity)
        assert archived.item == failed and archived.public_response == response
        assert failed.safe_result == response["data"]
    return stage, evidence, agent, current_ref, carried, inspect_calls


@pytest.mark.asyncio
async def test_old_content_source_and_receipt_do_not_authorize_forward_until_current_content_consumed(
    tmp_path, monkeypatch, artifact_query_boundary,
):
    stage, evidence, agent, ref, _carried, _calls = await _native_case(
        tmp_path, monkeypatch, artifact_query_boundary, mode="historical_then_current")
    assert [r["success"] for r in agent.prepared_responses] == [False, True]
    current = next(response for name, response in agent.responses if name == "artifact_query"
        and response["data"].get("artifact_id") == str(ref.artifact_id))
    assert current["data"]["records"] == [{"item": "current-B", "value": 0}, {"item": "current-B", "value": 3}]
    selected = {str(item.observation_id) for item in evidence.prepared_handoff.observations}
    assert current["capability_invocation_id"] in selected
    assert current["artifact_read_receipt"]["delivered_invocation_id"] == str(stage.invocation_id)


@pytest.mark.asyncio
async def test_metadata_only_does_not_authorize_forward_but_agent_can_fail(
    tmp_path, monkeypatch, artifact_query_boundary,
):
    _stage, _evidence, agent, _ref, _carried, _calls = await _native_case(
        tmp_path, monkeypatch, artifact_query_boundary, mode="metadata_only", final_action=FAIL)
    assert [r["success"] for r in agent.prepared_responses] == [False, True]


@pytest.mark.asyncio
async def test_same_batch_query_has_no_credit_until_the_agent_consumes_feedback(
    tmp_path, monkeypatch, artifact_query_boundary,
):
    _stage, _evidence, agent, _ref, _carried, _calls = await _native_case(
        tmp_path, monkeypatch, artifact_query_boundary, mode="same_batch_current")
    assert [r["success"] for r in agent.prepared_responses] == [False, True]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", [REWORK, FAIL, RETRY_CURRENT, RETRY_REWORK])
async def test_agent_owns_rework_fail_or_nonforward_retry_without_reading_current_output(
    tmp_path, monkeypatch, artifact_query_boundary, action,
):
    _stage, _evidence, agent, _ref, _carried, _calls = await _native_case(
        tmp_path, monkeypatch, artifact_query_boundary, mode="no_current_read", final_action=action)
    assert len(agent.prepared_responses) == 1
    assert not any(name == "artifact_query" for name, _response in agent.responses)


@pytest.mark.asyncio
async def test_retry_cannot_forward_without_current_content_but_remains_available_after_read(
    tmp_path, monkeypatch, artifact_query_boundary,
):
    _stage, _evidence, agent, _ref, _carried, _calls = await _native_case(
        tmp_path, monkeypatch, artifact_query_boundary, mode="historical_then_current",
        forward_action=RETRY_FORWARD, final_action=RETRY_FORWARD)
    assert [r["success"] for r in agent.prepared_responses] == [False, True]


@pytest.mark.asyncio
async def test_exact_carried_current_read_is_reauthorized_and_consumed_before_forward(
    tmp_path, monkeypatch, artifact_query_boundary,
):
    stage, evidence, agent, ref, carried, _calls = await _native_case(
        tmp_path, monkeypatch, artifact_query_boundary, mode="carried_current")
    assert carried["artifact_read_receipt"]["source_invocation_id"] != str(stage.invocation_id)
    assert not any(name == "artifact_query" for name, _response in agent.responses)
    payload = json.loads(agent.finalizer_requests[0].messages[-1].content)
    control = payload.get("validation_result_control")
    assert control is not None, "Finalization must retain the same current-output read control"
    assert control["satisfied"] is True
    assert control["current_queryable_output_ids"] == [str(ref.artifact_id)]
    assert control["delivered_current_content_artifact_ids"] == [str(ref.artifact_id)]
    reads = evidence.model_dump(mode="json").get("validated_artifact_reads", [])
    assert any(item["artifact_id"] == str(ref.artifact_id)
        and item["source_invocation_id"] == carried["artifact_read_receipt"]["source_invocation_id"]
        and item["delivered_invocation_id"] == str(stage.invocation_id) for item in reads)
