"""Trusted file budgets reach the sandbox and model without widening exposure."""

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from labbioagentos import (
    ApplicationRunRequest, DockerCommandBuilder, DockerExecutor, ExecutionPlan,
    ExecutionPolicy, OutputArtifactSpec,
    RuntimeExecutionCapabilityView, WorkflowStage,
)
from labbioagentos.local_config import build_application, load_settings, runtime_manifest
from test_local_configuration import settings_file


DEFAULTS = {
    "max_output_file_bytes": 16 * 1024 * 1024,
    "max_collected_output_bytes": 64 * 1024 * 1024,
    "tmpfs_size_mb": 64,
}
LARGER = {
    "max_output_file_bytes": 2 * 1024 ** 3,
    "max_collected_output_bytes": 4 * 1024 ** 3,
    "tmpfs_size_mb": 256,
}


def _configure(path, values):
    text = path.read_text(encoding="utf-8")
    fields = "".join(f"{key} = {json.dumps(value)}\n" for key, value in values.items())
    path.write_text(text.replace("[execution]\n", "[execution]\n" + fields), encoding="utf-8")
    return load_settings(path)


def test_old_configuration_keeps_file_budget_defaults(settings_file):
    settings = load_settings(settings_file)
    for name, expected in DEFAULTS.items():
        assert getattr(settings.execution, name) == expected
        assert getattr(ExecutionPolicy(), name) == expected


@pytest.mark.parametrize("name", DEFAULTS)
@pytest.mark.parametrize("value", (0, -1, True, 1.5, "1024"))
def test_file_budgets_reject_invalid_or_coerced_values(settings_file, name, value):
    with pytest.raises(ValidationError):
        _configure(settings_file, {name: value})
    with pytest.raises(ValidationError):
        ExecutionPolicy(**{name: value})


def test_collection_budget_must_cover_one_file(settings_file):
    values = {"max_output_file_bytes": 1024, "max_collected_output_bytes": 1023}
    with pytest.raises(ValidationError):
        _configure(settings_file, values)
    with pytest.raises(ValidationError):
        ExecutionPolicy(**values)
    assert ExecutionPolicy(max_output_file_bytes=1024, max_collected_output_bytes=1024)


@pytest.mark.parametrize("name", ("max_output_file_bytes", "tmpfs_size_mb"))
@pytest.mark.parametrize("value", (0, -1, True, 1.5, "1024"))
def test_direct_command_builder_rejects_invalid_file_budgets(name, value):
    with pytest.raises(ValueError):
        DockerCommandBuilder(**{name: value})


@pytest.mark.parametrize("name", DEFAULTS)
def test_each_file_budget_is_reproducible_runtime_configuration(settings_file, name):
    previous = runtime_manifest(load_settings(settings_file))
    settings = _configure(settings_file, {name: DEFAULTS[name] * 2})
    manifest = runtime_manifest(settings)
    assert manifest["configuration"]["execution"][name] == DEFAULTS[name] * 2
    assert manifest["runtime_revision"] != previous["runtime_revision"]
    assert manifest["profile"] == previous["profile"]


def test_file_budgets_wire_policy_collection_docker_and_model_input(settings_file):
    before = runtime_manifest(load_settings(settings_file))
    settings = _configure(settings_file, LARGER)
    application = build_application(settings, settings.result_root / "budgets", load_provider=False)
    try:
        collector = application.docker_executor.output_collector
        builder = application.docker_executor.command_builder
        for name, expected in LARGER.items():
            assert getattr(application.execution_policy, name) == expected
            assert getattr(application.execution_capability, name) == expected
        assert collector.max_output_file_bytes == builder.max_output_file_bytes == LARGER["max_output_file_bytes"]
        assert collector.max_collected_output_bytes == LARGER["max_collected_output_bytes"]
        assert builder.tmpfs_size_mb == LARGER["tmpfs_size_mb"]
        direct = DockerExecutor(
            store=application.artifact_store, image_registry=application.image_registry,
            execution_policy=application.execution_policy,
            mount_resolver=application.docker_executor.mount_resolver,
            workspace_manager=application.docker_executor.workspace_manager,
            output_collector=collector,
        )
        assert direct.command_builder.max_output_file_bytes == LARGER["max_output_file_bytes"]
        assert direct.command_builder.tmpfs_size_mb == LARGER["tmpfs_size_mb"]
        contract = application.configuration.output_contracts[0]
        plan = ExecutionPlan(
            run_id=uuid4(), stage_id=WorkflowStage.EXECUTE,
            image_key=settings.execution.image_key, script_content="pass\n",
            resources=settings.execution.resources,
            requested_outputs=(OutputArtifactSpec(
                relative_path="summary.json", artifact_type="scalar-records",
                requested_exposure="DERIVED", output_contract_id=contract.contract_id,
            ),),
        )
        command = application.docker_executor.build_command(plan)
        limit = LARGER["max_output_file_bytes"]
        assert command[command.index("--ulimit") + 1] == f"fsize={limit}:{limit}"
        assert command[command.index("--tmpfs") + 1] == "/tmp:rw,noexec,nosuid,size=256m"
        assert command[command.index("--network") + 1] == "none"
        assert command[command.index("--cap-drop") + 1] == "ALL"
        assert command[command.index("--pull") + 1] == "never"
        assert "no-new-privileges" in command and "--read-only" in command

        source = settings.input_roots[0] / "synthetic.input"
        source.write_bytes(b"PRIVATE_RAW_SENTINEL")
        raw = application.register_input_file(
            source, principal=settings.principal, workspace=settings.workspace, artifact_type="raw-file",
        )
        handle = application.create_run(ApplicationRunRequest(
            task_text="Synthetic resource test", principal=settings.principal,
            workspace=settings.workspace, input_artifact_ids=(raw.artifact_id,),
        ))
        session = application._session(handle)
        application.workflow_engine.start(session.run)
        for stage in (WorkflowStage.UNDERSTAND, WorkflowStage.PLAN):
            application.workflow_engine.transition(session.run, stage)
        stage_input = session.coordinator.build_stage_input(session.run, instruction="Synthetic resource test")
        payload = json.loads(stage_input.model_dump_json())
        assert {name: payload["execution_capability"][name] for name in LARGER} == LARGER
        assert payload["input_artifact_usage"][0]["remote_view_types"] == []
        assert payload["input_artifact_usage"][0]["execution_input_eligible"] is True
        assert "PRIVATE_RAW_SENTINEL" not in stage_input.model_dump_json()
        assert str(source) not in stage_input.model_dump_json()
        assert runtime_manifest(settings)["profile"]["output_contract"] == before["profile"]["output_contract"]
        assert contract.max_file_bytes == 1024 * 1024

        # Old persisted capability views stay readable, without inventing known limits.
        old = payload["execution_capability"]
        for name in LARGER:
            old.pop(name)
        restored = RuntimeExecutionCapabilityView.model_validate_json(json.dumps(old))
        assert all(getattr(restored, name) is None for name in LARGER)
    finally:
        application.run_state_store.close()
