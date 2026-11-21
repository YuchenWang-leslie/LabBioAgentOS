"""Locked conda packages for derived environments (DECIDED-C15-E1).

The platform's own micromamba, checked by sha256, solves on the host against
allowlisted channels only. Every package is downloaded and checked against the
sha256 the solver reported, then installed offline from an explicit lock that
repeats each hash, so the install step re-checks every file. The Anaconda
`defaults` channels are never used.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx

CONDA_RECIPE = "conda-v1"
CONDA_ENV_PREFIX = "/opt/labbio-env"
MAX_CONDA_SPECS = 64
MAX_CONDA_PACKAGES = 2048
_SPEC = re.compile(
    r"[a-z0-9][a-z0-9._-]{0,127}"
    r"(?:(?:==|=|>=|<=|>|<|!=)[0-9A-Za-z.*+_!-]{1,64}(?:,(?:==|>=|<=|>|<|!=)[0-9A-Za-z.*+_!-]{1,64}){0,3})?"
)
_CHANNEL = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
# The Anaconda distribution channels need a paid licence above an organization size.
FORBIDDEN_CHANNELS = frozenset({"defaults", "main", "r", "free", "pro", "msys2", "anaconda"})
_PLATFORMS = {"x86_64": "linux-64", "aarch64": "linux-aarch64", "ppc64le": "linux-ppc64le"}
# conda-forge names may start with "_" (for example _openmp_mutex); no path separators.
_FILENAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._+-]{0,255}\.(?:conda|tar\.bz2)")


class CondaError(ValueError):
    """A fixed failure code for a conda request; no solver or log text is copied."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class LockedPackage:
    name: str
    version: str
    build: str
    url: str
    sha256: str

    @property
    def filename(self) -> str:
        return self.url.rsplit("/", 1)[1]


@dataclass(frozen=True)
class CondaPolicy:
    micromamba: Path
    micromamba_sha256: str
    channels: tuple[str, ...]
    machine: str
    proxy_url: str | None = None
    timeout_seconds: float = 900.0

    @property
    def platform(self) -> str:
        try:
            return _PLATFORMS[self.machine]
        except KeyError:
            raise CondaError("CONDA_PLATFORM_UNSUPPORTED") from None

    @property
    def channel_bases(self) -> tuple[str, ...]:
        return tuple(f"https://conda.anaconda.org/{channel}/" for channel in self.channels)


def validate_channels(channels: tuple[str, ...]) -> tuple[str, ...]:
    if not channels or len(set(channels)) != len(channels):
        raise ValueError("Give each allowed conda channel once")
    for channel in channels:
        if _CHANNEL.fullmatch(channel) is None or channel in FORBIDDEN_CHANNELS:
            raise ValueError("Conda channels are plain names other than the Anaconda defaults")
    return channels


def normalize_conda_specs(values: tuple[str, ...]) -> tuple[str, ...]:
    """Names with optional version constraints only: no channels, URLs, paths or options."""
    if len(values) > MAX_CONDA_SPECS:
        raise CondaError("INVALID_CONDA_PACKAGES")
    specs = []
    for value in values:
        if not isinstance(value, str) or _SPEC.fullmatch(value.strip().lower()) is None:
            raise CondaError("INVALID_CONDA_PACKAGES")
        specs.append(value.strip().lower())
    if len({re.split(r"[=<>!]", spec)[0] for spec in specs}) != len(specs):
        raise CondaError("INVALID_CONDA_PACKAGES")
    return tuple(sorted(specs))


def verify_micromamba(policy: CondaPolicy) -> Path:
    """The platform's micromamba: a regular, non-writable file with the configured sha256."""
    path = policy.micromamba
    try:
        info = path.lstat()
    except OSError:
        raise CondaError("CONDA_UNAVAILABLE") from None
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o222:
        raise CondaError("CONDA_UNAVAILABLE")
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1_048_576), b""):
            digest.update(chunk)
    if digest.hexdigest() != policy.micromamba_sha256:
        raise CondaError("CONDA_UNAVAILABLE")
    return path


def solve(policy: CondaPolicy, specs: tuple[str, ...], work: Path) -> tuple[LockedPackage, ...]:
    """Resolve on the host, with no user configuration, against the allowed channels only."""
    return lock_from_solution(_solve_payload(policy, specs, work), policy)


def _solve_payload(policy: CondaPolicy, specs: tuple[str, ...], work: Path) -> dict:
    micromamba = verify_micromamba(policy)
    environment = {"PATH": "/usr/bin:/bin", "HOME": str(work)}
    if policy.proxy_url:
        environment.update(HTTP_PROXY=policy.proxy_url, HTTPS_PROXY=policy.proxy_url)
    argv = [str(micromamba), "create", "--dry-run", "--json", "--yes", "--no-rc",
            "-r", str(work / "root"), "-p", str(work / "solve"), "--override-channels",
            "--strict-channel-priority", "--platform", policy.platform]
    for channel in policy.channels:
        argv += ["-c", channel]
    try:
        completed = subprocess.run([*argv, *specs], capture_output=True, timeout=policy.timeout_seconds,
                                   env=environment, check=False)
    except subprocess.TimeoutExpired:
        raise CondaError("BUILD_TIMEOUT") from None
    try:
        payload = json.loads(completed.stdout)
    except ValueError:
        payload = None
    if completed.returncode != 0 or not isinstance(payload, dict) or payload.get("success") is not True:
        raise CondaError("CONDA_NO_SOLUTION")
    return payload


@dataclass(frozen=True)
class ExplicitEntry:
    """One line of a member's explicit lock: a package file in an allowed channel and its hash."""

    url: str
    name: str
    version: str
    build: str
    md5: str | None
    sha256: str | None


_EXPLICIT_HASH = re.compile(r"(?:md5:)?(?P<md5>[0-9a-f]{32})|(?:sha256:)?(?P<sha256>[0-9a-f]{64})")
_PACKAGE_NAME = re.compile(r"[a-z0-9_][a-z0-9._-]{0,127}")
_PACKAGE_VERSION = re.compile(r"[A-Za-z0-9_.+!]{1,64}")
_PACKAGE_BUILD = re.compile(r"[A-Za-z0-9_.+]{1,64}")
MAX_EXPLICIT_BYTES = 4_194_304


def parse_explicit(text: str, policy: CondaPolicy) -> tuple[ExplicitEntry, ...]:
    """An @EXPLICIT lock as conda and micromamba export it, with an md5 or sha256 per file.

    Every URL must be a package file of an allowed channel for the compute nodes'
    platform or noarch; there are no options, local paths or other hosts.
    """

    if len(text) > MAX_EXPLICIT_BYTES or "\x00" in text:
        raise CondaError("CONDA_LOCK_FILE_INVALID")
    lines = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
    if not lines or lines[0] != "@EXPLICIT" or not 2 <= len(lines) <= MAX_CONDA_PACKAGES + 1:
        raise CondaError("CONDA_LOCK_FILE_INVALID")
    entries: dict[str, ExplicitEntry] = {}
    for line in lines[1:]:
        url, _, digest = line.partition("#")
        match = _EXPLICIT_HASH.fullmatch(digest)
        if match is None or not _within_channel(url, policy):
            raise CondaError("CONDA_LOCK_FILE_INVALID")
        stem = re.sub(r"\.(?:conda|tar\.bz2)$", "", url.rsplit("/", 1)[1])
        parts = stem.rsplit("-", 2)
        if (len(parts) != 3 or _PACKAGE_NAME.fullmatch(parts[0]) is None
                or _PACKAGE_VERSION.fullmatch(parts[1]) is None or _PACKAGE_BUILD.fullmatch(parts[2]) is None
                or parts[0] in entries):
            raise CondaError("CONDA_LOCK_FILE_INVALID")
        entries[parts[0]] = ExplicitEntry(url=url, name=parts[0], version=parts[1], build=parts[2],
                                          md5=match["md5"], sha256=match["sha256"])
    return tuple(sorted(entries.values(), key=lambda item: item.name))


def lock_explicit(policy: CondaPolicy, entries: tuple[ExplicitEntry, ...], work: Path) -> tuple[LockedPackage, ...]:
    """Solve exactly the listed builds; the solution must be the listed files with their hashes.

    The channel metadata supplies each file's sha256, so an md5-only export is
    locked by sha256 too. A lock that is incomplete, inconsistent or changed
    upstream fails here, before anything is downloaded.
    """

    payload = _solve_payload(policy, tuple(f"{item.name}=={item.version}={item.build}" for item in entries), work)
    locked = {package.url: package for package in lock_from_solution(payload, policy)}
    md5s = {item.get("url"): item.get("md5") for item in payload["actions"]["LINK"]}
    if set(locked) != {item.url for item in entries}:
        raise CondaError("CONDA_LOCK_MISMATCH")
    for item in entries:
        if ((item.sha256 is not None and item.sha256 != locked[item.url].sha256)
                or (item.md5 is not None and item.md5 != md5s.get(item.url))):
            raise CondaError("CONDA_LOCK_MISMATCH")
    return tuple(sorted(locked.values(), key=lambda package: package.name))


def lock_from_solution(payload: dict, policy: CondaPolicy) -> tuple[LockedPackage, ...]:
    """Every package must come from an allowed channel and carry a sha256; nothing is guessed."""
    try:
        entries = payload["actions"]["LINK"]
        if not isinstance(entries, list) or len(entries) > MAX_CONDA_PACKAGES:
            raise ValueError()
        locked = []
        for item in entries:
            url, sha = item["url"], item["sha256"]
            if (not _within_channel(url, policy)
                    or not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{64}", sha) is None):
                raise ValueError()
            locked.append(LockedPackage(name=str(item["name"]), version=str(item["version"]),
                                        build=str(item["build_string"]), url=url, sha256=sha))
    except (KeyError, TypeError, ValueError):
        raise CondaError("CONDA_LOCK_INVALID") from None
    if len({item.name for item in locked}) != len(locked):
        raise CondaError("CONDA_LOCK_INVALID")
    return tuple(sorted(locked, key=lambda item: item.name))


def _within_channel(url: object, policy: CondaPolicy) -> bool:
    """An allowed channel, then exactly <platform or noarch>/<package file>: no "..", no deeper path."""
    if not isinstance(url, str):
        return False
    base = next((base for base in policy.channel_bases if url.startswith(base)), None)
    if base is None:
        return False
    parts = url[len(base):].split("/")
    return (len(parts) == 2 and parts[0] in {policy.platform, "noarch"}
            and _FILENAME.fullmatch(parts[1]) is not None)


def download(locked: tuple[LockedPackage, ...], destination: Path, *, proxy_url: str | None,
             transport: httpx.BaseTransport | None = None, timeout: float = 300.0) -> None:
    """Fetch each locked file and keep it only if its sha256 matches the lock."""
    destination.mkdir(mode=0o700)
    client = httpx.Client(transport=transport, timeout=timeout, follow_redirects=True,
                          proxy=proxy_url if transport is None else None, trust_env=False)
    try:
        for package in locked:
            path = destination / package.filename
            digest = hashlib.sha256()
            try:
                with client.stream("GET", package.url) as response, open(path, "xb") as output:
                    if response.status_code != 200 or response.url.scheme != "https":
                        raise CondaError("NETWORK_FAILURE")
                    for chunk in response.iter_bytes():
                        digest.update(chunk)
                        output.write(chunk)
            except httpx.HTTPError:
                raise CondaError("NETWORK_FAILURE") from None
            if digest.hexdigest() != package.sha256:
                raise CondaError("VERIFICATION_FAILED")
    finally:
        client.close()


def render_explicit(locked: tuple[LockedPackage, ...], mount: str) -> str:
    """An explicit lock of local files; the installer re-checks every sha256."""
    return "@EXPLICIT\n" + "".join(f"file://{mount}/{package.filename}#{package.sha256}\n" for package in locked)


def lock_entries(locked: tuple[LockedPackage, ...]) -> list[dict]:
    """Provenance and identity entries; conda names are kept apart from wheel names."""
    return [{"name": "conda::" + package.name, "version": package.version, "sha256": package.sha256}
            for package in locked]


def file_entries(locked: tuple[LockedPackage, ...]) -> list[dict]:
    """Where each locked package was downloaded from, so it can be installed again without a solve."""
    return [{"name": package.name, "version": package.version, "build": package.build, "url": package.url,
             "sha256": package.sha256} for package in locked]


def packages_from_files(values, policy: CondaPolicy) -> tuple[LockedPackage, ...]:
    """Recorded files back to locked packages: allowed channels and sha256 only, nothing is guessed."""
    try:
        if not isinstance(values, (list, tuple)) or len(values) > MAX_CONDA_PACKAGES:
            raise ValueError()
        locked = []
        for item in values:
            if not isinstance(item, dict) or set(item) != {"name", "version", "build", "url", "sha256"}:
                raise ValueError()
            if (not all(isinstance(item[name], str) and item[name] for name in item)
                    or not _within_channel(item["url"], policy)
                    or re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is None):
                raise ValueError()
            locked.append(LockedPackage(name=item["name"], version=item["version"], build=item["build"],
                                        url=item["url"], sha256=item["sha256"]))
    except (KeyError, TypeError, ValueError):
        raise CondaError("CONDA_LOCK_INVALID") from None
    if len({item.name for item in locked}) != len(locked):
        raise CondaError("CONDA_LOCK_INVALID")
    return tuple(sorted(locked, key=lambda item: item.name))


__all__ = [
    "CONDA_ENV_PREFIX", "CONDA_RECIPE", "CondaError", "CondaPolicy", "ExplicitEntry", "LockedPackage",
    "download", "file_entries", "lock_entries", "packages_from_files", "lock_explicit", "lock_from_solution",
    "normalize_conda_specs", "parse_explicit", "render_explicit", "solve", "validate_channels",
    "verify_micromamba",
]
