"""A fixed Docker wheel-install recipe with no data mounts or model-written build code."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlsplit

from .environments import EnvironmentBuildResult, EnvironmentRequestError, normalize_modules, normalize_requirements
from .images import ApprovedImage


_PROBE = """import contextlib, importlib, importlib.metadata, json, sys
modules = json.loads(sys.argv[1])
failed = []
with open('/tmp/import-check.log', 'w') as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
    for module in modules:
        try:
            importlib.import_module(module)
        except BaseException:
            failed.append(module)
    packages = {item.metadata['Name']: item.version for item in importlib.metadata.distributions()}
print(json.dumps({'installed_packages': packages, 'failed_imports': failed}, sort_keys=True))
"""


class DockerEnvironmentBuilder:
    def __init__(self, proxy_url: str | None = None, timeout_seconds: float = 600):
        if not 1 <= timeout_seconds <= 3600:
            raise EnvironmentRequestError()
        if proxy_url is not None:
            parsed = urlsplit(proxy_url)
            if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                    or parsed.username is not None or parsed.password is not None
                    or any(ord(char) < 32 for char in proxy_url)):
                raise EnvironmentRequestError()
        self.proxy_url, self.timeout_seconds = proxy_url, timeout_seconds

    async def build(self, *, base_image: ApprovedImage, requirements: tuple[str, ...],
                    import_modules: tuple[str, ...], build_root: Path, request_hash: str) -> EnvironmentBuildResult:
        requirements, import_modules = normalize_requirements(requirements), normalize_modules(import_modules, max_count=256)
        return await asyncio.to_thread(self._build, base_image, requirements, import_modules, build_root, request_hash)

    @staticmethod
    def _environment() -> dict[str, str]:
        # Do not pass provider keys or arbitrary task environment to the Docker client.
        names = {"PATH", "HOME", "USER", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG",
                 "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "XDG_RUNTIME_DIR"}
        return {**{name: value for name, value in os.environ.items() if name in names}, "DOCKER_BUILDKIT": "0"}

    def _run(self, argv: list[str], path: Path, deadline: float) -> tuple[int | None, str | None]:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as log:
            try:
                process = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT,
                                           shell=False, env=self._environment())
            except OSError:
                return None, "BUILDER_UNAVAILABLE"
            while True:
                try:
                    return process.wait(timeout=0.2), None
                except subprocess.TimeoutExpired:
                    error = ("BUILD_TIMEOUT" if time.monotonic() >= deadline else
                             "BUILD_LOG_LIMIT" if os.fstat(log.fileno()).st_size > 16_777_216 else None)
                    if error is not None:
                        process.kill()
                        process.wait(timeout=10)
                        return None, error

    @staticmethod
    def _tail(path: Path) -> str:
        with path.open("rb") as stream:
            stream.seek(max(0, path.stat().st_size - 65_536))
            return stream.read(65_536).decode("utf-8", errors="replace")

    @staticmethod
    def _diagnose(text: str) -> str:
        patterns = (
            ("PIP_NO_MATCH", ("No matching distribution found", "Could not find a version that satisfies")),
            ("PIP_RESOLUTION_CONFLICT", ("ResolutionImpossible", "conflicting dependencies")),
            ("PYTHON_VERSION_INCOMPATIBLE", ("requires a different Python", "Requires-Python")),
            ("TLS_FAILURE", ("CERTIFICATE_VERIFY_FAILED", "certificate verify failed")),
            ("NETWORK_FAILURE", ("ProxyError", "ConnectionError", "Connection refused", "Temporary failure in name resolution", "ReadTimeoutError")),
            ("PIP_CHECK_FAILED", ("has requirement", "which is not installed")),
        )
        return next((code for code, matches in patterns if any(value in text for value in matches)), "BUILD_FAILED")

    @staticmethod
    def _diagnostics(text: str, code: str) -> tuple[dict, ...]:
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

    def _cleanup(self, build_root: Path, *, probe_name: str | None = None) -> None:
        if probe_name is not None:
            argv = ["docker", "rm", "--force", probe_name]
        else:
            # Only containers bearing this attempt's generated label are owned here.
            listing = build_root / "cleanup-list.log"
            code, _ = self._run(["docker", "ps", "-aq", "--filter",
                "label=labbio.environment.build=" + build_root.name], listing, time.monotonic() + 15)
            if code != 0:
                return
            identifiers = listing.read_text().split()
            if not identifiers or any(re.fullmatch(r"[0-9a-f]{12,64}", value) is None for value in identifiers):
                return
            argv = ["docker", "rm", "--force", *identifiers]
        self._run(argv, build_root / ("probe-cleanup.log" if probe_name else "build-cleanup.log"), time.monotonic() + 15)

    def _build(self, base_image, requirements, modules, build_root, request_hash):
        build_root = Path(build_root)
        if (re.fullmatch(r"[0-9a-f]{32}", build_root.name) is None
                or re.fullmatch(r"[0-9a-f]{64}", request_hash) is None
                or any(path.is_symlink() for path in (build_root, *build_root.parents))):
            raise EnvironmentRequestError()
        deadline = time.monotonic() + self.timeout_seconds

        def failed(code, diagnostics=()):
            return EnvironmentBuildResult(failure_code=code, diagnostics=diagnostics, log_id=build_root.name)

        inspect_log = build_root / "base-inspect.log"
        code, error = self._run(["docker", "image", "inspect", "--format", "{{.Id}}",
                                 base_image.resolved_reference], inspect_log, deadline)
        if error or code != 0:
            return failed(error or "BUILD_FAILED")
        base_id = self._tail(inspect_log).strip()
        if re.fullmatch(r"sha256:[0-9a-f]{64}", base_id) is None:
            return failed("VERIFICATION_FAILED")
        context = build_root / "context"
        context.mkdir(mode=0o700)
        with (context / "requirements.txt").open("x", encoding="utf-8") as stream:
            stream.write("\n".join(requirements) + "\n")
        recipe = (f"FROM {base_id}\nUSER root\n"
                  f"LABEL labbio.environment.build={build_root.name}\n"
                  "COPY requirements.txt /opt/labbio-environment/requirements.txt\n")
        if requirements:
            recipe += ("RUN python -m pip download --disable-pip-version-check --no-input --progress-bar off "
                       "--only-binary=:all: --dest /opt/labbio-environment/wheels "
                       "-r /opt/labbio-environment/requirements.txt\n"
                       "RUN python -m pip install --disable-pip-version-check --no-input --no-index "
                       "--find-links=/opt/labbio-environment/wheels -r /opt/labbio-environment/requirements.txt\n")
        recipe += "RUN python -m pip check\n"
        with (context / "Dockerfile").open("x", encoding="utf-8") as stream:
            stream.write(recipe)
        image_file = build_root / "image-id.txt"
        argv = ["docker", "build", "--pull=false", "--force-rm", "--memory", "4g",
                "--memory-swap", "4g", "--iidfile", str(image_file),
                "--network", "host" if self.proxy_url else "default"]
        if self.proxy_url:
            # Docker's predefined proxy args are not ARG/ENV instructions or image history.
            argv += ["--build-arg", "HTTP_PROXY=" + self.proxy_url,
                     "--build-arg", "HTTPS_PROXY=" + self.proxy_url]
        build_log = build_root / "build.log"
        code, error = self._run([*argv, str(context)], build_log, deadline)
        if error or code != 0:
            self._cleanup(build_root)
            text = self._tail(build_log)
            failure_code = error or self._diagnose(text)
            return failed(failure_code, self._diagnostics(text, failure_code))
        if not image_file.is_file() or image_file.stat().st_size > 128:
            return failed("VERIFICATION_FAILED")
        image_id = image_file.read_text().strip()
        if re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) is None:
            return failed("VERIFICATION_FAILED")
        probe_name = "labbio-env-check-" + build_root.name
        probe_log = build_root / "verification.log"
        code, error = self._run(["docker", "run", "--rm", "--pull", "never", "--name", probe_name,
            "--network", "none", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--memory", "4g", "--memory-swap", "4g", "--pids-limit", "128",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m", "--entrypoint", "python",
            image_id, "-I", "-c", _PROBE, json.dumps(modules)], probe_log, deadline)
        if error or code != 0:
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
            return EnvironmentBuildResult(image_id=image_id, installed_packages=inventory["installed_packages"],
                                          available_python_modules=modules, log_id=build_root.name)
        except (ValueError, KeyError, TypeError):
            return failed("VERIFICATION_FAILED")
