"""Test-side semantic grounding oracle for numeric report claims."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Mapping, Sequence


_UUID_TEXT = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_NUMBER_TEXT = re.compile(
    r"(?<![A-Za-z0-9_])[-+]?\d[\d,]*(?:\.\d+)?(?:[eE][-+]?\d+)?"
    r"(?![A-Za-z0-9_])"
)
_SEPARATOR_CELL = re.compile(r"^:?-{3,}:?$")
_REJECTED_CLAIM = re.compile(
    r"\b(?:not supported|unsupported|incorrect|disregard(?:ed)?|refut(?:e|ed))\b",
    re.IGNORECASE,
)
_PROPOSAL_CUE = re.compile(
    r"\b(?:consider|recommend(?:ed|ation)?|propos(?:e|ed|al)|suggest(?:ed|ion)?|"
    r"could|should|may|might|would|try|test(?:ing)?|evaluate|explore)\b",
    re.IGNORECASE,
)
_IMPERATIVE_CUE = re.compile(
    r"\b(?:filter|remove|retain|set|apply|run|use|flag)\b",
    re.IGNORECASE,
)
_PARAMETER_CUE = re.compile(
    r"(?:%|\bpercent(?:age)?\b|\bMAD\b|\bthreshold\b|\bcutoff\b|"
    r"\blimit\b|\bbound\b|\bparameter\b)",
    re.IGNORECASE,
)

_RECORD_ALIASES = {
    "n_cells": {"cell", "cells", "cell count", "observation count", "dataset"},
    "n_genes": {"gene", "genes", "gene count", "feature count", "gene panel"},
    "n_mt_genes": {"mt gene", "mt genes", "mitochondrial gene", "mitochondrial genes"},
    "n_ribo_genes": {"ribo gene", "ribo genes", "ribosomal gene", "ribosomal genes"},
    "total_counts_distribution": {
        "total count",
        "total counts",
        "umi count",
        "umi counts",
    },
    "total_counts_outliers": {
        "outlier",
        "outliers",
        "total count outlier",
        "total count outliers",
    },
    "detected_genes_distribution": {
        "detected gene",
        "detected genes",
        "gene count distribution",
        "detects",
    },
    "detected_genes_outliers": {
        "outlier",
        "outliers",
        "detected gene outlier",
        "detected gene outliers",
    },
    "combined_outlier_summary": {"combined outlier", "outlier cell", "outlier cells"},
    "n_outlier_cells": {"combined outlier", "outlier cell", "outlier cells"},
}
_FIELD_ALIASES = {
    "count": {"cell", "cells", "feature", "features", "gene", "genes", "n"},
    "minimum": {"minimum", "min"},
    "maximum": {"maximum", "max"},
    "mean": {"mean", "average"},
    "median": {"median"},
    "q05": {"q05", "5th percentile"},
    "q25": {"q25", "25th percentile"},
    "q75": {"q75", "75th percentile"},
    "q95": {"q95", "95th percentile"},
    "value": {"value"},
}


@dataclass(frozen=True)
class _EvidenceFact:
    value: float
    record_name: str
    record_aliases: frozenset[str]
    field_aliases: frozenset[str]
    singleton: bool


def _normalize_phrase(value: str) -> str:
    value = re.sub(r"[_-]+", " ", value.lower())
    return re.sub(r"\s+", " ", value).strip()


def _name_aliases(value: str) -> set[str]:
    phrase = _normalize_phrase(value)
    aliases = {phrase}
    if phrase.startswith("n "):
        aliases.add(phrase[2:])
    for suffix in (" distribution", " summary"):
        if phrase.endswith(suffix):
            aliases.add(phrase[: -len(suffix)])
    aliases.update(_RECORD_ALIASES.get(value, ()))
    return {_normalize_phrase(alias) for alias in aliases if alias}


def _record_facts(records: Sequence[dict]) -> tuple[_EvidenceFact, ...]:
    facts: list[_EvidenceFact] = []
    for record in records:
        names = tuple(
            value
            for key in ("metric", "record_type")
            if isinstance((value := record.get(key)), str)
        )
        if not names:
            continue
        record_name = str(record.get("metric") or record.get("record_type"))
        record_aliases = set().union(*(_name_aliases(name) for name in names))
        numeric_fields = tuple(
            key
            for key, value in record.items()
            if key not in {"metric", "record_type"}
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
        )
        singleton = len(numeric_fields) == 1
        for field in numeric_fields:
            aliases = set() if field == "count" else {_normalize_phrase(field)}
            aliases.update(_FIELD_ALIASES.get(field, ()))
            facts.append(
                _EvidenceFact(
                    value=float(record[field]),
                    record_name=record_name,
                    record_aliases=frozenset(record_aliases),
                    field_aliases=frozenset(_normalize_phrase(item) for item in aliases),
                    singleton=singleton,
                )
            )
        for key, value in record.items():
            if not isinstance(value, str) or key in {"record_type", "unit"}:
                continue
            for match in _NUMBER_TEXT.finditer(value):
                field_aliases = {_normalize_phrase(key), "identifier"}
                lowered = value.lower()
                if "mad" in lowered:
                    field_aliases.update({"mad", "threshold", "outlier"})
                facts.append(
                    _EvidenceFact(
                        value=float(match.group(0).replace(",", "")),
                        record_name=record_name,
                        record_aliases=frozenset(record_aliases),
                        field_aliases=frozenset(field_aliases),
                        singleton=False,
                    )
                )
    return tuple(facts)


def _alias_score(alias: str, context: str) -> int:
    alias = _normalize_phrase(alias)
    context = _normalize_phrase(context)
    if not alias:
        return 0
    word_count = len(alias.split())
    if re.search(rf"(?<!\w){re.escape(alias)}(?!\w)", context):
        return 1000 * word_count + len(alias)
    words = tuple(word for word in alias.split() if len(word) > 1)
    if len(words) > 1 and all(
        re.search(rf"\b{re.escape(word)}(?:s|es)?\b", context)
        for word in words
    ):
        return 800 * len(words) + len(alias)
    return 0


def _fact_candidates(
    facts: Sequence[_EvidenceFact],
    *,
    record_context: str,
    field_context: str,
    select_best_record: bool = True,
    field_anchor: tuple[int, int] | None = None,
    record_focus: str = "",
) -> tuple[_EvidenceFact, ...]:
    record_scores = {
        fact: max((_alias_score(alias, record_context) for alias in fact.record_aliases), default=0)
        + (
            10000
            + max((_alias_score(alias, record_focus) for alias in fact.record_aliases), default=0)
            if any(_alias_score(alias, record_focus) for alias in fact.record_aliases)
            else 0
        )
        for fact in facts
    }
    matched = tuple(fact for fact, score in record_scores.items() if score > 0)
    if not matched:
        return ()
    if select_best_record:
        best_record_score = max(record_scores[fact] for fact in matched)
        matched = tuple(fact for fact in matched if record_scores[fact] == best_record_score)
    field_scores = {
        fact: max(
            (
                _field_alias_score(alias, field_context, field_anchor)
                for alias in fact.field_aliases
            ),
            default=0,
        )
        for fact in matched
    }
    field_matched = tuple(fact for fact in matched if field_scores[fact] > 0)
    if field_matched:
        best_field_score = max(field_scores[fact] for fact in field_matched)
        return tuple(fact for fact in field_matched if field_scores[fact] == best_field_score)
    return tuple(fact for fact in matched if fact.singleton)


def _markdown_contexts(lines: Sequence[str]) -> tuple[dict[int, str], dict[int, str]]:
    headings: dict[int, str] = {}
    table_contexts: dict[int, str] = {}
    current_heading = ""
    table_headers: tuple[str, ...] | None = None
    in_table = False
    for line_number, line in enumerate(lines, start=1):
        heading_match = re.match(r"^\s*#{1,6}\s+(.*)$", line)
        if heading_match:
            current_heading = heading_match.group(1).strip()
        headings[line_number] = current_heading
        if "|" not in line:
            table_headers = None
            in_table = False
            continue
        cells = tuple(cell.strip() for cell in line.strip().strip("|").split("|"))
        if not in_table:
            table_headers = cells
            in_table = True
            continue
        if cells and all(_SEPARATOR_CELL.fullmatch(cell) for cell in cells):
            continue
        labels = tuple(
            label
            for cell in cells
            if (label := _NUMBER_TEXT.sub("", cell).strip())
        )
        table_contexts[line_number] = " ".join(
            (current_heading, *(table_headers or ()), *labels)
        )
    return headings, table_contexts


def _display_tolerance(token: str) -> float:
    normalized = token.replace(",", "")
    mantissa = re.split(r"[eE]", normalized, maxsplit=1)[0]
    decimals = len(mantissa.partition(".")[2])
    return 0.5 * (10 ** -decimals) if decimals else 0.5


def _field_alias_score(
    alias: str,
    context: str,
    anchor: tuple[int, int] | None,
) -> int:
    if anchor is None:
        return _alias_score(alias, context)
    alias = _normalize_phrase(alias)
    searchable = re.sub(r"[_-]", " ", context.lower())
    pattern = r"(?<!\w)" + r"\s+".join(
        re.escape(word) for word in alias.split()
    ) + r"(?!\w)"
    distances = []
    for match in re.finditer(pattern, searchable):
        if match.end() <= anchor[0]:
            distances.append(anchor[0] - match.end())
        elif anchor[1] <= match.start():
            distances.append(match.start() - anchor[1])
        else:
            distances.append(0)
    if not distances:
        return 0
    specificity = 0
    if alias in {
        "minimum",
        "min",
        "maximum",
        "max",
        "mean",
        "average",
        "median",
        "q05",
        "5th percentile",
        "q25",
        "25th percentile",
        "q75",
        "75th percentile",
        "q95",
        "95th percentile",
    }:
        specificity = 1000
    elif alias == "mad":
        specificity = 1000
    return max(1, 2000 + specificity - min(distances) * 50 + len(alias))


def _table_cell_context(
    line: str,
    match: re.Match[str],
) -> tuple[str, tuple[int, int]]:
    start = line.rfind("|", 0, match.start()) + 1
    end = line.find("|", match.end())
    if end < 0:
        end = len(line)
    return line[start:end], (match.start() - start, match.end() - start)


def _table_row_focus(line: str) -> str:
    cells = tuple(cell.strip() for cell in line.strip().strip("|").split("|"))
    return cells[0] if cells else ""


def _sentence_context(
    line: str,
    match: re.Match[str],
) -> tuple[str, tuple[int, int]]:
    start = max(line.rfind(".", 0, match.start()), line.rfind(";", 0, match.start())) + 1
    ends = tuple(
        index
        for delimiter in (".", ";")
        if (index := line.find(delimiter, match.end())) >= 0
    )
    end = min(ends) if ends else len(line)
    return line[start:end], (match.start() - start, match.end() - start)


def _matches(value: float, candidates: Sequence[float], token: str) -> bool:
    tolerance = _display_tolerance(token)
    return any(abs(value - candidate) <= tolerance + 1e-12 for candidate in candidates)


def _quoted_rejection(line: str, start: int, end: int) -> bool:
    if _REJECTED_CLAIM.search(line) is None:
        return False
    for pattern in (r'"[^"\n]*"', r"'[^'\n]*'", r"“[^”\n]*”"):
        for quoted in re.finditer(pattern, line):
            if quoted.start() <= start and end <= quoted.end():
                return True
    return False


def _structural_number(line: str, match: re.Match[str]) -> bool:
    start, end = match.span()
    before = line[max(0, start - 24) : start]
    after = line[end : min(len(line), end + 24)]
    local = before + match.group(0) + after
    if re.search(rf"\btop[-_]\s*{re.escape(match.group(0))}\b", local, re.IGNORECASE):
        return True
    if re.search(
        rf"\b(?:step|section|figure|table|appendix)\s*(?:#|no\.)?\s*{re.escape(match.group(0))}\b",
        local,
        re.IGNORECASE,
    ):
        return True
    return _quoted_rejection(line, start, end)


def _recommendation_number(line: str, heading: str, match: re.Match[str]) -> bool:
    sentence, _ = _sentence_context(line, match)
    if _PARAMETER_CUE.search(sentence) is None:
        return False
    if _PROPOSAL_CUE.search(sentence) is not None:
        return True
    return "recommend" in heading.lower() and _IMPERATIVE_CUE.search(
        re.sub(r"^\s*\d+[.)]\s*", "", sentence)
    ) is not None


def _derived_candidates(
    line: str,
    match: re.Match[str],
    operands: Sequence[float],
) -> tuple[float, ...]:
    local = line[max(0, match.start() - 32) : min(len(line), match.end() + 32)]
    candidates: list[float] = []
    if "%" in local or re.search(r"\bpercent(?:age)?\b", local, re.IGNORECASE):
        candidates.extend(value * 100 for value in operands if 0 <= value <= 1)
        candidates.extend(
            left / right * 100
            for left in operands
            for right in operands
            if right != 0 and left != right
        )
    if re.search(r"(?:×|\bx\b|\bfold\b|\bratio\b|\bper\b)", local, re.IGNORECASE):
        candidates.extend(
            left / right
            for left in operands
            for right in operands
            if right != 0 and left != right
        )
    if re.search(r"\b(?:difference|less|more)\b", local, re.IGNORECASE):
        candidates.extend(
            (left - right)
            for left in operands
            for right in operands
            if left != right
        )
    if re.search(r"\b(?:sum|combined total|total of)\b", local, re.IGNORECASE):
        candidates.extend(
            left + right
            for index, left in enumerate(operands)
            for right in operands[index + 1 :]
        )
    return tuple(candidates)


def numeric_claim_failures(
    report_text: str,
    records: Sequence[dict],
    *,
    completeness_values: Sequence[int | float] = (),
    governed_metadata: Mapping[str, Sequence[int | float]] | None = None,
) -> list[dict]:
    """Return factual numeric tokens that are not grounded or boundedly derived."""

    facts = _record_facts(records)
    governed_metadata = governed_metadata or {}
    lines = report_text.splitlines()
    headings, table_contexts = _markdown_contexts(lines)
    failures: list[dict] = []

    for line_number, original in enumerate(lines, start=1):
        line = _UUID_TEXT.sub("", original)
        line = re.sub(r"^\s*(?:#{1,6}\s*)?\d+[.)]\s*", "", line)
        matches = tuple(_NUMBER_TEXT.finditer(line))
        if not matches:
            continue
        heading = headings[line_number]
        table_context = table_contexts.get(line_number)
        direct_candidates: list[tuple[float, ...]] = []
        matched_names: list[tuple[str, ...]] = []
        for match in matches:
            local = line[max(0, match.start() - 32) : min(len(line), match.end() + 32)]
            field_context, field_anchor = (
                _table_cell_context(line, match)
                if table_context is not None
                else _sentence_context(line, match)
            )
            contexts = (
                (table_context, field_context)
                if table_context is not None
                else (f"{heading} {local}", field_context)
            )
            candidates = _fact_candidates(
                facts,
                record_context=contexts[0],
                field_context=contexts[1],
                field_anchor=field_anchor,
                record_focus=_table_row_focus(line) if table_context is not None else "",
            )
            if not candidates:
                candidates = _fact_candidates(
                    facts,
                    record_context=table_context or f"{heading} {line}",
                    field_context=line if table_context is not None else local,
                    field_anchor=match.span() if table_context is not None else None,
                    record_focus=_table_row_focus(line) if table_context is not None else "",
                )
            if table_context is None:
                broad_candidates = _fact_candidates(
                    facts,
                    record_context=f"{heading} {line}",
                    field_context=field_context,
                    select_best_record=False,
                    field_anchor=field_anchor,
                )
                candidates = tuple(dict.fromkeys((*candidates, *broad_candidates)))
            values = [fact.value for fact in candidates]
            names = [fact.record_name for fact in candidates]
            lowered_context = _normalize_phrase(table_context or f"{heading} {line}")
            for alias, metadata_numbers in governed_metadata.items():
                if _alias_score(alias, lowered_context):
                    values.extend(float(value) for value in metadata_numbers)
                    names.append(f"metadata:{alias}")
            if re.search(
                r"\b(?:returned|available|records?|truncat(?:ed|ion)?|complete(?:ness)?|"
                r"effective limit|top n)\b",
                lowered_context,
            ):
                values.extend(float(value) for value in completeness_values)
                names.append("completeness")
            direct_candidates.append(tuple(values))
            matched_names.append(tuple(dict.fromkeys(names)))

        operands = [
            float(match.group(0).replace(",", ""))
            for match, candidates in zip(matches, direct_candidates)
            if _matches(
                float(match.group(0).replace(",", "")),
                candidates,
                match.group(0),
            )
        ]
        if re.search(r"\b(?:dataset|all cells|population)\b", line, re.IGNORECASE):
            operands.extend(
                fact.value
                for fact in facts
                if any(alias in {"dataset", "cell count", "observation count"} for alias in fact.record_aliases)
            )

        for match, candidates, names in zip(matches, direct_candidates, matched_names):
            token = match.group(0)
            value = float(token.replace(",", ""))
            if _structural_number(line, match):
                continue
            if _matches(value, candidates, token):
                continue
            if _matches(value, _derived_candidates(line, match, operands), token):
                continue
            if _recommendation_number(line, heading, match):
                continue
            failures.append(
                {
                    "line": line_number,
                    "token": token,
                    "matched_metrics": list(names),
                    "text": original[:500],
                }
            )
    return failures
