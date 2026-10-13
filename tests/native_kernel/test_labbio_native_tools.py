"""P2 acceptance for LabBio capability registration, authority, and evidence."""

import json
from uuid import UUID, uuid4

import pytest

from labbioagentos import (
    ExecutionRuntime, RequestedResources, RuntimeExecutionCapabilityView,
)
from labbioagentos.contracts import InformationAuthority, WorkflowStage
from labbioagentos.kernel import FinishReason, ToolBatchRejectedError, ToolCall
from labbioagentos.runtime.capability_catalog import (
    ALL_CAPABILITIES,
    CAPABILITY_CEILINGS,
    CAPABILITY_INFORMATION_AUTHORITY,
)
from labbioagentos.runtime.native_tools import (
    ArtifactAggregateArguments,
    ExecutionSubmitArguments,
    NativeCapabilityBinding,
    NativeCapabilityDispatcher,
    NativeRuntimeCapabilityHost,
    build_labbio_tool_registry,
)


IDENTITY = "11111111-1111-4111-8111-111111111111"
OTHER_IDENTITY = "22222222-2222-4222-8222-222222222222"


VALID_ARGUMENTS = {
    "artifact_aggregate": {
        "artifact_id": IDENTITY,
        "operation": "COUNT",
    },
    "artifact_list": {},
    "artifact_query": {
        "artifact_id": IDENTITY,
        "view_type": "METADATA",
    },
    "environment_build": {
        "base_image_key": "scientific-python",
        "requirements": ["example-package==1.0"],
    },
    "environment_list": {},
    "execution_inspect": {"execution_id": IDENTITY},
    "execution_revise": {
        "execution_id": IDENTITY,
        "expected_script_hash": "0" * 64,
    },
    "execution_submit": {
        "image_key": "scientific-python",
        "script_content": "print('ok')",
        "input_artifact_ids": [],
        "requested_outputs": [],
    },
    "file_read": {"artifact_id": IDENTITY},
    "literature_search": {"query": "bounded public query"},
    "memory_propose_update": {
        "target_scope": "PROJECT",
        "reason": "Retain a bounded project fact.",
    },
    "memory_search": {},
    "memory_view": {"memory_id": IDENTITY, "version": 1},
    "report_check": {"evidence_artifact_ids": []},
    "report_read": {"artifact_id": IDENTITY},
    "report_submit": {
        "title": "Bounded report",
        "report_text": "Bounded content.",
    },
    "skill_propose_use": {
        "skill_id": IDENTITY,
        "version": 1,
        "mode": "REFERENCE",
        "reason": "Use as bounded context.",
    },
    "skill_search": {},
    "skill_view": {"authorization_id": OTHER_IDENTITY},
}


class RecordingHost:
    def __init__(self):
        self.calls = []
        self.fail_with = None

    async def invoke(self, capability_name, arguments):
        self.calls.append((capability_name, arguments))
        if self.fail_with is not None:
            raise self.fail_with
        return {"capability": capability_name, "accepted": True}


class StableRuntimeToolSet:
    def __init__(self):
        self.stop_reason = None
        self.aggregate_results = [{"count": 3}]
        self.calls = []

    async def artifact_aggregate(self, **payload):
        self.calls.append(("artifact_aggregate", payload))
        value = self.aggregate_results.pop(0) if len(self.aggregate_results) > 1 else (
            self.aggregate_results[0]
        )
        return {
            "success": True,
            "capability_invocation_id": str(uuid4()),
            "information_authority": "AUTHORITATIVE_EVIDENCE",
            "data": value,
            "error": None,
        }

    async def execution_submit(self, **payload):
        self.calls.append(("execution_submit", payload))
        return {
            "success": True,
            "capability_invocation_id": str(uuid4()),
            "information_authority": "AUTHORITATIVE_EVIDENCE",
            "data": {"execution_id": str(uuid4())},
            "error": None,
        }

    def mark_feedback_delivered(self):
        pass

    def evidence_items(self):
        return ()


def _runtime_host(monkeypatch, tool_set):
    monkeypatch.setattr(
        "labbioagentos.runtime.tooling.LabBioRuntimeToolSet",
        lambda _binding, _services: tool_set,
    )
    return NativeRuntimeCapabilityHost(None, None)


def _stage_for(capability_name: str) -> WorkflowStage:
    return next(
        stage
        for stage, capabilities in CAPABILITY_CEILINGS.items()
        if capability_name in capabilities
    )


def _binding(capability_name: str) -> NativeCapabilityBinding:
    return NativeCapabilityBinding(
        run_id=uuid4(),
        stage_id=_stage_for(capability_name),
        invocation_id=uuid4(),
        actor_profile_key="test-profile",
        actor_agent_name="test-agent",
        capability_allowlist=(capability_name,),
    )


def test_complete_capability_catalog_registers_without_legacy_runtime():
    host = RecordingHost()
    registry = build_labbio_tool_registry(host)

    assert set(registry.names) == set(ALL_CAPABILITIES)
    assert len(registry.names) == len(ALL_CAPABILITIES) == len(VALID_ARGUMENTS)
    for spec in registry.specs:
        assert spec.parameters["type"] == "object"
        assert spec.parameters["additionalProperties"] is False
        assert "$ref" not in repr(spec.parameters)
        assert "$defs" not in repr(spec.parameters)

    schemas = {spec.name: spec.parameters for spec in registry.specs}
    artifact_list = schemas["artifact_list"]["properties"]
    assert artifact_list["offset"]["minimum"] == 0
    assert artifact_list["limit"]["minimum"] == 1
    assert artifact_list["limit"]["maximum"] == 100
    environment_list = schemas["environment_list"]["properties"]
    assert environment_list["requirements"]["maxItems"] == 64
    assert environment_list["limit"]["maximum"] == 10
    assert schemas["memory_view"]["properties"]["version"]["minimum"] == 1
    report_submit = schemas["report_submit"]["properties"]
    assert report_submit["title"]["maxLength"] == 256
    assert report_submit["report_text"]["maxLength"] == 64_000
    execution_submit = schemas["execution_submit"]
    assert set(execution_submit["required"]) == {"image_key", "script_content"}
    assert "mountable_input_artifact_ids" in (
        execution_submit["properties"]["input_artifact_ids"]["description"]
    )
    assert "approved output contract" in (
        execution_submit["properties"]["requested_outputs"]["description"]
    )
    descriptions = {spec.name: spec.description for spec in registry.specs}
    assert "LABBIO_INPUT_MANIFEST_PATH" in descriptions["execution_submit"]
    assert "manifest[str(artifact_uuid)]" in descriptions["execution_submit"]
    assert "LABBIO_OUTPUT_DIR" in descriptions["execution_submit"]
    execution_revise = schemas["execution_revise"]["properties"]
    assert "preserves the original selection" in (
        execution_revise["input_artifact_ids"]["description"]
    )
    assert "LABBIO_INPUT_MANIFEST_PATH" in descriptions["execution_revise"]
    assert "direct JSON mapping" in descriptions["execution_revise"]


def test_required_output_count_is_projected_into_current_submit_schema():
    host = RecordingHost()
    required = RuntimeExecutionCapabilityView(
        runtime=ExecutionRuntime.PYTHON,
        image_key="scientific-python",
        resources=RequestedResources(),
        network_required=False,
        minimum_queryable_output_count=1,
    )

    schemas = {
        spec.name: spec.parameters
        for spec in build_labbio_tool_registry(host, required).specs
    }

    assert "requested_outputs" in schemas["execution_submit"]["required"]
    requested_outputs = schemas["execution_submit"]["properties"]["requested_outputs"]
    assert requested_outputs["minItems"] == 1
    assert set(requested_outputs["items"]["required"]) == {
        "relative_path",
        "artifact_type",
        "requested_exposure",
        "output_contract_id",
    }
    assert requested_outputs["items"]["properties"]["requested_exposure"][
        "const"
    ] == "DERIVED"


def test_zero_output_count_keeps_submit_outputs_optional():
    host = RecordingHost()
    optional = RuntimeExecutionCapabilityView(
        runtime=ExecutionRuntime.PYTHON,
        image_key="scientific-python",
        resources=RequestedResources(),
        network_required=False,
        minimum_queryable_output_count=0,
    )

    schema = next(
        spec.parameters
        for spec in build_labbio_tool_registry(host, optional).specs
        if spec.name == "execution_submit"
    )

    assert "requested_outputs" not in schema["required"]


@pytest.mark.asyncio
async def test_unchanged_successful_read_after_feedback_requests_stop(monkeypatch):
    tool_set = StableRuntimeToolSet()
    host = _runtime_host(monkeypatch, tool_set)
    arguments = ArtifactAggregateArguments(
        artifact_id=UUID(IDENTITY),
        operation="COUNT",
    )

    first = await host.invoke("artifact_aggregate", arguments)
    host.mark_feedback_delivered()
    second = await host.invoke("artifact_aggregate", arguments)

    assert first["data"] == second["data"] == {"count": 3}
    assert len(tool_set.calls) == 2
    assert host.stop_reason == "REPEATED_NO_PROGRESS_REQUEST"


@pytest.mark.asyncio
async def test_changed_read_result_after_feedback_remains_live(monkeypatch):
    tool_set = StableRuntimeToolSet()
    tool_set.aggregate_results = [{"count": 3}, {"count": 4}]
    host = _runtime_host(monkeypatch, tool_set)
    arguments = ArtifactAggregateArguments(
        artifact_id=UUID(IDENTITY),
        operation="COUNT",
    )

    first = await host.invoke("artifact_aggregate", arguments)
    host.mark_feedback_delivered()
    second = await host.invoke("artifact_aggregate", arguments)

    assert first["data"] == {"count": 3}
    assert second["data"] == {"count": 4}
    assert len(tool_set.calls) == 2
    assert host.stop_reason is None


@pytest.mark.asyncio
async def test_repeated_side_effect_is_not_classified_as_stable_read(monkeypatch):
    tool_set = StableRuntimeToolSet()
    host = _runtime_host(monkeypatch, tool_set)
    arguments = ExecutionSubmitArguments(
        image_key="scientific-python",
        script_content="print('ok')",
    )

    await host.invoke("execution_submit", arguments)
    host.mark_feedback_delivered()
    await host.invoke("execution_submit", arguments)

    assert len(tool_set.calls) == 2
    assert host.stop_reason is None


@pytest.mark.asyncio
async def test_every_capability_dispatches_without_model_supplied_authority():
    host = RecordingHost()
    registry = build_labbio_tool_registry(host)

    for index, capability_name in enumerate(sorted(ALL_CAPABILITIES), start=1):
        dispatcher = NativeCapabilityDispatcher(
            binding=_binding(capability_name),
            registry=registry,
        )
        call = ToolCall(
            call_id=f"call-{index}",
            name=capability_name,
            arguments_json=json.dumps(VALID_ARGUMENTS[capability_name]),
        )
        outcomes = await dispatcher.dispatch_batch(
            (call,),
            finish_reason=FinishReason.TOOL_CALLS,
        )
        evidence = dispatcher.evidence_items()

        assert outcomes[0].result.status == "SUCCEEDED"
        assert evidence[0].capability_name == capability_name
        assert evidence[0].information_authority is CAPABILITY_INFORMATION_AUTHORITY[
            capability_name
        ]
        assert evidence[0].actor_profile_key == "test-profile"
        assert evidence[0].actor_agent_name == "test-agent"
        bundle = dispatcher.evidence_bundle()
        assert bundle.authority_mode == "ITEM_LEVEL"
        assert bundle.run_id == dispatcher.binding.run_id
        assert bundle.stage_id is dispatcher.binding.stage_id
        assert bundle.invocation_id == dispatcher.binding.invocation_id
        assert bundle.items == evidence

    assert [name for name, _arguments in host.calls] == sorted(ALL_CAPABILITIES)


@pytest.mark.asyncio
async def test_denied_or_authority_shaped_batch_fails_before_host_effects():
    host = RecordingHost()
    registry = build_labbio_tool_registry(host)
    dispatcher = NativeCapabilityDispatcher(
        binding=_binding("artifact_list"),
        registry=registry,
    )

    with pytest.raises(ToolBatchRejectedError) as denied:
        await dispatcher.dispatch_batch(
            (
                ToolCall(
                    call_id="denied",
                    name="execution_submit",
                    arguments_json=json.dumps(VALID_ARGUMENTS["execution_submit"]),
                ),
            ),
            finish_reason=FinishReason.TOOL_CALLS,
        )
    assert denied.value.code == "CAPABILITY_NOT_ALLOWED"

    with pytest.raises(ToolBatchRejectedError) as injected:
        await dispatcher.dispatch_batch(
            (
                ToolCall(
                    call_id="injected",
                    name="artifact_list",
                    arguments_json=json.dumps(
                        {
                            "principal": "model-principal",
                            "workspace": "model-workspace",
                        }
                    ),
                ),
            ),
            finish_reason=FinishReason.TOOL_CALLS,
        )
    assert injected.value.code == "INVALID_TOOL_ARGUMENTS"
    assert host.calls == []
    assert dispatcher.evidence_items() == ()


@pytest.mark.asyncio
async def test_malformed_resource_identity_fails_before_host_effects():
    host = RecordingHost()
    registry = build_labbio_tool_registry(host)
    dispatcher = NativeCapabilityDispatcher(
        binding=_binding("artifact_query"),
        registry=registry,
    )

    with pytest.raises(ToolBatchRejectedError) as malformed:
        await dispatcher.dispatch_batch(
            (
                ToolCall(
                    call_id="malformed-id",
                    name="artifact_query",
                    arguments_json=json.dumps(
                        {"artifact_id": "not-a-uuid", "view_type": "METADATA"}
                    ),
                ),
            ),
            finish_reason=FinishReason.TOOL_CALLS,
        )

    assert malformed.value.code == "INVALID_TOOL_ARGUMENTS"
    assert host.calls == []


@pytest.mark.asyncio
async def test_unknown_host_failure_is_bounded_and_retains_item_authority():
    host = RecordingHost()
    host.fail_with = RuntimeError("PRIVATE_HOST_FAILURE_SENTINEL")
    registry = build_labbio_tool_registry(host)
    dispatcher = NativeCapabilityDispatcher(
        binding=_binding("artifact_list"),
        registry=registry,
    )
    outcome = await dispatcher.dispatch_batch(
        (
            ToolCall(
                call_id="failed",
                name="artifact_list",
                arguments_json="{}",
            ),
        ),
        finish_reason=FinishReason.TOOL_CALLS,
    )
    evidence = dispatcher.evidence_items()[0]

    assert outcome[0].result.status == "FAILED"
    assert outcome[0].result.error.code == "INTERNAL_TOOL_ERROR"
    assert evidence.status.value == "FAILED"
    assert evidence.information_authority is InformationAuthority.AUTHORITATIVE_EVIDENCE
    assert "PRIVATE_HOST_FAILURE_SENTINEL" not in repr(outcome)
    assert "PRIVATE_HOST_FAILURE_SENTINEL" not in repr(evidence)


@pytest.mark.asyncio
async def test_unsafe_host_result_is_rejected_by_the_authoritative_projector():
    class UnsafeResultHost(RecordingHost):
        async def invoke(self, capability_name, arguments):
            self.calls.append((capability_name, arguments))
            return {"script_content": "PRIVATE_SCRIPT_SENTINEL"}

    host = UnsafeResultHost()
    registry = build_labbio_tool_registry(host)
    dispatcher = NativeCapabilityDispatcher(
        binding=_binding("artifact_list"),
        registry=registry,
    )
    outcome = await dispatcher.dispatch_batch(
        (ToolCall(call_id="unsafe", name="artifact_list", arguments_json="{}"),),
        finish_reason=FinishReason.TOOL_CALLS,
    )
    evidence = dispatcher.evidence_items()[0]

    assert outcome[0].result.status == "FAILED"
    assert outcome[0].result.error.code == "INVALID_MODEL_RESULT"
    assert evidence.status.value == "FAILED"
    assert "PRIVATE_SCRIPT_SENTINEL" not in repr(outcome)
    assert "PRIVATE_SCRIPT_SENTINEL" not in repr(evidence)


def test_native_binding_preserves_existing_stage_ceiling():
    with pytest.raises(ValueError, match="approved ceiling"):
        NativeCapabilityBinding(
            run_id=uuid4(),
            stage_id=WorkflowStage.INTAKE,
            invocation_id=uuid4(),
            actor_profile_key="test-profile",
            actor_agent_name="test-agent",
            capability_allowlist=("execution_submit",),
        )
