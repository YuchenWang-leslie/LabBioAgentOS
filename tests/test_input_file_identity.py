"""Local input identity survives storage without entering paths or remote views."""

import json
from dataclasses import replace
from pathlib import PurePosixPath
from uuid import uuid4

import pytest

from labbioagentos import (
    ApprovedImage, ArtifactExposureClass, ArtifactRef, ArtifactReleaseBasis,
    ArtifactRepresentation, DockerCommandBuilder, ExecutionPlan, ExecutionRuntime,
    ExecutionWorkspaceManager, LabBioRuntimeToolSet, LocalArtifactStore,
    MountResolver, WorkflowStage,
)
from test_c7_4_artifact_query_audit import artifact_query_boundary


def _register(store, source, **kwargs):
    return store.register_file(
        source, artifact_type="synthetic-input", exposure_class=ArtifactExposureClass.RAW,
        representation=ArtifactRepresentation(), **kwargs,
    )


def _workspace(tmp_path, store, refs):
    mounts = MountResolver(store, approved_input_roots=(store.root,)).resolve_inputs(
        tuple(ref.artifact_id for ref in refs),
    )
    plan = ExecutionPlan(
        run_id=uuid4(), stage_id=WorkflowStage.EXECUTE, image_key="identity-test",
        script_content="pass\n", input_artifact_ids=tuple(ref.artifact_id for ref in refs),
    )
    workspace = ExecutionWorkspaceManager(tmp_path / "executions").prepare(plan, mounts)
    return plan, mounts, workspace


@pytest.mark.parametrize("filenames", (("first.csv.gz", "second.csv.gz"), ("count.csv.gz", "count.csv.gz")))
def test_identical_bytes_and_duplicate_names_remain_distinct_inputs(tmp_path, filenames):
    store = LocalArtifactStore(tmp_path / "artifacts")
    refs = []
    for index, filename in enumerate(filenames):
        directory = tmp_path / f"source-{index}"
        directory.mkdir()
        source = directory / filename
        source.write_bytes(b"identical synthetic bytes")
        refs.append(_register(store, source))
    # The old mount basename came from the store's fixed blob name, "content".
    # Reconstruct its dictionary collision without claiming a pre-edit test run.
    old_by_basename = {PurePosixPath(ref.storage_locator).name: ref.artifact_id for ref in refs}
    assert set(old_by_basename) == {"content"}
    assert len(old_by_basename) == 1
    assert [ref.original_filename for ref in refs] == list(filenames)
    reopened = LocalArtifactStore(store.root)
    assert [reopened.get_ref(ref.artifact_id).original_filename for ref in refs] == list(filenames)
    _, mounts, workspace = _workspace(tmp_path, reopened, refs)
    assert len({mount.target for mount in mounts}) == len(refs)
    assert [mount.target.name for mount in mounts] == [str(ref.artifact_id) for ref in refs]
    # A consumer keyed by mounted basename cannot silently overwrite an input.
    by_basename = {mount.target.name: mount.source.read_bytes() for mount in mounts}
    assert len(by_basename) == len(refs)
    paths = json.loads(workspace.input_manifest_path.read_text(encoding="utf-8"))
    identities = json.loads(workspace.input_identities_path.read_text(encoding="utf-8"))
    assert paths == {str(mount.artifact_id): str(mount.target) for mount in mounts}
    assert identities == {str(ref.artifact_id): {"original_filename": filename}
                          for ref, filename in zip(refs, filenames)}


def test_input_identity_manifest_excludes_unselected_artifacts(tmp_path):
    store = LocalArtifactStore(tmp_path / "artifacts")
    refs = []
    for filename in ("selected.csv.gz", "PRIVATE_UNSELECTED.csv.gz"):
        source = tmp_path / filename
        source.write_bytes(b"synthetic")
        refs.append(_register(store, source))
    _, mounts, workspace = _workspace(tmp_path, store, (refs[0],))
    identities = workspace.input_identities_path.read_text(encoding="utf-8")
    paths = workspace.input_manifest_path.read_text(encoding="utf-8")
    assert len(mounts) == 1
    assert json.loads(identities) == {str(refs[0].artifact_id): {"original_filename": "selected.csv.gz"}}
    for excluded in (str(refs[1].artifact_id), refs[1].original_filename):
        assert excluded not in identities
        assert excluded not in paths


def test_source_rename_does_not_change_persisted_identity_or_owned_bytes(tmp_path):
    source = tmp_path / "original.csv.gz"
    source.write_bytes(b"original synthetic bytes")
    store = LocalArtifactStore(tmp_path / "artifacts")
    ref = _register(store, source)
    source.rename(tmp_path / "renamed.csv.gz")
    assert not source.exists()
    reopened = LocalArtifactStore(store.root)
    restored = reopened.get_ref(ref.artifact_id)
    assert restored.original_filename == "original.csv.gz"
    _, mounts, workspace = _workspace(tmp_path, reopened, (restored,))
    assert mounts[0].source.read_bytes() == b"original synthetic bytes"
    assert mounts[0].original_filename == "original.csv.gz"
    assert json.loads(workspace.input_identities_path.read_text(encoding="utf-8")) == {
        str(ref.artifact_id): {"original_filename": "original.csv.gz"},
    }


def test_metadata_cannot_override_the_registered_source_filename(tmp_path):
    source = tmp_path / "actual.csv.gz"
    source.write_bytes(b"synthetic")
    store = LocalArtifactStore(tmp_path / "artifacts")
    ref = _register(store, source, metadata={"original_filename": "forged.csv.gz"})
    assert ref.original_filename == "actual.csv.gz"
    _, mounts, workspace = _workspace(tmp_path, store, (ref,))
    assert mounts[0].original_filename == "actual.csv.gz"
    assert "forged.csv.gz" not in workspace.input_identities_path.read_text(encoding="utf-8")


def test_legacy_and_nonfile_artifacts_do_not_invent_original_names(tmp_path):
    store = LocalArtifactStore(tmp_path / "artifacts")
    ref = store.register(
        artifact_type="synthetic-old", exposure_class=ArtifactExposureClass.RAW,
        representation=ArtifactRepresentation(), metadata={"original_filename": "not-authority.csv"},
    )
    assert ref.original_filename is None
    old = ref.model_dump(mode="json")
    old.pop("original_filename")
    assert ArtifactRef.model_validate_json(json.dumps(old)).original_filename is None
    _, mounts, workspace = _workspace(tmp_path, store, (ref,))
    assert mounts[0].original_filename is None
    assert json.loads(workspace.input_identities_path.read_text(encoding="utf-8")) == {
        str(ref.artifact_id): {"original_filename": None},
    }


@pytest.mark.parametrize("filename", (
    "private, --privileged\n=value.csv.gz",
    "private\\name 'quoted'.csv.gz",
    "细胞计数.csv.gz",
    "a" * 255,
))
def test_unusual_original_names_are_json_data_not_mount_syntax(tmp_path, filename):
    source = tmp_path / filename
    source.write_bytes(b"synthetic")
    store = LocalArtifactStore(tmp_path / "artifacts")
    ref = _register(store, source)
    plan, mounts, workspace = _workspace(tmp_path, store, (ref,))
    assert mounts[0].target.name == str(ref.artifact_id)
    assert mounts[0].target.is_relative_to(PurePosixPath("/labbio/inputs"))
    assert mounts[0].original_filename == filename
    identities = json.loads(workspace.input_identities_path.read_text(encoding="utf-8"))
    assert identities[str(ref.artifact_id)]["original_filename"] == filename
    image = ApprovedImage(
        key=plan.image_key, reference="sha256:" + "a" * 64, runtime=ExecutionRuntime.PYTHON,
    )
    argv = DockerCommandBuilder().build(plan, image, workspace, mounts)
    assert filename not in " ".join(argv)
    assert "LABBIO_INPUT_IDENTITIES_PATH=/labbio/input-identities.json" in argv
    identity_mount = next(value for value in argv if "target=/labbio/input-identities.json" in value)
    assert identity_mount.endswith(",readonly")
    assert str(workspace.input_identities_path) in identity_mount
    assert argv[argv.index("--network") + 1] == "none"


@pytest.mark.asyncio
@pytest.mark.parametrize("exposure", (ArtifactExposureClass.RAW, ArtifactExposureClass.DERIVED))
async def test_private_identity_never_enters_remote_tools_or_trace(tmp_path, artifact_query_boundary, exposure):
    sink, binding, _, original_tools = artifact_query_boundary
    tools = LabBioRuntimeToolSet(
        replace(binding, stage_id=WorkflowStage.UNDERSTAND,
                capability_allowlist=("artifact_query", "artifact_list")),
        original_tools.services,
    )
    source = tmp_path / "PRIVATE_FILENAME_SENTINEL.csv.gz"
    source.write_bytes(b"PRIVATE_CONTENT_SENTINEL")
    ref = tools.services.artifact_store.register_file(
        source, artifact_type="synthetic-input", exposure_class=exposure,
        release_basis=ArtifactReleaseBasis.TRUSTED_EXECUTION_DECLASSIFICATION
        if exposure is ArtifactExposureClass.DERIVED else ArtifactReleaseBasis.RAW_INGESTION,
        representation=ArtifactRepresentation(summary={"count": 1}, records=({"count": 1},), record_count=1),
        metadata={"original_filename": "PRIVATE_METADATA_FILENAME"},
        owner_user_id=binding.principal.user_id, project_id=binding.workspace.project_id,
        lab_id=binding.workspace.lab_id, run_id=binding.run_id,
    )
    assert ref.original_filename == source.name
    replies = [await tools.artifact_list()]
    for view in ("METADATA", "SCHEMA", "SUMMARY", "TOP_N"):
        response = await tools.artifact_query(str(ref.artifact_id), view)
        assert response["success"] is (exposure is ArtifactExposureClass.DERIVED)
        replies.append(response)
    encoded = json.dumps({
        "replies": replies,
        "evidence": [item.model_dump(mode="json") for item in tools.evidence_items()],
        "trace": [event.model_dump(mode="json") for event in sink.read(binding.run_id)],
    })
    for private in (source.name, str(source), ref.storage_locator, "original_filename",
                    "PRIVATE_METADATA_FILENAME", "PRIVATE_CONTENT_SENTINEL"):
        assert private not in encoded
