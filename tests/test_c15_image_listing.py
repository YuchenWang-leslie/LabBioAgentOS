"""C15 cluster validation, round 5: the model sees every selectable image with its conda facts.

A live Agent did not pick the samtools image, because the list showed it with an unknown
inventory and called itself a list of Python images (MOHAN CUI, 2026-11-21). These tests
follow the model-visible list through tool arguments to the next legal execution choice.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from labbioagentos import ApprovedImage, ApprovedImageRegistry
from labbioagentos.execution.catalog import CatalogStore, ExecutionBackendName, ImageCatalog
from labbioagentos.execution.environments import EnvironmentRequestError, ImageListing
from labbioagentos.execution.inventory import InventoryOrigin
from labbioagentos.execution.models import ExecutionRuntime
from labbioagentos import LabBioRuntimeToolSet, WorkflowStage
from native_test_provider import LocalProvider

from test_c15_promotion import _record
from test_c7_4_artifact_query_audit import artifact_query_boundary  # noqa: F401

PYTHON = ApprovedImage(key="py-base", reference="sha256:" + "a" * 64, entry_points={"PYTHON": ("python",)},
                       installed_packages={"pip": "24.0"})


def _catalog(tmp_path, owner="admin"):
    """A configured Python image and the member's own SHELL tool image with a conda inventory."""
    from labbioagentos.execution.catalog import InventorySnapshot

    personal = CatalogStore(tmp_path / "personal").initialize()
    personal.add_record(_record(key="samtools-1-21", owner=owner).model_copy(update={
        "entry_points": {ExecutionRuntime.SHELL: ("sh",)},
        "inventory": InventorySnapshot(origin=InventoryOrigin.CONDA_META,
                                       packages={"samtools": "1.21", "htslib": "1.21", "zlib": "1.3.1"}),
        "purpose": "Alignment and BAM tools."}))
    catalog = ImageCatalog(owner_user_id=owner, backend=ExecutionBackendName.DOCKER, configured_keys=("py-base",),
                           personal=personal)
    registry = ApprovedImageRegistry((PYTHON, *catalog.admitted_images()))
    return catalog, registry


def _items(result):
    return {item["image_key"]: item for item in result["items"]}


def test_a_conda_inventory_is_listed_and_known(tmp_path):
    catalog, registry = _catalog(tmp_path)
    items = _items(ImageListing(registry, owner_user_id="admin", catalog=catalog).list())

    tool = items["samtools-1-21"]
    assert tool["runtimes"] == ["SHELL"] and tool["inventory_origin"] == "CONDA_META"
    assert tool["conda_packages"] == {"htslib": "1.21", "samtools": "1.21", "zlib": "1.3.1"}
    assert tool["conda_package_count"] == 3 and tool["inventory_known"] is True
    assert tool["installed_packages"] == {} and tool["package_count"] == 0  # Separate namespaces.
    assert items["py-base"]["conda_packages"] == {} and items["py-base"]["inventory_known"] is True
    text = json.dumps(items)
    assert "sha256:" not in text and "/" not in text.replace("conda-forge/", "")  # No references or paths.


@pytest.mark.parametrize("names, expected", [
    (["samtools"], {"samtools-1-21": "1.21", "py-base": None}),
    (["samtools=1.21"], {"samtools-1-21": "1.21", "py-base": None}),  # A version part is ignored.
    (["bwa"], {"samtools-1-21": None, "py-base": None}),
])
def test_named_conda_packages_report_their_recorded_versions(tmp_path, names, expected):
    catalog, registry = _catalog(tmp_path)
    items = _items(ImageListing(registry, owner_user_id="admin", catalog=catalog).list(conda_packages=tuple(names)))
    for key, version in expected.items():
        name = names[0].split("=")[0]
        assert items[key]["requested_conda_packages"] == [name]
        assert items[key]["conda_matching_facts"] == [{"package": name, "installed_version": version}]
        assert items[key]["conda_packages"] == ({name: version} if version else {})


def test_without_requirements_no_image_claims_to_satisfy_them(tmp_path):
    """Cluster round 6: an empty request read as satisfied, even for an unknown inventory."""
    catalog, registry = _catalog(tmp_path)
    unknown = ApprovedImage(key="cuda-12-6", reference="sha256:" + "c" * 64, entry_points={"PYTHON": ("python",)})
    registry = ApprovedImageRegistry((*registry.list(), unknown))
    items = _items(ImageListing(registry, owner_user_id="admin", catalog=catalog).list())
    assert items["cuda-12-6"]["inventory_known"] is False
    assert {key: item["requirements_satisfied"] for key, item in items.items()} == dict.fromkeys(items, None)


@pytest.mark.parametrize("requirement, expected", [
    ("pip", {"py-base": True, "samtools-1-21": None, "cuda-12-6": None}),
    ("pip>=99", {"py-base": False, "samtools-1-21": None, "cuda-12-6": None}),
    (("pip", "six"), {"py-base": None, "samtools-1-21": None, "cuda-12-6": None}),  # One fact unknown.
])
def test_with_requirements_the_answer_is_unchanged(tmp_path, requirement, expected):
    catalog, registry = _catalog(tmp_path)
    unknown = ApprovedImage(key="cuda-12-6", reference="sha256:" + "c" * 64, entry_points={"PYTHON": ("python",)})
    registry = ApprovedImageRegistry((*registry.list(), unknown))
    items = _items(ImageListing(registry, owner_user_id="admin", catalog=catalog).list(
        requirement if isinstance(requirement, tuple) else (requirement,)))
    assert {key: items[key]["requirements_satisfied"] for key in expected} == expected


@pytest.mark.parametrize("value", ["https://example.org/x.conda", "conda-forge::samtools", "samtools --force"])
def test_a_conda_name_that_is_not_a_package_is_refused(tmp_path, value):
    catalog, registry = _catalog(tmp_path)
    with pytest.raises(EnvironmentRequestError) as raised:
        ImageListing(registry, owner_user_id="admin", catalog=catalog).list(conda_packages=(value,))
    assert raised.value.code == "INVALID_CONDA_PACKAGES"


# --- Through the model-facing tool ---


def _toolset(boundary, listing):
    _, binding, _, original = boundary
    return LabBioRuntimeToolSet(replace(
        binding, stage_id=WorkflowStage.PLAN, actor_profile_key="planner", actor_agent_name="PlanAgent",
        capability_allowlist=("environment_list",),
    ), replace(original.services, environment_service=None, image_listing=listing))


@pytest.mark.asyncio
async def test_without_environment_builds_the_tool_still_lists_every_selectable_image(
        artifact_query_boundary, tmp_path):
    _, binding, _, _ = artifact_query_boundary
    catalog, registry = _catalog(tmp_path, owner=binding.principal.user_id)
    toolset = _toolset(artifact_query_boundary, ImageListing(registry, owner_user_id=binding.principal.user_id,
                                                             catalog=catalog))

    result = await toolset.environment_list(conda_packages=["samtools"])

    assert result["success"] is True
    tool = next(item for item in result["data"]["items"] if item["image_key"] == "samtools-1-21")
    assert tool["conda_matching_facts"] == [{"package": "samtools", "installed_version": "1.21"}]
    # The next legal action: the listed key with the runtime it declares.
    assert registry.resolve("samtools-1-21", runtime=ExecutionRuntime.SHELL).key == "samtools-1-21"


@pytest.mark.asyncio
async def test_another_users_listing_is_refused(artifact_query_boundary, tmp_path):
    catalog, registry = _catalog(tmp_path, owner="someone-else")
    toolset = _toolset(artifact_query_boundary, ImageListing(registry, owner_user_id="someone-else",
                                                             catalog=catalog))
    result = await toolset.environment_list()
    assert result["success"] is False and result["error"]["error_code"] == "AUTHORIZATION_DENIED"


@pytest.mark.asyncio
async def test_the_model_reads_that_every_listed_image_is_a_choice(artifact_query_boundary, tmp_path):
    from labbioagentos.runtime.contracts import RuntimeExecutionCapabilityView

    _, binding, _, _ = artifact_query_boundary
    catalog, registry = _catalog(tmp_path, owner=binding.principal.user_id)
    provider = LocalProvider(_toolset(artifact_query_boundary, ImageListing(
        registry, owner_user_id=binding.principal.user_id, catalog=catalog)))
    schemas = {item.name: item.inputSchema for item in await provider.list_tools()}
    listing = schemas["environment_list"]
    for fact in ("SHELL", "conda", "lab's", "every returned image_key", "registered inventory"):
        assert fact in listing["description"]
    assert "Python images" not in listing["description"]
    assert "conda package names" in listing["parameters"]["properties"]["conda_packages"]["description"]
    default = RuntimeExecutionCapabilityView.model_fields["image_key"].description
    assert "Default" in default and "environment_list" in default and "SHELL" in default


# --- Which stages offer the tools ---


def _stages(tmp_path, extra=""):
    from labbioagentos import cli
    from labbioagentos.local_config import build_application, load_settings
    from test_c15_lab_area import _settings_file
    from test_c15_promotion import _area

    _area(tmp_path)
    path = _settings_file(tmp_path)
    path.write_text(path.read_text() + extra)
    settings = load_settings(path)
    application = build_application(settings, tmp_path / "run", load_provider=False)
    try:
        return ({spec.stage_id: spec.capability_allowlist for spec in application.configuration.stage_assemblies},
                application.capability_services)
    finally:
        cli._close(application)


def test_a_lab_without_environment_builds_offers_listing_but_not_building(tmp_path):
    stages, services = _stages(tmp_path)
    assert "environment_list" in stages[WorkflowStage.PLAN] and "environment_list" in stages[WorkflowStage.EXECUTE]
    assert "environment_build" not in stages[WorkflowStage.EXECUTE]
    assert services.environment_service is None and isinstance(services.image_listing, ImageListing)


def test_with_environment_builds_the_environment_service_lists(tmp_path):
    stages, services = _stages(tmp_path, '[environment]\nroot = "envs"\n')
    assert "environment_build" in stages[WorkflowStage.EXECUTE]
    assert services.environment_service is not None and services.image_listing is None


def test_one_configured_image_and_no_lab_offer_no_listing(tmp_path):
    from labbioagentos.local_config import build_application, load_settings
    from labbioagentos import cli
    from test_c15_image_identity import _write

    one = """
[[execution.images]]
key = "python-base"
reference = "sha256:{a}"
""".format(a="a" * 64)
    settings = load_settings(_write(tmp_path, images=one))
    application = build_application(settings, tmp_path / "run", load_provider=False)
    try:
        stages = {spec.stage_id: spec.capability_allowlist for spec in application.configuration.stage_assemblies}
    finally:
        cli._close(application)
    assert "environment_list" not in stages[WorkflowStage.PLAN]


def test_several_configured_images_and_no_lab_offer_listing(tmp_path):
    from labbioagentos import cli
    from labbioagentos.local_config import build_application, load_settings
    from test_c15_image_identity import _write

    settings = load_settings(_write(tmp_path))  # Two configured images, no lab.
    application = build_application(settings, tmp_path / "run", load_provider=False)
    try:
        stages = {spec.stage_id: spec.capability_allowlist for spec in application.configuration.stage_assemblies}
        listing = application.capability_services.image_listing
    finally:
        cli._close(application)
    assert "environment_list" in stages[WorkflowStage.PLAN] and listing.owner_user_id == "u"
    assert [item["image_key"] for item in listing.list()["items"]] == ["python-base", "tools"]
