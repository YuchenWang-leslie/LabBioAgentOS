"""Trusted, bounded inspection of supported biological file formats."""

from __future__ import annotations

import math
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import anndata as ad
import numpy as np
from pandas.api.types import is_bool_dtype, is_numeric_dtype
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    StringConstraints,
)


BoundedName = Annotated[
    StrictStr,
    StringConstraints(min_length=1, max_length=128),
]
BoundedDType = Annotated[
    StrictStr,
    StringConstraints(min_length=1, max_length=128),
]


class H5ADInspectionError(ValueError):
    """A trusted local input is not suitable for bounded h5ad inspection."""


class H5ADMatrixStorage(StrEnum):
    """Technical storage representation; it carries no scientific meaning."""

    ABSENT = "ABSENT"
    DENSE = "DENSE"
    SPARSE_CSR = "SPARSE_CSR"
    SPARSE_CSC = "SPARSE_CSC"
    SPARSE_OTHER = "SPARSE_OTHER"


class H5ADCategoryEnumeration(StrEnum):
    """Whether bounded categorical labels were safe to enumerate."""

    ENUMERATED = "ENUMERATED"
    ENUMERATED_WITH_OVERFLOW = "ENUMERATED_WITH_OVERFLOW"
    HIGH_CARDINALITY_SUPPRESSED = "HIGH_CARDINALITY_SUPPRESSED"


class H5ADInspectionPolicy(BaseModel):
    """Hard output bounds for trusted inspection, independent of biology."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    max_axis_fields: int = Field(default=64, ge=1, le=128)
    max_named_arrays: int = Field(default=32, ge=1, le=64)
    max_aggregate_fields: int = Field(default=32, ge=1, le=64)
    max_categories_per_field: int = Field(default=20, ge=1, le=64)
    max_name_length: int = Field(default=128, ge=16, le=128)
    max_category_label_length: int = Field(default=128, ge=16, le=256)
    high_cardinality_fraction: float = Field(default=0.8, gt=0.0, le=1.0)


class H5ADFieldStructure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    name: BoundedName
    dtype: BoundedDType


class H5ADAxisStructure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    field_count: int = Field(ge=0)
    fields: tuple[H5ADFieldStructure, ...] = Field(default=(), max_length=128)
    overflow_field_count: int = Field(default=0, ge=0)
    index_dtype: BoundedDType
    index_unique: bool


class H5ADMatrixStructure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    shape: tuple[int, ...] = Field(max_length=8)
    dtype: BoundedDType | None = None
    storage: H5ADMatrixStorage


class H5ADNamedArrayStructure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    key: BoundedName
    shape: tuple[int, ...] = Field(max_length=8)


class H5ADStructuralSummary(BaseModel):
    """Schema-only h5ad description with no matrix values or axis index values."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    format: Annotated[StrictStr, StringConstraints(pattern="^h5ad$")] = "h5ad"
    n_obs: int = Field(ge=0)
    n_vars: int = Field(ge=0)
    x: H5ADMatrixStructure
    obs: H5ADAxisStructure
    var: H5ADAxisStructure
    layer_names: tuple[BoundedName, ...] = Field(default=(), max_length=64)
    layer_overflow_count: int = Field(default=0, ge=0)
    obsm: tuple[H5ADNamedArrayStructure, ...] = Field(default=(), max_length=64)
    obsm_overflow_count: int = Field(default=0, ge=0)
    varm: tuple[H5ADNamedArrayStructure, ...] = Field(default=(), max_length=64)
    varm_overflow_count: int = Field(default=0, ge=0)
    raw_present: bool
    raw_shape: tuple[int, ...] | None = Field(default=None, max_length=8)


class H5ADCategoryCount(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    label: Annotated[StrictStr, StringConstraints(min_length=1, max_length=256)]
    count: int = Field(ge=0)
    label_truncated: bool = False


class H5ADCategoricalSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    field_name: BoundedName
    dtype: BoundedDType
    count: int = Field(ge=0)
    missing_count: int = Field(ge=0)
    unique_count: int = Field(ge=0)
    enumeration: H5ADCategoryEnumeration
    categories: tuple[H5ADCategoryCount, ...] = Field(default=(), max_length=64)
    overflow_category_count: int = Field(default=0, ge=0)


class H5ADNumericSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    field_name: BoundedName
    dtype: BoundedDType
    count: int = Field(ge=0)
    missing_count: int = Field(ge=0)
    non_finite_count: int = Field(default=0, ge=0)
    minimum: float | None = None
    maximum: float | None = None
    mean: float | None = None
    median: float | None = None


class H5ADAggregateSummary(BaseModel):
    """Bounded obs-column aggregates; observation rows are never represented."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    n_obs: int = Field(ge=0)
    supported_field_count: int = Field(ge=0)
    returned_field_count: int = Field(ge=0)
    overflow_field_count: int = Field(ge=0)
    categorical: tuple[H5ADCategoricalSummary, ...] = Field(
        default=(), max_length=64
    )
    numeric: tuple[H5ADNumericSummary, ...] = Field(default=(), max_length=64)


class H5ADInspectionResult(BaseModel):
    """Safe host result suitable for STRUCTURAL/AGGREGATE registration."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    structural: H5ADStructuralSummary
    aggregate: H5ADAggregateSummary


class H5ADInspector:
    """Read one h5ad locally in backed mode and emit only bounded safe DTOs."""

    def __init__(self, policy: H5ADInspectionPolicy | None = None):
        self.policy = policy or H5ADInspectionPolicy()

    def inspect(self, source: str | Path) -> H5ADInspectionResult:
        path = Path(source)
        if path.is_symlink():
            raise H5ADInspectionError("h5ad inspection input cannot be a symlink")
        try:
            path = path.resolve(strict=True)
        except OSError as exc:
            raise H5ADInspectionError("h5ad inspection input does not exist") from exc
        if not path.is_file():
            raise H5ADInspectionError("h5ad inspection input must be a regular file")

        data = None
        try:
            data = ad.read_h5ad(path, backed="r")
            return H5ADInspectionResult(
                structural=self._structural(data),
                aggregate=self._aggregate(data),
            )
        except H5ADInspectionError:
            raise
        except Exception as exc:
            raise H5ADInspectionError("Could not inspect h5ad input") from exc
        finally:
            if data is not None and getattr(data, "isbacked", False):
                data.file.close()

    def _structural(self, data) -> H5ADStructuralSummary:
        layer_names, layer_overflow = self._bounded_names(data.layers.keys())
        obsm, obsm_overflow = self._named_arrays(data.obsm)
        varm, varm_overflow = self._named_arrays(data.varm)
        raw_shape = (
            tuple(int(item) for item in data.raw.shape)
            if data.raw is not None
            else None
        )
        return H5ADStructuralSummary(
            n_obs=int(data.n_obs),
            n_vars=int(data.n_vars),
            x=self._matrix(data.X, fallback_shape=(data.n_obs, data.n_vars)),
            obs=self._axis(data.obs),
            var=self._axis(data.var),
            layer_names=layer_names,
            layer_overflow_count=layer_overflow,
            obsm=obsm,
            obsm_overflow_count=obsm_overflow,
            varm=varm,
            varm_overflow_count=varm_overflow,
            raw_present=data.raw is not None,
            raw_shape=raw_shape,
        )

    def _axis(self, frame) -> H5ADAxisStructure:
        columns = list(frame.columns)
        selected = columns[: self.policy.max_axis_fields]
        return H5ADAxisStructure(
            field_count=len(columns),
            fields=tuple(
                H5ADFieldStructure(
                    name=self._name(column),
                    dtype=self._dtype(frame[column].dtype),
                )
                for column in selected
            ),
            overflow_field_count=max(0, len(columns) - len(selected)),
            index_dtype=self._dtype(frame.index.dtype),
            index_unique=bool(frame.index.is_unique),
        )

    def _matrix(self, value, *, fallback_shape) -> H5ADMatrixStructure:
        if value is None:
            return H5ADMatrixStructure(
                shape=tuple(int(item) for item in fallback_shape),
                storage=H5ADMatrixStorage.ABSENT,
            )
        sparse_format = str(getattr(value, "format", "")).lower()
        if sparse_format == "csr":
            storage = H5ADMatrixStorage.SPARSE_CSR
        elif sparse_format == "csc":
            storage = H5ADMatrixStorage.SPARSE_CSC
        elif sparse_format:
            storage = H5ADMatrixStorage.SPARSE_OTHER
        else:
            storage = H5ADMatrixStorage.DENSE
        dtype = getattr(value, "dtype", None)
        return H5ADMatrixStructure(
            shape=tuple(int(item) for item in value.shape),
            dtype=self._dtype(dtype) if dtype is not None else None,
            storage=storage,
        )

    def _named_arrays(self, mapping) -> tuple[tuple[H5ADNamedArrayStructure, ...], int]:
        keys = list(mapping.keys())
        selected = keys[: self.policy.max_named_arrays]
        values = tuple(
            H5ADNamedArrayStructure(
                key=self._name(key),
                shape=tuple(int(item) for item in mapping[key].shape),
            )
            for key in selected
        )
        return values, max(0, len(keys) - len(selected))

    def _bounded_names(self, values) -> tuple[tuple[str, ...], int]:
        names = list(values)
        selected = names[: self.policy.max_named_arrays]
        return (
            tuple(self._name(value) for value in selected),
            max(0, len(names) - len(selected)),
        )

    def _aggregate(self, data) -> H5ADAggregateSummary:
        categorical = []
        numeric = []
        supported = 0
        returned = 0
        for column in data.obs.columns:
            series = data.obs[column]
            supported += 1
            if returned >= self.policy.max_aggregate_fields:
                continue
            if is_numeric_dtype(series.dtype) and not is_bool_dtype(series.dtype):
                numeric.append(self._numeric(column, series))
            else:
                categorical.append(self._categorical(column, series, int(data.n_obs)))
            returned += 1
        return H5ADAggregateSummary(
            n_obs=int(data.n_obs),
            supported_field_count=supported,
            returned_field_count=returned,
            overflow_field_count=max(0, supported - returned),
            categorical=tuple(categorical),
            numeric=tuple(numeric),
        )

    def _categorical(self, column, series, n_obs: int) -> H5ADCategoricalSummary:
        missing = int(series.isna().sum())
        count = int(len(series) - missing)
        unique = int(series.nunique(dropna=True))
        denominator = max(1, count)
        high_cardinality = unique / denominator >= self.policy.high_cardinality_fraction
        if high_cardinality and unique:
            return H5ADCategoricalSummary(
                field_name=self._name(column),
                dtype=self._dtype(series.dtype),
                count=count,
                missing_count=missing,
                unique_count=unique,
                enumeration=H5ADCategoryEnumeration.HIGH_CARDINALITY_SUPPRESSED,
                overflow_category_count=unique,
            )

        counts = [
            (value, int(value_count))
            for value, value_count in series.value_counts(dropna=True).items()
            if int(value_count) > 0
        ]
        selected = counts[: self.policy.max_categories_per_field]
        categories = tuple(self._category_count(value, value_count) for value, value_count in selected)
        overflow = max(0, unique - len(selected))
        enumeration = (
            H5ADCategoryEnumeration.ENUMERATED_WITH_OVERFLOW
            if overflow
            else H5ADCategoryEnumeration.ENUMERATED
        )
        return H5ADCategoricalSummary(
            field_name=self._name(column),
            dtype=self._dtype(series.dtype),
            count=count,
            missing_count=missing,
            unique_count=unique,
            enumeration=enumeration,
            categories=categories,
            overflow_category_count=overflow,
        )

    def _numeric(self, column, series) -> H5ADNumericSummary:
        missing = int(series.isna().sum())
        values = np.asarray(series.dropna(), dtype=np.float64)
        finite = values[np.isfinite(values)]
        return H5ADNumericSummary(
            field_name=self._name(column),
            dtype=self._dtype(series.dtype),
            count=int(len(series) - missing),
            missing_count=missing,
            non_finite_count=int(values.size - finite.size),
            minimum=self._finite_stat(np.min(finite) if finite.size else None),
            maximum=self._finite_stat(np.max(finite) if finite.size else None),
            mean=self._finite_stat(np.mean(finite) if finite.size else None),
            median=self._finite_stat(np.median(finite) if finite.size else None),
        )

    def _category_count(self, value, count: int) -> H5ADCategoryCount:
        label = str(value) or "(empty)"
        truncated = len(label) > self.policy.max_category_label_length
        return H5ADCategoryCount(
            label=label[: self.policy.max_category_label_length],
            count=count,
            label_truncated=truncated,
        )

    def _name(self, value) -> str:
        text = str(value) or "(empty)"
        return text[: self.policy.max_name_length]

    @staticmethod
    def _dtype(value) -> str:
        text = str(value) or "unknown"
        return text[:128]

    @staticmethod
    def _finite_stat(value) -> float | None:
        if value is None:
            return None
        number = float(value)
        return number if math.isfinite(number) else None


__all__ = [
    "H5ADAggregateSummary",
    "H5ADCategoryCount",
    "H5ADCategoryEnumeration",
    "H5ADCategoricalSummary",
    "H5ADFieldStructure",
    "H5ADInspectionError",
    "H5ADInspectionPolicy",
    "H5ADInspectionResult",
    "H5ADInspector",
    "H5ADMatrixStorage",
    "H5ADMatrixStructure",
    "H5ADNamedArrayStructure",
    "H5ADNumericSummary",
    "H5ADStructuralSummary",
]
