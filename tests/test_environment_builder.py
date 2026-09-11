"""Fixed-recipe Docker backend boundaries without contacting a Docker daemon."""

import json
import stat
import subprocess
from types import SimpleNamespace

import pytest

from labbioagentos import ApprovedImage, ExecutionRuntime
from labbioagentos.execution import environment_builder
from labbioagentos.execution.environment_builder import DockerEnvironmentBuilder


BASE_ID = "sha256:" + "b" * 64
IMAGE_ID = "sha256:" + "c" * 64
REQUEST_HASH = "d" * 64


@pytest.fixture
def backend(tmp_path, monkeypatch):
    root = tmp_path / ("a" * 32)
    root.mkdir(mode=0o700)
    boundary = SimpleNamespace(
        root=root,
        builder=DockerEnvironmentBuilder(proxy_url="http://127.0.0.1:12199"),
        base=ApprovedImage(
            key="base", reference="example/base", digest="sha256:" + "e" * 64,
            runtime=ExecutionRuntime.PYTHON,
        ),
        calls=[],
        responses={
            "base-inspect.log": (0, None, BASE_ID + "\n"),
            "verification.log": (0, None, json.dumps({
                "installed_packages": {"examplepkg": "2.0"}, "failed_imports": [],
            })),
        },
    )

    def run(argv, path, deadline):
        boundary.calls.append(argv)
        code, error, output = boundary.responses.get(path.name, (0, None, ""))
        path.write_text(output, encoding="utf-8")
        if path.name == "build.log" and code == 0 and error is None:
            (root / "image-id.txt").write_text(IMAGE_ID + "\n", encoding="utf-8")
        return code, error

    monkeypatch.setattr(boundary.builder, "_run", run)
    return boundary


async def build(boundary):
    return await boundary.builder.build(
        base_image=boundary.base, requirements=("ExamplePkg == 2.0",),
        import_modules=("examplepkg",), build_root=boundary.root,
        request_hash=REQUEST_HASH,
    )


@pytest.mark.asyncio
async def test_fixed_wheel_recipe_uses_inspected_identity_and_no_data_mounts(backend):
    result = await build(backend)
    assert result.image_id == IMAGE_ID
    assert result.installed_packages == {"examplepkg": "2.0"}
    assert result.available_python_modules == ("examplepkg",)
    context = backend.root / "context"
    assert {path.name for path in context.iterdir()} == {"Dockerfile", "requirements.txt"}
    assert (context / "requirements.txt").read_text() == "examplepkg==2.0\n"
    recipe = (context / "Dockerfile").read_text()
    assert recipe.startswith(f"FROM {BASE_ID}\n")
    assert backend.base.resolved_reference not in recipe
    assert "--only-binary=:all:" in recipe
    assert "pip download" in recipe and "pip install" in recipe
    assert "--no-index --find-links=/opt/labbio-environment/wheels" in recipe
    assert recipe.endswith("RUN python -m pip check\n")
    assert not any(line.startswith(("ADD ", "ENV ", "ARG ")) for line in recipe.splitlines())
    inspect, docker_build, probe = backend.calls
    assert inspect == ["docker", "image", "inspect", "--format", "{{.Id}}", backend.base.resolved_reference]
    assert docker_build[-1] == str(context)
    assert "--pull=false" in docker_build and "--force-rm" in docker_build
    assert docker_build[docker_build.index("--network") + 1] == "host"
    assert "HTTP_PROXY=http://127.0.0.1:12199" in docker_build
    assert probe[probe.index("--network") + 1] == "none"
    assert "--read-only" in probe and probe[probe.index("--cap-drop") + 1] == "ALL"
    assert probe[probe.index("--security-opt") + 1] == "no-new-privileges"
    assert probe[probe.index("--pull") + 1] == "never"
    assert probe[-5:] == [IMAGE_ID, "-I", "-c", environment_builder._PROBE, '["examplepkg"]']
    for argv in backend.calls:
        assert not {"--mount", "--volume", "-v", "--volumes-from", "--env-file", "--secret", "-e"}.intersection(argv)


@pytest.mark.asyncio
@pytest.mark.parametrize("identity", ("python:latest", "sha256:short", BASE_ID + "\n" + IMAGE_ID))
async def test_invalid_inspected_identity_stops_before_build(backend, identity):
    backend.responses["base-inspect.log"] = (0, None, identity)
    result = await build(backend)
    assert result.failure_code == "VERIFICATION_FAILED"
    assert result.image_id is None
    assert len(backend.calls) == 1
    assert not (backend.root / "context").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("log, code, diagnostics", (
    (
        "ERROR: No matching distribution found for ExamplePkg==999.0\n",
        "PIP_NO_MATCH",
        ({"code": "PIP_NO_MATCH", "requirement": "examplepkg==999.0"},),
    ),
    (
        "ERROR: ResolutionImpossible: conflicting dependencies\n"
        "examplepkg 2.0 depends on shared-dependency<2,>=1\n"
        "otherpkg 3.0 depends on shared-dependency>=2\n",
        "PIP_RESOLUTION_CONFLICT",
        (
            {"code": "PIP_RESOLUTION_CONFLICT", "package": "examplepkg", "version": "2.0", "dependency": "shared-dependency<2,>=1"},
            {"code": "PIP_RESOLUTION_CONFLICT", "package": "otherpkg", "version": "3.0", "dependency": "shared-dependency>=2"},
        ),
    ),
))
async def test_failed_build_preserves_safe_package_and_transitive_constraint_facts(backend, log, code, diagnostics):
    backend.responses["build.log"] = (1, None, log)
    result = await build(backend)
    assert result.failure_code == code
    assert result.diagnostics == diagnostics
    assert result.image_id is None and result.installed_packages == {}
    assert not any(argv[:2] == ["docker", "run"] for argv in backend.calls)


def test_failure_diagnostics_are_bounded_without_copying_log_text():
    log = "\n".join(f"No matching distribution found for example{index}==999" for index in range(20))
    diagnostics = DockerEnvironmentBuilder._diagnostics(log, "PIP_NO_MATCH")
    assert len(diagnostics) == 8
    assert all(set(item) == {"code", "requirement"} for item in diagnostics)
    assert DockerEnvironmentBuilder._diagnostics("arbitrary opaque output", "BUILD_FAILED") == ({"code": "BUILD_FAILED"},)


@pytest.mark.asyncio
@pytest.mark.parametrize("identifiers", ("0123456789ab\nabcdef012345\n", "", "0123456789ab /unowned/path\n"))
async def test_build_timeout_cleanup_only_uses_this_attempt_label_and_valid_container_ids(backend, identifiers):
    backend.responses["build.log"] = (None, "BUILD_TIMEOUT", "")
    backend.responses["cleanup-list.log"] = (0, None, identifiers)
    result = await build(backend)
    assert result.failure_code == "BUILD_TIMEOUT" and result.image_id is None
    assert backend.calls[2] == [
        "docker", "ps", "-aq", "--filter", "label=labbio.environment.build=" + backend.root.name,
    ]
    if identifiers.startswith("0123456789ab\n"):
        assert backend.calls[3:] == [["docker", "rm", "--force", "0123456789ab", "abcdef012345"]]
    else:
        assert len(backend.calls) == 3


@pytest.mark.asyncio
async def test_probe_timeout_removes_only_the_generated_probe_name(backend):
    backend.responses["verification.log"] = (None, "BUILD_TIMEOUT", "")
    result = await build(backend)
    assert result.failure_code == "BUILD_TIMEOUT" and result.image_id is None
    probe = backend.calls[2]
    name = "labbio-env-check-" + backend.root.name
    assert probe[probe.index("--name") + 1] == name
    assert backend.calls[3:] == [["docker", "rm", "--force", name]]


def test_subprocess_timeout_kills_its_process_and_filters_host_secrets(tmp_path, monkeypatch):
    secret_names = ("OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY", "PIP_INDEX_URL", "HTTP_PROXY", "HTTPS_PROXY")
    for name in secret_names:
        monkeypatch.setenv(name, "SYNTHETIC_PRIVATE_VALUE")
    monkeypatch.setenv("DOCKER_BUILDKIT", "1")
    captured = {}
    waits = []

    class Process:
        killed = False

        def wait(self, timeout):
            waits.append(timeout)
            if timeout == 0.2:
                raise subprocess.TimeoutExpired("synthetic-docker", timeout)
            return -9

        def kill(self):
            self.killed = True

    process = Process()

    def popen(argv, **kwargs):
        captured.update(argv=argv, **kwargs)
        return process

    monkeypatch.setattr(environment_builder.subprocess, "Popen", popen)
    log = tmp_path / "process.log"
    result = DockerEnvironmentBuilder()._run(["docker", "build", "synthetic-context"], log, deadline=0)
    assert result == (None, "BUILD_TIMEOUT")
    assert process.killed and waits == [0.2, 10]
    assert captured["shell"] is False
    assert captured["env"]["DOCKER_BUILDKIT"] == "0"
    assert not set(secret_names).intersection(captured["env"])
    assert "SYNTHETIC_PRIVATE_VALUE" not in captured["env"].values()
    assert stat.S_IMODE(log.stat().st_mode) == 0o600
