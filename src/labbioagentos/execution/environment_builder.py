"""A fixed Docker wheel-install recipe with no data mounts or model-written build code.

Wheels are downloaded in a separate container, pinned by sha256 on the host with
the same lock the Apptainer builder uses, and installed offline with
--require-hashes. Both backends therefore give one environment one derived
source identity (DECIDED-C15-D).
"""

from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import threading
import time
from urllib.parse import urlsplit
from uuid import UUID

from .conda import (CONDA_ENV_PREFIX, CondaError, CondaPolicy, download as conda_download, lock_entries,
                    render_explicit, solve as conda_solve, verify_micromamba)
from .wheel_lock import WheelLockError, lock_wheelhouse, render_lock
from .environments import (EnvironmentBuildResult, EnvironmentBuildUnconfirmedError,
                           EnvironmentRequestError, normalize_modules, normalize_requirements)
from .images import ApprovedImage


_PROBE = """import contextlib, glob, importlib, importlib.metadata, json, sys
modules = json.loads(sys.argv[1])
failed = []
with open('/tmp/import-check.log', 'w') as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
    for module in modules:
        try:
            importlib.import_module(module)
        except BaseException:
            failed.append(module)
    packages = {item.metadata['Name']: item.version for item in importlib.metadata.distributions()}
    conda = {}
    for path in sorted(glob.glob('/opt/labbio-env/conda-meta/*.json'))[:4096]:
        try:
            with open(path) as stream:
                record = json.load(stream)
            conda[record['name']] = record['version']
        except Exception:
            pass
print(json.dumps({'installed_packages': packages, 'conda_packages': conda, 'failed_imports': failed}, sort_keys=True))
"""


_CONDA_FAILURES = {"CONDA_NO_SOLUTION", "CONDA_LOCK_INVALID", "CONDA_PLATFORM_UNSUPPORTED", "CONDA_UNAVAILABLE",
                   "NETWORK_FAILURE", "VERIFICATION_FAILED", "BUILD_TIMEOUT"}


def diagnose_pip_log(text: str) -> str:
    """Map pip output to a model-safe failure code; unrecognized output is BUILD_FAILED."""
    patterns = (
        ("PIP_NO_MATCH", ("No matching distribution found", "Could not find a version that satisfies")),
        ("PIP_RESOLUTION_CONFLICT", ("ResolutionImpossible", "conflicting dependencies")),
        ("PYTHON_VERSION_INCOMPATIBLE", ("requires a different Python", "Requires-Python")),
        ("TLS_FAILURE", ("CERTIFICATE_VERIFY_FAILED", "certificate verify failed")),
        ("NETWORK_FAILURE", ("ProxyError", "ConnectionError", "Connection refused", "Temporary failure in name resolution", "ReadTimeoutError")),
        ("PIP_CHECK_FAILED", ("has requirement", "which is not installed")),
    )
    return next((code for code, matches in patterns if any(value in text for value in matches)), "BUILD_FAILED")


def pip_log_diagnostics(text: str, code: str) -> tuple[dict, ...]:
    """Bounded, re-validated dependency facts from pip output; no log text is copied."""
    diagnostics = []
    for match in re.finditer(r"No matching distribution found for ([^\r\n]{1,256})", text):
        try:
            requirement = normalize_requirements((match.group(1).strip(),))[0]
        except EnvironmentRequestError:
            continue
        diagnostics.append({"code": "PIP_NO_MATCH", "requirement": requirement})
    pattern = r"\b([A-Za-z0-9][A-Za-z0-9._-]{0,127}) ([A-Za-z0-9][A-Za-z0-9.!+_-]{0,127}) depends on ([^\r\n]{1,256})"
    for match in re.finditer(pattern, text):
        try:
            dependency = normalize_requirements((match.group(3).strip(),))[0]
        except EnvironmentRequestError:
            continue
        diagnostics.append({"code": "PIP_RESOLUTION_CONFLICT", "package": match.group(1),
                            "version": match.group(2), "dependency": dependency})
    return tuple(diagnostics[:8]) or ({"code": code},)


class DockerEnvironmentBuilder:
    def __init__(self, proxy_url: str | None = None, timeout_seconds: float = 600,
                 cpus: float = 4.0, conda: CondaPolicy | None = None):
        if not 1 <= timeout_seconds <= 3600:
            raise EnvironmentRequestError()
        if (isinstance(cpus, bool) or not isinstance(cpus, (int, float))
                or not math.isfinite(cpus) or not 0.01 <= cpus <= 1024):
            raise EnvironmentRequestError()
        if proxy_url is not None:
            parsed = urlsplit(proxy_url)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or any(ord(char) < 32 for char in proxy_url)):
                raise EnvironmentRequestError()
        self.proxy_url, self.timeout_seconds, self.cpus = proxy_url, timeout_seconds, cpus
        self.conda = conda
        self._attempt_state = threading.local()

    @property
    def supports_conda(self) -> bool:
        return self.conda is not None

    async def build(self, *, base_image: ApprovedImage, requirements: tuple[str, ...],
                    import_modules: tuple[str, ...], build_root: Path, request_hash: str,
                    run_id: UUID | None = None, conda_packages: tuple[str, ...] = ()) -> EnvironmentBuildResult:
        # Docker builds run on the control-plane host and submit no job, so run_id is unused.
        requirements, import_modules = normalize_requirements(requirements), normalize_modules(import_modules, max_count=256)
        stop = threading.Event()

        def work():
            self._attempt_state.stop = stop
            try:
                return self._build(base_image, requirements, import_modules, build_root, request_hash,
                                   tuple(conda_packages))
            finally:
                del self._attempt_state.stop

        worker = asyncio.create_task(asyncio.to_thread(work))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            stop.set()
            settle_by = time.monotonic() + 45
            while not worker.done():
                try:
                    await asyncio.wait_for(asyncio.shield(worker), max(0.001, settle_by - time.monotonic()))
                except asyncio.CancelledError:
                    continue
                except asyncio.TimeoutError as exc:
                    raise EnvironmentBuildUnconfirmedError("CANCELLATION_SETTLE_TIMEOUT") from exc
            try:
                result = worker.result()
            except EnvironmentBuildUnconfirmedError:
                raise
            except Exception as exc:
                raise EnvironmentBuildUnconfirmedError("CANCELLED_WORKER_FAILED") from exc
            if result.image_id is not None:
                raise EnvironmentBuildUnconfirmedError("CANCELLED_IMAGE_UNREGISTERED", result.image_id)
            raise

    def _stop_requested(self) -> bool:
        stop = getattr(self._attempt_state, "stop", None)
        return stop is not None and stop.is_set()

    @staticmethod
    def _environment() -> dict[str, str]:
        # Do not pass provider keys or arbitrary task environment to the Docker client.
        names = {"PATH", "HOME", "USER", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG",
                 "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "XDG_RUNTIME_DIR"}
        return {**{name: value for name, value in os.environ.items() if name in names}, "DOCKER_BUILDKIT": "0"}

    def _run(self, argv: list[str], path: Path, deadline: float) -> tuple[int | None, str | None]:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as log:
            if self._stop_requested():
                return None, "BUILD_CANCELLED"
            try:
                process = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT,
                                           shell=False, env=self._environment())
            except OSError:
                return None, "BUILDER_UNAVAILABLE"
            while True:
                if self._stop_requested():
                    error = "BUILD_CANCELLED"
                else:
                    error = None
                try:
                    if error is None:
                        code = process.wait(timeout=0.2)
                        return (code, "BUILD_LOG_LIMIT") if os.fstat(log.fileno()).st_size > 16_777_216 else (code, None)
                except subprocess.TimeoutExpired:
                    error = ("BUILD_TIMEOUT" if time.monotonic() >= deadline else
                             "BUILD_LOG_LIMIT" if os.fstat(log.fileno()).st_size > 16_777_216 else None)
                if error is not None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    except OSError as exc:
                        raise EnvironmentBuildUnconfirmedError("LOCAL_PROCESS_NOT_REAPED") from exc
                    try:
                        process.wait(timeout=10)
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        raise EnvironmentBuildUnconfirmedError("LOCAL_PROCESS_NOT_REAPED") from exc
                    return None, error

    def _run_cleanup(self, argv: list[str], path: Path) -> tuple[int | None, str | None]:
        stop = getattr(self._attempt_state, "stop", None)
        self._attempt_state.stop = None
        try:
            return self._run(argv, path, time.monotonic() + 15)
        finally:
            self._attempt_state.stop = stop

    @staticmethod
    def _tail(path: Path) -> str:
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 65_536))
            return stream.read(65_536).decode("utf-8", errors="replace")

    _diagnose = staticmethod(diagnose_pip_log)
    _diagnostics = staticmethod(pip_log_diagnostics)

    def _cleanup(self, build_root: Path, *, probe_name: str | None = None) -> None:
        # Query only this attempt's generated label or exact probe name.
        selector = ("name=^/" + probe_name + "$" if probe_name is not None else
                    "label=labbio.environment.build=" + build_root.name)
        argv = ["docker", "ps", "-aq", "--filter", selector]

        def list_owned(path: Path) -> tuple[str, ...]:
            code, error = self._run_cleanup(argv, path)
            if error is not None or code != 0:
                raise EnvironmentBuildUnconfirmedError("CLEANUP_LIST_FAILED")
            try:
                if path.stat().st_size > 65_536:
                    raise ValueError()
                identifiers = tuple(path.read_text().split())
            except (OSError, UnicodeError, ValueError) as exc:
                raise EnvironmentBuildUnconfirmedError("CLEANUP_LIST_INVALID") from exc
            if (len(set(identifiers)) != len(identifiers)
                    or any(re.fullmatch(r"[0-9a-f]{12,64}", value) is None for value in identifiers)):
                raise EnvironmentBuildUnconfirmedError("CLEANUP_IDENTITY_INVALID")
            return identifiers

        identifiers = list_owned(build_root / "cleanup-list.log")
        if identifiers:
            code, error = self._run_cleanup(
                ["docker", "rm", "--force", *identifiers],
                build_root / ("probe-cleanup.log" if probe_name else "build-cleanup.log"),
            )
            if error is not None or code != 0:
                raise EnvironmentBuildUnconfirmedError("CLEANUP_REMOVE_FAILED")
        if list_owned(build_root / "cleanup-confirm.log"):
            raise EnvironmentBuildUnconfirmedError("CLEANUP_CONFIRM_FAILED")

    def _download(self, base_id: str, requirements: tuple[str, ...], build_root: Path, deadline: float):
        """Download wheels in a labelled, data-free container into a host directory."""
        wheels, requested = build_root / "wheels", build_root / "requirements.txt"
        wheels.mkdir(mode=0o700)
        requested.write_text("\n".join(requirements) + "\n", encoding="utf-8")
        name = "labbio-env-download-" + build_root.name
        argv = ["docker", "run", "--rm", "--pull", "never", "--name", name,
                "--label", "labbio.environment.build=" + build_root.name,
                "--user", f"{os.getuid()}:{os.getgid()}", "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--memory", "4g", "--cpus", format(self.cpus, "g"),
                "--pids-limit", "256", "--tmpfs", "/tmp:rw,nosuid,size=1024m", "--env", "HOME=/tmp",
                "--network", "host" if self.proxy_url else "bridge"]
        if self.proxy_url:
            argv += ["--env", "HTTP_PROXY=" + self.proxy_url, "--env", "HTTPS_PROXY=" + self.proxy_url]
        argv += ["--mount", f"type=bind,source={wheels},target=/labbio/wheels",
                 "--mount", f"type=bind,source={requested},target=/labbio/requirements.txt,readonly",
                 "--entrypoint", "python", base_id, "-I", "-m", "pip", "download",
                 "--disable-pip-version-check", "--no-input", "--progress-bar", "off", "--no-cache-dir",
                 "--only-binary=:all:", "--dest", "/labbio/wheels", "-r", "/labbio/requirements.txt"]
        log = build_root / "download.log"
        code, error = self._run(argv, log, deadline)
        if self._stop_requested() and error is None:
            error = "BUILD_CANCELLED"
        if error or code != 0:
            if error == "BUILD_CANCELLED" or (code is None and error in {"BUILD_TIMEOUT", "BUILD_LOG_LIMIT"}):
                self._cleanup(build_root, probe_name=name)
                raise EnvironmentBuildUnconfirmedError("DAEMON_TERMINATION_UNCONFIRMED", failure_code=error)
            self._cleanup(build_root, probe_name=name)
            text = self._tail(log)
            failure_code = error or self._diagnose(text)
            return EnvironmentBuildResult(failure_code=failure_code, diagnostics=self._diagnostics(text, failure_code),
                                          log_id=build_root.name)
        return wheels

    def _build(self, base_image, requirements, modules, build_root, request_hash, conda_packages=()):
        build_root = Path(build_root)
        if (re.fullmatch(r"[0-9a-f]{32}", build_root.name) is None
                or re.fullmatch(r"[0-9a-f]{64}", request_hash) is None
                or any(path.is_symlink() for path in (build_root, *build_root.parents))):
            raise EnvironmentRequestError()
        deadline = time.monotonic() + self.timeout_seconds

        def failed(code, diagnostics=()):
            return EnvironmentBuildResult(failure_code=code, diagnostics=diagnostics, log_id=build_root.name)

        def interrupted_daemon(code: str, *, probe_name: str | None = None,
                               image_id: str | None = None):
            try:
                self._cleanup(build_root, probe_name=probe_name)
            except EnvironmentBuildUnconfirmedError as exc:
                raise EnvironmentBuildUnconfirmedError(
                    exc.reason_code, image_id=image_id, failure_code=code,
                ) from exc
            raise EnvironmentBuildUnconfirmedError(
                "DAEMON_TERMINATION_UNCONFIRMED", image_id=image_id, failure_code=code,
            )

        if self._stop_requested():
            return failed("BUILD_CANCELLED")

        inspect_log = build_root / "base-inspect.log"
        code, error = self._run(["docker", "image", "inspect", "--format", "{{.Id}}",
                                 base_image.resolved_reference], inspect_log, deadline)
        if error or code != 0:
            return failed(error or "BUILD_FAILED")
        base_id = self._tail(inspect_log).strip()
        if re.fullmatch(r"sha256:[0-9a-f]{64}", base_id) is None:
            return failed("VERIFICATION_FAILED")
        if self._stop_requested():
            return failed("BUILD_CANCELLED")
        context = build_root / "context"
        context.mkdir(mode=0o700)
        recipe, locked_conda = "", ()
        if conda_packages:
            if self.conda is None:
                return failed("BUILDER_UNAVAILABLE")
            try:
                (build_root / "conda").mkdir(mode=0o700)
                locked_conda = conda_solve(self.conda, conda_packages, build_root / "conda")
                conda_download(locked_conda, context / "conda-pkgs", proxy_url=self.conda.proxy_url)
                shutil.copy2(verify_micromamba(self.conda), context / "micromamba")
            except CondaError as exc:
                return failed(exc.code if exc.code in _CONDA_FAILURES else "BUILD_FAILED")
            (context / "explicit.txt").write_text(render_explicit(locked_conda, "/labbio/conda-pkgs"))
            # Installed offline in a separate stage; only the finished environment is copied on.
            recipe += (f"FROM {base_id} AS labbio-conda\nUSER root\n"
                       "COPY micromamba /labbio/micromamba\nCOPY conda-pkgs /labbio/conda-pkgs\n"
                       "COPY explicit.txt /labbio/explicit.txt\n"
                       "RUN /labbio/micromamba create -y -q --no-rc -r /tmp/labbio-mamba "
                       f"-p {CONDA_ENV_PREFIX} --offline --file /labbio/explicit.txt && rm -rf /tmp/labbio-mamba\n")
        recipe += (f"FROM {base_id}\nUSER root\n"
                   f"LABEL labbio.environment.build={build_root.name}\n")
        if conda_packages:
            # Appended, so the image's own python stays the PYTHON entry point.
            recipe += (f"COPY --from=labbio-conda {CONDA_ENV_PREFIX} {CONDA_ENV_PREFIX}\n"
                       f"ENV PATH=$PATH:{CONDA_ENV_PREFIX}/bin\n")
        locked: tuple = ()
        machine = self.conda.machine if self.conda is not None else platform.machine()
        if requirements:
            downloaded = self._download(base_id, requirements, build_root, deadline)
            if isinstance(downloaded, EnvironmentBuildResult):
                return downloaded
            try:
                locked = lock_wheelhouse(downloaded, machine=machine)
            except WheelLockError:
                return failed("VERIFICATION_FAILED")
            shutil.copytree(downloaded, context / "wheels")
            (context / "lock.txt").write_text(render_lock(locked), encoding="utf-8")
            # Exactly the locked files, offline: never re-resolved inside the image.
            recipe += ("COPY wheels /opt/labbio-environment/wheels\n"
                       "COPY lock.txt /opt/labbio-environment/lock.txt\n"
                       "RUN python -I -m pip install --disable-pip-version-check --no-input --no-cache-dir "
                       "--progress-bar off --no-index --no-deps --require-hashes "
                       "--find-links=/opt/labbio-environment/wheels -r /opt/labbio-environment/lock.txt\n")
        recipe += "RUN python -m pip check\n"
        with (context / "Dockerfile").open("x", encoding="utf-8") as stream:
            stream.write(recipe)
        image_file = build_root / "image-id.txt"
        argv = ["docker", "build", "--pull=false", "--force-rm", "--memory", "4g",
                "--cpu-period", "100000", "--cpu-quota", str(math.floor(self.cpus * 100000)),
                "--memory-swap", "4g", "--iidfile", str(image_file), "--network", "none"]
        build_log = build_root / "build.log"
        code, error = self._run([*argv, str(context)], build_log, deadline)
        if self._stop_requested() and error is None:
            error = "BUILD_CANCELLED"
        if error or code != 0:
            if error == "BUILD_CANCELLED" or (code is None and error in {"BUILD_TIMEOUT", "BUILD_LOG_LIMIT"}):
                interrupted_daemon(error)
            self._cleanup(build_root)
            text = self._tail(build_log)
            failure_code = error or self._diagnose(text)
            return failed(failure_code, self._diagnostics(text, failure_code))
        if not image_file.is_file() or image_file.stat().st_size > 128:
            return failed("VERIFICATION_FAILED")
        image_id = image_file.read_text().strip()
        if re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
            return failed("VERIFICATION_FAILED")
        if self._stop_requested():
            interrupted_daemon("BUILD_CANCELLED", image_id=image_id)
        probe_name = "labbio-env-check-" + build_root.name
        probe_log = build_root / "verification.log"
        code, error = self._run(["docker", "run", "--rm", "--pull", "never", "--name", probe_name,
            "--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--memory", "4g", "--memory-swap", "4g", "--cpus", format(self.cpus, "g"),
            "--pids-limit", "128",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m", "--entrypoint", "python",
            image_id, "-I", "-c", _PROBE, json.dumps(modules)], probe_log, deadline)
        if self._stop_requested() and error is None:
            error = "BUILD_CANCELLED"
        if error or code != 0:
            if error == "BUILD_CANCELLED" or (code is None and error in {"BUILD_TIMEOUT", "BUILD_LOG_LIMIT"}):
                interrupted_daemon(error, probe_name=probe_name, image_id=image_id)
            self._cleanup(build_root, probe_name=probe_name)
            return failed(error or "VERIFICATION_FAILED")
        try:
            if probe_log.stat().st_size > 1_048_576:
                raise ValueError()
            inventory = json.loads(probe_log.read_text())
            failed_modules = inventory["failed_imports"]
            if not isinstance(failed_modules, list) or any(module not in modules for module in failed_modules):
                raise ValueError()
            if failed_modules:
                return failed("IMPORT_CHECK_FAILED", tuple({"code": "IMPORT_CHECK_FAILED", "module": module}
                                                           for module in failed_modules[:8]))
            if self._stop_requested():
                interrupted_daemon("BUILD_CANCELLED", probe_name=probe_name, image_id=image_id)
            return EnvironmentBuildResult(
                image_id=image_id, installed_packages=inventory["installed_packages"],
                available_python_modules=modules, log_id=build_root.name,
                locked_wheels=tuple({"name": wheel.name, "version": wheel.version, "sha256": wheel.sha256}
                                    for wheel in locked),
                machine=machine if locked or locked_conda else None,
                locked_conda=tuple(lock_entries(locked_conda)),
                conda_packages={name: version for name, version in inventory.get("conda_packages", {}).items()
                                if isinstance(name, str) and isinstance(version, str)
                                and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._+-]{0,127}", name)
                                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.!+_-]{0,127}", version)},
            )
        except (ValueError, KeyError, TypeError):
            return failed("VERIFICATION_FAILED")
