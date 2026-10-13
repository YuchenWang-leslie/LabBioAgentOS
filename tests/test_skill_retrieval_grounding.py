"""Current tool receipts, not model prose, ground PLAN Skill assessments."""

from uuid import uuid4

import pytest

from labbioagentos import RuntimeStageInput, RuntimeWorkspaceIdentifiers, SQLiteSkillStore
from test_c9_gold_skill_lifecycle import _create_gold, _governed, _toolset


@pytest.fixture
def boundary(tmp_path):
    store = SQLiteSkillStore(tmp_path / "skills.sqlite")
    principal, workspace, artifacts, exposure, _, service, sink, recorder = _governed(tmp_path, store=store)
    tools = _toolset(principal, workspace, artifacts, exposure, service, uuid4(),
                     ("skill_search", "skill_propose_use"), recorder)
    binding = tools.binding
    stage_input = RuntimeStageInput(
        run_id=binding.run_id, stage_id=binding.stage_id,
        invocation_id=binding.invocation_id, instruction="Evaluate optional references.",
        workspace=RuntimeWorkspaceIdentifiers(**workspace.model_dump()),
        allowed_capabilities=binding.capability_allowlist,
    )
    yield tools, stage_input, sink
    store.close()


async def _pending_proposal(boundary):
    tools, _, _ = boundary
    service = tools.services.skill_service
    _create_gold(service, service.store, tools.binding.principal, tools.services.artifact_store)
    page = (await tools.skill_search())["data"]
    candidate = page["items"][0]
    return (await tools.skill_propose_use(candidate["skill_id"], candidate["version"],
        "ADAPT", "Agent-selected fixture candidate."))["data"]
