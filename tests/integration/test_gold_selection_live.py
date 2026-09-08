"""Opt-in live selection over synthetic catalogs, never real Gold promotion.

Only metadata relevance is tested. No task execution, use approval, skill_view,
scientific procedure, or production-library write is performed.
"""

import asyncio
from dataclasses import asdict
import json
import os
from pathlib import Path
from uuid import UUID

import pytest

from labbioagentos import PantheonRuntimeFactory, WorkflowStage
from labbioagentos.cli import _close
from labbioagentos.local_config import build_application, load_settings
from labbioagentos.runtime.profiles import RuntimeInvocationMode
from test_runtime_milestone_c9_gold_skills import (
    _retrieval_smoke_toolset, _seed_retrieval_smoke_gold,
)


# Synthetic metadata fixtures, not Agent-authored or scientifically accepted Gold.
# Expected matches below stay host-side and are never passed to the Agent.
CATALOG = {
    "validation": {
        "description": "Check newly received tabular data before analysis.",
        "applicability": "New delimited tables requiring schema, inferred-type, missingness and duplicate checks.",
        "limitations": ("Not for presentation-only editing of already validated results.",),
        "tags": frozenset({"tabular", "validation"}),
        "artifact_types": frozenset({"delimited-table"}),
        "input_contract_ids": ("new-delimited-table",),
        "output_contract_ids": ("bounded-validation-report",),
    },
    "presentation": {
        "description": "Format already validated tabular results for presentation.",
        "applicability": "Validated result tables requiring readable headings, layout and number display without changing values.",
        "limitations": ("Does not validate new source data or translate narrative prose.",),
        "tags": frozenset({"tabular", "presentation"}),
        "artifact_types": frozenset({"validated-summary-table"}),
        "input_contract_ids": ("validated-summary-table",),
        "output_contract_ids": ("presentation-ready-summary",),
    },
    "images": {
        "description": "Arrange image tiles for visual inspection.",
        "applicability": "Image collections requiring tiled visual inspection.",
        "limitations": ("Not applicable to tabular data or narrative-only inputs.",),
        "tags": frozenset({"images", "tiling"}),
        "artifact_types": frozenset({"image-collection"}),
        "input_contract_ids": ("image-collection",),
        "output_contract_ids": ("image-tile-index",),
    },
}
VALIDATION_TASK = "收到一份新的 CSV 表格，请检查列结构、数据类型、缺失和重复情况，汇总问题，不修改原文件。"
PRESENTATION_TASK = "这份汇总表的数据已经核验过，现在只需要整理列标题、排版和数字显示，让它适合汇报；不要更改数值或重新做数据检查。"
NO_MATCH_TASK = "我有一段法语短诗，希望翻译成中文并保留原来的抒情语气。"
CASES = (
    ("fit-first", ("validation", "presentation", "images"), VALIDATION_TASK, "validation"),
    ("fit-middle", ("presentation", "validation", "images"), VALIDATION_TASK, "validation"),
    ("fit-last", ("presentation", "images", "validation"), VALIDATION_TASK, "validation"),
    ("different-task", ("validation", "images", "presentation"), PRESENTATION_TASK, "presentation"),
    ("no-match", ("validation", "presentation", "images"), NO_MATCH_TASK, None),
)
REQUEST = "请判断个人技能库里有没有适合下面任务的参考；有则提出使用申请，没有就说明不采用。这里只测试技能选择，不执行任务。任务："


@pytest.mark.skipif(os.environ.get("LABBIO_GOLD_SELECTION_LIVE") != "1",
                    reason="Explicit provider selection diagnostic required")
@pytest.mark.parametrize("case_id,order,task,expected", CASES, ids=[case[0] for case in CASES])
@pytest.mark.asyncio
async def test_real_model_selects_from_multiple_skills(case_id, order, task, expected):
    output = Path(os.environ["LABBIO_GOLD_SELECTION_OUTPUT"]) / case_id
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    settings = load_settings(Path(os.environ["LABBIO_GOLD_SELECTION_CONFIG"]))
    settings = settings.model_copy(update={
        "managed_root": None,
        "gold_root": output / "synthetic-library",
        "result_root": output,
        "input_roots": (output,),
        "identity": settings.identity.model_copy(update={
            "user_id": "synthetic-selection-test",
            "project_id": "synthetic-selection-project",
            "lab_id": "synthetic-selection-lab",
        }),
    })
    application = build_application(settings, output / "runtime")
    try:
        service = application.configuration.skill_service
        candidates = {
            key: _seed_retrieval_smoke_gold(
                service.store, principal=settings.principal,
                name=f"Catalog {index + 1}", **CATALOG[key],
            ) for index, key in enumerate(order)
        }
        toolset = _retrieval_smoke_toolset(
            principal=settings.principal, workspace=settings.workspace,
            artifacts=application.artifact_store, access=application.access_service,
            service=service, recorder=application.trace_recorder,
            actor_name="CoordinatorAgent",
        )
        assembly = next(item for item in application.configuration.stage_assemblies
                        if item.stage_id is WorkflowStage.PLAN)
        factory = PantheonRuntimeFactory(application.configuration.profile_catalog)
        agent, prompt = await factory.create_agent(
            assembly.root_profile_key, toolset=toolset,
            prompt_values=dict(assembly.capability_prompt_values),
            invocation_mode=RuntimeInvocationMode.CAPABILITY,
        )
        context = {
            "scope": "SYNTHETIC_METADATA_SELECTION_ONLY_NOT_REAL_GOLD_OR_ANALYSIS",
            "runtime_revision": application.configuration.runtime_revision,
            "model": settings.provider.model_identifier,
            "instructions": prompt.sanitized_text,
            "user_request": REQUEST + task,
            "catalog": {key: item.model_dump(mode="json") for key, item in candidates.items()},
            "expected_key_not_sent_to_model": expected,
            "expected_id_not_sent_to_model": str(candidates[expected].skill_id) if expected else None,
            "max_turns": assembly.max_capability_turns,
            "timeout_seconds": 240,
        }
        (output / "test-context.json").write_text(json.dumps(context, ensure_ascii=False, indent=2) + "\n")
        # Real provider and production ToolSet; no forced tool call or answer injection.
        turns = []
        response = await asyncio.wait_for(agent.run(
            REQUEST + task, max_turns=assembly.max_capability_turns, tool_timeout=60,
            process_turn_observation=lambda observation: turns.append(asdict(observation)),
        ), 240)
        last = response.details.messages[-1] if response.details and response.details.messages else {}
        completed = (not response.interrupt and last.get("role") == "assistant"
                     and not last.get("tool_calls") and bool(last.get("content"))
                     and bool(turns) and turns[-1]["finish_reason"] == "stop")
        evidence = toolset.evidence_items()
        proposals = [service.store.get_use_proposal(UUID(item.safe_result["proposal_id"]))
                     for item in evidence if item.capability_name == "skill_propose_use"
                     and item.safe_result is not None]
        selected = [next(key for key, value in candidates.items() if value.skill_id == proposal.skill_id)
                    for proposal in proposals]
        pages = [item.safe_result for item in evidence
                 if item.capability_name == "skill_search" and item.safe_result is not None]
        expected_ids = {str(item.skill_id) for item in candidates.values()}
        seen_ids = {item["skill_id"] for page in pages for item in page["items"]}
        full_pages = [page for page in pages if page["returned_count"] == 3]
        expected_selection = [] if expected is None else [expected]
        result = {
            "case_id": case_id, "selected_keys": selected, "expected_keys": expected_selection,
            "passed": selected == expected_selection and seen_ids == expected_ids and completed,
            "all_candidates_returned": seen_ids == expected_ids,
            "assistant_completed": completed,
            "last_role": last.get("role"),
            "provider_turns": turns,
            "returned_orders": [[item["name"] for item in page["items"]] for page in pages],
            "proposals": [proposal.model_dump(mode="json") for proposal in proposals],
            "evidence": [item.model_dump(mode="json") for item in evidence],
            "response": str(response.content),
        }
        (output / "selection-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({"case": case_id, "selected": selected, "passed": result["passed"]}), flush=True)
        assert seen_ids == expected_ids, "Selection was not evaluated against the full catalog"
        assert full_pages and [item["skill_id"] for item in full_pages[0]["items"]] == [
            str(candidates[key].skill_id) for key in order]
        assert selected == expected_selection, "Persisted model choice did not match fixture applicability"
        assert completed, "A final tool result or interrupted response is not an Agent decision"
        assert all(proposal.skill_version == candidates[key].version for proposal, key in zip(proposals, selected))
        assert str(response.content).strip()
    finally:
        _close(application)
