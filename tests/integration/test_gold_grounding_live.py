"""Real two-mode PLAN selection; synthetic catalogs, no execution or approval."""

import asyncio
import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from labbioagentos import (
    NextAction, PerInvocationPantheonStageInvoker, RuntimeStageInput,
    RuntimeStageResult, RuntimeWorkflowControlView, RuntimeWorkspaceIdentifiers,
    WorkflowStage,
)
from labbioagentos.cli import _close
from labbioagentos.local_config import build_application, load_settings, runtime_manifest
from test_gold_selection_live import CATALOG, CASES, REQUEST
from test_runtime_milestone_c9_gold_skills import _seed_retrieval_smoke_gold


@pytest.mark.skipif(os.environ.get("LABBIO_GOLD_GROUNDING_LIVE") != "1",
                    reason="Explicit real two-mode PLAN verification required")
@pytest.mark.parametrize("case_id,order,task,expected", (CASES[3], CASES[4], CASES[2]),
                         ids=("original-failure", "no-match", "neighbor-fit"))
@pytest.mark.asyncio
async def test_current_plan_grounds_skill_selection(case_id, order, task, expected):
    output = Path(os.environ["LABBIO_GOLD_GROUNDING_OUTPUT"]) / case_id
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    settings = load_settings(Path(os.environ["LABBIO_GOLD_SELECTION_CONFIG"]))
    settings = settings.model_copy(update={
        "managed_root": None, "gold_root": output / "synthetic-library",
        "result_root": output, "input_roots": (output,),
        "identity": settings.identity.model_copy(update={
            "user_id": "synthetic-grounding-test", "project_id": "synthetic-grounding-project",
            "lab_id": "synthetic-grounding-lab",
        }),
    })
    application = build_application(settings, output / "runtime")
    try:
        service = application.configuration.skill_service
        candidates = {key: _seed_retrieval_smoke_gold(service.store, principal=settings.principal,
            name=f"Catalog {index + 1}", **CATALOG[key]) for index, key in enumerate(order)}
        assembly = next(item for item in application.configuration.stage_assemblies
                        if item.stage_id is WorkflowStage.PLAN)
        captured = {}

        def observe(kind, value):
            application.configuration.boundary_observer(kind, value)
            captured[kind] = value

        stage_input = RuntimeStageInput(
            run_id=uuid4(), stage_id=WorkflowStage.PLAN, instruction=REQUEST + task,
            workspace=RuntimeWorkspaceIdentifiers(**settings.workspace.model_dump()),
            allowed_capabilities=assembly.capability_allowlist,
            execution_capability=application.execution_capability,
            workflow_control=RuntimeWorkflowControlView(
                current_stage=WorkflowStage.PLAN, transition_targets=(WorkflowStage.PREFLIGHT,),
                request_user_input_available=assembly.user_input_enabled,
                retry_available=assembly.retry_enabled, retry_transition_targets=(WorkflowStage.PLAN,),
                finish_available=False,
            ),
        )
        invoker = PerInvocationPantheonStageInvoker(
            assembly=assembly, factory=application.runtime_factory,
            principal=settings.principal, workspace=settings.workspace,
            services=application.capability_services, trace_recorder=application.trace_recorder,
            execution_capability=application.execution_capability, boundary_observer=observe,
        )
        (output / "test-context.json").write_text(json.dumps({
            "scope": "TWO_MODE_PLAN_ONLY_SYNTHETIC_CATALOG_NOT_ANALYSIS_OR_GOLD_PROMOTION",
            "manifest": runtime_manifest(settings),
            "request": REQUEST + task,
            "catalog": {key: value.model_dump(mode="json") for key, value in candidates.items()},
            "expected_not_sent_to_model": expected,
        }, ensure_ascii=False, indent=2) + "\n")
        result = await asyncio.wait_for(invoker.invoke(stage_input), timeout=300)
        (output / "stage-result.json").write_text(result.model_dump_json(indent=2) + "\n")
        assert RuntimeStageResult.model_validate_json((output / "stage-result.json").read_text()) == result
        evidence = captured["capability_evidence"]
        assessment = result.body.skill_assessment
        assert assessment is not None
        search_ids = {item.capability_invocation_id for item in evidence.items
                      if item.capability_name == "skill_search" and item.status.value == "COMPLETED"}
        assert assessment.search_capability_invocation_ids
        assert set(assessment.search_capability_invocation_ids).issubset(search_ids)
        if expected is None:
            assert assessment.status == "NO_SUITABLE_RETURNED_CANDIDATE"
            assert assessment.proposal_id is None
            assert result.next_action.action is NextAction.TRANSITION
        else:
            assert assessment.status == "USE_PROPOSED"
            proposal = service.store.get_use_proposal(assessment.proposal_id)
            assert proposal.skill_id == candidates[expected].skill_id
            assert proposal.skill_version == candidates[expected].version
            assert result.next_action.action is NextAction.REQUEST_USER_INPUT
            matching = [item.safe_result for item in evidence.items
                        if item.capability_name == "skill_propose_use" and item.safe_result
                        and UUID(item.safe_result["proposal_id"]) == proposal.proposal_id]
            assert len(matching) == 1
            assert result.next_action.domain_reference_id == matching[0]["domain_reference_id"]
        print(json.dumps({"case": case_id, "status": assessment.status,
                          "search_receipts": len(assessment.search_capability_invocation_ids),
                          "next_action": result.next_action.action.value}), flush=True)
    finally:
        _close(application)
