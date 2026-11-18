"""A READY handoff must remain satisfiable under current execution grounding."""
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import socket
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import TypeAdapter, ValidationError

import labbioagentos
from labbioagentos import (ExecutionRuntime, InMemoryTraceSink, RequestedResources,
    RunTraceRecorder, RuntimeExecutionCapabilityView, RuntimeWorkflowControlView, WorkflowStage)
from labbioagentos.contracts import InformationAuthority
from labbioagentos.execution.models import ExecutionStatus
from labbioagentos.kernel import FinishReason, ModelTurn, TokenUsage, ToolBatchRejectedError, ToolCall
from labbioagentos.runtime import native_stage
from labbioagentos.runtime.assembly import RuntimeAgentCapabilitySpec, RuntimeStageAssemblySpec
from labbioagentos.runtime.contracts import CapabilityEvidenceItem, CapabilityEvidenceStatus, runtime_stage_result_format
from labbioagentos.runtime.execution_grounding import execution_result_control, execution_result_response_format
from labbioagentos.runtime.handoff import decision_bound_response_format, stage_decision_control
from labbioagentos.runtime.native_stage import NativeKernelStageInvoker, NativeRuntimeFactory
from labbioagentos.runtime.native_tools import NativeRuntimeCapabilityHost, build_labbio_tool_registry
from labbioagentos.runtime.observations import ObservationStore
from test_execution_result_grounding import _receipt
from test_native_stage_application import WorkflowProvider, _capability_catalog, _stage_input, _toolset
from test_stage_decision_handoff import CONTINUE, decision_case


ADVANCE = {"action": "transition", "target_stage": "VALIDATE", "reason": "Agent-selected next stage."}
FAIL = {"action": "fail", "reason": "Agent-selected unresolved technical outcome."}
RETRY = {"action": "retry", "target_stage": "EXECUTE", "reason": "Agent-selected further work."}
CLARIFY = {"action": "request_clarification", "reason": None, "question": {
    "issue_key": "output-language", "prompt": "Which output language is required?",
    "why_needed": "A required user preference is missing.", "followup_to": None}}


@pytest.fixture(autouse=True)
def no_live_provider_and_explicit_consumer(monkeypatch):
    expected = os.environ.get("LABBIO_TEST_EXPECTED_PACKAGE_ROOT")
    if expected:
        assert Path(labbioagentos.__file__).resolve().parent == Path(expected).resolve()

    def forbidden(*_args, **_kwargs):
        pytest.fail("Handoff contract tests must not contact a provider or network")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


async def _case(tmp_path, monkeypatch, *, receipt_kind="failed", actions=None,
                minimum_outputs=1, peer_success=False, check_rejection_feedback=False,
                control_override=None):
    selected_input = uuid4()
    control = control_override or RuntimeWorkflowControlView(
        current_stage=WorkflowStage.EXECUTE, transition_targets=(WorkflowStage.VALIDATE,),
        retry_available=True, retry_transition_targets=(WorkflowStage.EXECUTE,),
        request_user_input_available=False, finish_available=False,
        clarification_available=True, clarification_rounds_remaining=3)
    capabilities = ("artifact_list", "execution_submit", "execution_inspect", "prepare_handoff")
    stage = _stage_input(WorkflowStage.EXECUTE).model_copy(update={
        "workflow_control": control, "allowed_capabilities": capabilities,
        "execution_capability": RuntimeExecutionCapabilityView(
            runtime=ExecutionRuntime.PYTHON, image_key="fixture-image",
            resources=RequestedResources(), network_required=False,
            minimum_queryable_output_count=minimum_outputs,
            mountable_input_artifact_ids=(selected_input,))})
    sink = InMemoryTraceSink()
    recorder = RunTraceRecorder(sink)
    fixture = _toolset(tmp_path, stage, recorder)
    current = _receipt(
        status=ExecutionStatus.SUCCEEDED if receipt_kind in ("success", "no_outputs", "no_inputs")
        else ExecutionStatus.FAILED,
        outputs=(uuid4(),) if receipt_kind in ("success", "no_inputs") else (),
        inputs=() if receipt_kind == "no_inputs" else (selected_input,))
    historical = _receipt(status=ExecutionStatus.SUCCEEDED, outputs=(uuid4(),), inputs=(selected_input,))
    calls = []

    async def submit(_draft, **_scope):
        calls.append("submit")
        return current

    def inspect(execution_id, **_scope):
        assert execution_id == historical.execution_id
        calls.append("inspect_prior")
        return {"execution_id": str(execution_id), "producer_run_id": str(stage.run_id),
            "receipt": historical.model_dump(mode="json"), "submitted_contract": None,
            "submitted_program": {"authority": "MODEL_CONTEXT", "script_hash": historical.script_hash,
                "source_offset": 0, "source_end": 5, "total_characters": 5, "complete": True,
                "diagnostic_line_offsets": [], "diagnostic_source_lines": [],
                "diagnostic_source_lines_truncated": False, "source": "pass\n"}}

    services = replace(fixture.services,
        execution_submission=SimpleNamespace(submit=submit, inspect=inspect),
        observation_store=ObservationStore(tmp_path / 'observations'))
    hosts = []
    real_host = NativeRuntimeCapabilityHost
    peer_receipt = _receipt(status=ExecutionStatus.SUCCEEDED,
        outputs=(uuid4(),), inputs=(selected_input,))
    peer_item = CapabilityEvidenceItem(
        capability_name="execution_submit", capability_invocation_id=uuid4(),
        actor_profile_key="peer", actor_agent_name="PeerAgent",
        information_authority=InformationAuthority.AUTHORITATIVE_EVIDENCE,
        status=CapabilityEvidenceStatus.COMPLETED,
        safe_result=peer_receipt.model_dump(mode="json"))

    def capture_host(binding, services):
        host = real_host(binding, services)
        hosts.append(host)
        if binding.actor_profile_key == "peer":
            # The fake evidence seam represents a completed current peer call.
            # No peer/scientific process or real provider is invoked.
            original = host.evidence_items
            host.evidence_items = lambda: (*original(), peer_item)
        return host
    monkeypatch.setattr(native_stage, "NativeRuntimeCapabilityHost", capture_host)
    actions = actions or [(FAIL, True)]
    steps = [("artifact_list", {})]
    if receipt_kind == "prior_inspection":
        steps.append(("execution_inspect", {"execution_id": str(historical.execution_id)}))
    if receipt_kind != "none" and not peer_success:
        steps.append(("execution_submit", {
            "image_key": "fixture-image", "script_content": "pass\n",
            "input_artifact_ids": [str(selected_input)],
            "requested_outputs": [{"relative_path": "result.json", "artifact_type": "flat-records",
                "requested_exposure": "DERIVED", "output_contract_id": "fixture-flat"}]}))
    steps.extend(("prepare_handoff", {"action": action, "accepted": accepted}) for action, accepted in actions)

    class Investigator:
        binding = WorkflowProvider.binding
        index = 0
        previous = None
        observation_id = None
        handoff_results = []

        async def generate(self, request):
            if self.previous is not None:
                outer = next(json.loads(m.content) for m in reversed(request.messages) if m.role == "TOOL")
                response = outer["content"]
                if self.previous[0] == "artifact_list":
                    self.observation_id = response["capability_invocation_id"]
                if self.previous[0] == "prepare_handoff":
                    expected = self.previous[1]["accepted"]
                    self.handoff_results.append(
                        (response, expected, hosts[0].prepared_handoff is None))
                    if not expected and check_rejection_feedback:
                        control = (response.get('data') or {}).get('handoff_action_control')
                        self.current_action_feedback = control
                        # The scripted Agent consumes feedback before proposing its
                        # own next action; the host does not select that action.
                        if control is not None and control.get('schema_status', 'AVAILABLE') == 'AVAILABLE':
                            following = steps[self.index][1]['action']
                            assert control['action_schema_encoding'] == 'JSON'
                            schema = json.loads(control['action_schema'])
                            assert following['action'] in schema['discriminator']['mapping']
            if self.index == len(steps):
                return ModelTurn(content="Agent-selected handoff complete.",
                    finish_reason=FinishReason.STOP, usage=TokenUsage(total_tokens=1))
            step = steps[self.index]
            self.index += 1
            self.previous = step
            name, arguments = step
            if name == "prepare_handoff":
                arguments = {"observation_ids": [self.observation_id],
                    "summary": "Bounded synthetic decision.", "next_action": arguments["action"]}
            return ModelTurn(tool_calls=(ToolCall(call_id=f"step-{self.index}", name=name,
                arguments_json=json.dumps(arguments)),), finish_reason=FinishReason.TOOL_CALLS,
                usage=TokenUsage(total_tokens=1))

    investigator = Investigator()
    built = []

    def provider(_model):
        item = investigator if not built else WorkflowProvider([])
        built.append(item)
        return item

    catalog = _capability_catalog()
    catalog.capabilities["coordinator-capabilities"] = catalog.capabilities[
        "coordinator-capabilities"].model_copy(update={"capability_allowlist": capabilities})
    peers = ()
    if peer_success:
        catalog.agents["peer"] = catalog.agents["coordinator"].model_copy(update={
            "profile_key": "peer", "agent_name": "PeerAgent",
            "capability_profile_key": "peer-capabilities"})
        catalog.capabilities["peer-capabilities"] = catalog.capabilities[
            "coordinator-capabilities"].model_copy(update={
                "profile_key": "peer-capabilities", "capability_allowlist": ("execution_submit",)})
        peers = (RuntimeAgentCapabilitySpec(profile_key="peer",
            capability_allowlist=("execution_submit",)),)
    assembly = RuntimeStageAssemblySpec(stage_id=stage.stage_id,
        root_profile_key="coordinator", prompt_template_key="runtime-generic",
        capability_allowlist=capabilities, capability_peer_specs=peers,
        max_capability_turns=16)
    factory = NativeRuntimeFactory(catalog, provider)
    invoker = NativeKernelStageInvoker(assembly=assembly, factory=factory,
        principal=fixture.binding.principal, workspace=fixture.binding.workspace,
        services=services, execution_capability=stage.execution_capability)
    evidence = await invoker._run_capability(stage,
        resolve_finalization=lambda: factory.resolve("coordinator", None))
    investigator.trace_events = sink.read(stage.run_id)
    investigator.observation_store = services.observation_store
    investigator.host_binding = hosts[0]._host.binding
    assert len(investigator.handoff_results) == len(actions)
    for response, expected, cleared in investigator.handoff_results:
        assert response["success"] is expected, (
            "prepare_handoff admitted an action the current execution guard cannot format", response)
        if expected:
            assert response["data"]["status"] == "READY"
        else:
            assert cleared, "An invoked invalid replacement retained the old handoff"
    expected_action = evidence.prepared_handoff.stage_decision.next_action.model_dump(mode="json")
    grounded = execution_result_response_format(
        runtime_stage_result_format(stage.stage_id, stage.workflow_control),
        execution_result_control(stage, evidence))
    bound = decision_bound_response_format(grounded, stage_decision_control(stage, evidence))
    assert TypeAdapter(bound.model_fields["next_action"].rebuild_annotation()).validate_python(
        expected_action).model_dump(mode="json") == expected_action
    return stage, evidence, investigator, calls


@pytest.mark.asyncio
@pytest.mark.parametrize('receipt_kind', ['none', 'failed', 'no_outputs', 'no_inputs', 'prior_inspection'])
async def test_current_action_feedback_reaches_agent_and_preserves_failed_evidence(
        tmp_path, monkeypatch, receipt_kind):
    stage, evidence, agent, _calls = await _case(tmp_path, monkeypatch,
        receipt_kind=receipt_kind, actions=[(ADVANCE, False), (RETRY, True)],
        check_rejection_feedback=True)
    response = agent.handoff_results[0][0]
    assert not response['success']
    assert response['information_authority'] == 'CONTROL_STATE'
    assert response['error']['error_code'] == 'HANDOFF_ACTION_NOT_CURRENT'
    assert response['error']['retryable'] is False
    current = execution_result_response_format(
        runtime_stage_result_format(stage.stage_id, stage.workflow_control),
        execution_result_control(stage, evidence)).model_fields['next_action'].annotation
    control = response['data']['handoff_action_control']
    assert control['authority'] == 'CONTROL_STATE'
    assert control['field'] == 'next_action'
    assert agent.current_action_feedback == control
    assert control['action_schema_encoding'] == 'JSON'
    assert control['schema_status'] == 'AVAILABLE'
    assert json.loads(control['action_schema']) == current.model_json_schema()
    assert control['action_schema'] == json.dumps(
        current.model_json_schema(), sort_keys=True, separators=(',', ':'), ensure_ascii=False)
    assert control['action_schema_characters'] == len(control['action_schema'])
    assert control['action_schema_sha256'] == sha256(control['action_schema'].encode()).hexdigest()
    with pytest.raises(ValidationError):
        current.model_validate_json(json.dumps(ADVANCE))
    assert current.model_validate_json(json.dumps(RETRY)).model_dump(mode='json') == RETRY
    failed = next(item for item in evidence.items
        if str(item.capability_invocation_id) == response['capability_invocation_id'])
    assert failed.status is CapabilityEvidenceStatus.FAILED
    assert failed.error_code == 'HANDOFF_ACTION_NOT_CURRENT'
    assert failed.safe_result == response['data']
    archived = agent.observation_store.load(agent.host_binding, UUID(response['capability_invocation_id']))
    assert archived.item == failed
    assert archived.public_response == response
    event = next(item for item in agent.trace_events
        if item.event_type.value == 'CAPABILITY_FAILED'
        and item.payload.get('capability_invocation_id') == response['capability_invocation_id'])
    assert event.payload['correlation_id'] == response['error']['correlation_id']
    assert event.payload['handoff_action_control'] == control
    # The common helper validates the exact decision-bound final action schema.
    assert evidence.prepared_handoff.stage_decision.next_action.model_dump(mode='json') == RETRY


@pytest.mark.asyncio
@pytest.mark.parametrize('oversized', [False, True], ids=['ordinary_followups', 'large_valid_followups'])
async def test_valid_recovered_followups_preserve_rejection_when_schema_is_too_large(
        tmp_path, monkeypatch, oversized):
    from labbioagentos import runtime_workflow_definition
    from labbioagentos.contracts import (
        ClarificationQuestion, ClarificationRecord, WorkflowDefinition, WorkflowRun, WorkflowTransition)

    # Legal recovered/custom state, projected through the real control compiler;
    # no oversized mock schema or changed field/string limits.
    records = tuple(ClarificationRecord(
        question_id=(chr(1) * 255 + str(i)) if oversized else ('clarification-' + str(uuid4())),
        source_stage=WorkflowStage.EXECUTE,
        question=ClarificationQuestion(issue_key=f'choice{i}', prompt='Which value?',
            why_needed='A user-owned fact is missing.'),
        status='ANSWERED', answer_text='Provided.', answered_by='fixture-user') for i in range(2))
    run = WorkflowRun(current_stage=WorkflowStage.EXECUTE, retry_limit=1, clarifications=records)
    base = runtime_workflow_definition()
    definition = WorkflowDefinition(workflow_id=base.workflow_id, nodes=base.nodes,
        initial_stage=base.initial_stage, terminal_stages=base.terminal_stages,
        allowed_transitions=base.allowed_transitions | {
            WorkflowTransition(source=WorkflowStage.EXECUTE, target=WorkflowStage.USER_GATE)})
    control = RuntimeWorkflowControlView.from_run(definition, run)
    assert control.clarification_followup_ids == tuple(item.question_id for item in records)
    stage, evidence, agent, _calls = await _case(tmp_path, monkeypatch,
        actions=[(ADVANCE, False), (RETRY, True)], control_override=control,
        check_rejection_feedback=True)
    response = agent.handoff_results[0][0]
    assert response['error']['error_code'] == 'HANDOFF_ACTION_NOT_CURRENT'
    assert not response['success'] and response['error']['retryable'] is False
    detail = response['data']['handoff_action_control']
    current = execution_result_response_format(
        runtime_stage_result_format(stage.stage_id, stage.workflow_control),
        execution_result_control(stage, evidence)).model_fields['next_action'].annotation
    canonical = json.dumps(current.model_json_schema(), sort_keys=True,
        separators=(',', ':'), ensure_ascii=False)
    assert detail['authority'] == 'CONTROL_STATE' and detail['field'] == 'next_action'
    assert detail['action_schema_characters'] == len(canonical)
    assert detail['action_schema_sha256'] == sha256(canonical.encode()).hexdigest()
    if oversized:
        assert len(canonical) > 8_000
        assert detail['schema_status'] == 'WITHHELD_TOO_LARGE'
        assert detail['action_schema'] is None
        assert 'withheld' in response['error']['safe_message'].lower()
        assert chr(1) not in json.dumps(response)
    else:
        assert len(canonical) <= 8_000
        assert detail['schema_status'] == 'AVAILABLE'
        assert detail['action_schema'] == canonical
        assert json.loads(detail['action_schema']) == current.model_json_schema()
    failed = next(item for item in evidence.items
        if str(item.capability_invocation_id) == response['capability_invocation_id'])
    assert failed.status is CapabilityEvidenceStatus.FAILED and failed.safe_result == response['data']
    archived = agent.observation_store.load(agent.host_binding, UUID(response['capability_invocation_id']))
    assert archived.public_response == response
    event = next(item for item in agent.trace_events if item.event_type.value == 'CAPABILITY_FAILED'
        and item.payload.get('capability_invocation_id') == response['capability_invocation_id'])
    assert event.payload['handoff_action_control'] == detail
    assert evidence.prepared_handoff.stage_decision.next_action.model_dump(mode='json') == RETRY


@pytest.mark.asyncio
async def test_action_rejection_does_not_publish_rejected_reason_or_validation_text(tmp_path, monkeypatch):
    marker = 'CALLER_ONLY_UNTRUSTED_REASON_7c66f3'
    _stage, evidence, agent, _calls = await _case(tmp_path, monkeypatch,
        actions=[({**ADVANCE, 'reason': marker}, False), (RETRY, True)],
        check_rejection_feedback=True)
    response = agent.handoff_results[0][0]
    assert response['error']['error_code'] == 'HANDOFF_ACTION_NOT_CURRENT'
    failed_events = [item.model_dump(mode='json') for item in agent.trace_events
        if item.event_type.value == 'CAPABILITY_FAILED']
    failed_items = [item.model_dump(mode='json') for item in evidence.items
        if item.status is CapabilityEvidenceStatus.FAILED]
    public = json.dumps([response, failed_events, failed_items])
    for forbidden in (marker, 'union_tag_invalid', 'Input tag', 'ValidationError'):
        assert forbidden not in public
    assert 'next_action' in response['data']['handoff_action_control']['field']


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt_kind", ["none", "failed", "no_outputs", "no_inputs", "prior_inspection"])
async def test_handoff_rejects_advance_without_qualifying_current_execution(
        tmp_path, monkeypatch, receipt_kind):
    await _case(tmp_path, monkeypatch, receipt_kind=receipt_kind,
        actions=[(ADVANCE, False), (FAIL, True)])


@pytest.mark.asyncio
@pytest.mark.parametrize("action", [FAIL, RETRY, CLARIFY], ids=["fail", "retry", "clarification"])
async def test_handoff_preserves_legal_agent_actions_after_failed_execution(
        tmp_path, monkeypatch, action):
    await _case(tmp_path, monkeypatch, actions=[(action, True)])


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["current", "peer", "zero_minimum"])
async def test_handoff_allows_qualified_current_peer_and_zero_minimum_advancement(
        tmp_path, monkeypatch, source):
    await _case(tmp_path, monkeypatch,
        receipt_kind="none" if source in ("peer", "zero_minimum") else "success",
        minimum_outputs=0 if source == "zero_minimum" else 1,
        peer_success=source == "peer", actions=[(ADVANCE, True)])


@pytest.mark.asyncio
async def test_invoked_execution_invalid_replacement_clears_prior_selection(tmp_path, monkeypatch):
    await _case(tmp_path, monkeypatch, actions=[(FAIL, True), (ADVANCE, False), (FAIL, True)])


@pytest.mark.asyncio
@pytest.mark.parametrize("after_gate", [False, True])
async def test_nonexecution_handoff_and_legitimate_gate_rules_remain_unchanged(tmp_path, after_gate):
    _stage, evidence, _invoker, _calls, _tools = await decision_case(
        tmp_path, CONTINUE, after_gate=after_gate)
    assert evidence.prepared_handoff.stage_decision.next_action.target_stage is WorkflowStage.UNDERSTAND


@pytest.mark.asyncio
async def test_argument_schema_rejection_preserves_existing_selection(tmp_path):
    _stage, _evidence, _invoker, _calls, tools = await decision_case(tmp_path)
    host = NativeRuntimeCapabilityHost(tools.binding, tools.services)
    host._host.prepared_handoff = tools.prepared_handoff
    before = host.prepared_handoff.model_dump_json()
    registry = build_labbio_tool_registry(host)
    with pytest.raises(ToolBatchRejectedError):
        registry.validate_batch((ToolCall(call_id="invalid-action", name="prepare_handoff",
            arguments_json=json.dumps({"observation_ids": [str(uuid4())],
                "summary": "Invalid schema must never invoke a replacement.",
                "next_action": "transition"})),),
            finish_reason=FinishReason.TOOL_CALLS, max_calls=1)
    assert host.prepared_handoff.model_dump_json() == before
