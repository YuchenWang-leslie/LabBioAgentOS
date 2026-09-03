"""C12 bounded-scalar execution-output declassification tests."""

from __future__ import annotations

import json

import pytest

from labbioagentos import (
    ArtifactExposureClass,
    ArtifactRegistrationPolicy,
    ArtifactReleaseBasis,
    OutputArtifactSpec,
    OutputDeclassificationMode,
    StructuredOutputContract,
)


SCHEMA_ID = "c12.scalar.records.v1"
CONTRACT_ID = "c12-records-v1"


def _contract(
    *,
    allowed_fields: frozenset[str] = frozenset({"label", "value"}),
    required_fields: frozenset[str] = frozenset({"label", "value"}),
    max_records: int = 100,
    max_file_bytes: int = 1_048_576,
    mode: OutputDeclassificationMode = OutputDeclassificationMode.BOUNDED_SCALARS,
) -> StructuredOutputContract:
    return StructuredOutputContract(
        contract_id=CONTRACT_ID,
        schema_id=SCHEMA_ID,
        allowed_fields=allowed_fields,
        required_fields=required_fields,
        max_records=max_records,
        max_file_bytes=max_file_bytes,
        declassification_mode=mode,
    )


def _assess(tmp_path, records, *, contract=None):
    contract = contract or _contract()
    path = tmp_path / "result.json"
    path.write_text(
        json.dumps({"schema_id": SCHEMA_ID, "records": records}),
        encoding="utf-8",
    )
    spec = OutputArtifactSpec(
        relative_path="result.json",
        artifact_type="bounded-result",
        requested_exposure=ArtifactExposureClass.DERIVED,
        output_contract_id=contract.contract_id,
    )
    return ArtifactRegistrationPolicy((contract,)).assess(spec, path)


def _assert_derived(decision) -> None:
    assert decision.contract_valid is True
    assert decision.release_authorized is True
    assert decision.actual_exposure is ArtifactExposureClass.DERIVED
    assert (
        decision.release_basis
        is ArtifactReleaseBasis.TRUSTED_EXECUTION_DECLASSIFICATION
    )


def _assert_raw(decision, *, contract_valid: bool) -> None:
    assert decision.contract_valid is contract_valid
    assert decision.release_authorized is False
    assert decision.actual_exposure is ArtifactExposureClass.RAW
    assert decision.representation.records == ()


def test_ds1_numeric_only_bounded_output_is_derived(tmp_path):
    contract = _contract(
        allowed_fields=frozenset({"value"}),
        required_fields=frozenset({"value"}),
    )

    decision = _assess(tmp_path, [{"value": 3.5}], contract=contract)

    _assert_derived(decision)


def test_ds2_gene_and_feature_identifier_strings_are_derived(tmp_path):
    records = [
        {"label": "GZMK", "value": 2.8},
        {"label": "CXCL13", "value": 1.7},
        {"label": "ENSG00000141510", "value": 0.9},
    ]

    decision = _assess(tmp_path, records)

    _assert_derived(decision)
    assert decision.representation.records == tuple(records)


def test_ds3_pathway_strings_are_derived(tmp_path):
    records = [
        {"label": "Interferon gamma response", "value": 4.2},
        {"label": "GO:0006955", "value": 3.1},
    ]

    decision = _assess(tmp_path, records)

    _assert_derived(decision)
    assert decision.representation.records == tuple(records)


def test_ds4_donor_sample_and_cluster_strings_are_derived(tmp_path):
    records = [
        {"label": "P_01", "value": 1},
        {"label": "donor_2", "value": 2},
        {"label": "sample_A", "value": 3},
        {"label": "cluster_3", "value": 4},
    ]

    decision = _assess(tmp_path, records)

    _assert_derived(decision)
    assert decision.representation.records == tuple(records)


def test_ds5_barcode_like_string_is_derived(tmp_path):
    records = [{"label": "AAACCCAAGT...", "value": 5}]

    decision = _assess(tmp_path, records)

    _assert_derived(decision)
    assert decision.representation.records == tuple(records)


def test_ds6_mixed_flat_json_scalars_are_derived(tmp_path):
    contract = _contract(
        allowed_fields=frozenset({"label", "integer", "number", "flag", "missing"}),
        required_fields=frozenset({"label", "integer", "number", "flag", "missing"}),
    )
    records = [
        {
            "label": "bounded-result",
            "integer": 2,
            "number": 2.5,
            "flag": True,
            "missing": None,
        }
    ]

    decision = _assess(tmp_path, records, contract=contract)

    _assert_derived(decision)
    assert decision.representation.records == tuple(records)


def test_ds7_unknown_field_stays_raw(tmp_path):
    decision = _assess(
        tmp_path,
        [{"label": "GZMK", "value": 2.8, "unexpected": "field"}],
    )

    _assert_raw(decision, contract_valid=False)


def test_ds8_missing_required_field_stays_raw(tmp_path):
    decision = _assess(tmp_path, [{"label": "GZMK"}])

    _assert_raw(decision, contract_valid=False)


@pytest.mark.parametrize("nested", [[1, 2], {"row": 1}])
def test_ds9_nested_values_stay_raw(tmp_path, nested):
    decision = _assess(tmp_path, [{"label": "GZMK", "value": nested}])

    _assert_raw(decision, contract_valid=False)


def test_ds10_record_count_overflow_stays_raw(tmp_path):
    contract = _contract(max_records=1)
    decision = _assess(
        tmp_path,
        [{"label": "GZMK", "value": 1}, {"label": "CXCL13", "value": 2}],
        contract=contract,
    )

    _assert_raw(decision, contract_valid=False)


def test_ds11_file_size_overflow_stays_raw(tmp_path):
    contract = _contract(max_file_bytes=64)
    decision = _assess(
        tmp_path,
        [{"label": "x" * 80, "value": 1}],
        contract=contract,
    )

    _assert_raw(decision, contract_valid=False)


def test_ds12_absolute_host_path_string_is_not_released(tmp_path):
    decision = _assess(
        tmp_path,
        [{"label": "/private/host/input.h5ad", "value": 1}],
    )

    _assert_raw(decision, contract_valid=True)


@pytest.mark.parametrize(
    "field",
    [
        "storage_locator",
        "script_content",
        "provider_raw_body",
        "reasoning_content",
        "credentials",
    ],
)
def test_ds13_forbidden_system_field_is_not_released(tmp_path, field):
    contract = _contract(
        allowed_fields=frozenset({field, "value"}),
        required_fields=frozenset({field, "value"}),
    )
    decision = _assess(
        tmp_path,
        [{field: "system material", "value": 1}],
        contract=contract,
    )

    _assert_raw(decision, contract_valid=True)


def test_ds14_private_key_material_is_not_released(tmp_path):
    key = "-----BEGIN PRIVATE KEY-----\nnot-a-real-key\n-----END PRIVATE KEY-----"
    decision = _assess(tmp_path, [{"label": key, "value": 1}])

    _assert_raw(decision, contract_valid=True)


def test_ds15_none_mode_shape_valid_output_stays_raw(tmp_path):
    contract = _contract(mode=OutputDeclassificationMode.NONE)
    decision = _assess(
        tmp_path,
        [{"label": "GZMK", "value": 2.8}],
        contract=contract,
    )

    _assert_raw(decision, contract_valid=True)
