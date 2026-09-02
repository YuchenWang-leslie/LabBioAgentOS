"""C7.5 synthetic coverage for semantic numeric-claim grounding."""

from __future__ import annotations

from tests.numeric_claim_oracle import numeric_claim_failures


EVIDENCE = (
    {
        "record_type": "dataset_overview",
        "metric": "n_cells",
        "count": 100,
    },
    {
        "record_type": "signal_distribution",
        "metric": "signal",
        "count": 100,
        "minimum": 5,
        "mean": 12.5,
    },
    {
        "record_type": "flagged_cell_summary",
        "metric": "n_flagged_cells",
        "count": 25,
    },
)


def _failures(text: str, **kwargs):
    return numeric_claim_failures(text, EVIDENCE, **kwargs)


def test_n1_observed_scalar_with_exact_evidence_passes():
    assert _failures("The observed signal mean was 12.5.") == []


def test_n2_observed_scalar_with_wrong_number_fails():
    failures = _failures("The observed signal mean was 5.")
    assert [item["token"] for item in failures] == ["5"]


def test_n3_markdown_table_uses_row_and_column_metric_context():
    report = """\
### Signal Distribution
| Statistic | Value |
|---|---:|
| Minimum | 5 |
| Mean | 12.5 |
"""
    assert _failures(report) == []


def test_n4_markdown_table_wrong_factual_cell_fails():
    report = """\
### Signal Distribution
| Statistic | Value |
|---|---:|
| Mean | 5 |
"""
    failures = _failures(report)
    assert [item["token"] for item in failures] == ["5"]


def test_n5_transparent_percentage_is_recomputed_from_evidence():
    assert _failures("The 25 flagged cells represent 25% of the 100 cells.") == []


def test_n6_fabricated_percentage_fails():
    failures = _failures("The 25 flagged cells represent 26% of the 100 cells.")
    assert [item["token"] for item in failures] == ["26"]


def test_n7_clearly_marked_recommendation_threshold_is_allowed():
    assert _failures("Consider testing a 10% threshold downstream.") == []
    failures = _failures(
        """\
## Recommended Next Steps
The observed value was 77. Consider testing a 10% threshold downstream.
"""
    )
    assert [item["token"] for item in failures] == ["77"]


def test_n8_observed_threshold_without_evidence_fails():
    failures = _failures("The observed threshold was 10%.")
    assert [item["token"] for item in failures] == ["10"]


def test_n9_number_inside_metric_identifier_is_not_a_factual_claim():
    assert _failures("The top-50 and top-100 metric identifiers are available.") == []


def test_n10_section_and_list_numbering_are_presentational():
    report = """\
## 2. Results
1. The observed signal mean was 12.5.
"""
    assert _failures(report) == []


def test_n11_ambiguous_numeric_sentence_fails_closed():
    failures = _failures("The unexplained value was 77.")
    assert [item["token"] for item in failures] == ["77"]


def test_n12_completeness_metadata_is_grounded_separately():
    assert _failures(
        "The query returned 3 of 3 records with effective limit 10.",
        completeness_values=(3, 10),
    ) == []
    failures = _failures(
        "The query returned 4 of 3 records with effective limit 10.",
        completeness_values=(3, 10),
    )
    assert [item["token"] for item in failures] == ["4"]
