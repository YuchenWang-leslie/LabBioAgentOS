"""Fixed leading-record inspection; no offsets, arbitrary reads or axis inference."""

from __future__ import annotations

import codecs
import csv
import gzip
import io
import json
import re
from itertools import islice
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from labbioagentos.model_safety import (
    FORBIDDEN_MODEL_KEYS, UnsafeModelContentError, normalized_model_key,
    validate_model_visible_json,
)

MAX_SCAN_BYTES = 1024 * 1024
MAX_RECORDS = 6  # Includes the first record; it is not assumed to be a header.
MAX_COLUMNS = 8
MAX_CELL_CHARS = 64

Cell = Annotated[str, Field(strict=True, max_length=MAX_CELL_CHARS)]
Record = Annotated[tuple[Cell, ...], Field(max_length=MAX_COLUMNS)]


class RawHeadPreview(BaseModel):
    """Technical file evidence only; first record and axes remain uninterpreted."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    status: Literal["AVAILABLE", "UNSUPPORTED_FORMAT", "INVALID_TEXT"]
    format: Annotated[str, Field(max_length=32)] | None = None
    format_hint: Annotated[str, Field(max_length=32)] | None = None
    format_basis: Literal["CONTENT", "SUFFIX_AND_PARSER", "UNKNOWN"] = "UNKNOWN"
    size_bytes: int | None = Field(default=None, ge=0)
    compression: Literal["none", "gzip", "bzip2", "xz"] = "none"
    encoding: Literal["utf-8"] | None = None
    structure: dict[str, JsonValue] = Field(default_factory=dict)
    leading_records: tuple[Record, ...] = Field(default=(), max_length=MAX_RECORDS)
    record_field_counts: tuple[int, ...] = Field(default=(), max_length=MAX_RECORDS)
    total_records: int | None = Field(default=None, ge=0)
    more_records: bool | None = None
    columns_truncated: bool = False
    values_truncated: bool = False
    values_redacted: bool = False
    scan_limit_reached: bool = False

    @model_validator(mode="after")
    def bounded_structure(self):
        validate_model_visible_json(self.model_dump(mode="json"),
            max_serialized_bytes=24_000, reject_absolute_paths=True)
        return self


def delimited_format(filename: str | None) -> tuple[str | None, str]:
    name = (filename or "").lower()
    compression = "gzip" if name.endswith(".gz") else "none"
    if compression == "gzip":
        name = name[:-3]
    for format_key in ("csv", "tsv"):
        if name.endswith("." + format_key):
            return format_key, compression
    return None, compression


def inspect_head(stream, *, format_key: str, compression: str) -> RawHeadPreview:
    """Read at most 1 MiB + a sentinel of decompressed bytes, return a fixed head."""
    options = {"format": format_key, "compression": compression,
               "encoding": "utf-8", "format_basis": "SUFFIX_AND_PARSER"}
    try:
        if compression == "gzip":
            with gzip.GzipFile(fileobj=stream) as decoded:
                prefix = decoded.read(MAX_SCAN_BYTES + 1)
        else:
            prefix = stream.read(MAX_SCAN_BYTES + 1)
        capped = len(prefix) > MAX_SCAN_BYTES
        text = codecs.getincrementaldecoder("utf-8-sig")().decode(
            prefix[:MAX_SCAN_BYTES], final=not capped,
        )
        if "\x00" in text:
            raise UnicodeError()
    except (OSError, EOFError, UnicodeError):
        return RawHeadPreview(status="INVALID_TEXT", **{**options, "format_basis": "UNKNOWN"})

    text_stream = io.StringIO(text, newline="")
    reader = csv.reader(text_stream,
                        delimiter="," if format_key == "csv" else "\t", strict=True)
    records = []
    more = None
    try:
        for row in reader:
            # A byte-capped final record is not a complete observation, even
            # when csv.reader can parse it. Never expose a repaired fragment.
            if capped and text_stream.tell() == len(text) and not text.endswith(("\n", "\r")):
                break
            if len(records) == MAX_RECORDS:
                more = True
                break
            records.append(row)
        else:
            more = None if capped else False
    except csv.Error:
        if not capped:
            return RawHeadPreview(status="INVALID_TEXT", **{**options, "format_basis": "UNKNOWN"})

    sensitive = FORBIDDEN_MODEL_KEYS | {"password", "token", "secret", "access_token"}
    secret_columns = {
        index for index, name in enumerate(records[0][:MAX_COLUMNS] if records else ())
        if normalized_model_key(name) in sensitive
    }
    redacted = False
    truncated = False
    displayed = []
    for row in records:
        cells = []
        for index, value in enumerate(row[:MAX_COLUMNS]):
            unsafe = index in secret_columns or bool(re.search(
                r"(?i)(?:\bbearer\s+\S+|\b(?:api[_-]?key|password|token|secret)\s*[:=]\s*\S+"
                r"|\b(?:ghp_|github_pat_|sk-)[A-Za-z0-9_-]{12,})", value,
            ))
            try:
                validate_model_visible_json(value, max_string_length=MAX_SCAN_BYTES,
                    max_serialized_bytes=MAX_SCAN_BYTES * 6, reject_absolute_paths=True)
            except UnsafeModelContentError:
                unsafe = True
            if unsafe:
                value = "[REDACTED]"
                redacted = True
            truncated |= len(value) > MAX_CELL_CHARS
            cells.append(value[:MAX_CELL_CHARS])
        displayed.append(tuple(cells))
    return RawHeadPreview(
        status="AVAILABLE", **options, leading_records=tuple(displayed),
        record_field_counts=tuple(len(row) for row in records),
        total_records=len(records) if more is False else None, more_records=more,
        columns_truncated=any(len(row) > MAX_COLUMNS for row in records),
        values_truncated=truncated, values_redacted=redacted, scan_limit_reached=capped,
    )


def inspect_file(stream, *, filename: str, size_bytes: int) -> RawHeadPreview:
    """Format facts for any ingested file; binary payloads are never previewed."""
    format_key, hinted_compression = delimited_format(filename)
    suffix = filename.lower().removesuffix(".gz").rsplit(".", 1)[-1]
    hint = suffix if re.fullmatch(r"[a-z0-9]{1,16}", suffix) else None
    signature = stream.read(8)
    stream.seek(0)
    compression = "gzip" if signature.startswith(b"\x1f\x8b") else "none"
    facts = {"size_bytes": size_bytes, "format_hint": hint}
    if format_key is not None:
        preview = inspect_head(stream, format_key=format_key,
            compression=compression if compression == "gzip" else hinted_compression)
        return RawHeadPreview.model_validate({**preview.model_dump(), **facts})
    facts.update(compression=compression)
    try:
        if compression == "gzip":
            with gzip.GzipFile(fileobj=stream) as decoded:
                prefix = decoded.read(MAX_SCAN_BYTES + 1)
        else:
            prefix = stream.read(MAX_SCAN_BYTES + 1)
    except (OSError, EOFError):
        return RawHeadPreview(status="INVALID_TEXT", **facts)
    capped = len(prefix) > MAX_SCAN_BYTES
    facts.update(scan_limit_reached=capped)
    signatures = (
        (b"\x89HDF\r\n\x1a\n", "hdf5"), (b"\x93NUMPY", "npy"),
        (b"BAM\x01", "bam"), (b"CRAM", "cram"),
        (b"PK\x03\x04", "zip"), (b"PK\x05\x06", "zip"),
        (b"PAR1", "parquet"), (b"ARROW1", "arrow"),
        (b"%PDF-", "pdf"), (b"\x89PNG\r\n\x1a\n", "png"),
        (b"\xff\xd8\xff", "jpeg"), (b"%%MatrixMarket ", "matrix_market"),
        (b"BZh", "bzip2"), (b"\xfd7zXZ\x00", "xz"),
    )
    detected = next((name for magic, name in signatures if prefix.startswith(magic)), None)
    if detected is None and prefix[257:262] == b"ustar":
        detected = "tar"
    structure = {}
    if detected == "hdf5" and compression == "none":
        stream.seek(0)
        structure = _hdf5_structure(stream)
    elif detected == "npy":
        # Header-only numpy parsing; never np.load (including object/pickle arrays).
        from numpy.lib import format as numpy_format
        header = io.BytesIO(prefix)
        try:
            version = numpy_format.read_magic(header)
            read = {(1, 0): numpy_format.read_array_header_1_0,
                    (2, 0): numpy_format.read_array_header_2_0}.get(version)
            if read is not None:
                shape, order, dtype = read(header, max_header_size=10_000)
                structure = {"shape": list(shape[:16]), "dtype": str(dtype)[:64],
                             "fortran_order": order}
        except (ValueError, EOFError):
            structure = {"inspection_status": "INVALID_HEADER"}
    elif detected == "matrix_market":
        lines = prefix[:MAX_SCAN_BYTES].decode("ascii", errors="replace").splitlines()
        dimensions = next((line.split() for line in lines[1:]
                           if line.strip() and not line.startswith("%")), [])
        if len(dimensions) in (2, 3) and all(re.fullmatch(r"[0-9]{1,18}", item) for item in dimensions):
            structure = {"shape": [int(item) for item in dimensions[:2]]}
            if len(dimensions) == 3:
                structure["stored_entries"] = int(dimensions[2])
    if detected is not None:
        if detected in {"bzip2", "xz"}:
            facts["compression"] = detected
        return RawHeadPreview(status="AVAILABLE", format=detected,
            format_basis="CONTENT", structure=structure, **facts)
    try:
        text = codecs.getincrementaldecoder("utf-8-sig")().decode(
            prefix[:MAX_SCAN_BYTES], final=not capped)
        if any(ord(char) < 32 and char not in "\n\r\t" for char in text):
            raise UnicodeError()
    except UnicodeError:
        return RawHeadPreview(status="UNSUPPORTED_FORMAT", format="binary", **facts)
    if not capped and text.lstrip().startswith(("{", "[")):
        try:
            value = json.loads(text)
        except (ValueError, RecursionError):
            pass
        else:
            structure = {"root_type": type(value).__name__, "item_count": len(value)}
            if isinstance(value, dict):
                # Names/types only, not the full JSON or arbitrary nested values.
                structure["fields"] = [
                    {"name": key[:64], "name_truncated": len(key) > 64, "type": type(item).__name__}
                    for key, item in islice(value.items(), 12)
                ]
                structure["fields_truncated"] = len(value) > 12
            try:
                return RawHeadPreview(status="AVAILABLE", format="json", encoding="utf-8",
                    format_basis="CONTENT", structure=structure, **facts)
            except ValueError:
                return RawHeadPreview(status="AVAILABLE", format="json", encoding="utf-8",
                    format_basis="CONTENT", structure={"inspection_status": "UNSAFE_NAMES"}, **facts)
    # An extension is a hint, not proof. Unknown text/binary still has useful
    # size/compression/encoding information without an arbitrary file dump.
    return RawHeadPreview(status="UNSUPPORTED_FORMAT", format="text", encoding="utf-8",
        format_basis="CONTENT", **facts)


def _hdf5_structure(stream) -> dict:
    """Bounded root/child names, shapes and dtypes; no arrays or external links."""
    import h5py

    class MetadataReader:
        # HDF5 metadata need not be at the file beginning. Permit bounded seeks
        # and at most another 1 MiB of reads, not unrestricted HDF5 I/O.
        remaining = MAX_SCAN_BYTES

        def read(self, size):
            if not 0 <= size <= self.remaining:
                raise OSError("HDF5 metadata read budget exceeded")
            data = stream.read(size)
            self.remaining -= len(data)
            return data

        def readinto(self, target):
            data = self.read(len(target))
            target[:len(data)] = data
            return len(data)

        def seek(self, offset, whence=0):
            return stream.seek(offset, whence)

        def tell(self):
            return stream.tell()

    def entries(group):
        result = []
        for name in islice(group, 12):
            if not isinstance(group.get(name, getlink=True), h5py.HardLink):
                result.append({"name": name[:64], "name_truncated": len(name) > 64,
                               "kind": "LINK_NOT_FOLLOWED"})
                continue
            obj = group[name]
            item = {"name": name[:64], "name_truncated": len(name) > 64, "kind": "group"}
            if isinstance(obj, h5py.Dataset):
                item.update(kind="dataset", shape=list((obj.shape or ())[:16]), dtype=str(obj.dtype)[:64])
                item.update(shape_truncated=len(obj.shape or ()) > 16, dtype_truncated=len(str(obj.dtype)) > 64)
            else:
                item["fields"] = list(islice(obj, 12))
                item["field_names_truncated"] = any(len(key) > 64 for key in item["fields"])
                item["fields"] = [key[:64] for key in item["fields"]]
                item["fields_truncated"] = len(obj) > 12
                if "shape" in obj.attrs:
                    attribute = obj.attrs.get_id("shape")
                    if (attribute.dtype.kind in "iu" and len(attribute.shape) == 1
                            and attribute.shape[0] <= 16 and attribute.get_storage_size() <= 128):
                        item["shape"] = [int(value) for value in obj.attrs["shape"]]
            result.append(item)
        return result

    try:
        with h5py.File(MetadataReader(), "r") as handle:
            structure = {"entries": entries(handle), "entries_truncated": len(handle) > 12}
        validate_model_visible_json(structure, reject_absolute_paths=True)
        return structure
    except (OSError, ValueError, RuntimeError):
        return {"inspection_status": "STRUCTURE_UNAVAILABLE"}
