from dataclasses import replace
from uuid import uuid4

import pytest

from labbioagentos.contracts import InformationAuthority
from labbioagentos.runtime.contracts import CapabilityEvidenceItem, CapabilityEvidenceStatus
from labbioagentos.runtime.tooling import ToolResult
from labbioagentos.runtime.observations import ObservationStore
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401


def observation():
    return CapabilityEvidenceItem(
        capability_invocation_id=uuid4(), actor_profile_key="test",
        actor_agent_name="Test", capability_name="artifact_query",
        information_authority=InformationAuthority.AUTHORITATIVE_EVIDENCE,
        status=CapabilityEvidenceStatus.COMPLETED,
        safe_result={"text": "中文" * 1000},
    )


def public_response(item):
    return ToolResult(success=True, capability_invocation_id=item.capability_invocation_id,
                      information_authority=item.information_authority,
                      data=item.safe_result).model_dump(mode="json")


def test_observation_persists_exact_content_and_refuses_foreign_run(tmp_path, artifact_query_boundary):
    _, binding, _, _ = artifact_query_boundary
    item = observation()
    store = ObservationStore(tmp_path / "observations")
    store.save(binding, item, public_response(item))
    restored = ObservationStore(store.root).load(binding, item.capability_invocation_id)
    assert restored.item == item
    assert restored.public_response == public_response(item)
    with pytest.raises(PermissionError):
        store.load(replace(binding, run_id=uuid4()), item.capability_invocation_id)
    changed = item.model_copy(update={"safe_result": {"text": "changed"}})
    with pytest.raises(ValueError, match="immutable"):
        store.save(binding, changed, public_response(changed))


def test_observation_store_refuses_symlink(tmp_path, artifact_query_boundary):
    _, binding, _, _ = artifact_query_boundary
    item = observation()
    store = ObservationStore(tmp_path / "observations")
    store.save(binding, item, public_response(item))
    path = store.root / f"{item.capability_invocation_id}.json"
    alias = tmp_path / "alias"
    alias.symlink_to(store.root, target_is_directory=True)
    with pytest.raises(ValueError):
        ObservationStore(alias)
    assert path.is_file()
