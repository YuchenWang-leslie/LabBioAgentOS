"""Cluster environment builds: the wheel lock and the fixed build-job program.

DECIDED-C13-8: wheels are downloaded with network and pinned by sha256. A
network-disabled scheduler job installs exactly the locked wheels, checks
them, packs a SIF and runs the import probe. Nothing here comes from model
text except validated import module names; requirements reach the job only
as the lock file.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

from packaging.utils import InvalidWheelFilename, parse_wheel_filename

from .apptainer import ISOLATION_ARGUMENTS
from .environment_builder import _PROBE  # The same import probe as the Docker builder.
from .environments import normalize_modules

MAX_LOCKED_WHEELS = 2048
PROBE_OUTPUT = "probe.json"
_MACHINE = re.compile(r"[a-z0-9_]{1,32}")
_LINUX_PLATFORMS = ("manylinux", "musllinux", "linux_")
_UNSAFE_PATH = re.compile(r"[\s\x00-\x1f\x7f:,]")  # ':' and ',' would split a bind spec.
_CHUNK_BYTES = 1_048_576
# Created in the sandbox only while installing, and removed before packing.
_BUILD_MOUNT = "/.labbio-build"
_WRITABLE_CHECK = (
    "import sysconfig, tempfile; "
    "tempfile.TemporaryFile(dir=sysconfig.get_paths()['purelib']).close()"
)


class WheelLockError(ValueError):
    """The downloaded wheel set cannot be pinned safely."""


@dataclass(frozen=True)
class LockedWheel:
    name: str
    version: str
    filename: str
    sha256: str


class BuildExit(IntEnum):
    """Build-program exit statuses that the control plane maps to failure codes."""

    UNAVAILABLE = 81  # The node cannot provide a writable sandbox or pack a SIF.
    INSTALL_FAILED = 82
    CHECK_FAILED = 83
    PACK_FAILED = 84
    PROBE_FAILED = 85


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


def render_build_program(
    *,
    apptainer_binary: Path,
    base_sif: Path,
    wheel_dir: Path,
    lock_file: Path,
    output_sif: Path,
    build_dir: Path,
    log_dir: Path,
    import_modules: tuple[str, ...],
) -> str:
    """Return the fixed bash program a build job runs; every value is validated and quoted."""

    for path in (apptainer_binary, base_sif, wheel_dir, lock_file, output_sif, build_dir, log_dir):
        text = str(path)
        if not Path(text).is_absolute() or _UNSAFE_PATH.search(text):
            raise ValueError("Build paths must be plain absolute paths")
    modules = normalize_modules(import_modules, max_count=256)
    quote = shlex.quote

    def log(name: str) -> str:
        return quote(str(log_dir / name))

    isolated = '"$A" --silent exec "${ISOLATION[@]}" --workdir "$work/tmp"'
    partial = quote(f"{output_sif}.partial")
    binds = (f"--bind {quote(f'{wheel_dir}:{_BUILD_MOUNT}/wheels:ro')} "
             f"--bind {quote(f'{lock_file}:{_BUILD_MOUNT}/lock.txt:ro')}")
    install = ("python -I -m pip install --disable-pip-version-check --no-input --no-cache-dir "
               f"--progress-bar off --no-index --no-deps --require-hashes "
               f"--find-links {_BUILD_MOUNT}/wheels -r {_BUILD_MOUNT}/lock.txt")
    unavailable = int(BuildExit.UNAVAILABLE)
    lines = [
        "#!/bin/bash",
        "# Generated by LabBio from a fixed template. Builds one Python environment",
        "# without network; only the locked wheels are installed.",
        "set -u",
        "umask 077",
        f"A={quote(str(apptainer_binary))}",
        "ISOLATION=(" + " ".join(quote(item) for item in ISOLATION_ARGUMENTS) + ")",
        # Each step runs in the background so that a cancel or timeout stops it at once.
        "child=",
        'run() { "$@" & child=$!; wait "$child"; local status=$?; child=; return $status; }',
        'stop() { if [ -n "$child" ]; then kill -TERM "$child" 2>/dev/null; wait "$child" 2>/dev/null; fi; exit 143; }',
        "trap stop TERM INT HUP",
        f"work=$(mktemp -d {quote(str(build_dir))}/labbio-build.XXXXXXXX) || exit {unavailable}",
        'cleanup() { chmod -R u+w -- "$work" 2>/dev/null; rm -rf -- "$work"; }',
        "trap cleanup EXIT",
        f'mkdir -- "$work/tmp" "$work/apptainer-tmp" "$work/pack-check" || exit {unavailable}',
        'export APPTAINER_TMPDIR="$work/apptainer-tmp"',
        'sandbox="$work/sandbox"',
        # Capabilities only the node can confirm, checked before any real work.
        f'run "$A" --silent build --sandbox "$sandbox" {quote(str(base_sif))} > {log("sandbox.log")} 2>&1'
        f" || exit {unavailable}",
        f'run {isolated} --writable "$sandbox" python -I -c {quote(_WRITABLE_CHECK)}'
        f' > {log("writable.log")} 2>&1 || exit {unavailable}',
        f'run "$A" --silent build "$work/pack-check.sif" "$work/pack-check" > {log("pack-check.log")} 2>&1'
        f" || exit {unavailable}",
        f'mount="$sandbox"{_BUILD_MOUNT}',
        f"if [ -s {quote(str(lock_file))} ]; then",
        f'  mkdir -- "$mount" "$mount/wheels" && : > "$mount/lock.txt" || exit {int(BuildExit.INSTALL_FAILED)}',
        f'  run {isolated} --writable {binds} "$sandbox" {install} > {log("install.log")} 2>&1'
        f" || exit {int(BuildExit.INSTALL_FAILED)}",
        f'  rm -rf -- "$mount" || exit {int(BuildExit.INSTALL_FAILED)}',
        "fi",
        f'run {isolated} "$sandbox" python -I -m pip check > {log("check.log")} 2>&1'
        f" || exit {int(BuildExit.CHECK_FAILED)}",
        f'run "$A" --silent build {partial} "$sandbox" > {log("pack.log")} 2>&1'
        f" || exit {int(BuildExit.PACK_FAILED)}",
        f"run {isolated} {partial} python -I -c {quote(_PROBE)} {quote(json.dumps(list(modules)))}"
        f" > {log(PROBE_OUTPUT)} 2> {log('probe.log')} || exit {int(BuildExit.PROBE_FAILED)}",
        f"mv -f -- {partial} {quote(str(output_sif))} || exit {int(BuildExit.PACK_FAILED)}",
    ]
    return "\n".join(lines) + "\n"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()
