"""C7 non-live generalization checks for generic AnnData/QC boundaries."""

from __future__ import annotations

import json

import anndata as ad
import numpy as np
import pandas as pd
import pytest
from scipy.sparse import csc_matrix

from labbioagentos import (
    ArtifactConsumer,
    ArtifactExposureClass,
    ArtifactExposureDenied,
    ArtifactExposureService,
    ArtifactQuery,
    ArtifactRegistrationPolicy,
    ArtifactRepresentation,
    ArtifactViewType,
    ExposurePolicy,
    H5ADInspector,
    H5ADMatrixStorage,
    LocalArtifactStore,
    OutputArtifactSpec,
    OutputDeclassificationMode,
    StructuredOutputContract,
)


def _write_generalization_fixture(path) -> None:
    n_obs, n_vars = 37, 53
    obs = pd.DataFrame(
        {
            "batch_label": pd.Categorical(
                [f"batch-{index % 4}" for index in range(n_obs)]
            ),
            "arbitrary_quality_score": np.linspace(
                -3.0, 5.0, n_obs, dtype=np.float64
            ),
        },
        index=[f"private-observation-{index:03d}" for index in range(n_obs)],
    )
    var = pd.DataFrame(
        {
            "feature_annotation": pd.Categorical(
                [f"family-{index % 7}" for index in range(n_vars)]
            )
        },
        index=[f"private-feature-{index:03d}" for index in range(n_vars)],
    )
    values = np.arange(n_obs * n_vars, dtype=np.int16).reshape(n_obs, n_vars)
    data = ad.AnnData(X=csc_matrix(values), obs=obs, var=var)
    data.write_h5ad(path)


def _generic_contract() -> StructuredOutputContract:
    return StructuredOutputContract(
        contract_id="generic-qc-scalar-records-v1",
        schema_id="labbio.generic.qc.scalar-records.v1",
        allowed_fields=frozenset(
            {
                "record_type",
                "metric",
                "count",
                "minimum",
                "maximum",
                "mean",
                "median",
                "q05",
                "q25",
                "q75",
                "q95",
                "value",
                "unit",
                "explanation",
            }
        ),
        required_fields=frozenset({"record_type"}),
        max_records=128,
        declassification_mode=OutputDeclassificationMode.PREDECLARED_SCALARS,
    )


def test_different_anndata_schema_uses_generic_inspection_and_output_policy(tmp_path):
    source = tmp_path / "unrelated-schema.h5ad"
    _write_generalization_fixture(source)

    inspection = H5ADInspector().inspect(source)
    assert (inspection.structural.n_obs, inspection.structural.n_vars) == (37, 53)
    assert inspection.structural.x.storage is H5ADMatrixStorage.SPARSE_CSC
    assert {item.name for item in inspection.structural.obs.fields} == {
        "batch_label",
        "arbitrary_quality_score",
    }
    assert {item.name for item in inspection.structural.var.fields} == {
        "feature_annotation"
    }
    serialized = inspection.model_dump_json()
    assert "private-observation-" not in serialized
    assert "private-feature-" not in serialized

    result_path = tmp_path / "generic-qc.json"
    result_path.write_text(
        json.dumps(
            {
                "schema_id": "labbio.generic.qc.scalar-records.v1",
                "records": [
                    {
                        "record_type": "dataset_summary",
                        "metric": "observation_count",
                        "value": 37,
                        "unit": "observations",
                    },
                    {
                        "record_type": "dataset_summary",
                        "metric": "variable_count",
                        "value": 53,
                        "unit": "variables",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    decision = ArtifactRegistrationPolicy((_generic_contract(),)).assess(
        OutputArtifactSpec(
            relative_path="generic-qc.json",
            artifact_type="generic-qc-summary",
            requested_exposure=ArtifactExposureClass.DERIVED,
            output_contract_id="generic-qc-scalar-records-v1",
            predeclared_string_values={
                "record_type": ("dataset_summary",),
                "metric": ("observation_count", "variable_count"),
                "unit": ("observations", "variables"),
            },
        ),
        result_path,
    )
    assert decision.contract_valid is True
    assert decision.actual_exposure is ArtifactExposureClass.DERIVED
    assert decision.representation.record_count == 2


def test_observation_level_output_and_raw_input_remain_non_model_visible(tmp_path):
    store = LocalArtifactStore(tmp_path / "artifacts")
    raw_input = store.register(
        artifact_type="h5ad",
        exposure_class=ArtifactExposureClass.RAW,
        representation=ArtifactRepresentation(stored_content="opaque-input"),
    )
    exposure = ArtifactExposureService(store, ExposurePolicy())
    with pytest.raises(ArtifactExposureDenied, match="RAW artifacts"):
        exposure.artifact_query(
            raw_input.artifact_id,
            ArtifactQuery(view_type=ArtifactViewType.METADATA),
            ArtifactConsumer.REMOTE_LLM,
        )

    detail_path = tmp_path / "observation-detail.json"
    detail_path.write_text('[{"observation":"opaque","value":1}]', encoding="utf-8")
    detail_decision = ArtifactRegistrationPolicy((_generic_contract(),)).assess(
        OutputArtifactSpec(
            relative_path="observation-detail.json",
            artifact_type="qc-observation-detail",
            requested_exposure=ArtifactExposureClass.RAW,
        ),
        detail_path,
    )
    assert detail_decision.actual_exposure is ArtifactExposureClass.RAW
    assert detail_decision.contract_valid is False
