"""Trusted, bounded inspection of supported biological file formats."""

from __future__ import annotations

import math
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Protocol

import anndata as ad
import h5py
import numpy as np
from pandas.api.types import is_bool_dtype, is_numeric_dtype
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    StringConstraints,
    model_validator,
)

from .artifacts import (
    ArtifactExposureClass,
    ArtifactReleaseBasis,
    ArtifactRepresentation,
    ArtifactSchema,
)


BoundedName = Annotated[
    StrictStr,
    StringConstraints(min_length=1, max_length=128),
]
BoundedDType = Annotated[
    StrictStr,
    StringConstraints(min_length=1, max_length=128),
]


class BioFormatInspectionError(ValueError):
    """A trusted biological-format inspection request cannot be completed."""


class BioFormatArtifactSpec(BaseModel):
    """One safe Artifact emitted by a trusted format inspector."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    artifact_type: BoundedName
    exposure_class: ArtifactExposureClass
    representation: ArtifactRepresentation
    artifact_schema: ArtifactSchema
    release_basis: ArtifactReleaseBasis

    @model_validator(mode="after")
    def require_safe_inspection_class(self) -> "BioFormatArtifactSpec":
        if self.exposure_class not in {
            ArtifactExposureClass.STRUCTURAL,
            ArtifactExposureClass.AGGREGATE,
        }:
            raise ValueError(
                "Bioformat inspection Artifacts must be STRUCTURAL or AGGREGATE"
            )
        expected_basis = {
            ArtifactExposureClass.STRUCTURAL: (
                ArtifactReleaseBasis.TRUSTED_STRUCTURAL_INSPECTOR
            ),
            ArtifactExposureClass.AGGREGATE: (
                ArtifactReleaseBasis.TRUSTED_AGGREGATE_INSPECTOR
            ),
        }[self.exposure_class]
        if self.release_basis is not expected_basis:
            raise ValueError(
                "Bioformat Artifact release basis must match its inspection class"
            )
        return self


class BioFormatInspectionBundle(BaseModel):
    """Format-neutral safe Artifact specifications for one inspected RAW input."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    format_key: BoundedName
    inspection_schema_version: BoundedName
    artifacts: tuple[BioFormatArtifactSpec, ...] = Field(
        min_length=1, max_length=8
    )


class BioFormatInspector(Protocol):
    """Small trusted seam implemented by each supported biological format."""

    format_key: str

    def inspect_artifacts(self, source: str | Path) -> BioFormatInspectionBundle:
        """Inspect a local source and return only safe Artifact specifications."""


class BioFormatInspectionRegistry:
    """Explicit host-selected format inspectors; it performs no content routing."""

    def __init__(self, inspectors: tuple[BioFormatInspector, ...]):
        configured: dict[str, BioFormatInspector] = {}
        for inspector in inspectors:
            key = str(inspector.format_key)
            if not key:
                raise BioFormatInspectionError(
                    "Bioformat inspector key cannot be empty"
                )
            if key in configured:
                raise BioFormatInspectionError(
                    "Bioformat inspector keys must be unique"
                )
            configured[key] = inspector
        self._inspectors = configured

    def inspect(self, format_key: str, source: str | Path) -> BioFormatInspectionBundle:
        inspector = self._inspectors.get(format_key)
        if inspector is None:
            raise BioFormatInspectionError(
                "No trusted inspector is configured for this biological format"
            )
        bundle = inspector.inspect_artifacts(source)
        if bundle.format_key != format_key:
            raise BioFormatInspectionError(
                "Bioformat inspector returned a mismatched format key"
            )
        return bundle


class H5ADInspectionError(BioFormatInspectionError):
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
    POLICY_SUPPRESSED = "POLICY_SUPPRESSED"


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
    max_source_bytes: int = Field(default=4_294_967_296, ge=1)
    max_axis_rows: int = Field(default=1_000_000, ge=1)
    max_source_axis_fields: int = Field(default=256, ge=1)
    max_axis_metadata_cells: int = Field(default=25_000_000, ge=1)
    max_eager_array_bytes: int = Field(default=1_073_741_824, ge=1)
    enumerated_categorical_fields: frozenset[BoundedName] = Field(
        default_factory=frozenset,
        max_length=64,
    )


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

    format_key = "h5ad"

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
            expected_shape = self._preflight(path)
            data = ad.read_h5ad(path, backed="r")
            if (int(data.n_obs), int(data.n_vars)) != expected_shape:
                raise H5ADInspectionError(
                    "h5ad axis shape changed during trusted inspection"
                )
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

    def inspect_artifacts(self, source: str | Path) -> BioFormatInspectionBundle:
        """Convert the format-specific result into generic safe Artifact specs."""

        inspection = self.inspect(source)
        obs_fields = inspection.structural.obs.fields
        aggregate = inspection.aggregate
        aggregate_fields = (*aggregate.categorical, *aggregate.numeric)
        return BioFormatInspectionBundle(
            format_key=self.format_key,
            inspection_schema_version="1",
            artifacts=(
                BioFormatArtifactSpec(
                    artifact_type="h5ad-structural",
                    exposure_class=ArtifactExposureClass.STRUCTURAL,
                    release_basis=ArtifactReleaseBasis.TRUSTED_STRUCTURAL_INSPECTOR,
                    representation=ArtifactRepresentation(),
                    artifact_schema=ArtifactSchema(
                        shape=(
                            inspection.structural.n_obs,
                            inspection.structural.n_vars,
                        ),
                        columns=tuple(field.name for field in obs_fields),
                        dtypes={field.name: field.dtype for field in obs_fields},
                        properties=inspection.structural.model_dump(mode="json"),
                    ),
                ),
                BioFormatArtifactSpec(
                    artifact_type="h5ad-aggregate",
                    exposure_class=ArtifactExposureClass.AGGREGATE,
                    release_basis=ArtifactReleaseBasis.TRUSTED_AGGREGATE_INSPECTOR,
                    representation=ArtifactRepresentation(
                        summary=aggregate.model_dump(mode="json")
                    ),
                    artifact_schema=ArtifactSchema(
                        shape=(aggregate.returned_field_count,),
                        columns=tuple(
                            field.field_name for field in aggregate_fields
                        ),
                        dtypes={
                            field.field_name: field.dtype
                            for field in aggregate_fields
                        },
                        properties={"summary_kind": "bounded_obs_aggregates"},
                    ),
                ),
            ),
        )

    def _preflight(self, path: Path) -> tuple[int, int]:
        if path.stat().st_size > self.policy.max_source_bytes:
            raise H5ADInspectionError("h5ad input exceeds the source-size limit")

        with h5py.File(path, "r") as handle:
            n_obs, obs_fields = self._axis_layout(handle, "obs")
            n_vars, var_fields = self._axis_layout(handle, "var")
            if max(n_obs, n_vars) > self.policy.max_axis_rows:
                raise H5ADInspectionError("h5ad input exceeds the axis-row limit")
            if max(obs_fields, var_fields) > self.policy.max_source_axis_fields:
                raise H5ADInspectionError(
                    "h5ad input exceeds the source-axis-field limit"
                )
            metadata_cells = n_obs * obs_fields + n_vars * var_fields
            if metadata_cells > self.policy.max_axis_metadata_cells:
                raise H5ADInspectionError(
                    "h5ad input exceeds the axis-metadata limit"
                )
            if self._eager_array_bytes(handle) > self.policy.max_eager_array_bytes:
                raise H5ADInspectionError(
                    "h5ad input exceeds the eager-array memory limit"
                )
        return n_obs, n_vars

    @classmethod
    def _axis_layout(cls, handle: h5py.File, axis: str) -> tuple[int, int]:
        node = handle.get(axis)
        if isinstance(node, h5py.Dataset):
            if not node.shape:
                raise H5ADInspectionError("h5ad axis metadata is malformed")
            return int(node.shape[0]), len(node.dtype.names or ())
        if not isinstance(node, h5py.Group):
            raise H5ADInspectionError("h5ad axis metadata is missing")

        index_key = cls._hdf5_text(node.attrs.get("_index"))
        column_order = node.attrs.get("column-order")
        if not index_key or column_order is None:
            raise H5ADInspectionError("h5ad axis metadata is malformed")
        index_node = node.get(index_key)
        if isinstance(index_node, h5py.Dataset) and index_node.shape:
            row_count = int(index_node.shape[0])
        elif isinstance(index_node, h5py.Group):
            codes = index_node.get("codes")
            if not isinstance(codes, h5py.Dataset) or not codes.shape:
                raise H5ADInspectionError("h5ad axis index is malformed")
            row_count = int(codes.shape[0])
        else:
            raise H5ADInspectionError("h5ad axis index is malformed")
        return row_count, len(column_order)

    @staticmethod
    def _hdf5_text(value) -> str | None:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="strict")
        return value if isinstance(value, str) else None

    @staticmethod
    def _eager_array_bytes(handle: h5py.File) -> int:
        total = 0

        def add_dataset(name: str, node) -> None:
            nonlocal total
            parts = name.split("/")
            if parts[0] == "X" or parts[:2] == ["raw", "X"]:
                return
            if isinstance(node, h5py.Dataset):
                logical_bytes = int(node.size) * max(1, int(node.dtype.itemsize))
                total += max(logical_bytes, int(node.id.get_storage_size()))

        handle.visititems(add_dataset)
        return total

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
        bounded = self._unique_bounded_names(selected)
        return H5ADAxisStructure(
            field_count=len(columns),
            fields=tuple(
                H5ADFieldStructure(
                    name=name,
                    dtype=self._dtype(frame[column].dtype),
                )
                for column, name in zip(selected, bounded, strict=True)
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
        bounded = self._unique_bounded_names(selected)
        values = tuple(
            H5ADNamedArrayStructure(
                key=name,
                shape=tuple(int(item) for item in mapping[key].shape),
            )
            for key, name in zip(selected, bounded, strict=True)
        )
        return values, max(0, len(keys) - len(selected))

    def _bounded_names(self, values) -> tuple[tuple[str, ...], int]:
        names = list(values)
        selected = names[: self.policy.max_named_arrays]
        return (
            self._unique_bounded_names(selected),
            max(0, len(names) - len(selected)),
        )

    def _aggregate(self, data) -> H5ADAggregateSummary:
        categorical = []
        numeric = []
        columns = list(data.obs.columns)
        supported = len(columns)
        selected = columns[: self.policy.max_aggregate_fields]
        self._unique_bounded_names(selected)
        for column in selected:
            series = data.obs[column]
            if is_numeric_dtype(series.dtype) and not is_bool_dtype(series.dtype):
                numeric.append(self._numeric(column, series))
            else:
                categorical.append(self._categorical(column, series, int(data.n_obs)))
        returned = len(selected)
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
        if str(column) not in self.policy.enumerated_categorical_fields:
            return H5ADCategoricalSummary(
                field_name=self._name(column),
                dtype=self._dtype(series.dtype),
                count=count,
                missing_count=missing,
                unique_count=unique,
                enumeration=H5ADCategoryEnumeration.POLICY_SUPPRESSED,
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

    def _unique_bounded_names(self, values) -> tuple[str, ...]:
        bounded = tuple(self._name(value) for value in values)
        if len(bounded) != len(set(bounded)):
            raise H5ADInspectionError(
                "h5ad field or key names collide after safe bounding"
            )
        return bounded

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
    "BioFormatArtifactSpec",
    "BioFormatInspectionBundle",
    "BioFormatInspectionError",
    "BioFormatInspectionRegistry",
    "BioFormatInspector",
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
