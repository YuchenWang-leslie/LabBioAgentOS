"""C12 falsification tests for semantic privacy and remote Artifact projection."""

from __future__ import annotations

import sqlite3
from uuid import uuid4

import anndata as ad
import numpy as np
import pandas as pd
import pytest

from labbioagentos import (
    ArtifactApproval,
    ArtifactApprovalStoreError,
    ArtifactConsumer,
    ArtifactExposureClass,
    ArtifactExposureDenied,
    ArtifactExposureService,
    ArtifactQuery,
    ArtifactReleaseBasis,
    ArtifactRepresentation,
    ArtifactSchema,
    ArtifactViewType,
    ExposurePolicy,
    H5ADInspectionPolicy,
    H5ADInspector,
    InMemoryArtifactApprovalStore,
    LocalArtifactStore,
    SQLiteArtifactApprovalStore,
)


@pytest.mark.parametrize(
    ("field_name", "values"),
    [
        ("condition", ["primary", "bone_metastasis"] * 2),
        ("donor", ["P_01", "P_01", "P_02", "P_02"]),
        ("sample", ["sample_A", "sample_A", "sample_B", "sample_B"]),
    ],
)
def test_hc1_hc3_low_cardinality_labels_enumerate_by_default(
    tmp_path, field_name, values
):
    source = tmp_path / f"bounded-{field_name}.h5ad"
    data = ad.AnnData(
        X=np.ones((4, 2), dtype=np.float32),
        obs=pd.DataFrame(
            {field_name: values},
            index=[f"cell-{index}" for index in range(4)],
        ),
        var=pd.DataFrame(index=["feature-a", "feature-b"]),
    )
    data.write_h5ad(source)

    result = H5ADInspector().inspect(source)
    categorical = result.aggregate.categorical[0]

    assert categorical.enumeration.value == "ENUMERATED"
    assert {item.label for item in categorical.categories} == set(values)
    assert categorical.unique_count == 2


def test_hc4_high_cardinality_categories_remain_bounded(tmp_path):
    source = tmp_path / "high-cardinality.h5ad"
    data = ad.AnnData(
        X=np.ones((100, 2), dtype=np.float32),
        obs=pd.DataFrame(
            {"barcode": [f"barcode-{index:03d}" for index in range(100)]},
            index=[f"cell-{index:03d}" for index in range(100)],
        ),
        var=pd.DataFrame(index=["feature-a", "feature-b"]),
    )
    data.write_h5ad(source)

    result = H5ADInspector().inspect(source)
    categorical = result.aggregate.categorical[0]

    assert categorical.enumeration.value == "HIGH_CARDINALITY_SUPPRESSED"
    assert categorical.categories == ()
    assert categorical.unique_count == 100
    assert categorical.overflow_category_count == 100


def test_hc5_observation_rows_are_not_exposed(tmp_path):
    source = tmp_path / "no-observation-rows.h5ad"
    data = ad.AnnData(
        X=np.ones((4, 2), dtype=np.float32),
        obs=pd.DataFrame(
            {"condition": ["primary", "bone_metastasis"] * 2},
            index=[f"PRIVATE_OBSERVATION_{index:03d}" for index in range(4)],
        ),
        var=pd.DataFrame(index=["feature-a", "feature-b"]),
    )
    data.write_h5ad(source)

    serialized = H5ADInspector().inspect(source).model_dump_json()

    assert "PRIVATE_OBSERVATION" not in serialized
    assert "dataframe_rows" not in serialized


def test_hc6_expression_matrix_values_are_not_exposed(tmp_path):
    source = tmp_path / "no-expression-values.h5ad"
    data = ad.AnnData(
        X=np.asarray([[9137.25, 2918.5], [7712.75, 6621.5]], dtype=np.float32),
        obs=pd.DataFrame(
            {"condition": ["primary", "bone_metastasis"]},
            index=["cell-a", "cell-b"],
        ),
        var=pd.DataFrame(index=["feature-a", "feature-b"]),
    )
    data.write_h5ad(source)

    serialized = H5ADInspector().inspect(source).model_dump_json()

    assert "9137.25" not in serialized
    assert "2918.5" not in serialized
    assert "7712.75" not in serialized
    assert "6621.5" not in serialized
    assert "expression_matrix" not in serialized


def test_remote_projection_rejects_internal_metadata_schema_and_summary(tmp_path):
    store = LocalArtifactStore(tmp_path / "artifacts")
    ref = store.register(
        artifact_type="adversarial-aggregate",
        exposure_class=ArtifactExposureClass.AGGREGATE,
        release_basis=ArtifactReleaseBasis.TRUSTED_AGGREGATE_INSPECTOR,
        representation=ArtifactRepresentation(
            summary={"credentials": "SECRET_PROVIDER_TOKEN"}
        ),
        schema=ArtifactSchema(properties={"host_path": "/private/host/input.h5ad"}),
        metadata={"storage_locator": "/private/host/input.h5ad"},
    )
    service = ArtifactExposureService(store, ExposurePolicy())

    metadata = service.artifact_query(
        ref.artifact_id,
        ArtifactQuery(view_type=ArtifactViewType.METADATA),
        ArtifactConsumer.REMOTE_LLM,
    )
    assert metadata.metadata == {}
    with pytest.raises(ValueError):
        service.artifact_query(
            ref.artifact_id,
            ArtifactQuery(view_type=ArtifactViewType.SCHEMA),
            ArtifactConsumer.REMOTE_LLM,
        )
    with pytest.raises(ValueError):
        service.artifact_query(
            ref.artifact_id,
            ArtifactQuery(view_type=ArtifactViewType.SUMMARY),
            ArtifactConsumer.REMOTE_LLM,
        )


def test_user_approved_exposure_is_disabled_by_default_and_durable_when_enabled(
    tmp_path,
):
    store = LocalArtifactStore(tmp_path / "artifacts")
    ref = store.register(
        artifact_type="user-approved-result",
        exposure_class=ArtifactExposureClass.USER_APPROVED,
        release_basis=ArtifactReleaseBasis.USER_APPROVED_RELEASE,
        representation=ArtifactRepresentation(
            records=({"label": "explicit-user-release"},), record_count=1
        ),
    )
    approval = ArtifactApproval(
        artifact_id=ref.artifact_id,
        consumer=ArtifactConsumer.REMOTE_LLM,
        approved_by="user-c12",
    )
    in_memory = InMemoryArtifactApprovalStore()
    in_memory.record(approval)
    disabled = ArtifactExposureService(
        store, ExposurePolicy(approval_store=in_memory)
    )
    query = ArtifactQuery(view_type=ArtifactViewType.TOP_N, limit=1)

    with pytest.raises(ArtifactExposureDenied, match="disabled"):
        disabled.artifact_query(
            ref.artifact_id, query, ArtifactConsumer.REMOTE_LLM
        )

    database = tmp_path / "approvals.sqlite3"
    SQLiteArtifactApprovalStore(database).record(approval)
    reconstructed = SQLiteArtifactApprovalStore(database)
    enabled = ArtifactExposureService(
        LocalArtifactStore(tmp_path / "artifacts"),
        ExposurePolicy(
            approval_store=reconstructed,
            user_approved_enabled=True,
        ),
    )

    view = enabled.artifact_query(
        ref.artifact_id, query, ArtifactConsumer.REMOTE_LLM
    )
    assert view.records == ({"label": "explicit-user-release"},)
    assert reconstructed.get(ref.artifact_id, ArtifactConsumer.REMOTE_LLM) == approval


def test_durable_approval_payload_must_match_its_persistence_key(tmp_path):
    database = tmp_path / "approvals.sqlite3"
    artifact_id = uuid4()
    approval = ArtifactApproval(
        artifact_id=artifact_id,
        consumer=ArtifactConsumer.REMOTE_LLM,
        approved_by="user-c12",
    )
    store = SQLiteArtifactApprovalStore(database)
    store.record(approval)
    mismatched = ArtifactApproval(
        artifact_id=uuid4(),
        consumer=ArtifactConsumer.SYSTEM,
        approved_by="attacker",
    )
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE artifact_approvals SET payload = ? "
            "WHERE artifact_id = ? AND consumer = ?",
            (
                mismatched.model_dump_json(),
                str(artifact_id),
                ArtifactConsumer.REMOTE_LLM.value,
            ),
        )

    with pytest.raises(ArtifactApprovalStoreError, match="persistence key"):
        store.get(artifact_id, ArtifactConsumer.REMOTE_LLM)
