"""Backend-neutral source identity of an execution image (DECIDED-C15-D).

A source identity says which environment an image is. A realization (a Docker
image id or a SIF sha256) says which file ran on one backend. One source may have
several realizations, so rebuilding or converting an image does not change what
environment it is.
"""

from __future__ import annotations

import hashlib
import json
import re
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, StrictStr, model_validator

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_PLATFORM = re.compile(r"[a-z0-9]+/[a-z0-9_]+(?:/[a-z0-9]+)?")
_MACHINE = re.compile(r"[a-z0-9_]{1,32}")


class SourceKind(StrEnum):
    OCI_MANIFEST = "OCI_MANIFEST"  # A platform-specific manifest digest, never an index digest.
    OCI_ARCHIVE = "OCI_ARCHIVE"
    SIF = "SIF"  # A SIF with no upstream source: its own sha256.
    DERIVED = "DERIVED"  # Base source, lock list, recipe and machine.
    LEGACY = "LEGACY"  # No backend-neutral identity: the realization stands in for it.


class SourceIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: SourceKind
    digest: StrictStr
    platform: StrictStr | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "SourceIdentity":
        oci = self.kind in {SourceKind.OCI_MANIFEST, SourceKind.OCI_ARCHIVE}
        if oci != (self.platform is not None):
            raise ValueError("Only an OCI source names a platform, and it must")
        if self.platform is not None and _PLATFORM.fullmatch(self.platform) is None:
            raise ValueError("The platform must look like linux/amd64")
        if self.kind is SourceKind.LEGACY:
            if not 1 <= len(self.digest) <= 512 or any(c.isspace() for c in self.digest):
                raise ValueError("A legacy identity is one realization reference")
        elif _DIGEST.fullmatch(self.digest) is None:
            raise ValueError("A source digest must be sha256:<64 hex>")
        return self

    @property
    def canonical(self) -> str:
        """Stable text form. A legacy identity is its realization reference verbatim, so
        environment request hashes computed before C15 stay unchanged."""
        if self.kind is SourceKind.LEGACY:
            return self.digest
        suffix = f"@{self.platform}" if self.platform else ""
        return f"{self.kind.value.lower()}:{self.digest}{suffix}"


def legacy_identity(realization: str) -> SourceIdentity:
    return SourceIdentity(kind=SourceKind.LEGACY, digest=realization)


def derived_identity(base: SourceIdentity, locked: list[dict], recipe: str, machine: str) -> SourceIdentity:
    """Same base, same exact packages, same recipe and machine: the same environment."""
    if not locked:
        raise ValueError("A derived identity needs the exact locked packages")
    if _MACHINE.fullmatch(machine) is None:
        raise ValueError("machine must be a wheel architecture name such as x86_64")
    payload = {"base": base.canonical, "locked": sorted(locked, key=lambda item: item["name"]),
               "recipe": recipe, "machine": machine}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return SourceIdentity(kind=SourceKind.DERIVED, digest="sha256:" + hashlib.sha256(encoded).hexdigest())
