"""Untrusted image inventory and metadata, validated before any of it can reach a model.

An admission probe reads package facts from inside an image that LabBio did not
build. Names and versions are therefore treated as untrusted text: each entry is
checked on its own, and an entry that fails is counted as invalid instead of
being passed on (DECIDED-C15-G).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from labbioagentos.model_safety import UnsafeModelContentError, validate_model_visible_json

# The same identifier shapes ApprovedImage accepts for a verified inventory.
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9.!+_-]{0,127}")
_HIDDEN_REFERENCE = re.compile(r"(?:^|[\s(\"'=,;])(?:/|~/|[A-Za-z]:[\\/])|://|sha256:[0-9a-fA-F]")
MAX_INVENTORY_ENTRIES = 4096
MAX_PURPOSE_CHARACTERS = 500


class InventoryOrigin(StrEnum):
    """Where an inventory layer came from; UNKNOWN is never shown as an empty list."""

    CONDA_META = "CONDA_META"
    PYTHON = "PYTHON"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ValidatedInventory:
    origin: InventoryOrigin
    packages: dict[str, str] = field(default_factory=dict)
    invalid_count: int = 0
    truncated: bool = False


def validate_inventory(raw: object, origin: InventoryOrigin) -> ValidatedInventory:
    """Keep only plain name/version pairs; never repair or guess an entry."""

    if origin is InventoryOrigin.UNKNOWN:
        return ValidatedInventory(origin)
    if not isinstance(raw, Mapping):
        raise ValueError("A probed inventory must be a mapping of names to versions")
    packages: dict[str, str] = {}
    invalid = 0
    items = list(raw.items())
    for name, version in items[:MAX_INVENTORY_ENTRIES]:
        if not _plain_entry(name, version) or name in packages:
            invalid += 1
            continue
        packages[name] = version
    return ValidatedInventory(origin, packages, invalid, len(items) > MAX_INVENTORY_ENTRIES)


def validate_purpose(text: object) -> str:
    """A member's purpose text: one bounded paragraph with no paths or control characters."""

    if not isinstance(text, str):
        raise ValueError("The image purpose must be text")
    value = text.strip()
    if not value or len(value) > MAX_PURPOSE_CHARACTERS:
        raise ValueError(f"The image purpose must have 1 to {MAX_PURPOSE_CHARACTERS} characters")
    if re.search(r"[\x00-\x1f\x7f]", value):
        raise ValueError("The image purpose must be one line without control characters")
    # Host paths, URLs and digest references stay hidden from models (DECIDED-C15-B, H).
    if _HIDDEN_REFERENCE.search(value):
        raise ValueError("The image purpose must not contain paths, URLs or image references")
    try:
        validate_model_visible_json({"purpose": value}, max_string_length=MAX_PURPOSE_CHARACTERS,
                                    reject_absolute_paths=True)
    except UnsafeModelContentError:
        raise ValueError("The image purpose cannot be shown to a model") from None
    return value


def _plain_entry(name: object, version: object) -> bool:
    if not isinstance(name, str) or not isinstance(version, str):
        return False
    if _NAME.fullmatch(name) is None or _VERSION.fullmatch(version) is None:
        return False
    try:
        validate_model_visible_json({name: version}, max_string_length=128)
    except UnsafeModelContentError:
        return False
    return True
