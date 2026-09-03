"""Shared structural bounds for content intentionally released to a model."""

from __future__ import annotations

import json
import re
from typing import Any


class UnsafeModelContentError(ValueError):
    """A value cannot cross the bounded model-visible projection boundary."""


FORBIDDEN_MODEL_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "authorization_secret",
        "bam_contents",
        "biological_matrix",
        "chain_of_thought",
        "count_matrix",
        "credentials",
        "dataframe_rows",
        "docker_socket",
        "expression_matrix",
        "fastq_contents",
        "file_contents",
        "h5ad_contents",
        "host_path",
        "private_key",
        "provider_raw_body",
        "provider_request_body",
        "provider_response_body",
        "raw_data",
        "raw_matrix",
        "raw_provider_body",
        "reasoning_content",
        "script_body",
        "script_content",
        "stderr_body",
        "stdout_body",
        "storage_locator",
    }
)


def normalized_model_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def validate_model_visible_json(
    value: Any,
    *,
    max_depth: int = 8,
    max_mapping_items: int = 256,
    max_list_items: int = 256,
    max_string_length: int = 8_000,
    max_nodes: int = 4_096,
    max_serialized_bytes: int = 64_000,
    reject_absolute_paths: bool = False,
) -> None:
    """Reject explicit unsafe fields and recursively unbounded JSON content."""

    nodes = _validate_node(
        value,
        depth=0,
        max_depth=max_depth,
        max_mapping_items=max_mapping_items,
        max_list_items=max_list_items,
        max_string_length=max_string_length,
        max_nodes=max_nodes,
        reject_absolute_paths=reject_absolute_paths,
    )
    if nodes > max_nodes:
        raise UnsafeModelContentError(
            f"Model-visible content exceeds {max_nodes} JSON nodes"
        )
    encoded = json.dumps(
        value,
        separators=(",", ":"),
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > max_serialized_bytes:
        raise UnsafeModelContentError(
            f"Model-visible content exceeds {max_serialized_bytes} serialized bytes"
        )


def _validate_node(
    value: Any,
    *,
    depth: int,
    max_depth: int,
    max_mapping_items: int,
    max_list_items: int,
    max_string_length: int,
    max_nodes: int,
    reject_absolute_paths: bool,
) -> int:
    if depth > max_depth:
        raise UnsafeModelContentError(
            f"Model-visible content nesting exceeds {max_depth} levels"
        )
    nodes = 1
    if isinstance(value, dict):
        if len(value) > max_mapping_items:
            raise UnsafeModelContentError(
                f"Model-visible object exceeds {max_mapping_items} fields"
            )
        for key, item in value.items():
            if not isinstance(key, str):
                raise UnsafeModelContentError("Model-visible object keys must be strings")
            if normalized_model_key(key) in FORBIDDEN_MODEL_KEYS:
                raise UnsafeModelContentError(
                    f"Model-visible field {key!r} is prohibited"
                )
            nodes += _validate_node(
                item,
                depth=depth + 1,
                max_depth=max_depth,
                max_mapping_items=max_mapping_items,
                max_list_items=max_list_items,
                max_string_length=max_string_length,
                max_nodes=max_nodes,
                reject_absolute_paths=reject_absolute_paths,
            )
            if nodes > max_nodes:
                raise UnsafeModelContentError(
                    f"Model-visible content exceeds {max_nodes} JSON nodes"
                )
    elif isinstance(value, (list, tuple)):
        if len(value) > max_list_items:
            raise UnsafeModelContentError(
                f"Model-visible list exceeds {max_list_items} items"
            )
        for item in value:
            nodes += _validate_node(
                item,
                depth=depth + 1,
                max_depth=max_depth,
                max_mapping_items=max_mapping_items,
                max_list_items=max_list_items,
                max_string_length=max_string_length,
                max_nodes=max_nodes,
                reject_absolute_paths=reject_absolute_paths,
            )
            if nodes > max_nodes:
                raise UnsafeModelContentError(
                    f"Model-visible content exceeds {max_nodes} JSON nodes"
                )
    elif isinstance(value, str):
        if len(value) > max_string_length:
            raise UnsafeModelContentError(
                f"Model-visible string exceeds {max_string_length} characters"
            )
        if reject_absolute_paths and (
            value.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", value)
        ):
            raise UnsafeModelContentError(
                "Absolute host paths are prohibited in model-visible content"
            )
        if "-----BEGIN " in value and "PRIVATE KEY-----" in value:
            raise UnsafeModelContentError(
                "Private key material is prohibited in model-visible content"
            )
    elif value is not None and not isinstance(value, (bool, int, float)):
        raise UnsafeModelContentError("Model-visible content must be JSON-compatible")
    return nodes


__all__ = [
    "FORBIDDEN_MODEL_KEYS",
    "UnsafeModelContentError",
    "normalized_model_key",
    "validate_model_visible_json",
]
