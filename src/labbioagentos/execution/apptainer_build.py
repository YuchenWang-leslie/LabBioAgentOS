"""Cluster environment builds: download, wheel lock, and the fixed build-job program.

DECIDED-C13-8: wheels are resolved and downloaded inside the base SIF with
network, then pinned by sha256. A network-disabled scheduler job installs
exactly the locked wheels, checks them, packs a SIF and runs the import
probe. Nothing here comes from model text except validated import module
names; requirements reach the programs only as files.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shlex
import shutil
import stat
from enum import IntEnum
from pathlib import Path
from urllib.parse import urlsplit

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from .apptainer import ISOLATION_ARGUMENTS, SubprocessApptainerRunner
from .conda import CONDA_ENV_PREFIX
# The same import probe and pip diagnosis as the Docker builder.
from .wheel_lock import MAX_LOCKED_WHEELS, LockedWheel, WheelLockError, lock_wheelhouse
from .environment_builder import _PROBE, diagnose_pip_log, pip_log_diagnostics
from .environments import LEASE_FILE, attempt_lease, normalize_modules  # noqa: F401 - re-exported

MAX_REPORT_BYTES = 16_777_216
PROBE_OUTPUT = "probe.json"
# The download step runs contained but with the host network; nothing else does.
CONTAIN_ARGUMENTS = ISOLATION_ARGUMENTS[:ISOLATION_ARGUMENTS.index("--net")]
_MACHINE = re.compile(r"[a-z0-9_]{1,32}")
_UNSAFE_PATH = re.compile(r"[\s\x00-\x1f\x7f:,]")  # ':' and ',' would split a bind spec.
_CHUNK_BYTES = 1_048_576
# Created in the sandbox only while installing, and removed before packing.
_BUILD_MOUNT = "/.labbio-build"
# Apptainer sources /.singularity.d/env/*.sh in order; this one runs after the image's own PATH.
CONDA_PATH_SCRIPT = "96-labbio-conda.sh"
_WRITABLE_CHECK = (
    "import sysconfig, tempfile; "
    "tempfile.TemporaryFile(dir=sysconfig.get_paths()['purelib']).close()"
)


# Runs inside the base SIF: turns pip's resolution report into exact pins.
# Names and versions are checked so that no line can carry a pip option.
_PINS = """import json, re, sys
name_pattern = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?")
version_pattern = re.compile(r"[A-Za-z0-9][A-Za-z0-9.!+_-]{0,127}")
with open(sys.argv[1], encoding="utf-8") as stream:
    report = json.load(stream)
if report.get("version") != "1" or len(report["install"]) > MAX_LOCKED_WHEELS:
    sys.exit(1)
lines = []
for item in report["install"]:
    name, version = item["metadata"]["name"], item["metadata"]["version"]
    if not (isinstance(name, str) and isinstance(version, str)
            and name_pattern.fullmatch(name) and version_pattern.fullmatch(version)):
        sys.exit(1)
    lines.append(name + "==" + version + "\\n")
with open(sys.argv[2], "w", encoding="utf-8") as stream:
    stream.write("".join(sorted(lines)))
""".replace("MAX_LOCKED_WHEELS", str(MAX_LOCKED_WHEELS))


class DownloadExit(IntEnum):
    """Download-program exit statuses that the control plane maps to failure codes."""

    WORKSPACE_FAILED = 90
    RESOLVE_FAILED = 91
    PIN_FAILED = 92
    DOWNLOAD_FAILED = 93


class BuildExit(IntEnum):
    """Build-program exit statuses that the control plane maps to failure codes."""

    UNAVAILABLE = 81  # The node cannot provide a writable sandbox or pack a SIF.
    INSTALL_FAILED = 82
    CHECK_FAILED = 83
    PACK_FAILED = 84
    PROBE_FAILED = 85
    CONDA_INSTALL_FAILED = 86


def prepare_cache_dir(path: Path) -> None:
    """Create the pip cache the download step binds, or accept an existing private one.

    Apptainer refuses to bind a missing source, so the directory must exist
    before the first download. Wheels are pinned only after they are downloaded,
    so a cache other users could write to could plant packages: it must be a
    real directory owned by this user and writable by nobody else.
    """

    _check_path(path)
    if path.is_symlink():
        raise ValueError("The pip cache directory cannot be a symbolic link")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError("The pip cache must be a directory owned by this user and writable only by it")


def render_download_program(
    *,
    apptainer_binary: Path,
    base_sif: Path,
    requirements_file: Path,
    download_dir: Path,
    work_root: Path,
    log_dir: Path,
    cache_dir: Path | None = None,
    index_url: str | None = None,
    proxy_url: str | None = None,
    ca_bundle: Path | None = None,
    pinned: bool = False,
) -> str:
    """Return the fixed bash program that resolves and downloads wheels; the only networked step.

    pip resolves inside the base SIF with --dry-run, so packages the base image
    already satisfies are kept, as in the Docker builder. Exactly the resolved
    distributions are then downloaded without dependency resolution.

    With pinned, the requirements file is a member's hashed lock: nothing is
    resolved, and pip downloads exactly those files, refusing any other hash.
    """

    paths = [apptainer_binary, base_sif, requirements_file, download_dir, work_root, log_dir]
    paths += [path for path in (cache_dir, ca_bundle) if path is not None]
    for path in paths:
        _check_path(path)
    for url in (index_url, proxy_url):
        if url is not None and not _plain_url(url):
            raise ValueError("Index and proxy settings must be HTTP(S) endpoints without credentials")
    quote = shlex.quote

    def log(name: str) -> str:
        return quote(str(log_dir / name))

    cache = quote(f"{cache_dir}:/labbio/cache:rw") if cache_dir else '"$work/pip-cache:/labbio/cache:rw"'
    binds = [f"--bind {quote(f'{requirements_file}:/labbio/requirements.txt:ro')}",
             f"--bind {quote(f'{download_dir}:/labbio/download:rw')}", f"--bind {cache}"]
    options = ["--disable-pip-version-check", "--no-input", "--progress-bar off",
               "--only-binary=:all:", "--cache-dir /labbio/cache"]
    if index_url is not None:
        options.append(f"--index-url {quote(index_url)}")
    if proxy_url is not None:
        options.append(f"--proxy {quote(proxy_url)}")
    if ca_bundle is not None:
        binds.append(f"--bind {quote(f'{ca_bundle}:/labbio/ca.pem:ro')}")
        options.append("--cert /labbio/ca.pem")
    contained = '"$A" --silent exec "${CONTAIN[@]}" --workdir "$work/tmp" "${BINDS[@]}"'
    base = quote(str(base_sif))
    failed = int(DownloadExit.WORKSPACE_FAILED)
    if pinned:
        steps = [
            f'run {contained} {base} python -I -m pip download --no-deps --require-hashes'
            f' --dest /labbio/download/wheels "${{PIP[@]}}" -r /labbio/requirements.txt > {log("download.log")} 2>&1'
            f" || exit {int(DownloadExit.DOWNLOAD_FAILED)}",
        ]
    else:
        steps = [
            f'run {contained} {base} python -I -m pip install --dry-run --report /labbio/download/report.json'
            f' "${{PIP[@]}}" -r /labbio/requirements.txt > {log("resolve.log")} 2>&1'
            f" || exit {int(DownloadExit.RESOLVE_FAILED)}",
            f"run {contained} {base} python -I -c {quote(_PINS)} /labbio/download/report.json"
            f" /labbio/download/pins.txt > {log('pins.log')} 2>&1 || exit {int(DownloadExit.PIN_FAILED)}",
            f"if [ -s {quote(str(download_dir / 'pins.txt'))} ]; then",
            f'  run {contained} {base} python -I -m pip download --no-deps --dest /labbio/download/wheels'
            f' "${{PIP[@]}}" -r /labbio/download/pins.txt > {log("download.log")} 2>&1'
            f" || exit {int(DownloadExit.DOWNLOAD_FAILED)}",
            "fi",
        ]
    lines = [
        "#!/bin/bash",
        "# Generated by LabBio from a fixed template. Resolves and downloads the wheels",
        "# for one environment; this is the only build step that uses the network.",
        "set -u",
        "umask 077",
        f"A={quote(str(apptainer_binary))}",
        "CONTAIN=(" + " ".join(quote(item) for item in CONTAIN_ARGUMENTS) + ")",
        *_SIGNAL_SAFE_STEPS,
        f"work=$(mktemp -d {quote(str(work_root))}/labbio-download.XXXXXXXX) || exit {failed}",
        'cleanup() { rm -rf -- "$work"; }',
        "trap cleanup EXIT",
        f'mkdir -- "$work/tmp" "$work/pip-cache" "$work/apptainer-tmp" || exit {failed}',
        'export APPTAINER_TMPDIR="$work/apptainer-tmp"',
        "BINDS=(" + " ".join(binds) + ")",
        "PIP=(" + " ".join(options) + ")",
        *steps,
    ]
    return "\n".join(lines) + "\n"


def download_check_argv(
    *,
    apptainer_binary: Path,
    base_sif: Path,
    work_dir: Path,
    cache_dir: Path | None = None,
    index_url: str | None = None,
    proxy_url: str | None = None,
    ca_bundle: Path | None = None,
) -> tuple[str, ...]:
    """Operator check: the base image's pip can resolve from the index with the download settings.

    pip resolves itself, ignoring the installed copy, so the index must be
    reached; --dry-run --report also proves the pip version the download needs.
    The binds are the download step's, so a missing cache or CA file fails here too.
    """

    for path in (apptainer_binary, base_sif, work_dir, *(item for item in (cache_dir, ca_bundle) if item)):
        _check_path(path)
    for url in (index_url, proxy_url):
        if url is not None and not _plain_url(url):
            raise ValueError("Index and proxy settings must be HTTP(S) endpoints without credentials")
    argv = [str(apptainer_binary), "--silent", "exec", *CONTAIN_ARGUMENTS, "--workdir", str(work_dir),
            "--bind", f"{work_dir}:/labbio/check:rw"]
    options = ["--no-cache-dir"]
    if cache_dir is not None:
        argv += ["--bind", f"{cache_dir}:/labbio/cache:rw"]
        options = ["--cache-dir", "/labbio/cache"]
    if ca_bundle is not None:
        argv += ["--bind", f"{ca_bundle}:/labbio/ca.pem:ro"]
        options += ["--cert", "/labbio/ca.pem"]
    if index_url is not None:
        options += ["--index-url", index_url]
    if proxy_url is not None:
        options += ["--proxy", proxy_url]
    return (*argv, str(base_sif), "python", "-I", "-m", "pip", "install", "--dry-run", "--ignore-installed",
            "--no-deps", "--only-binary=:all:", "--disable-pip-version-check", "--no-input",
            "--progress-bar", "off", "--report", "/labbio/check/report.json", *options, "pip")


def classify_download_check(exit_code: int | None, *, timed_out: bool, output: str, report: Path) -> str:
    """OK, PIP_TOO_OLD, BUILD_TIMEOUT, or the pip failure code (NETWORK_FAILURE, TLS_FAILURE, ...)."""

    if timed_out:
        return "BUILD_TIMEOUT"
    if exit_code == 0:
        try:
            if json.loads(report.read_text(encoding="utf-8"))["version"] == "1":
                return "OK"
        except (OSError, UnicodeError, ValueError, KeyError, TypeError):
            pass
        return "VERIFICATION_FAILED"
    if "no such option" in output:  # --dry-run and --report need pip 22.2 or later.
        return "PIP_TOO_OLD"
    return diagnose_pip_log(output)


def verify_download(download_dir: Path, *, machine: str) -> tuple[LockedWheel, ...]:
    """Lock the downloaded wheels and require them to be exactly pip's resolution."""

    report_path = download_dir / "report.json"
    if (report_path.is_symlink() or not report_path.is_file()
            or report_path.stat().st_size > MAX_REPORT_BYTES):
        raise WheelLockError("The resolution report is unavailable")
    expected: dict[str, Version] = {}
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        items = report["install"]
        if report["version"] != "1" or not isinstance(items, list) or len(items) > MAX_LOCKED_WHEELS:
            raise ValueError()
        for item in items:
            name = canonicalize_name(item["metadata"]["name"])
            if name in expected:
                raise ValueError()
            expected[name] = Version(item["metadata"]["version"])
    except (OSError, UnicodeError, ValueError, KeyError, TypeError, InvalidVersion):
        raise WheelLockError("The resolution report is invalid") from None
    wheels = lock_wheelhouse(download_dir / "wheels", machine=machine)
    if {wheel.name: Version(wheel.version) for wheel in wheels} != expected:
        raise WheelLockError("The downloaded wheels differ from the resolution")
    return wheels


def classify_build(exit_status: int | None, *, timed_out: bool, log_dir: Path) -> tuple[str, tuple[dict, ...]]:
    """Map a failed build job to a model-safe failure code and bounded diagnostics."""

    if timed_out:
        return "BUILD_TIMEOUT", ({"code": "BUILD_TIMEOUT"},)
    fixed = {BuildExit.UNAVAILABLE: "BUILDER_UNAVAILABLE", BuildExit.PROBE_FAILED: "VERIFICATION_FAILED",
             BuildExit.CONDA_INSTALL_FAILED: "CONDA_INSTALL_FAILED"}
    if exit_status in fixed:
        code = fixed[BuildExit(exit_status)]
        return code, ({"code": code},)
    if exit_status == BuildExit.INSTALL_FAILED:
        text = _tail(log_dir / "install.log")
        code = diagnose_pip_log(text)
        return code, pip_log_diagnostics(text, code)
    if exit_status == BuildExit.CHECK_FAILED:
        return "PIP_CHECK_FAILED", ({"code": "PIP_CHECK_FAILED"},)
    return "BUILD_FAILED", ({"code": "BUILD_FAILED"},)


def classify_download(exit_status: int | None, *, timed_out: bool, log_dir: Path) -> tuple[str, tuple[dict, ...]]:
    """Map a download outcome to a model-safe failure code and bounded diagnostics."""

    if timed_out:
        return "BUILD_TIMEOUT", ({"code": "BUILD_TIMEOUT"},)
    if exit_status == DownloadExit.PIN_FAILED:
        return "VERIFICATION_FAILED", ({"code": "VERIFICATION_FAILED"},)
    logs = {DownloadExit.RESOLVE_FAILED: "resolve.log", DownloadExit.DOWNLOAD_FAILED: "download.log"}
    if exit_status not in logs:
        return "BUILD_FAILED", ({"code": "BUILD_FAILED"},)
    text = _tail(log_dir / logs[DownloadExit(exit_status)])
    code = diagnose_pip_log(text)
    return code, pip_log_diagnostics(text, code)


class DownloadRunner(SubprocessApptainerRunner):
    """Runs the download program on the login node: low priority, CPU and file limits.

    The program also receives SIGTERM if the control plane dies, so it stops its
    current step and removes its temporary files instead of running unowned.
    """

    def __init__(self, *, max_file_bytes: int, cpu_seconds: int, cache_dir: str | Path | None = None,
                 tmp_dir: str | Path | None = None):
        super().__init__(max_output_file_bytes=max_file_bytes, cache_dir=cache_dir, tmp_dir=tmp_dir)
        if type(cpu_seconds) is not int or not 1 <= cpu_seconds <= 86_400:
            raise ValueError("cpu_seconds must be between 1 and 86400")
        self.cpu_seconds = cpu_seconds
        self._parent_pid = os.getpid()

    def entrypoint_options(self) -> tuple[str, ...]:
        return (*super().entrypoint_options(), "--cpu-seconds", str(self.cpu_seconds),
                "--parent-pid", str(self._parent_pid), "--nice", "10")


def remove_stale_downloads(attempts_root: Path) -> int:
    """Delete downloads and solver caches that no live attempt holds; logs and receipts stay."""

    removed = 0
    for attempt in sorted(attempts_root.iterdir()):
        downloads = [path for path in (attempt / "download" / "wheels", attempt / "conda" / "pkgs",
                                       attempt / "conda" / "solve")
                     if not path.is_symlink() and path.is_dir()]
        if attempt.is_symlink() or not downloads:
            continue
        try:
            descriptor = os.open(attempt / LEASE_FILE, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            descriptor = None
        try:
            if descriptor is not None:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue  # A live process is still using this attempt.
            for path in downloads:
                shutil.rmtree(path)
            removed += 1
        finally:
            if descriptor is not None:
                os.close(descriptor)
    return removed


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
    conda_dir: Path | None = None,
) -> str:
    """Return the fixed bash program a build job runs; every value is validated and quoted.

    With conda_dir (micromamba, explicit.txt and pkgs/ from the control plane),
    the locked conda packages are installed offline into CONDA_ENV_PREFIX first,
    and an environment script appends its bin directory to PATH, so the image's
    own python stays the PYTHON entry point.
    """

    for path in (apptainer_binary, base_sif, wheel_dir, lock_file, output_sif, build_dir, log_dir,
                 *((conda_dir,) if conda_dir is not None else ())):
        _check_path(path)
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
        # Packing a SIF needs mksquashfs, which sites install beside Apptainer or in sbin.
        f'export PATH={quote(str(apptainer_binary.parent))}:/usr/local/sbin:/usr/sbin:/sbin:"$PATH"',
        "ISOLATION=(" + " ".join(quote(item) for item in ISOLATION_ARGUMENTS) + ")",
        *_SIGNAL_SAFE_STEPS,
        f"work=$(mktemp -d {quote(str(build_dir))}/labbio-build.XXXXXXXX) || exit {unavailable}",
        'cleanup() { chmod -R u+w -- "$work" 2>/dev/null; rm -rf -- "$work"; }',
        "trap cleanup EXIT",
        f'mkdir -- "$work/tmp" "$work/apptainer-tmp" "$work/pack-check" || exit {unavailable}',
        # Everything below is created inside the image, which every lab member must be able to
        # read; the host side stays private because $work and the log directory are 0700.
        "umask 022",
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
        *(_conda_steps(conda_dir, isolated, log) if conda_dir is not None else ()),
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


def _conda_steps(conda_dir: Path, isolated: str, log) -> list[str]:
    """Offline install of the explicit lock inside the sandbox, then the PATH script.

    The mount point is created fresh (an image entry there makes mkdir fail) and
    removed, with the installer's package cache, before the wheel step. The PATH script
    is written from inside the container, so a link in the image cannot redirect
    the write to the host.
    """

    quote = shlex.quote
    failed = int(BuildExit.CONDA_INSTALL_FAILED)
    conda = f"{_BUILD_MOUNT}/conda"
    binds = " ".join(f"--bind {quote(f'{conda_dir / name}:{conda}/{name}:ro')}"
                     for name in ("micromamba", "explicit.txt", "pkgs"))
    install = (f"{conda}/micromamba create -y -q --no-rc -r {_BUILD_MOUNT}/root -p {CONDA_ENV_PREFIX}"
               f" --offline --file {conda}/explicit.txt")
    script = ("import os; os.makedirs('/.singularity.d/env', exist_ok=True); "
              f"open('/.singularity.d/env/{CONDA_PATH_SCRIPT}', 'x').write("
              f"'export PATH=\"$PATH:{CONDA_ENV_PREFIX}/bin\"\\n')")
    return [
        f'mkdir -- "$mount" "$mount/conda" "$mount/conda/pkgs" "$mount/root" && : > "$mount/conda/micromamba"'
        f' && : > "$mount/conda/explicit.txt" || exit {failed}',
        f'run {isolated} --writable {binds} "$sandbox" {install} > {log("conda.log")} 2>&1 || exit {failed}',
        f'chmod -R u+w -- "$mount" && rm -rf -- "$mount" || exit {failed}',
        f'run {isolated} --writable "$sandbox" python -I -c {quote(script)} > {log("conda-path.log")} 2>&1'
        f" || exit {failed}",
    ]


# Each step runs in the background so that a cancel, timeout or parent death stops it at once.
_SIGNAL_SAFE_STEPS = (
    "child=",
    'run() { "$@" & child=$!; wait "$child"; local status=$?; child=; return $status; }',
    'stop() { if [ -n "$child" ]; then kill -TERM "$child" 2>/dev/null; wait "$child" 2>/dev/null; fi; exit 143; }',
    "trap stop TERM INT HUP",
)


def _check_path(path: Path) -> None:
    text = str(path)
    if not Path(text).is_absolute() or _UNSAFE_PATH.search(text):
        raise ValueError("Build paths must be plain absolute paths")


def _plain_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (parsed.scheme in {"http", "https"} and bool(parsed.hostname)
            and parsed.username is None and parsed.password is None
            and not parsed.query and not parsed.fragment
            and re.search(r"[\s\x00-\x1f\x7f]", url) is None)


def _tail(path: Path, limit: int = 65_536) -> str:
    if path.is_symlink() or not path.is_file():
        return ""
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - limit))
        return stream.read(limit).decode("utf-8", errors="replace")
