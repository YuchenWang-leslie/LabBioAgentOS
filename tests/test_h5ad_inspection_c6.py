"""C6 contracts for trusted h5ad inspection and safe Artifact admission."""

from __future__ import annotations

import json

import anndata as ad
import numpy as np
import pandas as pd
import pytest
from scipy.sparse import csr_matrix

import labbioagentos.bioformats as bioformats
from labbioagentos import (
    AgentProfile,
    ApplicationRunRequest,
    ApplicationRuntimeConfiguration,
    ArtifactConsumer,
    ArtifactExposureClass,
    ArtifactExposureDenied,
    ArtifactQuery,
    ArtifactRepresentation,
    ArtifactReleaseBasis,
    ArtifactSchema,
    ArtifactViewType,
    AuthorizationDenied,
    BioFormatArtifactSpec,
    BioFormatInspectionBundle,
    CapabilityProfile,
    H5ADCategoryEnumeration,
    H5ADInspectionError,
    H5ADInspectionPolicy,
    H5ADInspector,
    LabBioApplication,
    ModelProfile,
    Principal,
    Project,
    PromptProfile,
    ProviderConfigRef,
    ResponseSchemaRef,
    RuntimeProfileCatalog,
    RuntimeStageAssemblySpec,
    WorkflowStage,
    WorkspaceContext,
)


MAIN_PATH = (
    WorkflowStage.INTAKE,
    WorkflowStage.UNDERSTAND,
    WorkflowStage.PLAN,
    WorkflowStage.PREFLIGHT,
    WorkflowStage.EXECUTE,
    WorkflowStage.VALIDATE,
    WorkflowStage.INTERPRET,
    WorkflowStage.REPORT,
    WorkflowStage.LEARN,
)
PRIVATE_BARCODES = tuple(f"private-cell-barcode-{index:03d}" for index in range(12))
PRIVATE_GENES = tuple(f"PRIVATE_GENE_{index:03d}" for index in range(8))


def _write_h5ad(path) -> None:
    values = np.arange(96, dtype=np.float32).reshape(12, 8)
    obs = pd.DataFrame(
        {
            "cell_id": PRIVATE_BARCODES,
            "sample": pd.Categorical(["sample-a"] * 4 + ["sample-b"] * 4 + ["sample-c"] * 4),
            "condition": pd.Categorical(["control", "treated"] * 6),
            "broad_cell_type": pd.Categorical(["T", "B", "myeloid"] * 4),
            "overlarge_group": pd.Categorical([f"group-{index % 6}" for index in range(12)]),
            "total_counts": np.arange(100, 220, 10, dtype=np.float64),
            "pct_counts_mt": np.linspace(1.0, 12.0, 12, dtype=np.float64),
        },
        index=PRIVATE_BARCODES,
    )
    obs.loc[PRIVATE_BARCODES[-1], "pct_counts_mt"] = np.nan
    var = pd.DataFrame(
        {"feature_type": pd.Categorical(["Gene Expression"] * 8)},
        index=PRIVATE_GENES,
    )
    data = ad.AnnData(X=csr_matrix(values), obs=obs, var=var)
    data.layers["counts"] = data.X.copy()
    data.obsm["X_pca"] = np.arange(36, dtype=np.float32).reshape(12, 3)
    data.varm["loadings"] = np.arange(24, dtype=np.float32).reshape(8, 3)
    data.raw = data.copy()
    data.write_h5ad(path)


def _write_alternative_h5ad(
    path,
    *,
    colliding_obs_names: bool = False,
    colliding_obsm_names: bool = False,
) -> None:
    n_obs = 37
    obs = pd.DataFrame(
        {
            "batch_code": pd.Categorical(
                [f"batch-{index % 4}" for index in range(n_obs)]
            ),
            "low_cardinality_sensitive_example": pd.Categorical(
                ["restricted", "ordinary"] * 18 + ["restricted"]
            ),
            "quality_score": np.linspace(-2.5, 3.5, n_obs, dtype=np.float64),
            "reviewed": np.asarray([index % 3 == 0 for index in range(n_obs)]),
            "record_key": [
                f"alternative-record-{index:03d}" for index in range(n_obs)
            ],
        },
        index=[f"alternative-index-{index:03d}" for index in range(n_obs)],
    )
    if colliding_obs_names:
        obs["collision-prefix-alpha-tail"] = np.arange(n_obs, dtype=np.float64)
        obs["collision-prefix-beta-tail"] = np.arange(n_obs, dtype=np.float64)
    var = pd.DataFrame(
        {
            "feature_family": pd.Categorical(
                ["family-a", "family-b", "family-a", "family-c", "family-b"]
            )
        },
        index=[f"alternative-feature-{index}" for index in range(5)],
    )
    data = ad.AnnData(
        X=np.arange(n_obs * 5, dtype=np.int16).reshape(n_obs, 5),
        obs=obs,
        var=var,
    )
    if colliding_obsm_names:
        data.obsm["collision-prefix-alpha-tail"] = np.ones(
            (n_obs, 2), dtype=np.float32
        )
        data.obsm["collision-prefix-beta-tail"] = np.ones(
            (n_obs, 3), dtype=np.float32
        )
    data.write_h5ad(path)


class _AlternativeFormatInspector:
    format_key = "alternative-format"

    def inspect_artifacts(self, source) -> BioFormatInspectionBundle:
        assert source.is_file()
        return BioFormatInspectionBundle(
            format_key=self.format_key,
            inspection_schema_version="test-1",
            artifacts=(
                BioFormatArtifactSpec(
                    artifact_type="alternative-structural",
                    exposure_class=ArtifactExposureClass.STRUCTURAL,
                    release_basis=ArtifactReleaseBasis.TRUSTED_STRUCTURAL_INSPECTOR,
                    representation=ArtifactRepresentation(),
                    artifact_schema=ArtifactSchema(
                        shape=(3,), columns=("field",), dtypes={"field": "string"}
                    ),
                ),
            ),
        )


def _catalog() -> RuntimeProfileCatalog:
    return RuntimeProfileCatalog(
        agents=(
            AgentProfile(
                profile_key="coordinator",
                version="c6-test",
                agent_name="CoordinatorAgent",
                role_description="Exercise the generic application boundary.",
                prompt_profile_key="runtime-generic",
                response_schema_key="runtime-stage-result",
                model_profile_key="runtime-default",
                capability_profile_key="coordinator-capabilities",
            ),
        ),
        prompts=(
            PromptProfile(
                template_id="runtime-generic",
                version="c6-test",
                template_text="{protocol}",
            ),
        ),
        models=(
            ModelProfile(
                profile_key="runtime-default",
                version="c6-test",
                model_identifier="mock/provider-model",
                provider_config=ProviderConfigRef(
                    config_id="external-mock", provider="mock"
                ),
            ),
        ),
        schemas=(ResponseSchemaRef(),),
        capabilities=(
            CapabilityProfile(
                profile_key="coordinator-capabilities",
                version="c6-test",
                capability_allowlist=(),
            ),
        ),
    )


def _application(tmp_path, *, bioformat_inspectors=()) -> LabBioApplication:
    input_root = tmp_path / "inputs"
    input_root.mkdir()
    return LabBioApplication(
        ApplicationRuntimeConfiguration(
            artifact_root=tmp_path / "artifacts",
            execution_workspace_root=tmp_path / "executions",
            runtime_revision="c6-test-runtime",
            allowed_input_roots=(input_root,),
            projects=(
                Project(
                    project_id="project-a", lab_id="lab-c6", owner_user_id="user-a"
                ),
                Project(
                    project_id="project-b", lab_id="lab-c6", owner_user_id="user-b"
                ),
            ),
            profile_catalog=_catalog(),
            stage_assemblies=tuple(
                RuntimeStageAssemblySpec(
                    stage_id=stage,
                    root_profile_key="coordinator",
                    prompt_template_key="runtime-generic",
                    capability_allowlist=(),
                    finalization_prompt_values={"protocol": f"Finalize {stage.value}"},
                    capability_phase_enabled=False,
                )
                for stage in MAIN_PATH
            ),
            h5ad_inspection_policy=H5ADInspectionPolicy(
                max_categories_per_field=3,
                high_cardinality_fraction=0.8,
            ),
            bioformat_inspectors=bioformat_inspectors,
        )
    )


def test_h5ad_inspector_emits_bounded_structure_and_aggregates(tmp_path):
    source = tmp_path / "real-format.h5ad"
    _write_h5ad(source)

    result = H5ADInspector(
        H5ADInspectionPolicy(
            max_categories_per_field=3,
            high_cardinality_fraction=0.8,
            enumerated_categorical_fields=frozenset(
                {"sample", "overlarge_group"}
            ),
        )
    ).inspect(source)

    assert result.structural.format == "h5ad"
    assert (result.structural.n_obs, result.structural.n_vars) == (12, 8)
    assert result.structural.x.shape == (12, 8)
    assert result.structural.x.storage == "SPARSE_CSR"
    assert result.structural.x.dtype == "float32"
    assert {field.name for field in result.structural.obs.fields} >= {
        "sample",
        "condition",
        "total_counts",
    }
    assert result.structural.var.index_unique is True
    assert result.structural.layer_names == ("counts",)
    assert result.structural.obsm[0].key == "X_pca"
    assert result.structural.obsm[0].shape == (12, 3)
    assert result.structural.varm[0].shape == (8, 3)
    assert result.structural.raw_present is True
    assert result.structural.raw_shape == (12, 8)

    categorical = {item.field_name: item for item in result.aggregate.categorical}
    assert categorical["cell_id"].enumeration is H5ADCategoryEnumeration.HIGH_CARDINALITY_SUPPRESSED
    assert categorical["cell_id"].categories == ()
    assert categorical["cell_id"].overflow_category_count == 12
    assert categorical["sample"].enumeration is H5ADCategoryEnumeration.ENUMERATED
    assert {item.label: item.count for item in categorical["sample"].categories} == {
        "sample-a": 4,
        "sample-b": 4,
        "sample-c": 4,
    }
    assert categorical["overlarge_group"].enumeration is H5ADCategoryEnumeration.ENUMERATED_WITH_OVERFLOW
    assert len(categorical["overlarge_group"].categories) == 3
    assert categorical["overlarge_group"].overflow_category_count == 3

    numeric = {item.field_name: item for item in result.aggregate.numeric}
    assert numeric["total_counts"].count == 12
    assert numeric["total_counts"].mean == pytest.approx(155.0)
    assert numeric["pct_counts_mt"].missing_count == 1
    serialized = result.model_dump_json()
    assert not any(value in serialized for value in PRIVATE_BARCODES)
    assert not any(value in serialized for value in PRIVATE_GENES)


def test_alternative_h5ad_schema_is_inspected_without_fixture_assumptions(tmp_path):
    source = tmp_path / "alternative-37-by-5.h5ad"
    _write_alternative_h5ad(source)

    inspector = H5ADInspector(
        H5ADInspectionPolicy(
            max_categories_per_field=5,
            high_cardinality_fraction=0.75,
        )
    )
    result = inspector.inspect(source)
    bundle = inspector.inspect_artifacts(source)

    assert (result.structural.n_obs, result.structural.n_vars) == (37, 5)
    assert result.structural.x.storage == "DENSE"
    assert result.structural.x.dtype == "int16"
    assert result.structural.layer_names == ()
    assert result.structural.raw_present is False
    categorical = {item.field_name: item for item in result.aggregate.categorical}
    assert (
        categorical["record_key"].enumeration
        is H5ADCategoryEnumeration.HIGH_CARDINALITY_SUPPRESSED
    )
    assert categorical["low_cardinality_sensitive_example"].categories == ()
    structural_spec, aggregate_spec = bundle.artifacts
    assert structural_spec.artifact_schema.shape == (37, 5)
    assert len(structural_spec.artifact_schema.columns) == len(
        structural_spec.artifact_schema.dtypes
    )
    assert aggregate_spec.exposure_class is ArtifactExposureClass.AGGREGATE
    serialized = bundle.model_dump_json()
    assert "alternative-index-" not in serialized
    assert "alternative-feature-" not in serialized
    assert "alternative-record-" not in serialized


def test_h5ad_resource_policy_rejects_before_anndata_eager_load(tmp_path, monkeypatch):
    source = tmp_path / "alternative-over-limit.h5ad"
    _write_alternative_h5ad(source)
    called = False

    def unexpected_read(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("AnnData read must not begin after failed preflight")

    monkeypatch.setattr(bioformats.ad, "read_h5ad", unexpected_read)
    with pytest.raises(H5ADInspectionError, match="axis-row limit"):
        H5ADInspector(H5ADInspectionPolicy(max_axis_rows=20)).inspect(source)
    assert called is False


def test_h5ad_bounded_names_cannot_collide_in_safe_artifacts(tmp_path):
    obs_collision = tmp_path / "alternative-obs-collision.h5ad"
    _write_alternative_h5ad(obs_collision, colliding_obs_names=True)
    with pytest.raises(H5ADInspectionError, match="collide after safe bounding") as exc:
        H5ADInspector(H5ADInspectionPolicy(max_name_length=16)).inspect_artifacts(
            obs_collision
        )
    assert "alpha-tail" not in str(exc.value)
    assert "beta-tail" not in str(exc.value)

    aggregate_collision = tmp_path / "alternative-aggregate-collision.h5ad"
    _write_alternative_h5ad(aggregate_collision, colliding_obs_names=True)
    with pytest.raises(H5ADInspectionError, match="collide after safe bounding"):
        H5ADInspector(
            H5ADInspectionPolicy(
                max_name_length=16,
                max_axis_fields=1,
                max_aggregate_fields=16,
            )
        ).inspect_artifacts(aggregate_collision)

    key_collision = tmp_path / "alternative-key-collision.h5ad"
    _write_alternative_h5ad(key_collision, colliding_obsm_names=True)
    with pytest.raises(H5ADInspectionError, match="collide after safe bounding"):
        H5ADInspector(H5ADInspectionPolicy(max_name_length=16)).inspect_artifacts(
            key_collision
        )


def test_generic_bioformat_inspection_does_not_require_application_changes(tmp_path):
    application = _application(
        tmp_path, bioformat_inspectors=(_AlternativeFormatInspector(),)
    )
    principal = Principal(user_id="user-a", lab_id="lab-c6")
    workspace = WorkspaceContext(
        user_id="user-a", project_id="project-a", lab_id="lab-c6"
    )
    source = tmp_path / "inputs" / "alternative.input"
    source.write_bytes(b"opaque alternative format")
    raw = application.register_input_file(
        source,
        principal=principal,
        workspace=workspace,
        artifact_type="alternative-format",
    )

    inspected = application.inspect_bioformat(
        raw.artifact_id,
        format_key="alternative-format",
        principal=principal,
        workspace=workspace,
    )

    assert inspected.format_key == "alternative-format"
    assert len(inspected.artifacts) == 1
    ref = application.artifact_store.get_ref(inspected.artifacts[0].artifact_id)
    assert ref.artifact_type == "alternative-structural"
    assert ref.metadata["format"] == "alternative-format"
    assert ref.metadata["source_artifact_id"] == str(raw.artifact_id)


def test_application_registers_scoped_safe_inspection_artifacts(tmp_path):
    application = _application(tmp_path)
    principal = Principal(user_id="user-a", lab_id="lab-c6")
    workspace = WorkspaceContext(
        user_id="user-a", project_id="project-a", lab_id="lab-c6"
    )
    source = tmp_path / "inputs" / "dataset.h5ad"
    _write_h5ad(source)
    raw = application.register_input_file(
        source,
        principal=principal,
        workspace=workspace,
        artifact_type="h5ad",
        metadata={"format": "h5ad"},
    )

    admitted = application.inspect_h5ad(
        raw.artifact_id,
        principal=principal,
        workspace=workspace,
    )

    assert admitted.source_artifact_id == raw.artifact_id
    assert admitted.structural_artifact.exposure_class is ArtifactExposureClass.STRUCTURAL
    assert admitted.aggregate_artifact.exposure_class is ArtifactExposureClass.AGGREGATE
    structural_ref = application.artifact_store.get_ref(
        admitted.structural_artifact.artifact_id
    )
    aggregate_ref = application.artifact_store.get_ref(
        admitted.aggregate_artifact.artifact_id
    )
    for ref in (structural_ref, aggregate_ref):
        assert (ref.owner_user_id, ref.project_id, ref.lab_id) == (
            "user-a",
            "project-a",
            "lab-c6",
        )
        assert ref.metadata["source_artifact_id"] == str(raw.artifact_id)

    with pytest.raises(ArtifactExposureDenied):
        application.artifact_exposure.artifact_query(
            raw.artifact_id,
            ArtifactQuery(view_type=ArtifactViewType.METADATA),
            ArtifactConsumer.REMOTE_LLM,
            principal=principal,
        )
    structural_view = application.artifact_exposure.artifact_query(
        structural_ref.artifact_id,
        ArtifactQuery(view_type=ArtifactViewType.SCHEMA),
        ArtifactConsumer.REMOTE_LLM,
        principal=principal,
    )
    aggregate_view = application.artifact_exposure.artifact_query(
        aggregate_ref.artifact_id,
        ArtifactQuery(view_type=ArtifactViewType.SUMMARY),
        ArtifactConsumer.REMOTE_LLM,
        principal=principal,
    )
    visible = json.dumps(
        [
            admitted.model_dump(mode="json"),
            structural_view.model_dump(mode="json"),
            aggregate_view.model_dump(mode="json"),
        ]
    )
    assert str(tmp_path) not in visible
    assert "storage_locator" not in visible
    assert not any(value in visible for value in PRIVATE_BARCODES)
    assert not any(value in visible for value in PRIVATE_GENES)

    handle = application.create_run(
        ApplicationRunRequest(
            task_text="Inspect only the safe metadata.",
            principal=principal,
            workspace=workspace,
            input_artifact_ids=(raw.artifact_id,),
            context_artifact_ids=(
                structural_ref.artifact_id,
                aggregate_ref.artifact_id,
            ),
        )
    )
    assert handle.run_id

    with pytest.raises(AuthorizationDenied):
        application.inspect_h5ad(
            raw.artifact_id,
            principal=Principal(user_id="user-b", lab_id="lab-c6"),
            workspace=WorkspaceContext(
                user_id="user-b", project_id="project-b", lab_id="lab-c6"
            ),
        )


def test_h5ad_inspection_rejects_malformed_or_non_raw_input(tmp_path):
    application = _application(tmp_path)
    principal = Principal(user_id="user-a", lab_id="lab-c6")
    workspace = WorkspaceContext(
        user_id="user-a", project_id="project-a", lab_id="lab-c6"
    )
    malformed = tmp_path / "inputs" / "malformed.h5ad"
    malformed.write_bytes(b"not an hdf5 file")
    raw = application.register_input_file(
        malformed,
        principal=principal,
        workspace=workspace,
        artifact_type="h5ad",
    )
    with pytest.raises(H5ADInspectionError, match="Could not inspect h5ad input"):
        application.inspect_h5ad(
            raw.artifact_id,
            principal=principal,
            workspace=workspace,
        )

    structural = application.register_structural_artifact(
        principal=principal,
        workspace=workspace,
        artifact_type="not-raw",
        schema=ArtifactSchema(shape=(1, 1)),
    )
    with pytest.raises(H5ADInspectionError, match="must be RAW"):
        application.inspect_h5ad(
            structural.artifact_id,
            principal=principal,
            workspace=workspace,
        )
