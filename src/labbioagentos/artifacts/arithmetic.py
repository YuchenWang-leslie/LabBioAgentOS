"""Mechanical arithmetic over an already-authorized complete record table."""

import math
from collections import Counter
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from labbioagentos.model_safety import validate_model_visible_json

from .models import ArtifactRepresentation


class ArtifactArithmeticError(ValueError):
    """Only fixed, non-content-bearing diagnostics cross the tool boundary."""

    MESSAGES = {
        "INCOMPLETE_AGGREGATE_SOURCE": "Complete stored records are required; no partial total was returned.",
        "INVALID_AGGREGATE_SELECTION": "SUM requires a numeric field; COUNT requires no field. A match value requires a match field.",
        "AGGREGATE_FIELD_NOT_FOUND": "The selected field is absent from the source table.",
        "AGGREGATE_NON_NUMERIC_VALUE": "Every matched SUM value must be a finite number; missing, null, string and boolean values are not coerced or skipped.",
    }

    def __init__(self, code, *, value_diagnostics=None):
        self.code = code
        self.safe_message = self.MESSAGES[code]
        self.value_diagnostics = value_diagnostics
        super().__init__(self.safe_message)


ArtifactAggregateMatchValue = Annotated[str, Field(max_length=256)] | int | float | bool | None


class ArtifactAggregateQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    operation: Literal["SUM", "COUNT"]
    field: str | None = Field(default=None, min_length=1, max_length=128)
    match_field: str | None = Field(default=None, min_length=1, max_length=128)
    match_value: ArtifactAggregateMatchValue = None


def aggregate_records(representation: ArtifactRepresentation, query: ArtifactAggregateQuery):
    """No imputation, numeric coercion, grouping inference or partial totals."""
    records = representation.records
    if len(records) != representation.record_count:
        raise ArtifactArithmeticError("INCOMPLETE_AGGREGATE_SOURCE")
    if (query.operation == "SUM") != (query.field is not None):
        raise ArtifactArithmeticError("INVALID_AGGREGATE_SELECTION")
    if query.match_field is None and query.match_value is not None:
        raise ArtifactArithmeticError("INVALID_AGGREGATE_SELECTION")
    if isinstance(query.match_value, float) and not math.isfinite(query.match_value):
        raise ValueError("Match value must be finite")
    for field in (query.field, query.match_field):
        if field is not None and records and not any(field in row for row in records):
            raise ArtifactArithmeticError("AGGREGATE_FIELD_NOT_FOUND")
    matched = [row for row in records if query.match_field is None or (
        query.match_field in row
        and type(row[query.match_field]) is type(query.match_value)
        and row[query.match_field] == query.match_value
    )]
    if query.operation == "COUNT":
        value = len(matched)
    else:
        values = [row.get(query.field) for row in matched]
        rejected = Counter()
        for row in matched:
            value = row.get(query.field)
            if query.field not in row:
                rejected["MISSING"] += 1
            elif value is None:
                rejected["NULL"] += 1
            elif type(value) is bool:
                rejected["BOOLEAN"] += 1
            elif type(value) is str:
                rejected["STRING"] += 1
            elif type(value) is list:
                rejected["ARRAY"] += 1
            elif type(value) is dict:
                rejected["OBJECT"] += 1
            elif not math.isfinite(value):
                rejected["NON_FINITE_NUMBER"] += 1
        if rejected:
            raise ArtifactArithmeticError("AGGREGATE_NON_NUMERIC_VALUE", value_diagnostics={
                "argument_path": "field", "source_record_count": len(records),
                "matched_count": len(matched), "numeric_count": len(matched) - sum(rejected.values()),
                "rejected_type_counts": dict(rejected),
            })
        value = (sum(values) if all(type(v) is int for v in values) else math.fsum(values)) if values else None
        if value is not None and not math.isfinite(value):
            raise ValueError("Aggregate must be finite")
    result = {**query.model_dump(mode="json"), "source_record_count": len(records),
        "matched_count": len(matched), "value": value}
    if not matched:
        # Do not persist arbitrary unmatched caller text as trusted evidence.
        result.pop("match_value")
    validate_model_visible_json(result, reject_absolute_paths=True)
    return result
