"""Pinning a downloaded wheel set by sha256, shared by the Docker and Apptainer builders.

Both backends lock the same way, so the same downloaded wheels give one
environment one derived source identity (DECIDED-C15-D).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from packaging.utils import InvalidWheelFilename, parse_wheel_filename

MAX_LOCKED_WHEELS = 2048
_MACHINE = re.compile(r"[a-z0-9_]{1,32}")
_LINUX_PLATFORMS = ("manylinux", "musllinux", "linux_")
_CHUNK_BYTES = 1_048_576


class WheelLockError(ValueError):
    """The downloaded wheel set cannot be pinned safely."""


@dataclass(frozen=True)
class LockedWheel:
    name: str
    version: str
    filename: str
    sha256: str


def lock_wheelhouse(directory: Path, *, machine: str) -> tuple[LockedWheel, ...]:
    """Pin every wheel of one download by sha256; anything else in the directory is refused."""

    if _MACHINE.fullmatch(machine) is None:
        raise WheelLockError("Unsupported machine name")
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise WheelLockError("The wheel directory is unavailable")
    entries = sorted(directory.iterdir())
    if len(entries) > MAX_LOCKED_WHEELS:
        raise WheelLockError("Too many wheels")
    wheels, names = [], set()
    for path in entries:
        if path.is_symlink() or not path.is_file() or path.suffix != ".whl":
            raise WheelLockError("Only regular wheel files can be locked")
        try:
            name, version, _, tags = parse_wheel_filename(path.name)
        except InvalidWheelFilename:
            raise WheelLockError("Invalid wheel file name") from None
        if name in names:
            raise WheelLockError("More than one wheel for one distribution")
        if not all(tag.platform == "any" or (tag.platform.startswith(_LINUX_PLATFORMS)
                                             and tag.platform.endswith("_" + machine))
                   for tag in tags):
            raise WheelLockError("A wheel does not match the target platform")
        names.add(name)
        wheels.append(LockedWheel(name=str(name), version=str(version), filename=path.name,
                                  sha256=_sha256(path)))
    return tuple(wheels)


def render_lock(wheels: tuple[LockedWheel, ...]) -> str:
    """pip requirements that install exactly these files: pinned, hashed, never re-resolved."""

    return "".join(f"{wheel.name}=={wheel.version} --hash=sha256:{wheel.sha256}\n"
                   for wheel in sorted(wheels, key=lambda item: item.name))

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()
