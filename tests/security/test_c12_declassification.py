"""C12 falsification tests for execution-output declassification."""

from __future__ import annotations

import json

from labbioagentos import (
    ArtifactExposureClass,
    ArtifactRegistrationPolicy,
    OutputArtifactSpec,
    OutputDeclassificationMode,
    StructuredOutputContract,
)


PRIVATE_SENTINEL = "PRIVATE_OBSERVATION_001"


def test_contract_valid_runtime_string_does_not_self_declassify(tmp_path):
    path = tmp_path / "laundered.json"
    path.write_text(
        json.dumps(
            {
                "schema_id": "c12.scalar.records.v1",
                "records": [{"name": PRIVATE_SENTINEL, "value": 1}],
            }
        ),
        encoding="utf-8",
    )
    contract = StructuredOutputContract(
        contract_id="c12-records-v1",
        schema_id="c12.scalar.records.v1",
        allowed_fields=frozenset({"name", "value"}),
        required_fields=frozenset({"name", "value"}),
        declassification_mode=OutputDeclassificationMode.PREDECLARED_SCALARS,
    )
    spec = OutputArtifactSpec(
        relative_path="laundered.json",
        artifact_type="adversarial-result",
        requested_exposure=ArtifactExposureClass.DERIVED,
        output_contract_id=contract.contract_id,
    )

    decision = ArtifactRegistrationPolicy((contract,)).assess(spec, path)

    assert decision.contract_valid is True
    assert decision.actual_exposure is ArtifactExposureClass.RAW
    assert decision.representation.records == ()


def test_predeclared_label_and_numeric_scalar_receive_exact_release(tmp_path):
    path = tmp_path / "safe.json"
    path.write_text(
        json.dumps(
            {
                "schema_id": "c12.scalar.records.v1",
                "records": [{"name": "declared_metric", "value": 3.5}],
            }
        ),
        encoding="utf-8",
    )
    contract = StructuredOutputContract(
        contract_id="c12-records-v1",
        schema_id="c12.scalar.records.v1",
        allowed_fields=frozenset({"name", "value"}),
        required_fields=frozenset({"name", "value"}),
        declassification_mode=OutputDeclassificationMode.PREDECLARED_SCALARS,
    )
    spec = OutputArtifactSpec(
        relative_path="safe.json",
        artifact_type="bounded-result",
        requested_exposure=ArtifactExposureClass.DERIVED,
        output_contract_id=contract.contract_id,
        predeclared_string_values={"name": ("declared_metric",)},
    )

    decision = ArtifactRegistrationPolicy((contract,)).assess(spec, path)

    assert decision.contract_valid is True
    assert decision.release_authorized is True
    assert decision.actual_exposure is ArtifactExposureClass.DERIVED
    assert decision.representation.records == ({"name": "declared_metric", "value": 3.5},)


def test_shape_contract_without_release_authority_stays_raw(tmp_path):
    path = tmp_path / "numeric-only.json"
    path.write_text(
        json.dumps(
            {
                "schema_id": "c12.scalar.records.v1",
                "records": [{"value": 3.5}],
            }
        ),
        encoding="utf-8",
    )
    contract = StructuredOutputContract(
        contract_id="c12-shape-only-v1",
        schema_id="c12.scalar.records.v1",
        allowed_fields=frozenset({"value"}),
        required_fields=frozenset({"value"}),
    )
    spec = OutputArtifactSpec(
        relative_path="numeric-only.json",
        artifact_type="bounded-result",
        requested_exposure=ArtifactExposureClass.DERIVED,
        output_contract_id=contract.contract_id,
    )

    decision = ArtifactRegistrationPolicy((contract,)).assess(spec, path)

    assert decision.contract_valid is True
    assert decision.release_authorized is False
    assert decision.actual_exposure is ArtifactExposureClass.RAW
