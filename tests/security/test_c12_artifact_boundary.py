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


PRIVATE_DONORS = {"PRIVATE_DONOR_A", "PRIVATE_DONOR_B"}


def test_low_cardinality_private_h5ad_categories_are_suppressed_by_default(tmp_path):
    source = tmp_path / "private-low-cardinality.h5ad"
    data = ad.AnnData(
        X=np.ones((4, 2), dtype=np.float32),
        obs=pd.DataFrame(
            {
                "donor_id": ["PRIVATE_DONOR_A", "PRIVATE_DONOR_A", "PRIVATE_DONOR_B", "PRIVATE_DONOR_B"],
                "condition": ["treated", "control", "treated", "control"],
            },
            index=[f"PRIVATE_OBSERVATION_{index:03d}" for index in range(4)],
        ),
        var=pd.DataFrame(index=["feature-a", "feature-b"]),
    )
    data.write_h5ad(source)

    result = H5ADInspector().inspect(source)
    categorical = {item.field_name: item for item in result.aggregate.categorical}
    serialized = result.model_dump_json()

    assert categorical["donor_id"].categories == ()
    assert categorical["condition"].categories == ()
    assert categorical["donor_id"].unique_count == 2
    assert categorical["condition"].unique_count == 2
    assert not any(value in serialized for value in PRIVATE_DONORS)
    assert "PRIVATE_OBSERVATION" not in serialized


def test_explicitly_approved_safe_h5ad_category_may_enumerate(tmp_path):
    source = tmp_path / "approved-category.h5ad"
    data = ad.AnnData(
        X=np.ones((4, 2), dtype=np.float32),
        obs=pd.DataFrame(
            {"condition": ["treated", "control", "treated", "control"]},
            index=[f"observation-{index}" for index in range(4)],
        ),
        var=pd.DataFrame(index=["feature-a", "feature-b"]),
    )
    data.write_h5ad(source)

    result = H5ADInspector(
        H5ADInspectionPolicy(
            enumerated_categorical_fields=frozenset({"condition"})
        )
    ).inspect(source)
    categorical = result.aggregate.categorical[0]

    assert {item.label for item in categorical.categories} == {"treated", "control"}


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
