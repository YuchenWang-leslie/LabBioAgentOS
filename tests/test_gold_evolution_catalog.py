"""The approved version, its parent and current applicability survive each catalog view."""

from uuid import UUID, uuid4

import pytest

from labbioagentos import RunStatus, SkillProposalContext, SkillUseMode
from labbioagentos.local_gold import build_personal_gold_service, close_personal_gold
from labbioagentos.local_gold_library import _export_dirname, export_gold_library, list_gold_library
from test_c9_gold_skill_lifecycle import (
    _context, _create_gold, _decide_use, _draft, _governed, _source_bundle,
    _submit_use, _toolset,
)


@pytest.fixture
def governed_library(tmp_path):
    library = build_personal_gold_service(tmp_path / "GoldSkills", "user-a")
    try:
        yield _governed(tmp_path, store=library.store)
    finally:
        close_personal_gold(library)


@pytest.mark.asyncio
async def test_linear_head_and_root_keep_exact_parent_in_both_catalogs(governed_library):
    principal, workspace, artifacts, exposure, store, service, _, recorder = governed_library
    _, _, root = _create_gold(service, store, principal, artifacts)
    _, _, updated = _create_gold(service, store, principal, artifacts, context=_context(parent=root))
    tools = _toolset(principal, workspace, artifacts, exposure, service, uuid4(),
                     ("skill_search",), recorder)
    result = await tools.skill_search()
    assert result["success"] and result["data"]["available_count"] == 1
    head = result["data"]["items"][0]
    assert (head["skill_id"], head["version"]) == (str(root.skill_id), updated.version)
    assert (head["parent_skill_id"], head["parent_version"]) == (str(root.skill_id), 1)
    assert head["evolution_kind"] == "UPDATE"
    assert "source_usage_record_id" not in head
    assert "workflow_outline" not in head
    historical = list_gold_library(service, principal, all_versions=True)["items"]
    assert [(row["version"], row["parent_skill_id"], row["parent_version"])
            for row in historical] == [(1, None, None), (2, str(root.skill_id), 1)]


@pytest.mark.asyncio
async def test_fork_and_parent_selectable_by_exact_id_with_owner_provenance(governed_library):
    principal, workspace, artifacts, exposure, store, service, _, recorder = governed_library
    _, _, parent = _create_gold(service, store, principal, artifacts)
    adapted_run = uuid4()
    use = _submit_use(service, principal, workspace, parent, adapted_run, mode=SkillUseMode.ADAPT)
    approved = _decide_use(service, principal, use)
    source_tools = _toolset(principal, workspace, artifacts, exposure, service, adapted_run,
                            ("skill_view",), recorder)
    assert (await source_tools.skill_view(str(approved.authorization_id)))["success"]
    usage, = service.finalize_run_usage(adapted_run, RunStatus.COMPLETED)
    draft = _draft()
    draft = draft.model_copy(update={"procedure": draft.procedure.model_copy(update={
        "applicability": "Use when the input includes the alternate fixture metadata.",
    })})
    context = SkillProposalContext.model_validate({
        **_context(parent=parent, source_usage_record_id=usage.usage_id).model_dump(),
        "evolution_kind": "FORK",
    })
    _, _, fork = _create_gold(service, store, principal, artifacts, draft=draft, context=context,
                              bundle=_source_bundle(artifacts, run_id=adapted_run))
    assert fork.skill_id != parent.skill_id

    run_id = uuid4()
    tools = _toolset(principal, workspace, artifacts, exposure, service, run_id,
                     ("skill_search", "skill_propose_use", "skill_view"), recorder)
    page = await tools.skill_search()
    assert page["success"] and page["data"]["available_count"] == 2
    items = {row["skill_id"]: row for row in page["data"]["items"]}
    for skill in (parent, fork):
        row = items[str(skill.skill_id)]
        assert row["applicability_preview"] == skill.procedure.applicability
        assert "source_usage_record_id" not in row
    assert items[str(fork.skill_id)]["evolution_kind"] == "FORK"
    assert items[str(fork.skill_id)]["parent_skill_id"] == str(parent.skill_id)
    assert items[str(fork.skill_id)]["parent_version"] == parent.version

    for skill in (parent, fork):
        selection = items[str(skill.skill_id)]
        proposed = await tools.skill_propose_use(selection["skill_id"], selection["version"],
                                                 "REFERENCE", "Review the fixture applicability.")
        assert proposed["success"]
        proposal = store.get_use_proposal(UUID(proposed["data"]["proposal_id"]))
        assert (proposal.skill_id, proposal.skill_version) == (skill.skill_id, skill.version)
        authorization = _decide_use(service, principal, proposal)
        view = await tools.skill_view(str(authorization.authorization_id))
        assert view["success"] and view["data"]["applicability"] == skill.procedure.applicability

    catalog = list_gold_library(service, principal, project_id=workspace.project_id)
    entry = next(row for row in catalog["items"] if row["skill_id"] == str(fork.skill_id))
    assert (entry["evolution_kind"], entry["parent_skill_id"], entry["parent_version"]) == (
        "FORK", str(parent.skill_id), parent.version,
    )
    assert entry["source_usage_record_id"] == str(usage.usage_id)
    export_gold_library(service, principal)
    index = (store.path.parent / "INDEX.md").read_text()
    version_file = (store.path.parent / _export_dirname(fork) / "skill.md").read_text()
    assert "FORK" in index and "FORK" in version_file
    assert str(parent.skill_id) in version_file
    assert str(usage.usage_id) in version_file
    assert fork.procedure.applicability in version_file
