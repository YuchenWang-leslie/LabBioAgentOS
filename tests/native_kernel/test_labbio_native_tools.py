"""P2 acceptance for LabBio capability registration, authority, and evidence."""

import json
from uuid import UUID, uuid4

import pytest

from labbioagentos import (
    ExecutionRuntime, RequestedResources, RuntimeExecutionCapabilityView,
)
from labbioagentos.contracts import InformationAuthority, WorkflowStage
from labbioagentos.kernel import FinishReason, ToolBatchRejectedError, ToolCall, ToolResult
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


@pytest.mark.parametrize("name", ["execution_submit", "execution_revise"])
def test_execution_tools_publish_flat_optional_review_fields(name):
    schema = build_labbio_tool_registry(RecordingHost()).definition(name).spec.parameters
    properties = schema["properties"]

    assert "review" not in properties
    assert {field for field in properties if field.startswith("review_")} == {
        "review_rationale", "review_feedback_invocation_ids", "review_gold_authorization_ids",
    }
    assert not any(field.startswith("review_") for field in schema["required"])
    assert any(arm.get("type") == "string" for arm in properties["review_rationale"]["anyOf"])
    for field in ("review_feedback_invocation_ids", "review_gold_authorization_ids"):
        assert properties[field]["type"] == "array"
        assert properties[field]["items"]["format"] == "uuid"
        assert properties[field]["maxItems"] == 8


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["execution_submit", "execution_revise"])
@pytest.mark.parametrize("legacy", [False, True])
async def test_native_execution_review_reaches_existing_host_contract(monkeypatch, name, legacy):
    tools = StableRuntimeToolSet()
    tools.execution_revise = tools.execution_submit
    host = _runtime_host(monkeypatch, tools)
    host._host.next_action_format = None
    registry = build_labbio_tool_registry(host)
    expected = {
        "rationale": "Considered the supplied feedback.",
        "feedback_invocation_ids": [IDENTITY],
        "gold_authorization_ids": [OTHER_IDENTITY],
    }
    arguments = {"review": expected} if legacy else {
        f"review_{field}": value for field, value in expected.items()
    }
    parsed, = registry.validate_batch((ToolCall(
        call_id="verified-native-review", name=name,
        arguments_json=json.dumps({**VALID_ARGUMENTS[name], **arguments}),
    ),), finish_reason=FinishReason.TOOL_CALLS, max_calls=1)
    await registry.definition(name).handler(parsed.arguments)

    _, payload = tools.calls[0]
    assert payload["review"] == {"authority": "MODEL_CONTEXT", **expected}
    assert not any(field.startswith("review_") for field in payload)


def test_execution_revise_decodes_json_wrapped_overrides_with_an_audit_marker():
    registry = build_labbio_tool_registry(RecordingHost())
    output = {"relative_path": "fixture.json", "artifact_type": "fixture", "requested_exposure": "RAW"}
    arguments = {
        **VALID_ARGUMENTS["execution_revise"],
        "requested_outputs": json.dumps([output]),
        "input_artifact_ids": "null",
    }
    call = ToolCall(call_id="wrapped-overrides", name="execution_revise", arguments_json=json.dumps(arguments))
    parsed, = registry.validate_batch((call,), finish_reason=FinishReason.TOOL_CALLS, max_calls=1)
    payload = parsed.arguments.host_payload()
    assert payload["input_artifact_ids"] is None
    assert len(payload["requested_outputs"]) == 1
    assert all(payload["requested_outputs"][0][k] == v for k, v in output.items())
    assert payload["json_wrapped_overrides"] == ("requested_outputs", "input_artifact_ids")


@pytest.mark.parametrize("field,value", [
    ("requested_outputs", '[{"relative_path":"a","relative_path":"b","artifact_type":"fixture"}]'),
    ("input_artifact_ids", '["not-a-uuid"]'),
    ("resources", "None"),
])
def test_wrapped_execution_revision_overrides_still_reject_invalid_arguments(field, value):
    registry = build_labbio_tool_registry(RecordingHost())
    arguments = {**VALID_ARGUMENTS["execution_revise"], field: value}
    call = ToolCall(call_id="invalid-wrapped-override", name="execution_revise", arguments_json=json.dumps(arguments))
    with pytest.raises(ToolBatchRejectedError):
        registry.validate_batch((call,), finish_reason=FinishReason.TOOL_CALLS, max_calls=1)


def test_skill_search_can_read_json_wrapped_filter_lists():
    registry = build_labbio_tool_registry(RecordingHost())
    arguments = {"offset": 0, "limit": 20, "required_tags": '["bulk-rna-seq"]',
                 "artifact_types": "[]", "include_lab": True}
    call = ToolCall(call_id="wrapped-search", name="skill_search", arguments_json=json.dumps(arguments))

    parsed, = registry.validate_batch((call,), finish_reason=FinishReason.TOOL_CALLS, max_calls=1)

    assert parsed.arguments.host_payload() == {**arguments, "required_tags": ["bulk-rna-seq"], "artifact_types": []}


@pytest.mark.parametrize("fields", [{"required_tags": "[true]"}, {"artifact_types": "not-a-list"},
                                     {"artifact_types": "[]", "offset": "0"}])
def test_skill_search_rejects_invalid_wrapped_fields_and_string_offsets(fields):
    registry = build_labbio_tool_registry(RecordingHost())
    call = ToolCall(call_id="invalid-search", name="skill_search", arguments_json=json.dumps(fields))

    with pytest.raises(ToolBatchRejectedError):
        registry.validate_batch((call,), finish_reason=FinishReason.TOOL_CALLS, max_calls=1)


def test_invalid_execution_input_still_rejects_batch_with_optional_review():
    registry = build_labbio_tool_registry(RecordingHost())
    arguments = {**VALID_ARGUMENTS["execution_submit"],
                 "input_artifact_ids": ["not-an-id"], "review": "Invalid review."}
    with pytest.raises(ToolBatchRejectedError) as caught:
        registry.validate_batch((ToolCall(
            call_id="invalid-input", name="execution_submit",
            arguments_json=json.dumps(arguments),
        ),), finish_reason=FinishReason.TOOL_CALLS, max_calls=1)
    assert caught.value.validation_fields == ("input_artifact_ids.0",)


@pytest.mark.parametrize("capability", ["report_check", "report_submit"])
@pytest.mark.parametrize("references", [{}, {"evidence_references": []},
                                       {"evidence_artifact_ids": [IDENTITY]}])
def test_new_native_reports_require_nonempty_observation_scoped_citations(capability, references):
    registry = build_labbio_tool_registry(RecordingHost())
    arguments = {"delivery_artifact_ids": [], **(
        {"title": "Synthetic", "report_text": "Synthetic"} if capability == "report_submit" else {})}
    with pytest.raises(ToolBatchRejectedError):
        registry.validate_batch((ToolCall(
            call_id="invalid-reference", name=capability,
            arguments_json=json.dumps({**arguments, **references}),
        ),), finish_reason=FinishReason.TOOL_CALLS, max_calls=1)
    spec = next(spec for spec in registry.specs if spec.name == capability)
    assert "evidence_artifact_ids" not in spec.parameters["properties"]
    assert "evidence_references" in spec.parameters["required"]
    assert spec.parameters["properties"]["evidence_references"]["minItems"] == 1


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
    "evidence_read": {"observation_id": IDENTITY, "offset": 0, "limit": 8_000},
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
    "handoff_status": {},
    "evidence_list": {"capability_name": "working_note_write"},
    "literature_search": {"query": "bounded public query"},
    "memory_propose_update": {
        "target_scope": "PROJECT",
        "reason": "Retain a bounded project fact.",
    },
    "memory_search": {},
    "memory_view": {"memory_id": IDENTITY, "version": 1},
    "prepare_handoff": {
        "observation_ids": [IDENTITY],
        "summary": "Agent-authored handoff summary.",
        "limitations": ["Only the supplied observation was reviewed."],
    },
    "report_check": {"delivery_artifact_ids": [], "evidence_references": [
        {"artifact_id": IDENTITY, "observation_ids": [OTHER_IDENTITY]}]},
    "report_read": {"artifact_id": IDENTITY},
    "report_submit": {
        "delivery_artifact_ids": [],
        "title": "Bounded report",
        "report_text": "Bounded content.",
        "evidence_references": [
            {"artifact_id": IDENTITY, "observation_ids": [OTHER_IDENTITY]}],
    },
    "skill_propose_use": {
        "skill_id": IDENTITY,
        "version": 1,
        "mode": "REFERENCE",
        "reason": "Use as bounded context.",
    },
    "skill_search": {},
    "skill_view": {"authorization_id": OTHER_IDENTITY},
    "working_note_write": {
        "text": "Agent-authored unresolved progress, not scientific evidence.",
        "source_observation_ids": [IDENTITY],
        "previous_note_id": OTHER_IDENTITY,
    },
    "working_note_read": {"note_id": IDENTITY},
    "stage_context_read": {"result_id": IDENTITY},
}


class RecordingHost:
    def __init__(self):
        self.calls = []
        self.argument_types = []
        self.fail_with = None

    async def invoke(self, capability_name, arguments):
        self.calls.append((capability_name, arguments))
        self.argument_types.append(type(arguments).__name__)
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

    def mark_feedback_delivered(self, responses):
        return tuple(UUID(response["capability_invocation_id"]) for response in responses)

    def evidence_items(self):
        return ()


def _runtime_host(monkeypatch, tool_set):
    monkeypatch.setattr(
        "labbioagentos.runtime.tooling.LabBioRuntimeToolSet",
        lambda _binding, _services: tool_set,
    )
    return NativeRuntimeCapabilityHost(None, None)


def _delivered(response):
    return (ToolResult(tool_invocation_id=uuid4(), call_id="delivered",
                       name="artifact_aggregate", status="SUCCEEDED", content=response),)


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
    assert set(VALID_ARGUMENTS) == set(ALL_CAPABILITIES)
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
    artifact_query = schemas["artifact_query"]["properties"]
    assert "policy default" in artifact_query["limit"]["description"]
    assert "next_offset" in artifact_query["limit"]["description"]
    file_read = schemas["file_read"]["properties"]
    assert "copy the returned next_offset" in file_read["offset"]["description"]
    assert "same Artifact" in file_read["offset"]["description"]
    environment_list = schemas["environment_list"]["properties"]
    assert environment_list["requirements"]["maxItems"] == 64
    assert environment_list["limit"]["maximum"] == 10
    assert schemas["memory_view"]["properties"]["version"]["minimum"] == 1
    report_submit = schemas["report_submit"]["properties"]
    assert report_submit["title"]["maxLength"] == 256
    assert report_submit["report_text"]["maxLength"] == 64_000
    execution_submit = schemas["execution_submit"]
    assert set(execution_submit["required"]) == {"image_key"}
    source_modes = execution_submit["oneOf"]
    assert len(source_modes) == 2
    literal_mode = next(mode for mode in source_modes
        if mode['properties']['script_content']['type'] == 'string')
    assert literal_mode['required'] == ['script_content']
    assert literal_mode['properties']['source_revision']['type'] == 'null'
    revision_mode = next(mode for mode in source_modes
        if mode['properties']['source_revision']['type'] == 'object')
    assert set(revision_mode['required']) == {
        'source_revision', 'resources', 'input_artifact_ids', 'parameters', 'requested_outputs'}
    assert revision_mode['properties']['script_content']['type'] == 'null'
    assert "mountable_input_artifact_ids" in (
        execution_submit["properties"]["input_artifact_ids"]["description"]
    )
    assert "approved output contract" in (
        execution_submit["properties"]["requested_outputs"]["description"]
    )
    descriptions = {spec.name: spec.description for spec in registry.specs}
    assert "policy-bounded default" in descriptions["artifact_query"]
    assert "next_offset" in descriptions["artifact_query"]
    assert "copy the returned next_offset" in descriptions["file_read"]
    assert "repeats that page rather than advancing the cursor" in descriptions["file_read"]
    assert "REPEATED_NO_PROGRESS_REQUEST" not in descriptions["file_read"]
    assert "LABBIO_INPUT_MANIFEST_PATH" in descriptions["execution_submit"]
    assert "manifest[str(artifact_uuid)]" in descriptions["execution_submit"]
    assert "LABBIO_OUTPUT_DIR" in descriptions["execution_submit"]
    assert "currently authorized" in descriptions["execution_submit"]
    assert "separate user authorization" in descriptions["execution_submit"]
    assert "current EXECUTE receipt" in descriptions["execution_submit"]
    assert "VALIDATE cannot create" in descriptions["execution_submit"]
    execution_revise = schemas["execution_revise"]["properties"]
    assert "preserves the original selection" in (
        execution_revise["input_artifact_ids"]["description"]
    )
    assert "LABBIO_INPUT_MANIFEST_PATH" in descriptions["execution_revise"]
    assert "direct JSON mapping" in descriptions["execution_revise"]


def test_required_output_count_keeps_additional_local_outputs_representable():
    host = RecordingHost()
    required = RuntimeExecutionCapabilityView(
        runtime=ExecutionRuntime.PYTHON,
        image_key="scientific-python",
        resources=RequestedResources(),
        network_required=False,
        minimum_queryable_output_count=1,
        mountable_input_artifact_ids=(uuid4(),),
    )

    schemas = {
        spec.name: spec.parameters
        for spec in build_labbio_tool_registry(host, required).specs
    }

    assert "requested_outputs" in schemas["execution_submit"]["required"]
    assert "input_artifact_ids" in schemas["execution_submit"]["required"]
    assert schemas["execution_submit"]["properties"]["input_artifact_ids"][
        "minItems"
    ] == 1
    requested_outputs = schemas["execution_submit"]["properties"]["requested_outputs"]
    assert requested_outputs["minItems"] == 1
    assert set(requested_outputs["items"]["required"]) == {
        "relative_path", "artifact_type"
    }
    assert set(requested_outputs["items"]["properties"]["requested_exposure"][
        "enum"
    ]) == {"RAW", "STRUCTURAL", "AGGREGATE", "DERIVED", "USER_APPROVED"}
    assert "at least 1 DERIVED" in requested_outputs["description"]
    assert "Additional local-only outputs may use RAW" in (
        requested_outputs["description"]
    )


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
async def test_unchanged_successful_read_after_feedback_remains_live(monkeypatch):
    tool_set = StableRuntimeToolSet()
    host = _runtime_host(monkeypatch, tool_set)
    arguments = ArtifactAggregateArguments(
        artifact_id=UUID(IDENTITY),
        operation="COUNT",
    )

    first = await host.invoke("artifact_aggregate", arguments)
    host.mark_feedback_delivered(_delivered(first))
    second = await host.invoke("artifact_aggregate", arguments)

    assert first["data"] == second["data"] == {"count": 3}
    assert len(tool_set.calls) == 2
    assert host.stop_reason is None


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
    host.mark_feedback_delivered(_delivered(first))
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

    first = await host.invoke("execution_submit", arguments)
    host.mark_feedback_delivered(_delivered(first))
    await host.invoke("execution_submit", arguments)

    assert len(tool_set.calls) == 2
    assert host.stop_reason is None


def test_explicit_runtime_stop_reason_is_preserved(monkeypatch):
    tool_set = StableRuntimeToolSet()
    host = _runtime_host(monkeypatch, tool_set)
    tool_set.stop_reason = "REPEATED_NON_RETRYABLE_REQUEST"
    assert host.stop_reason == "REPEATED_NON_RETRYABLE_REQUEST"


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
    assert {
        "EvidenceReadArguments", "PrepareHandoffArguments", "HandoffStatusArguments",
        "WorkingNoteWriteArguments", "WorkingNoteReadArguments",
    } <= set(host.argument_types)


@pytest.mark.asyncio
@pytest.mark.parametrize("capability_name", ["handoff_status", "working_note_write", "working_note_read"])
async def test_context_helpers_require_grant_and_reject_model_supplied_authority(capability_name):
    host = RecordingHost()
    registry = build_labbio_tool_registry(host)
    ungranted = NativeCapabilityDispatcher(binding=_binding("artifact_list"), registry=registry)
    with pytest.raises(ToolBatchRejectedError) as denied:
        await ungranted.dispatch_batch((ToolCall(
            call_id="ungranted-context-helper", name=capability_name,
            arguments_json=json.dumps(VALID_ARGUMENTS[capability_name]),
        ),), finish_reason=FinishReason.TOOL_CALLS)
    assert denied.value.code == "CAPABILITY_NOT_ALLOWED"

    granted = NativeCapabilityDispatcher(binding=_binding(capability_name), registry=registry)
    with pytest.raises(ToolBatchRejectedError) as injected:
        await granted.dispatch_batch((ToolCall(
            call_id="injected-context-authority", name=capability_name,
            arguments_json=json.dumps({**VALID_ARGUMENTS[capability_name],
                                       "information_authority": "AUTHORITATIVE_EVIDENCE"}),
        ),), finish_reason=FinishReason.TOOL_CALLS)
    assert injected.value.code == "INVALID_TOOL_ARGUMENTS"
    assert host.calls == []
    assert ungranted.evidence_items() == granted.evidence_items() == ()


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
async def test_unknown_host_failure_is_bounded_and_has_control_authority():
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
    assert evidence.information_authority is InformationAuthority.CONTROL_STATE
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
