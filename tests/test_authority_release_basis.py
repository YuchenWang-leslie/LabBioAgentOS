"""An authority test must first establish successful authorized source access."""

import pytest

from labbioagentos import ArtifactExposureClass, ArtifactReleaseBasis, ArtifactRepresentation
from test_c9_gold_skill_lifecycle import _governed, _toolset
from uuid import uuid4


@pytest.mark.asyncio
@pytest.mark.parametrize("released", [False, True])
async def test_derived_label_does_not_substitute_for_release_authority(tmp_path, released):
    principal, workspace, artifacts, exposure, _store, service, _, recorder = _governed(tmp_path)
    ref = artifacts.register(
        artifact_type="synthetic-authority-boundary", exposure_class=ArtifactExposureClass.DERIVED,
        release_basis=(ArtifactReleaseBasis.TRUSTED_EXECUTION_DECLASSIFICATION
                       if released else ArtifactReleaseBasis.INTERNAL_ONLY),
        representation=ArtifactRepresentation(summary={"count": 3}),
        owner_user_id=principal.user_id, project_id=workspace.project_id, lab_id=workspace.lab_id,
    )
    tools = _toolset(principal, workspace, artifacts, exposure, service, uuid4(),
                     ("artifact_query",), recorder)
    result = await tools.artifact_query(str(ref.artifact_id), "SUMMARY")
    assert result["success"] is released
    assert result["information_authority"] == ("AUTHORITATIVE_EVIDENCE" if released else "CONTROL_STATE")
    if released:
        assert result["data"]["summary"] == {"count": 3}
    else:
        assert result["data"] is None
        assert result["error"]["error_code"] == "ARTIFACT_EXPOSURE_DENIED"
