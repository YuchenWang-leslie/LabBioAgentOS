"""Approved guidance reaches the model intact within explicit view bounds."""

from uuid import uuid4

import pytest

from test_c9_gold_skill_lifecycle import (
    _context, _create_gold, _decide_use, _draft, _governed, _submit_use, _toolset,
)
from labbioagentos import SkillUsageOutcome, SkillUseMode


@pytest.mark.asyncio
async def test_parameter_failure_and_debug_guidance_reach_approved_tool_view(tmp_path):
    principal, workspace, artifacts, exposure, store, service, _, recorder = _governed(tmp_path)
    draft = _draft()
    draft = draft.model_copy(update={"procedure": draft.procedure.model_copy(update={
        "parameter_guidance": ("Source setting is a reference; inspect the current contract.",),
        "debug_lessons": ("Recheck the declared contract after revising a record writer.",),
    })})
    _, _, gold = _create_gold(service, store, principal, artifacts, draft=draft)
    run_id = uuid4()
    tools = _toolset(principal, workspace, artifacts, exposure, service, run_id,
                     ("skill_search", "skill_view"), recorder)
    search = await tools.skill_search()
    assert "parameter_guidance" not in search["data"]["items"][0]
    authorization = _decide_use(service, principal,
                               _submit_use(service, principal, workspace, gold, run_id))
    result = await tools.skill_view(str(authorization.authorization_id))
    assert result["success"]
    view = result["data"]
    for name in ("parameter_guidance", "known_failure_modes", "debug_lessons"):
        assert view[name] == list(getattr(gold.procedure, name))
    assert view["truncated_fields"] == []
    assert view["authority"] == "MODEL_CONTEXT"


@pytest.mark.asyncio
async def test_oversized_guidance_is_visibly_partial(tmp_path):
    principal, workspace, artifacts, exposure, store, service, _, _ = _governed(tmp_path)
    draft = _draft()
    draft = draft.model_copy(update={"procedure": draft.procedure.model_copy(update={
        "debug_lessons": tuple(f"Synthetic fixture note {index}" for index in range(17)),
    })})
    _, _, gold = _create_gold(service, store, principal, artifacts, draft=draft)
    run_id = uuid4()
    tools = _toolset(principal, workspace, artifacts, exposure, service, run_id,
                     ("skill_view",))
    authorization = _decide_use(service, principal,
                               _submit_use(service, principal, workspace, gold, run_id))
    result = await tools.skill_view(str(authorization.authorization_id))
    assert result["success"]
    assert len(result["data"]["debug_lessons"]) == 16
    assert result["data"]["truncated_fields"] == ["debug_lessons"]


@pytest.mark.asyncio
async def test_total_utf8_budget_does_not_emit_false_completed_evidence(tmp_path):
    from labbioagentos.model_safety import validate_model_visible_json
    principal, workspace, artifacts, exposure, store, service, _, recorder = _governed(tmp_path)
    draft = _draft()
    notes = tuple("合成测试文本" * 500 for _ in range(16))
    draft = draft.model_copy(update={"procedure": draft.procedure.model_copy(update={
        "debug_lessons": notes,
    })})
    _, _, gold = _create_gold(service, store, principal, artifacts, draft=draft)
    run_id = uuid4()
    tools = _toolset(principal, workspace, artifacts, exposure, service, run_id,
                     ("skill_view",), recorder)
    authorization = _decide_use(service, principal,
                               _submit_use(service, principal, workspace, gold, run_id))
    result = await tools.skill_view(str(authorization.authorization_id))
    assert result["success"]
    validate_model_visible_json(result["data"])
    assert "debug_lessons" in result["data"]["truncated_fields"]
    assert 0 < len(result["data"]["debug_lessons"]) < len(notes)
    assert all(item == notes[0] for item in result["data"]["debug_lessons"])
    assert tools.evidence_items()[-1].safe_result == result["data"]


@pytest.mark.asyncio
async def test_tag_filter_cannot_resurrect_superseded_version(tmp_path):
    principal, workspace, artifacts, exposure, store, service, _, _ = _governed(tmp_path)
    _, _, old = _create_gold(service, store, principal, artifacts)
    run_id = uuid4()
    authorization = _decide_use(service, principal, _submit_use(
        service, principal, workspace, old, run_id, mode=SkillUseMode.ADAPT,
    ))
    service.get_authorized_context(authorization.authorization_id, run_id=run_id,
                                   project_id=workspace.project_id, principal=principal)
    usage = service.record_usage(authorization.authorization_id,
                                  outcome=SkillUsageOutcome.SUCCEEDED)
    from test_c9_gold_skill_lifecycle import _source_bundle
    draft = _draft()
    draft = draft.model_copy(update={"procedure": draft.procedure.model_copy(update={
        "tags": frozenset({"revised-label"}),
    })})
    _, _, latest = _create_gold(service, store, principal, artifacts,
        bundle=_source_bundle(artifacts, run_id=run_id), draft=draft,
        context=_context(parent=old, source_usage_record_id=usage.usage_id))
    tools = _toolset(principal, workspace, artifacts, exposure, service, uuid4(),
                     ("skill_search",))
    assert (await tools.skill_search(required_tags=["validated-analysis"]))["data"]["items"] == []
    result = await tools.skill_search(required_tags=["revised-label"])
    assert result["data"]["items"][0]["version"] == latest.version == 2
