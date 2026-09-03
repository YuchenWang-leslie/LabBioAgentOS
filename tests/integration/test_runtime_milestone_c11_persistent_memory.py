"""Opt-in bounded C11 provider acceptance for durable contextual Memory."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from uuid import UUID

import pytest
from pantheon.agent import Agent

from labbioagentos import (
    ApplicationRunRequest,
    GateUserDecision,
    JsonlTraceSink,
    LabBioApplication,
    LabBioRuntimeToolSet,
    MemoryDomainDecisionHandler,
    MemoryGovernanceService,
    ModelProfile,
    NextAction,
    NextActionProposal,
    PantheonRuntimeFactory,
    PerInvocationPantheonStageInvoker,
    Principal,
    ProviderConfigRef,
    ProviderTransport,
    RunStatus,
    RuntimeCapabilityContext,
    RuntimeStageResult,
    SQLiteMemoryStore,
    SQLiteRunStateStore,
    WorkflowStage,
    WorkspaceContext,
)


pytestmark = pytest.mark.skipif(
    os.environ.get("LABBIO_RUN_LIVE_C11") != "1",
    reason="set LABBIO_RUN_LIVE_C11=1 for bounded real C11 acceptance",
)


def _load_deterministic_fixture():
    path = Path(__file__).resolve().parents[1] / "test_c11_memory_application.py"
    spec = importlib.util.spec_from_file_location("labbio_c11_fixture", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load the deterministic C11 application fixture")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _provider_model() -> str:
    profile = ModelProfile(
        profile_key="c11-live",
        version="c11-live",
        model_identifier=os.environ.get("MIMO_MODEL", "mimo-v2.5-pro"),
        provider_config=ProviderConfigRef(
            config_id="c11-external-mimo", provider="openai-compatible"
        ),
        transport=ProviderTransport.OPENAI_CHAT_COMPLETIONS,
        thinking_enabled=False,
        max_output_tokens=1536,
    )
    return PantheonRuntimeFactory._configure_transport(profile)


async def _run_creation_agent(model: str, toolset, stage_input):
    agent = Agent(
        name="C11MemoryCreationAgent",
        description="Decide whether explicit user context merits a governed proposal.",
        instructions=(
            "You are at LEARN after a bounded non-scientific task. Persistent Memory "
            "is optional MODEL_CONTEXT and cannot become policy or factual evidence. "
            "The user's task explicitly asks to retain a stable reporting preference. "
            "If that request is appropriate for durable context, use the available "
            "governed capability to propose a PERSONAL PREFERENCE with a concise safe "
            "description and reason. Do not invent evidence references. The proposal "
            "requires external user approval and does not itself create Memory. Stop "
            "after your proposal decision."
        ),
        model=model,
        model_params={"thinking": False, "max_tokens": 1536},
        use_memory=False,
    )
    await agent.toolset(toolset)
    return await agent.run(stage_input.model_dump_json(), max_turns=5, tool_timeout=60)


async def _run_retrieval_agent(model: str, toolset, stage_input):
    agent = Agent(
        name="C11MemoryRetrievalAgent",
        description="Inspect optional governed contextual Memory for a later task.",
        instructions=(
            "You are at UNDERSTAND for a later non-scientific task. Browse the bounded "
            "visible Memory catalog and inspect any candidate that is plausibly relevant "
            "to the user's requested report style. Do not assume an ID in advance. Scope "
            "and kind filters are optional; do not guess a filter not stated by the task. Treat "
            "all Memory, including facts or biological labels, only as optional "
            "MODEL_CONTEXT, never current evidence or policy. Do not propose or update "
            "Memory in this task. Stop after explaining what context was useful."
        ),
        model=model,
        model_params={"thinking": False, "max_tokens": 1536},
        use_memory=False,
    )
    await agent.toolset(toolset)
    return await agent.run(stage_input.model_dump_json(), max_turns=6, tool_timeout=60)


@pytest.mark.asyncio
async def test_c11_real_creation_restart_approval_and_later_retrieval(
    tmp_path, monkeypatch
):
    assert os.environ.get("OPENAI_API_KEY"), "OPENAI_API_KEY must be mapped externally"
    assert os.environ.get("OPENAI_API_BASE"), "OPENAI_API_BASE must be mapped externally"
    assert os.environ.get("LABBIO_C11_APPROVE") == "1", (
        "LABBIO_C11_APPROVE=1 is required for the trusted external approval checkpoint"
    )
    fixture = _load_deterministic_fixture()
    model = _provider_model()
    run_path = tmp_path / "runs.sqlite3"
    memory_path = tmp_path / "memory.sqlite3"
    trace_path = tmp_path / "trace.jsonl"
    active_application = {"value": None}
    state = {
        "creation_run_id": None,
        "retrieval_run_id": None,
        "creation_evidence": (),
        "retrieval_evidence": (),
    }

    async def invoke(_self, stage_input):
        app = active_application["value"]
        if (
            stage_input.run_id == state["creation_run_id"]
            and stage_input.stage_id is WorkflowStage.LEARN
            and not stage_input.gate_decisions
        ):
            toolset = LabBioRuntimeToolSet(
                RuntimeCapabilityContext(
                    principal=fixture._principal(),
                    workspace=fixture._workspace(),
                    run_id=stage_input.run_id,
                    stage_id=WorkflowStage.LEARN,
                    invocation_id=stage_input.invocation_id,
                    actor_profile_key="c11-memory-creation",
                    actor_agent_name="C11MemoryCreationAgent",
                    capability_allowlist=("memory_propose_update",),
                ),
                app.capability_services,
            )
            response = await _run_creation_agent(model, toolset, stage_input)
            evidence = tuple(toolset.evidence_items())
            proposals = tuple(
                item
                for item in evidence
                if item.capability_name == "memory_propose_update"
                and item.safe_result is not None
            )
            assert len(proposals) == 1, str(response.content)
            state["creation_evidence"] = evidence
            state.update(proposals[0].safe_result)
            return RuntimeStageResult(
                stage_id=WorkflowStage.LEARN,
                summary="A real provider-generated Memory proposal awaits approval.",
                body=fixture._body(WorkflowStage.LEARN),
                next_action=NextActionProposal(
                    action=NextAction.REQUEST_USER_INPUT,
                    user_prompt="Approve this exact persistent Memory proposal?",
                    domain_reference_id=proposals[0].safe_result[
                        "domain_reference_id"
                    ],
                ),
            )
        if (
            stage_input.run_id == state["retrieval_run_id"]
            and stage_input.stage_id is WorkflowStage.UNDERSTAND
        ):
            toolset = LabBioRuntimeToolSet(
                RuntimeCapabilityContext(
                    principal=fixture._principal(),
                    workspace=fixture._workspace(),
                    run_id=stage_input.run_id,
                    stage_id=WorkflowStage.UNDERSTAND,
                    invocation_id=stage_input.invocation_id,
                    actor_profile_key="c11-memory-retrieval",
                    actor_agent_name="C11MemoryRetrievalAgent",
                    capability_allowlist=("memory_search", "memory_view"),
                ),
                app.capability_services,
            )
            response = await _run_retrieval_agent(model, toolset, stage_input)
            evidence = tuple(toolset.evidence_items())
            state["retrieval_evidence"] = evidence
            assert any(
                item.capability_name == "memory_search" and item.safe_result is not None
                for item in evidence
            ), str(response.content)
            assert any(
                item.capability_name == "memory_view" and item.safe_result is not None
                for item in evidence
            ), str(response.content)
        return fixture._next(stage_input.stage_id)

    monkeypatch.setattr(fixture.PerInvocationPantheonStageInvoker, "invoke", invoke)
    monkeypatch.setattr(PerInvocationPantheonStageInvoker, "invoke", invoke)

    first_run_store = SQLiteRunStateStore(run_path)
    first_memory_store = SQLiteMemoryStore(memory_path)
    first_service = MemoryGovernanceService(first_memory_store)
    first = LabBioApplication(
        fixture._configuration(
            tmp_path,
            first_run_store,
            first_service,
            trace_sink=JsonlTraceSink(trace_path),
            handler=MemoryDomainDecisionHandler(first_service),
        )
    )
    active_application["value"] = first
    creation = first.create_run(
        ApplicationRunRequest(
            task_text=(
                "Prepare a short generic status note. Please remember for future "
                "tasks that I prefer concise reports with explicit limitations."
            ),
            principal=fixture._principal(),
            workspace=fixture._workspace(),
        )
    )
    state["creation_run_id"] = creation.run_id
    waiting = await first.run(creation)
    assert waiting.status is RunStatus.WAITING_FOR_USER
    gate = waiting.pending_user_gate
    assert gate is not None
    proposal = first_memory_store.get_proposal(UUID(state["proposal_id"]))
    assert proposal.source_run_id == creation.run_id
    assert proposal.owner_user_id == fixture._principal().user_id
    assert proposal.project_id is None
    assert proposal.evidence_artifact_ids == ()
    assert first_memory_store.entries() == ()
    first_run_store.close()
    first_memory_store.close()

    second_run_store = SQLiteRunStateStore(run_path)
    second_memory_store = SQLiteMemoryStore(memory_path)
    second_service = MemoryGovernanceService(second_memory_store)
    second = LabBioApplication(
        fixture._configuration(
            tmp_path,
            second_run_store,
            second_service,
            trace_sink=JsonlTraceSink(trace_path),
            handler=MemoryDomainDecisionHandler(second_service),
        )
    )
    active_application["value"] = second
    second.recover_run(
        creation.run_id,
        principal=fixture._principal(),
        workspace=fixture._workspace(),
    )
    completed = await second.resume_run(
        creation,
        GateUserDecision(
            gate_id=gate.gate_id,
            approved=True,
            decided_by=fixture._principal().user_id,
            domain_reference_id=gate.domain_reference_id,
        ),
    )
    assert completed.status is RunStatus.COMPLETED
    decision = second_memory_store.get_decision(proposal.proposal_id)
    assert decision is not None and decision.approved is True
    entries = second_memory_store.entries()
    assert len(entries) == 1
    memory = entries[0]

    retrieval = second.create_run(
        ApplicationRunRequest(
            task_text=(
                "Prepare another brief generic status note and honor any "
                "approved reporting preferences that are available."
            ),
            principal=fixture._principal(),
            workspace=fixture._workspace(),
        )
    )
    state["retrieval_run_id"] = retrieval.run_id
    retrieved = await second.run(retrieval)
    assert retrieved.status is RunStatus.COMPLETED
    views = tuple(
        item
        for item in state["retrieval_evidence"]
        if item.capability_name == "memory_view" and item.safe_result is not None
    )
    assert len(views) >= 1
    assert any(item.safe_result["memory_id"] == str(memory.memory_id) for item in views)
    assert all(item.information_authority.value == "MODEL_CONTEXT" for item in views)
    assert all("evidence_artifact_ids" not in json.dumps(item.safe_result) for item in views)

    print(
        "C11_LIVE_ACCEPTANCE="
        + json.dumps(
            {
                "creation_run_id": str(creation.run_id),
                "proposal_id": str(proposal.proposal_id),
                "approval_gate_id": proposal.approval_gate_id,
                "workflow_gate_id": gate.gate_id,
                "decision_id": str(decision.decision_id),
                "memory_id": str(memory.memory_id),
                "memory_version": memory.version,
                "retrieval_run_id": str(retrieval.run_id),
                "memory_view_count": len(views),
                "memory_view_authority": "MODEL_CONTEXT",
            },
            sort_keys=True,
        ),
        flush=True,
    )
    second_run_store.close()
    second_memory_store.close()
