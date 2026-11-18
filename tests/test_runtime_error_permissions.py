"""Error-reading permissions are visible facts, never implicit tool grants."""

import pytest
from pydantic import ValidationError

from labbioagentos import RuntimeExecutionCapabilityView
from labbioagentos.local_config import build_application, load_settings, runtime_manifest
from test_c12_execution_capability_contract import _trusted_view
from test_local_configuration import settings_file  # noqa: F401


@pytest.mark.parametrize("context", [False, True])
@pytest.mark.parametrize("files", [False, True])
def test_current_host_permissions_reach_model_view_and_manifest(settings_file, context, files):
    settings = load_settings(settings_file)
    settings = settings.model_copy(update={"execution": settings.execution.model_copy(update={
        "include_error_context": context, "allow_generated_file_read": files,
    })})
    application = build_application(settings, settings.result_root / "permissions", load_provider=False)
    try:
        view = application.execution_capability.model_dump(mode="json")
        assert view["include_error_context"] is context
        assert view["allow_generated_file_read"] is files
        assert view["scope"] == "RUN_CONFIGURATION"
        assert application.execution_policy.include_error_context is context
        assert application.configuration.exposure_policy.allow_generated_file_read is files
        manifest = runtime_manifest(settings)["configuration"]["execution"]
        assert manifest["include_error_context"] is context
        assert manifest["allow_generated_file_read"] is files
    finally:
        application.run_state_store.close()


def test_absent_permissions_in_older_snapshot_are_unknown_not_false():
    encoded = _trusted_view().model_dump()
    encoded.pop("include_error_context", None)
    encoded.pop("allow_generated_file_read", None)
    older = RuntimeExecutionCapabilityView.model_validate(encoded)
    assert older.include_error_context is None
    assert older.allow_generated_file_read is None


@pytest.mark.parametrize("name", ["include_error_context", "allow_generated_file_read"])
@pytest.mark.parametrize("value", ["true", 1, []])
def test_permission_snapshot_does_not_coerce_ambiguous_values(name, value):
    encoded = _trusted_view().model_dump()
    encoded[name] = value
    with pytest.raises(ValidationError):
        RuntimeExecutionCapabilityView.model_validate(encoded)
