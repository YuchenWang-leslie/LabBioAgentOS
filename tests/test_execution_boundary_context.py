"""The Agent receives host execution facts before choosing its program."""

import pytest

from labbioagentos import RuntimeExecutionCapabilityView, cli
from labbioagentos.local_config import build_application, load_settings
from labbioagentos.runtime.native_tools import build_labbio_tool_registry
from test_apptainer_configuration import _write
from test_c12_execution_capability_contract import _trusted_view
from test_local_configuration import settings_file  # noqa: F401


@pytest.mark.parametrize("backend,placement", [
    ("apptainer", "local"), ("apptainer", "scheduler"), ("docker", "local"),
])
def test_backend_and_host_managed_boundary_are_projected_from_actual_adapters(
    tmp_path, settings_file, backend, placement,
):
    settings = load_settings(settings_file if backend == "docker" else _write(
        tmp_path, launcher="" if placement == "local" else None,
        enforcement="systemd-affinity" if placement == "local" else "scheduler",
    ))
    application = build_application(settings, tmp_path / "run", load_provider=False)
    try:
        view = application.execution_capability.model_dump(mode="json")
        assert view.get("backend") == backend
        assert view.get("placement") == placement
        assert view.get("program_execution") == "HOST_MANAGED_CONTAINER"
        assert view["scope"] == "RUN_CONFIGURATION"  # Configuration, not a successful preflight.
        assert "sif_store" not in view and "binary" not in view
    finally:
        cli._close(application)


def test_old_snapshot_does_not_invent_a_backend_or_container_boundary():
    data = _trusted_view().model_dump()
    for name in ("backend", "placement", "program_execution"):
        data.pop(name, None)
    view = RuntimeExecutionCapabilityView.model_validate(data)
    assert getattr(view, "backend", "MISSING") is None
    assert getattr(view, "placement", "MISSING") is None
    assert getattr(view, "program_execution", "MISSING") is None


def test_submit_contract_states_host_owns_container_launch_not_program():
    registry = build_labbio_tool_registry(object())
    description = registry.definition("execution_submit").spec.description
    assert "host launches" in description
    assert "inside" in description and "container" in description
    assert "container CLI" in description
