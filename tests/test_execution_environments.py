"""Environment discovery and building remain explicit, bounded, and user-owned."""

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from labbioagentos import ApprovedImage, ApprovedImageRegistry, ExecutionRuntime


DIGEST = "sha256:" + "b" * 64
PRIVATE = "SYNTHETIC_PRIVATE_TOKEN"


def _image(key="base", digest="sha256:" + "a" * 64, packages=None):
    return ApprovedImage(
        key=key, reference=digest, runtime=ExecutionRuntime.PYTHON,
        available_python_modules=("basepkg",),
        installed_packages={"basepkg": "1.0"} if packages is None else packages,
    )


class RecordingBuilder:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def build(self, **kwargs):
        from labbioagentos.execution.environments import EnvironmentBuildResult

        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result or EnvironmentBuildResult(
            image_id=DIGEST, installed_packages={"basepkg": "1.0", "examplepkg": "2.0"},
            available_python_modules=kwargs["import_modules"],
        )


@pytest.fixture
def environment(tmp_path):
    from labbioagentos.execution.environments import EnvironmentService

    base = _image()
    registry = ApprovedImageRegistry((base,))
    builder = RecordingBuilder()
    root = tmp_path / "environments"
    service = EnvironmentService(root=root, image_registry=registry, builder=builder, owner_user_id="user-one")
    return SimpleNamespace(root=root, base=base, registry=registry, builder=builder, service=service)


def test_registry_registration_is_idempotent_and_never_overwrites_identity():
    base = _image()
    registry = ApprovedImageRegistry((base,))
    registry.register(base)
    assert registry.list() == (base,)
    with pytest.raises(ValueError):
        registry.register(_image(digest=DIGEST))
    assert registry.resolve("base") == base
    registry.register(_image(key="aaa", digest=DIGEST))
    assert [image.key for image in registry.list()] == ["aaa", "base"]


@pytest.mark.parametrize("packages", (
    {"package @ https://example.invalid": "1.0"},
    {"package": "/PRIVATE_PATH"},
    {"package": "1.0\nTOKEN"},
    {"package": "a" * 129},
    {f"package{index}": "1.0" for index in range(2049)},
))
def test_verified_package_inventory_rejects_unsafe_or_unbounded_values(packages):
    with pytest.raises(ValidationError):
        _image(packages=packages)


@pytest.mark.asyncio
async def test_verified_build_registers_exact_image_and_normalized_cache_without_mutating_base(environment):
    boundary = environment
    before = boundary.base.model_dump(mode="json")
    response = await boundary.service.build_environment(
        "base", ("examplepkg == 2.0", "basepkg==1.0"), ("examplepkg",),
    )
    assert response["status"] == "SUCCEEDED" and response["cache_hit"] is False
    image = boundary.registry.resolve(response["image_key"])
    assert image.reference == response["image_reference"] == DIGEST
    assert image.available_python_modules == ("basepkg", "examplepkg")
    assert image.installed_packages["examplepkg"] == "2.0"
    assert image.network_allowed is False
    assert len(boundary.builder.calls) == 1
    request = boundary.builder.calls[0]
    assert request["base_image"] == boundary.base
    assert request["requirements"] == ("basepkg==1.0", "examplepkg==2.0")
    assert request["build_root"].is_relative_to(boundary.root)
    cached = await boundary.service.build_environment(
        "base", ("basepkg==1.0", "examplepkg==2.0", "examplepkg==2.0"), ("examplepkg",),
    )
    assert cached["status"] == "SUCCEEDED" and cached["cache_hit"] is True
    assert cached["image_key"] == response["image_key"]
    assert len(boundary.builder.calls) == 1
    assert boundary.base.model_dump(mode="json") == before
    assert boundary.registry.resolve("base").model_dump(mode="json") == before
    assert str(boundary.root) not in json.dumps(response)


@pytest.mark.asyncio
async def test_restart_recovers_exact_image_and_never_rebuilds_a_valid_cache(environment):
    from labbioagentos.execution.environments import EnvironmentService

    first = await environment.service.build_environment("base", ("examplepkg==2.0",), ("examplepkg",))
    registry = ApprovedImageRegistry((environment.base,))
    builder = RecordingBuilder(error=AssertionError("Unexpected rebuild"))
    reopened = EnvironmentService(environment.root, registry, builder, "user-one")
    assert registry.resolve(first["image_key"]).reference == DIGEST
    second = await reopened.build_environment("base", ("examplepkg==2.0",), ("examplepkg",))
    assert second["request_hash"] == first["request_hash"]
    assert second["cache_hit"] is True
    assert builder.calls == []


@pytest.mark.asyncio
async def test_discovery_keeps_nonmatching_environments_visible_and_selects_nothing(environment):
    result = await environment.service.build_environment("base", ("examplepkg==2.0",))
    response = environment.service.list_environments(("examplepkg>=3.0",))
    assert {item["image_key"] for item in response["items"]} == {"base", result["image_key"]}
    by_key = {item["image_key"]: item for item in response["items"]}
    assert by_key["base"]["requirements_satisfied"] is None
    assert by_key[result["image_key"]]["requirements_satisfied"] is False
    assert by_key[result["image_key"]]["matching_facts"][0]["version_satisfied"] is False
    assert len(environment.builder.calls) == 1
    matching = environment.service.list_environments(("examplepkg>=1.0",))
    assert sum(item["requirements_satisfied"] is True for item in matching["items"]) == 1
    assert not any("selected" in key for key in matching)


@pytest.mark.asyncio
@pytest.mark.parametrize("requirement", (
    "https://example.invalid/pkg.whl", "examplepkg @ https://example.invalid/pkg.whl",
    "git+https://example.invalid/repo", "/PRIVATE_PATH/package", "../package",
    "./package.whl", "package.whl", "-e package", "--index-url=https://example.invalid",
    'examplepkg; python_version >= "3.11"', "examplepkg\notherpkg", "examplepkg\x00",
    "examplepkg\r", "", 123, True,
))
async def test_requirements_cannot_be_urls_paths_options_markers_or_control_text(environment, requirement):
    with pytest.raises(ValueError) as error:
        await environment.service.build_environment("base", (requirement,))
    assert "PRIVATE_PATH" not in str(error.value)
    assert "https://" not in str(error.value)
    assert environment.builder.calls == []
    assert environment.registry.list() == (environment.base,)


@pytest.mark.asyncio
async def test_package_extras_and_version_constraints_are_supported_without_assuming_base_extras(environment):
    response = await environment.service.build_environment("base", ("ExamplePkg[extra]>=1.0,<3",))
    assert response["status"] == "SUCCEEDED"
    assert response["requirements_satisfied"] is True
    assert response["matching_facts"][0]["extras_verified"] is True
    assert environment.builder.calls[0]["requirements"] == ("examplepkg[extra]<3,>=1.0",)
    assert environment.service.list_environments(("basepkg[extra]",))["items"][0]["requirements_satisfied"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("modules", (("os;print(1)",), ("../private",), ("pkg\nname",), ("p" * 129,), tuple(f"module{i}" for i in range(65))))
async def test_import_requests_are_bounded_module_names(environment, modules):
    with pytest.raises(ValueError):
        await environment.service.build_environment("base", ("examplepkg",), modules)
    assert environment.builder.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("requirements", (tuple(f"package{i}" for i in range(65)), ("x" * 257,)))
async def test_requirement_inventory_is_bounded_before_build(environment, requirements):
    with pytest.raises(ValueError):
        await environment.service.build_environment("base", requirements)
    assert environment.builder.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ("reported", "exception", "unknown_code", "bad_digest", "wrong_version", "missing_import"))
async def test_failed_or_unverified_builds_never_register_and_return_safe_feedback(environment, failure):
    from labbioagentos.execution.environments import EnvironmentBuildResult

    unsafe = f"https://example.invalid/?token={PRIVATE} /PRIVATE_PATH raw_build_log"
    if failure == "exception":
        environment.builder.error = RuntimeError(unsafe)
    elif failure in {"reported", "unknown_code"}:
        environment.builder.result = EnvironmentBuildResult(
            failure_code="PIP_RESOLUTION_CONFLICT" if failure == "reported" else unsafe,
            diagnostics=({"code": "PIP_RESOLUTION_CONFLICT", "message": unsafe, "raw_log": unsafe},),
            log_id=unsafe,
        )
    else:
        environment.builder.result = EnvironmentBuildResult(
            image_id="python:latest" if failure == "bad_digest" else DIGEST,
            installed_packages={"examplepkg": "0.5" if failure == "wrong_version" else "2.0"},
            available_python_modules=() if failure == "missing_import" else ("basepkg", "examplepkg"),
        )
    response = await environment.service.build_environment("base", ("examplepkg>=1.0",), ("examplepkg",))
    assert response["status"] == "FAILED"
    assert response["failure_code"]
    assert response["diagnostics"]
    assert environment.registry.list() == (environment.base,)
    assert list(environment.service.records_root.glob("*.json")) == []
    encoded = json.dumps(response)
    for value in (PRIVATE, "https://", "/PRIVATE_PATH", "raw_build_log", str(environment.root)):
        assert value not in encoded
    receipt = environment.service.attempts_root / response["log_id"] / "receipt.json"
    assert json.loads(receipt.read_text(encoding="utf-8")) == response


@pytest.mark.asyncio
async def test_cache_scope_cannot_be_opened_as_another_user(environment):
    from labbioagentos.execution.environments import EnvironmentRequestError, EnvironmentService

    await environment.service.build_environment("base", ("examplepkg==2.0",))
    registry = ApprovedImageRegistry((environment.base,))
    with pytest.raises(EnvironmentRequestError) as error:
        EnvironmentService(environment.root, registry, RecordingBuilder(), "user-two")
    assert error.value.code == "ENVIRONMENT_SCOPE_INVALID"
    assert registry.list() == (environment.base,)


@pytest.mark.asyncio
async def test_distinct_user_roots_do_not_reuse_each_others_cached_builds(environment, tmp_path):
    from labbioagentos.execution.environments import EnvironmentService

    await environment.service.build_environment("base", ("examplepkg==2.0",))
    builder = RecordingBuilder()
    registry = ApprovedImageRegistry((environment.base,))
    other = EnvironmentService(tmp_path / "other-user", registry, builder, "user-two")
    assert other.list_environments()["available_count"] == 1
    response = await other.build_environment("base", ("examplepkg==2.0",))
    assert response["cache_hit"] is False
    assert len(builder.calls) == 1


def test_pagination_and_inventory_views_are_bounded(environment):
    for index in range(23):
        environment.registry.register(_image(key=f"image-{index:02}", packages={f"package{i:03}": "1.0" for i in range(125)}))
    page = environment.service.list_environments(offset=1, limit=10)
    assert 1 <= page["returned_count"] == len(page["items"]) <= 10
    assert page["available_count"] == 24
    assert page["next_offset"] == 1 + len(page["items"])
    assert all(len(item["installed_packages"]) == 40 and item["inventory_truncated"] for item in page["items"])
    assert all(item["package_count"] == 125 for item in page["items"])
    last = environment.service.list_environments(offset=21, limit=10)
    assert last["returned_count"] == 3 and last["next_offset"] is None
    assert environment.service.list_environments(offset=100)["items"] == []


def test_unknown_package_inventory_is_not_reported_as_proven_absence(environment):
    environment.registry.register(_image(key="unknown", packages={}))
    response = environment.service.list_environments(("examplepkg>=1.0",))
    item = next(item for item in response["items"] if item["image_key"] == "unknown")
    assert item["inventory_known"] is False
    assert item["requirements_satisfied"] is None
    assert item["matching_facts"][0]["version_satisfied"] is None


def test_module_inventory_is_bounded_without_rewriting_registered_facts(environment):
    modules = tuple(f"module{index}" for index in range(130))
    image = _image(key="many-modules").model_copy(update={"available_python_modules": modules})
    environment.registry.register(image)
    response = environment.service.list_environments()
    item = next(item for item in response["items"] if item["image_key"] == image.key)
    assert len(item["available_python_modules"]) == 64
    assert item["module_count"] == 130 and item["modules_truncated"] is True
    assert environment.registry.resolve(image.key).available_python_modules == modules


@pytest.mark.asyncio
async def test_unknown_base_image_is_rejected_without_selecting_available_base(environment):
    from labbioagentos.execution.environments import EnvironmentRequestError

    with pytest.raises(EnvironmentRequestError) as error:
        await environment.service.build_environment("unknown-image", ("examplepkg",))
    assert error.value.code == "UNKNOWN_IMAGE"
    assert environment.builder.calls == []


@pytest.mark.parametrize("values", ({"limit": 0}, {"limit": 11}, {"limit": True}, {"offset": -1}, {"offset": False}, {"offset": "0"}))
def test_invalid_pagination_does_not_return_an_unbounded_inventory(environment, values):
    with pytest.raises(ValueError):
        environment.service.list_environments(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", ("invalid_json", "missing_image", "wrong_owner", "missing_owner", "symlink_record"))
async def test_corrupted_persisted_cache_fails_closed_on_restart(environment, corruption):
    from labbioagentos.execution.environments import EnvironmentRequestError, EnvironmentService

    response = await environment.service.build_environment("base", ("examplepkg==2.0",))
    record = environment.service.records_root / (response["request_hash"] + ".json")
    if corruption == "invalid_json":
        record.write_text("{ invalid " + PRIVATE, encoding="utf-8")
    elif corruption == "missing_owner":
        (environment.root / "owner.json").rename(environment.root / "saved-owner.json")
    elif corruption == "symlink_record":
        renamed = record.with_suffix(".saved")
        record.rename(renamed)
        record.symlink_to(renamed)
    else:
        payload = json.loads(record.read_text(encoding="utf-8"))
        if corruption == "missing_image":
            payload.pop("image")
        else:
            payload["owner_user_id"] = "different-user"
        record.write_text(json.dumps(payload), encoding="utf-8")
    registry = ApprovedImageRegistry((environment.base,))
    with pytest.raises(EnvironmentRequestError) as error:
        EnvironmentService(environment.root, registry, RecordingBuilder(), "user-one")
    assert error.value.code == "ENVIRONMENT_CACHE_INVALID"
    assert PRIVATE not in str(error.value)
    assert registry.list() == (environment.base,)


@pytest.mark.asyncio
async def test_known_cache_record_disappearance_is_not_a_hidden_rebuild(environment):
    from labbioagentos.execution.environments import EnvironmentRequestError

    response = await environment.service.build_environment("base", ("examplepkg==2.0",))
    record = environment.service.records_root / (response["request_hash"] + ".json")
    record.rename(record.with_suffix(".saved"))
    with pytest.raises(EnvironmentRequestError) as error:
        await environment.service.build_environment("base", ("examplepkg==2.0",))
    assert error.value.code == "ENVIRONMENT_CACHE_INVALID"
    assert len(environment.builder.calls) == 1
