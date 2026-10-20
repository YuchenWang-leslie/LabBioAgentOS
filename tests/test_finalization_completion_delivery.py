"""Finalizer receives legal Agent completions losslessly under the total bound."""

import hashlib
import json

import pytest

from labbioagentos import CapabilityEvidenceBundle, RuntimeIntegrationError, WorkflowStage
from labbioagentos.runtime.assembly import RuntimeStageAssemblySpec
from labbioagentos.runtime.contracts import HandoffObservation, PreparedHandoff
from labbioagentos.runtime.native_stage import NativeKernelStageInvoker, NativeRuntimeFactory
from test_native_stage_application import (
    WorkflowProvider, _capability_catalog, _stage_input, _toolset,
)


async def finalization_case(tmp_path, completion, *, prepared, max_request_bytes=256_000):
    stage = _stage_input()
    tools = _toolset(tmp_path, stage)
    response = await tools.artifact_list()
    item = tools.evidence_items()[0]
    handoff = PreparedHandoff(
        summary="Agent-selected original listing.",
        observations=(HandoffObservation(
            observation_id=item.capability_invocation_id,
            capability_name=item.capability_name,
            information_authority=item.information_authority,
            public_response=response,
        ),),
    ) if prepared else None
    evidence = CapabilityEvidenceBundle(
        run_id=stage.run_id, stage_id=stage.stage_id, invocation_id=stage.invocation_id,
        items=tools.evidence_items(), explicit_completion=completion, prepared_handoff=handoff,
    )
    calls = []
    invoker = NativeKernelStageInvoker(
        assembly=RuntimeStageAssemblySpec(
            stage_id=WorkflowStage.INTAKE, root_profile_key="coordinator",
            prompt_template_key="runtime-generic", capability_allowlist=("artifact_list",),
            max_request_bytes=max_request_bytes,
        ),
        factory=NativeRuntimeFactory(_capability_catalog(), lambda _model: WorkflowProvider(calls)),
        principal=tools.binding.principal, workspace=tools.binding.workspace, services=tools.services,
    )
    return stage, evidence, invoker, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("prepared", [False, True])
@pytest.mark.parametrize("completion", ["x" * 60_000, '"' * 64_000, "证" * 60_000],
                         ids=["ascii-60000", "escaped-64000", "unicode-60000"])
async def test_legal_long_completion_is_delivered_exactly_once(tmp_path, prepared, completion):
    stage, evidence, invoker, calls = await finalization_case(
        tmp_path, completion, prepared=prepared)
    result = await invoker.finalize_recovered(stage, evidence)
    assert result.stage_id is WorkflowStage.INTAKE
    assert len(calls) == 1
    request = calls[0]
    payload = json.loads(request.messages[-1].content)
    control = payload["explicit_completion_control"]
    assert control["authority"] == "MODEL_CONTEXT"
    assert control["delivery"] == "SEPARATE_PUBLIC_MESSAGE"
    assert request.messages[control["message_index"]].content == evidence.explicit_completion
    assert control["character_count"] == len(evidence.explicit_completion)
    assert control["sha256"] == hashlib.sha256(evidence.explicit_completion.encode()).hexdigest()
    assert "explicit_completion" not in payload["capability_evidence"]
    assert sum(message.content == evidence.explicit_completion for message in request.messages) == 1
    assert evidence.explicit_completion == completion.strip()
    if prepared:
        assert json.loads(request.messages[-2].content) == evidence.prepared_handoff.model_dump(
            mode="json", exclude_none=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("prepared", [False, True])
async def test_unicode_completion_obeys_total_request_capacity(tmp_path, prepared):
    stage, evidence, invoker, calls = await finalization_case(
        tmp_path, "证" * 60_000, prepared=prepared, max_request_bytes=80_000)
    with pytest.raises(RuntimeIntegrationError) as caught:
        await invoker.finalize_recovered(stage, evidence)
    assert caught.value.error_code == "NATIVE_INVOCATION_FAILED"
    assert caught.value.__cause__.code == "CONTEXT_CAPACITY_EXCEEDED"
    assert not calls
    assert evidence.explicit_completion == "证" * 60_000
