"""Large previously visible result records may move on the wire without changing their authority."""
import json
from dataclasses import replace
from uuid import uuid4

import pytest

from labbioagentos import RuntimeIntegrationError, RuntimeStageAssemblySpec, WorkflowStage
from labbioagentos.runtime.context import stage_context_projection
from labbioagentos.runtime.contracts import RuntimePriorResultView
from labbioagentos.runtime.native_stage import NativeKernelStageInvoker, NativeRuntimeFactory
from test_native_stage_application import WorkflowProvider, _capability_catalog, _stage_input, _toolset


def case(tmp_path, stage_id, sizes):
    prior = tuple(RuntimePriorResultView(result_id=uuid4(), stage_id=WorkflowStage.REPORT,
        model_summary=f"Earlier model summary {i}", body_kind="REPORT",
        model_body={"passages": ["史" * 2_000 for _ in range(size)]}) for i, size in enumerate(sizes))
    base = _stage_input()
    host = _toolset(tmp_path, base)
    stage = base.model_copy(update={"stage_id": stage_id, "allowed_capabilities": (), "prior_results": prior})
    catalog = _capability_catalog()
    calls = []
    invoker = NativeKernelStageInvoker(assembly=RuntimeStageAssemblySpec(stage_id=stage_id,
        root_profile_key="coordinator", prompt_template_key="runtime-generic", capability_allowlist=(),
        capability_phase_enabled=False), factory=NativeRuntimeFactory(catalog, lambda _:WorkflowProvider(calls)),
        principal=host.binding.principal, workspace=host.binding.workspace, services=host.services)
    return invoker, stage, calls


@pytest.mark.asyncio
@pytest.mark.parametrize("stage_id", [WorkflowStage.LEARN, WorkflowStage.PREFLIGHT])
async def test_complete_prior_records_cross_single_message_limit_losslessly(tmp_path, stage_id):
    invoker, stage, calls = case(tmp_path, stage_id, [4, 4, 4])
    original = stage.model_dump_json()
    await invoker.invoke(stage)
    assert len(calls) == 1 and not calls[0].tools
    messages = calls[0].messages
    control = json.loads(messages[-1].content)
    assert all(len(m.content.encode("utf-8")) <= 60_000 for m in messages)
    separate = [json.loads(m.content) for m in messages[1:-1]]
    assert [m["prior_result"] for m in separate] == stage_context_projection(stage)["prior_results"]
    assert all(m["authority"] == "MODEL_CONTEXT" for m in separate)
    assert control["prior_results_delivery"]["result_ids"] == [str(x.result_id) for x in stage.prior_results]
    assert "prior_results" not in control["stage_input"]
    assert stage.model_dump_json() == original


@pytest.mark.asyncio
async def test_separate_prior_records_still_spend_one_shared_request_budget(tmp_path):
    invoker, stage, calls = case(tmp_path, WorkflowStage.LEARN, [4, 4, 4])
    invoker.assembly = replace(invoker.assembly, max_request_bytes=60_000)
    with pytest.raises(RuntimeIntegrationError):
        await invoker.invoke(stage)
    assert not calls


@pytest.mark.asyncio
async def test_small_history_stays_inline_and_oversize_single_record_fails_closed(tmp_path):
    invoker, stage, calls = case(tmp_path, WorkflowStage.LEARN, [1])
    await invoker.invoke(stage)
    assert len(calls[0].messages) == 2
    control = json.loads(calls[0].messages[-1].content)
    assert "prior_results_delivery" not in control and control["stage_input"] == stage_context_projection(stage)
    invoker, stage, calls = case(tmp_path, WorkflowStage.LEARN, [10])
    with pytest.raises(RuntimeIntegrationError) as exc:
        await invoker.invoke(stage)
    assert exc.value.error_code == "FINALIZATION_CONTEXT_TOO_LARGE" and not calls
