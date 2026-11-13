"""Pinning a downloaded wheel set by sha256, shared by the Docker and Apptainer builders.

Both backends lock the same way, so the same downloaded wheels give one
environment one derived source identity (DECIDED-C15-D).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from packaging.utils import InvalidWheelFilename, canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion, Version

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


@dataclass(frozen=True)
class PinnedWheel:
    """One package of a member's wheel lock: an exact version and the hashes it may have."""

    name: str
    version: str
    hashes: frozenset[str]


_PIN = re.compile(r"(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?)==(?P<version>[A-Za-z0-9][A-Za-z0-9.!+_-]{0,127})")
_HASH = re.compile(r"--hash=sha256:(?P<digest>[0-9a-f]{64})")
MAX_LOCK_BYTES = 4_194_304


def parse_wheel_lock(text: str) -> tuple[PinnedWheel, ...]:
    """`name==version --hash=sha256:...` lines, as pip-compile and uv write them.

    Comments and line continuations are allowed. Anything else is refused:
    extras, markers, URLs, paths, options such as --index-url, and packages
    without a hash.
    """

    if len(text) > MAX_LOCK_BYTES or "\x00" in text:
        raise WheelLockError("The wheel lock is too large or not text")
    pins: dict[str, PinnedWheel] = {}
    for line in text.replace("\\\n", " ").splitlines():
        line = line.split(" #", 1)[0].strip() if not line.lstrip().startswith("#") else ""
        if not line:
            continue
        tokens = line.split()
        match = _PIN.fullmatch(tokens[0])
        hashes = [_HASH.fullmatch(token) for token in tokens[1:]]
        if match is None or not hashes or None in hashes:
            raise WheelLockError("Every lock line must be name==version with sha256 hashes only")
        try:
            name, version = canonicalize_name(match["name"]), str(Version(match["version"]))
        except InvalidVersion:
            raise WheelLockError("A locked version is invalid") from None
        if name in pins:
            raise WheelLockError("A package is locked more than once")
        pins[name] = PinnedWheel(name=name, version=version,
                                 hashes=frozenset(item["digest"] for item in hashes))
    if not pins or len(pins) > MAX_LOCKED_WHEELS:
        raise WheelLockError("A wheel lock names 1 to 2048 packages")
    return tuple(sorted(pins.values(), key=lambda item: item.name))


def render_pins(pins: tuple[PinnedWheel, ...]) -> str:
    """The member's lock re-rendered from parsed values only, for pip --require-hashes."""

    return "".join(f"{pin.name}=={pin.version}" + "".join(f" --hash=sha256:{digest}" for digest in sorted(pin.hashes))
                   + "\n" for pin in pins)


def check_pinned(wheels: tuple[LockedWheel, ...], pins: tuple[PinnedWheel, ...]) -> None:
    """The download is exactly the lock: every package once, at its version, with an allowed hash."""

    expected = {pin.name: pin for pin in pins}
    found = {canonicalize_name(wheel.name): wheel for wheel in wheels}
    if set(found) != set(expected):
        raise WheelLockError("The downloaded wheels differ from the lock")
    for name, wheel in found.items():
        pin = expected[name]
        if Version(wheel.version) != Version(pin.version) or wheel.sha256 not in pin.hashes:
            raise WheelLockError("A downloaded wheel is not the locked file")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()
