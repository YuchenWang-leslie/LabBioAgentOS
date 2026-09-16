"""User-authorized fixed RAW head, not a pageable raw-data query."""

import gzip
import json
from dataclasses import replace

import pytest

from labbioagentos import (
    AccessService, InMemoryProjectStore, Project,
    ArtifactConsumer, ArtifactExposureClass, ArtifactExposureDenied,
    ArtifactExposureService, ArtifactQuery, ArtifactReleaseBasis,
    ArtifactRepresentation, ArtifactViewType, ExposurePolicy, LocalArtifactStore,
    WorkflowStage,
)
from labbioagentos.runtime.contracts import RuntimeInputArtifactUsage
from labbioagentos.runtime.tooling import LabBioRuntimeToolSet
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401


def register(store, tmp_path, text, name="input.csv", **identity):
    source = tmp_path / name
    data = text.encode("utf-8")
    source.write_bytes(gzip.compress(data) if name.endswith(".gz") else data)
    return store.register_file(
        source, artifact_type="input", exposure_class=ArtifactExposureClass.RAW,
        release_basis=ArtifactReleaseBasis.RAW_INGESTION,
        representation=ArtifactRepresentation(), **identity,
    )


def query(store, ref, view=ArtifactViewType.METADATA):
    return ArtifactExposureService(store, ExposurePolicy()).artifact_query(
        ref.artifact_id, ArtifactQuery(view_type=view), ArtifactConsumer.REMOTE_LLM,
    )


@pytest.mark.parametrize("name,delimiter", [("input.csv", ","), ("input.csv.gz", ","),
                                         ("input.tsv", "\t"), ("input.tsv.gz", "\t")])
def test_fixed_head_and_format_without_assuming_axis_meaning(tmp_path, name, delimiter):
    store = LocalArtifactStore(tmp_path / "store")
    text = "\n".join(delimiter.join(f"r{r}c{c}" for c in range(12)) for r in range(20))
    ref = register(store, tmp_path, text, name)
    head = query(store, ref).head_preview
    assert head.status == "AVAILABLE"
    assert head.format == ("tsv" if "tsv" in name else "csv")
    assert head.compression == ("gzip" if name.endswith("gz") else "none")
    assert head.leading_records == tuple(tuple(f"r{r}c{c}" for c in range(8)) for r in range(6))
    assert head.record_field_counts == (12,) * 6
    assert head.total_records is None
    assert head.more_records is True and head.columns_truncated is True
    assert "genes" not in head.model_dump_json()
    assert query(store, ref).head_preview == head  # repeated calls cannot page
    for denied in (ArtifactViewType.SCHEMA, ArtifactViewType.SUMMARY, ArtifactViewType.TOP_N):
        with pytest.raises(ArtifactExposureDenied):
            query(store, ref, denied)


def test_small_quoted_csv_preserves_empty_header_and_records(tmp_path):
    store = LocalArtifactStore(tmp_path / "store")
    ref = register(store, tmp_path, '\ufeff,c1,c2\r\ng1,"quoted, value","two\nlines"\r\n')
    head = query(store, ref).head_preview
    assert head.leading_records == (("", "c1", "c2"), ("g1", "quoted, value", "two\nlines"))
    assert head.total_records == 2  # physical/logical records, not inferred cells
    assert head.more_records is False


def test_oversized_input_does_not_read_or_parse_unbounded_records(tmp_path):
    store = LocalArtifactStore(tmp_path / "store")
    ref = register(store, tmp_path, 'a,b\n1,2\n"' + "x" * (2 * 1024 * 1024), "input.csv.gz")
    head = query(store, ref).head_preview
    assert head.leading_records == (("a", "b"), ("1", "2"))
    assert head.scan_limit_reached is True
    assert head.total_records is None
    assert len(head.model_dump_json()) < 12000


def test_head_masks_known_secrets_paths_and_bounds_cells(tmp_path):
    store = LocalArtifactStore(tmp_path / "store")
    ref = register(store, tmp_path, 'gene,api_key,label\ng1,SECRET_SENTINEL,' + "x" * 200 +
                   '\ng2,ANOTHER_SECRET,/private/path\ng3,,Bearer CREDENTIAL_SENTINEL\n')
    head = query(store, ref).head_preview
    serialized = head.model_dump_json()
    assert "SECRET_SENTINEL" not in serialized and "ANOTHER_SECRET" not in serialized
    assert "/private/path" not in serialized
    assert "CREDENTIAL_SENTINEL" not in serialized
    assert head.values_redacted is True and head.values_truncated is True
    assert all(len(value) <= 64 for row in head.leading_records for value in row)


@pytest.mark.parametrize("name,content", [("input.bin", "PRIVATE_CONTENT"),
                                         ("input.csv", "a,b\n1,\x00bad")])
def test_unsupported_or_binary_content_is_not_released(tmp_path, name, content):
    store = LocalArtifactStore(tmp_path / "store")
    ref = register(store, tmp_path, content, name)
    head = query(store, ref).head_preview
    assert head.status in {"UNSUPPORTED_FORMAT", "INVALID_TEXT"}
    assert head.leading_records == ()
    assert content not in head.model_dump_json()
    assert head.size_bytes == len(content.encode("utf-8"))
    assert head.format_hint == name.rsplit(".", 1)[-1]


def test_preview_cannot_follow_replaced_blob_symlink(tmp_path):
    store = LocalArtifactStore(tmp_path / "store")
    ref = register(store, tmp_path, "a,b\n1,2")
    from pathlib import Path
    blob = Path(ref.storage_locator)
    secret = tmp_path / "secret.csv"
    secret.write_text("PRIVATE_CONTENT", encoding="utf-8")
    blob.unlink()
    blob.symlink_to(secret)
    with pytest.raises((ValueError, OSError)):
        query(store, ref)


@pytest.mark.asyncio
async def test_visible_permission_to_tool_to_evidence_and_no_extra_actions(tmp_path, artifact_query_boundary):
    sink, binding, _, previous = artifact_query_boundary
    tools = LabBioRuntimeToolSet(replace(binding, stage_id=WorkflowStage.UNDERSTAND,
        capability_allowlist=("artifact_query", "artifact_list")), previous.services)
    ref = register(tools.services.artifact_store, tmp_path, "axis,c1\ng1,2\n",
        owner_user_id=binding.principal.user_id, project_id=binding.workspace.project_id,
        lab_id=binding.workspace.lab_id, run_id=binding.run_id)
    usage = RuntimeInputArtifactUsage.from_authorized_artifact(ref, source="RUN_INPUT",
        exposure_policy=tools.services.artifact_exposure.policy,
        mountable_input_artifact_ids=(ref.artifact_id,))
    assert usage.remote_view_types == (ArtifactViewType.METADATA,)
    assert usage.execution_input_eligible is True
    listed = await tools.artifact_list()
    item = next(item for item in listed["data"] if item["artifact_id"] == str(ref.artifact_id))
    assert item["available_views"] == ["METADATA"]
    response = await tools.artifact_query(str(ref.artifact_id), "METADATA")
    assert response["success"] is True
    assert response["data"]["head_preview"]["leading_records"] == [["axis", "c1"], ["g1", "2"]]
    evidence = tools.evidence_items()[-1]
    assert evidence.safe_result == response["data"]
    assert type(evidence).model_validate_json(evidence.model_dump_json()) == evidence
    assert evidence.artifact_query_request.view_type == "METADATA"
    trace = json.dumps([event.model_dump(mode="json") for event in sink.read(binding.run_id)])
    assert "leading_records" not in trace and ref.storage_locator not in trace
    assert len(tools.evidence_items()) == 2
    invalid = await tools.artifact_query(str(ref.artifact_id), "METADATA", 100)
    assert invalid["error"]["error_code"] == "INVALID_QUERY_SHAPE"
    assert len(tools.evidence_items()) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign", ["owner", "project"])
async def test_unauthorized_head_fails_before_file_inspection(tmp_path, artifact_query_boundary, monkeypatch, foreign):
    _, binding, _, tools = artifact_query_boundary
    owner = "other-owner" if foreign == "owner" else binding.principal.user_id
    project = "other-project" if foreign == "project" else binding.workspace.project_id
    ref = register(tools.services.artifact_store, tmp_path, "PRIVATE_HEAD\n1\n",
        owner_user_id=owner, project_id=project, lab_id=binding.workspace.lab_id)
    projects = InMemoryProjectStore()
    projects.register(Project(project_id=project, owner_user_id=owner, lab_id=binding.workspace.lab_id))
    tools.services.artifact_exposure.access_service = AccessService(projects)

    def forbidden(*args):
        raise AssertionError("Unauthorized requests must not inspect bytes")

    monkeypatch.setattr(tools.services.artifact_store, "inspect_raw_head", forbidden)
    response = await tools.artifact_query(str(ref.artifact_id), "METADATA")
    assert response["error"]["error_code"] == "AUTHORIZATION_DENIED"
    assert "PRIVATE_HEAD" not in json.dumps(response)


def test_model_cannot_expand_head_or_turn_it_into_a_general_raw_query(tmp_path):
    from pydantic import ValidationError
    from labbioagentos.artifacts.preview import RawHeadPreview
    for extra in ({"offset": 6}, {"columns": [8, 9]}, {"limit": 500}):
        with pytest.raises(ValidationError):
            ArtifactQuery.model_validate({"view_type": ArtifactViewType.METADATA, **extra})
    with pytest.raises(ValidationError):
        RawHeadPreview(status="AVAILABLE", leading_records=tuple(("x",) for _ in range(7)))
    store = LocalArtifactStore(tmp_path / "store")
    ref = store.register(artifact_type="process-log", exposure_class=ArtifactExposureClass.RAW,
        release_basis=ArtifactReleaseBasis.INTERNAL_ONLY,
        representation=ArtifactRepresentation(stored_content="PRIVATE_PROCESS_OUTPUT"))
    with pytest.raises(ArtifactExposureDenied):
        query(store, ref)


def test_reader_byte_budget_is_enforced_without_consuming_remainder():
    import io
    from labbioagentos.artifacts.preview import MAX_SCAN_BYTES, inspect_head

    class BoundedStream(io.BytesIO):
        def read(self, size=-1):
            assert 0 <= size <= MAX_SCAN_BYTES + 1
            return super().read(size)

    stream = BoundedStream(b"a,b\n1,2\n" * MAX_SCAN_BYTES)
    result = inspect_head(stream, format_key="csv", compression="none")
    assert result.scan_limit_reached and len(result.leading_records) == 6
    assert stream.tell() == MAX_SCAN_BYTES + 1


@pytest.mark.parametrize("name,text,expected", [
    ("matrix.mtx", "%%MatrixMarket matrix coordinate integer general\n% comment\n10 20 2\n1 1 3\n", "matrix_market"),
    ("matrix.mtx.gz", "%%MatrixMarket matrix coordinate integer general\n10 20 2\n1 1 3\n", "matrix_market"),
    ("config.json", '{"sample": "PRIVATE_VALUE", "items": [1,2], "count": 3}', "json"),
    ("unknown.dat", "opaque text PRIVATE_VALUE", "text"),
])
def test_general_format_facts_and_structural_inspection(tmp_path, name, text, expected):
    store = LocalArtifactStore(tmp_path / "store")
    ref = register(store, tmp_path, text, name)
    head = query(store, ref).head_preview
    assert head.format == expected and head.size_bytes > 0
    assert head.leading_records == ()
    assert "PRIVATE_VALUE" not in head.model_dump_json()
    if expected == "matrix_market":
        assert head.structure == {"shape": [10, 20], "stored_entries": 2}
    if expected == "json":
        assert head.structure["fields"] == [
            {"name": "sample", "name_truncated": False, "type": "str"},
            {"name": "items", "name_truncated": False, "type": "list"},
            {"name": "count", "name_truncated": False, "type": "int"},
        ]


@pytest.mark.parametrize("kind", ["h5ad", "npy", "zip", "binary"])
def test_binary_formats_expose_structure_not_payloads(tmp_path, monkeypatch, kind):
    import h5py
    import numpy as np
    import zipfile

    source = tmp_path / ("input." + kind)
    if kind == "h5ad":
        with h5py.File(source, "w") as handle:
            handle.create_dataset("X", data=np.arange(6).reshape(2, 3))
            handle.create_group("obs").create_dataset("label", data=[b"PRIVATE_VALUE"])
            group = handle.create_group("sparse")
            group.attrs["shape"] = [100, 200]
            handle["external"] = h5py.ExternalLink("/private/external.h5", "/private")
        def forbidden(*args):
            raise AssertionError("HDF5 preview must not read dataset values")
        monkeypatch.setattr(h5py.Dataset, "__getitem__", forbidden)
    elif kind == "npy":
        np.save(source, np.zeros((2, 3)))
    elif kind == "zip":
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("secret.txt", "PRIVATE_VALUE")
    else:
        source.write_bytes(b"\x00\xffPRIVATE_VALUE")
    store = LocalArtifactStore(tmp_path / "store")
    ref = store.register_file(source, artifact_type="input", exposure_class=ArtifactExposureClass.RAW,
        release_basis=ArtifactReleaseBasis.RAW_INGESTION, representation=ArtifactRepresentation())
    head = query(store, ref).head_preview
    assert head.format == ("hdf5" if kind == "h5ad" else kind)
    assert head.format_hint == kind
    assert head.leading_records == ()
    assert "PRIVATE_VALUE" not in head.model_dump_json()
    assert "/private" not in head.model_dump_json()
    if kind == "h5ad":
        items = {entry["name"]: entry for entry in head.structure["entries"]}
        assert items["X"]["shape"] == [2, 3]
        assert items["obs"]["fields"] == ["label"]
        assert items["sparse"]["shape"] == [100, 200]
        assert items["external"]["kind"] == "LINK_NOT_FOLLOWED"
    elif kind == "npy":
        assert head.structure["shape"] == [2, 3]
