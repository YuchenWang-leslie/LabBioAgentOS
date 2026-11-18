"""The original Agent handoff survives formatting, persistence and the next stage."""

from dataclasses import replace
import json
from uuid import UUID, uuid4

import pytest

from labbioagentos import WorkflowStage
from labbioagentos.runtime.contracts import (
    CapabilityEvidenceBundle, RuntimePriorResultView, RuntimeStageResult,
    RuntimeWorkflowControlView, runtime_stage_result_format,
)
from labbioagentos.runtime.context import attach_handoff_context, stage_context_projection
from labbioagentos.runtime.assembly import RuntimeStageAssemblySpec
from labbioagentos.runtime.native_stage import NativeKernelStageInvoker, NativeRuntimeFactory
from labbioagentos.kernel import FinishReason, ModelTurn, PublicMessage, TokenUsage, ToolCall
from labbioagentos.runtime.native_tools import NativeRuntimeCapabilityHost
from labbioagentos.runtime.observations import ObservationStore
from labbioagentos.runtime.tooling import LabBioRuntimeToolSet
from test_stage_decision_handoff import decision_case
from test_native_stage_application import WorkflowProvider, _capability_catalog, _stage_input, _body
from test_runtime_evidence_handoff import tools_with_store
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401
from test_execution_inspection import inspection, _submit  # noqa: F401
from test_runtime_milestone_b import boundary, _toolset as execution_tools  # noqa: F401


@pytest.mark.asyncio
async def test_original_intent_limits_and_selected_evidence_survive_finalization(tmp_path):
    stage, evidence, invoker, _calls, _tools = await decision_case(tmp_path)
    result = await invoker.finalize_recovered(stage, evidence)
    context = getattr(result, "handoff_context", None)
    assert context is not None, "Only finalizer prose is currently retained; original Agent handoff is lost"
    assert context.summary == evidence.prepared_handoff.summary
    assert context.limitations == evidence.prepared_handoff.limitations
    assert context.source_invocation_id == stage.invocation_id
    assert context.run_id == stage.run_id and context.stage_id == stage.stage_id
    assert [item.observation_id for item in context.observations] == [
        item.observation_id for item in evidence.prepared_handoff.observations]
    persisted = RuntimeStageResult.model_validate_json(result.model_dump_json())
    assert RuntimePriorResultView.from_result(persisted).handoff_context == context


@pytest.mark.asyncio
async def test_handoff_body_is_reattached_only_after_current_model_consumes_it(tmp_path):
    stage, evidence, invoker, _calls, tools = await decision_case(tmp_path)
    result = await invoker.finalize_recovered(stage, evidence)
    context = getattr(result, "handoff_context", None)
    assert context is not None, "The next stage has no original selection to read"
    services = replace(tools.services, observation_store=ObservationStore(tmp_path / "observations"))
    for observation in evidence.prepared_handoff.observations:
        item = next(item for item in evidence.items if item.capability_invocation_id == observation.observation_id)
        services.observation_store.save(tools.binding, item, observation.public_response)
    host = NativeRuntimeCapabilityHost(replace(tools.binding,
        invocation_id=uuid4(), stage_id=WorkflowStage.UNDERSTAND,
        capability_allowlist=(*tools.binding.capability_allowlist, "evidence_read"),
        workflow_control=None, decision_context_sha256=None), services)
    messages, unavailable = host.prepare_context_handoff(context)
    assert not unavailable
    identity = evidence.prepared_handoff.observations[0].observation_id
    assert identity not in host._host._delivered_observations
    assert evidence.prepared_handoff.observations[0].public_response == json.loads(messages[0].content)["observation"]
    host.mark_context_delivered(())
    assert identity not in host._host._delivered_observations
    host.mark_context_delivered(messages)
    assert identity in host._host._delivered_observations


@pytest.mark.asyncio
async def test_prior_model_result_is_readable_by_exact_id_not_silently_dropped(tmp_path):
    stage, evidence, invoker, _calls, tools = await decision_case(tmp_path)
    result = await invoker.finalize_recovered(stage, evidence)
    view = RuntimePriorResultView.from_result(result)
    reader = LabBioRuntimeToolSet(replace(tools.binding, invocation_id=uuid4(),
        capability_allowlist=(*tools.binding.capability_allowlist, "stage_context_read"),
        prior_results=(view,)), tools.services)
    page = await reader.stage_context_read(str(result.result_id))
    assert page["success"]
    assert page["information_authority"] == "MODEL_CONTEXT"
    assert json.loads(page["data"]["text"]) == view.model_dump(mode="json")
    denied = await reader.stage_context_read(str(uuid4()))
    assert not denied["success"]


def test_host_handoff_is_not_a_model_writable_stage_result_field():
    assert "handoff_context" not in runtime_stage_result_format(WorkflowStage.INTAKE).model_json_schema()["properties"]


@pytest.mark.asyncio
async def test_real_native_next_stage_uses_original_selection_without_requery(tmp_path):
    stage, evidence, invoker, _calls, tools = await decision_case(tmp_path)
    result = await invoker.finalize_recovered(stage, evidence)
    services = replace(tools.services, observation_store=ObservationStore(tmp_path / "observations"))
    original = evidence.prepared_handoff.observations[0]
    item = next(item for item in evidence.items if item.capability_invocation_id == original.observation_id)
    services.observation_store.save(tools.binding, item, original.public_response)
    stale = result.model_copy(update={"summary": "UNSUPPORTED_OLD_SUMMARY",
        "body": _body(stage.stage_id).model_copy(update={"interpreted_goal": "OLD_MODEL_PLAN"})})
    grants = ("artifact_list", "evidence_read", "stage_context_read", "prepare_handoff")
    next_input = stage.model_copy(update={"stage_id": WorkflowStage.UNDERSTAND,
        "invocation_id": uuid4(), "allowed_capabilities": grants,
        "prior_results": (RuntimePriorResultView.from_result(stale),),
        "workflow_control": RuntimeWorkflowControlView(current_stage=WorkflowStage.UNDERSTAND,
            transition_targets=(WorkflowStage.PLAN,), request_user_input_available=False,
            retry_available=False, finish_available=False)})
    action = {"action": "transition", "target_stage": "PLAN", "reason": "The Agent chose to continue."}
    calls = []

    class Provider(WorkflowProvider):
        turns = 0

        async def generate(self, request):
            if not request.tools:
                calls.append(request)
                return ModelTurn(content=json.dumps({"stage_id": "UNDERSTAND", "summary": "Current typed handoff.",
                    "body": _body(WorkflowStage.UNDERSTAND).model_dump(mode="json"), "next_action": action}),
                    finish_reason=FinishReason.STOP,
                    usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2))
            calls.append(request)
            self.turns += 1
            if self.turns == 1:
                text = "\n".join(message.content or "" for message in request.messages)
                assert "UNSUPPORTED_OLD_SUMMARY" not in text and "OLD_MODEL_PLAN" not in text
                carried = [json.loads(message.content)["observation"] for message in request.messages
                           if message.content and '"context_type":"CARRIED_OBSERVATION"' in message.content]
                assert carried == [original.public_response]
                return ModelTurn(tool_calls=(ToolCall(call_id="select-original", name="prepare_handoff",
                    arguments_json=json.dumps({"observation_ids": [str(original.observation_id)],
                        "summary": "Current Agent assessment.", "limitations": ["Synthetic fixture only."],
                        "next_action": action})),), finish_reason=FinishReason.TOOL_CALLS,
                    usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2))
            feedback = json.loads(request.messages[-1].content)["content"]
            assert feedback["success"] and feedback["data"]["status"] == "READY"
            return ModelTurn(content="The selected original and current limitations are handed off.",
                finish_reason=FinishReason.STOP, usage=TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2))

    catalog = _capability_catalog()
    catalog.capabilities["coordinator-capabilities"] = catalog.capabilities["coordinator-capabilities"].model_copy(
        update={"capability_allowlist": grants})
    provider = Provider(calls)
    following = NativeKernelStageInvoker(assembly=RuntimeStageAssemblySpec(stage_id=next_input.stage_id,
        root_profile_key="coordinator", prompt_template_key="runtime-generic", capability_allowlist=grants),
        factory=NativeRuntimeFactory(catalog, lambda _model: provider),
        principal=tools.binding.principal, workspace=tools.binding.workspace, services=services)
    completed = await following.invoke(next_input)
    assert completed.next_action.target_stage is WorkflowStage.PLAN
    assert completed.handoff_context.summary == "Current Agent assessment."
    assert completed.handoff_context.observations[0].observation_id == original.observation_id
    assert [call.name for request in calls for call in request.tools]  # Actual Native tool declarations.
    assert len([request for request in calls if request.tools]) == 2


def query_handoff(tools, response):
    stage = _stage_input().model_copy(update={"run_id": tools.binding.run_id, "stage_id": tools.binding.stage_id})
    result = RuntimeStageResult(stage_id=stage.stage_id, summary="Finalizer summary.", body=_body(stage.stage_id),
        next_action={"action": "finish"})
    bundle = CapabilityEvidenceBundle(run_id=stage.run_id, stage_id=stage.stage_id,
        invocation_id=stage.invocation_id, items=tools.evidence_items(), prepared_handoff=tools.prepared_handoff)
    return attach_handoff_context(result, stage, bundle).handoff_context


@pytest.mark.asyncio
async def test_redelivered_partial_table_has_only_actual_scope_and_current_credit(tmp_path, artifact_query_boundary):
    tools, ref = tools_with_store(tmp_path, artifact_query_boundary)
    response = await tools.artifact_query(str(ref.artifact_id), "TOP_N", 2)
    tools.mark_feedback_delivered((response,))
    await tools.prepare_handoff([response["capability_invocation_id"]], "Selected two rows only.")
    host = NativeRuntimeCapabilityHost(replace(tools.binding, invocation_id=uuid4()), tools.services)
    messages, unavailable = host.prepare_context_handoff(query_handoff(tools, response))
    assert not unavailable
    reference = [{"artifact_id": str(ref.artifact_id), "observation_ids": [response["capability_invocation_id"]]}]
    assert not (await host._host.report_check(evidence_references=reference))["data"]["ready"]
    host.mark_context_delivered((messages[0].model_copy(update={"content": messages[0].content + " "}),))
    assert UUID(response["capability_invocation_id"]) not in host._host._delivered_observations
    host.mark_context_delivered(messages)
    check = (await host._host.report_check(evidence_references=reference))["data"]
    assert check["ready"]
    receipt = check["items"][0]["read_receipts"][0]
    assert receipt["delivered_invocation_id"] == str(host._host.binding.invocation_id)
    assert receipt["scope"]["kind"] == "TABLE_PAGE"
    assert receipt["scope"]["offset"] == 0 and receipt["scope"]["end_offset"] == 2
    assert receipt["scope"]["record_count"] > 2


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["run", "user", "project", "capability"])
async def test_handoff_does_not_bypass_scope_or_read_grant(tmp_path, artifact_query_boundary, scope):
    tools, ref = tools_with_store(tmp_path, artifact_query_boundary)
    response = await tools.artifact_query(str(ref.artifact_id), "TOP_N", 2)
    tools.mark_feedback_delivered((response,))
    await tools.prepare_handoff([response["capability_invocation_id"]], "Original intent.")
    context = query_handoff(tools, response)
    binding = replace(tools.binding, invocation_id=uuid4())
    if scope == "run":
        binding = replace(binding, run_id=uuid4())
    elif scope == "user":
        binding = replace(binding, principal=binding.principal.model_copy(update={"user_id": "other"}))
    elif scope == "project":
        binding = replace(binding, workspace=binding.workspace.model_copy(update={"project_id": "other"}))
    else:
        binding = replace(binding, capability_allowlist=("stage_context_read",))
    host = NativeRuntimeCapabilityHost(binding, tools.services)
    if scope == "run":
        with pytest.raises(ValueError, match="another run"):
            host.prepare_context_handoff(context)
    else:
        messages, unavailable = host.prepare_context_handoff(context)
        assert not messages and len(unavailable) == 1
        assert unavailable[0]["read_credit"] is False
    assert not host._host._delivered_observations


@pytest.mark.asyncio
async def test_source_tampering_is_not_silently_replaced_by_a_summary(tmp_path, artifact_query_boundary):
    tools, ref = tools_with_store(tmp_path, artifact_query_boundary)
    response = await tools.artifact_query(str(ref.artifact_id), "TOP_N", 2)
    tools.mark_feedback_delivered((response,))
    await tools.prepare_handoff([response["capability_invocation_id"]], "Original intent.")
    context = query_handoff(tools, response)
    changed = context.model_copy(update={"observations": (context.observations[0].model_copy(update={"sha256": "0" * 64}),)})
    host = NativeRuntimeCapabilityHost(replace(tools.binding, invocation_id=uuid4()), tools.services)
    with pytest.raises(ValueError, match="saved identity"):
        host.prepare_context_handoff(changed)
    assert not host._host._delivered_observations


@pytest.mark.asyncio
async def test_projection_keeps_current_user_and_original_intent_but_archives_old_prose(tmp_path):
    stage, evidence, invoker, _calls, _tools = await decision_case(tmp_path)
    result = await invoker.finalize_recovered(stage, evidence)
    original = RuntimePriorResultView.from_result(result.model_copy(update={"summary": "OLD_UNVERIFIED_TEXT"}))
    stage = stage.model_copy(update={"instruction": "New explicit user constraint.", "prior_results": (original,),
        "allowed_capabilities": (*stage.allowed_capabilities, "stage_context_read")})
    before = stage.model_dump_json()
    view = stage_context_projection(stage)
    assert view["instruction"] == "New explicit user constraint."
    assert view["incoming_handoff"]["summary"] == evidence.prepared_handoff.summary
    assert view["incoming_handoff"]["next_action"] == evidence.prepared_handoff.stage_decision.next_action.model_dump(mode="json")
    assert "OLD_UNVERIFIED_TEXT" not in json.dumps(view)
    assert stage.model_dump_json() == before
    legacy = stage.model_copy(update={"allowed_capabilities": ("artifact_list",)})
    legacy_wire = legacy.model_dump(mode="json")
    for previous in legacy_wire["prior_results"]:
        previous.pop("handoff_context", None)
    assert stage_context_projection(legacy) == legacy_wire


@pytest.mark.asyncio
async def test_actual_execution_receipt_and_source_cross_stage_without_reexecution(tmp_path, inspection, boundary, monkeypatch):
    receipt = await _submit(inspection, boundary, "# original engineering fixture, not science\npass\n")
    service, executor, run_id = inspection
    original = execution_tools(boundary, WorkflowStage.EXECUTE,
        ("execution_inspect", "evidence_read", "prepare_handoff"), execution_submission=service)
    tools = LabBioRuntimeToolSet(replace(original.binding, run_id=run_id),
        replace(original.services, observation_store=ObservationStore(tmp_path / "observations")))
    response = await tools.execution_inspect(str(receipt.execution_id), source_limit=100)
    tools.mark_feedback_delivered((response,))
    await tools.prepare_handoff([response["capability_invocation_id"]], "Original program was inspected.")
    context = query_handoff(tools, response)
    monkeypatch.setattr(executor, "execute", lambda _plan: pytest.fail("Handoff replayed execution"))
    services = replace(tools.services, observation_store=ObservationStore(tools.services.observation_store.root))
    reader = NativeRuntimeCapabilityHost(replace(tools.binding,
        stage_id=WorkflowStage.REPORT, invocation_id=uuid4(),
        capability_allowlist=("execution_inspect", "evidence_read")), services)
    messages, unavailable = reader.prepare_context_handoff(context)
    assert not unavailable and len(messages) == 1
    assert json.loads(messages[0].content)["observation"] == response
    assert UUID(response["capability_invocation_id"]) not in reader._host._delivered_observations
    reader.mark_context_delivered(messages)
    assert UUID(response["capability_invocation_id"]) in reader._host._delivered_observations
    denied = NativeRuntimeCapabilityHost(replace(reader._host.binding,
        capability_allowlist=("evidence_read",)), services)
    assert denied.prepare_context_handoff(context)[0] == ()


@pytest.mark.asyncio
async def test_revoked_exposure_never_enters_the_incoming_context(tmp_path, artifact_query_boundary, monkeypatch):
    tools, ref = tools_with_store(tmp_path, artifact_query_boundary)
    response = await tools.artifact_query(str(ref.artifact_id), "TOP_N", 2)
    tools.mark_feedback_delivered((response,))
    await tools.prepare_handoff([response["capability_invocation_id"]], "Original intent.")
    context = query_handoff(tools, response)
    policy = tools.services.artifact_exposure.policy
    original_decide = policy.decide
    monkeypatch.setattr(type(policy), "decide", lambda self, *args: original_decide(*args).model_copy(update={"allowed": False}))
    reader = NativeRuntimeCapabilityHost(replace(tools.binding, invocation_id=uuid4()), tools.services)
    messages, unavailable = reader.prepare_context_handoff(context)
    assert not messages and unavailable[0]["status"] == "UNAVAILABLE_IN_CURRENT_SCOPE"
    assert not reader._host._delivered_observations


@pytest.mark.asyncio
async def test_finalizer_does_not_reintroduce_superseded_prior_agent_assessment(tmp_path):
    stage, evidence, invoker, _calls, _tools = await decision_case(tmp_path)
    previous = await invoker.finalize_recovered(stage, evidence)
    previous = previous.model_copy(update={"handoff_context": previous.handoff_context.model_copy(
        update={"summary": "SUPERSEDED_PRIOR_AGENT_ASSESSMENT"})})
    current = stage.model_copy(update={"allowed_capabilities": (*stage.allowed_capabilities, "stage_context_read"),
        "prior_results": (RuntimePriorResultView.from_result(previous),)})
    _effective, _payload, messages, control = invoker._finalization_envelope(current, evidence)
    assert "SUPERSEDED_PRIOR_AGENT_ASSESSMENT" not in control
    assert evidence.prepared_handoff.summary in "\n".join(message.content for message in messages)
