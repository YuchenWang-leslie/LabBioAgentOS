"""Run execution configuration is visible without granting early-stage tools."""

from dataclasses import replace
import json
from types import MethodType, SimpleNamespace

import pytest

from labbioagentos import (
    ApplicationExecutionProfile, ApplicationRunRequest, ApprovedImage,
    ArtifactExposureClass, ExecutionRuntime, LabBioApplication, NextAction,
    NextActionProposal, PantheonRuntimeFactory, Principal, RunStatus,
    RuntimeInvocationMode, RuntimeProfileCatalog, RuntimeProfileConfigurationError,
    RuntimeStageResult, WorkflowStage, WorkspaceContext,
)
from test_application_runtime_c5 import _body, _configuration
from labbioagentos.run_state import RunInflightOperation, RunRecoveryState


class _MessageCaptureFactory(PantheonRuntimeFactory):
    """Retain real assembly and both serializers; replace only provider inference."""

    def __init__(self, catalog):
        super().__init__(catalog)
        self.messages = []
        self.created_modes = []
        self.toolsets = []
        self.illegal_call_results = []

    async def create_team(self, profile_keys, **kwargs):
        self.created_modes.append(kwargs["invocation_mode"])
        team, prompts = await super().create_team(profile_keys, **kwargs)
        mode = kwargs["invocation_mode"]
        toolsets = tuple(kwargs.get("toolsets", {}).values())
        self.toolsets.extend(toolsets)

        async def run(_team, message, **_kwargs):
            decoded = json.loads(message)
            stage_input = decoded.get("stage_input", decoded)
            self.messages.append((mode, stage_input))
            stage = WorkflowStage(stage_input["stage_id"])
            if mode is RuntimeInvocationMode.CAPABILITY:
                if stage is WorkflowStage.UNDERSTAND:
                    # An attempted out-of-stage call remains denied even though
                    # the run's real execution configuration is now visible.
                    self.illegal_call_results.append(await toolsets[0].execution_submit(
                        image_key="configured-python", script_content="pass\n",
                    ))
                return SimpleNamespace(content="Fixture capability phase ended.")
            action = (
                NextActionProposal(action=NextAction.TRANSITION, target_stage=WorkflowStage.UNDERSTAND)
                if stage is WorkflowStage.INTAKE else
                NextActionProposal(action=NextAction.REQUEST_USER_INPUT, user_prompt="Fixture stop boundary?")
            )
            return SimpleNamespace(content=RuntimeStageResult(
                stage_id=stage, summary="Fixture stage result", body=_body(stage), next_action=action,
            ).model_dump(mode="json"))

        team.run = MethodType(run, team)
        return team, prompts


def _application(tmp_path, *, configured):
    configuration = _configuration(
        tmp_path,
        execution_profile=ApplicationExecutionProfile(image_key="configured-python") if configured else None,
        approved_images=(ApprovedImage(
            key="configured-python", reference="sha256:" + "a" * 64,
            runtime=ExecutionRuntime.PYTHON, available_python_modules=("fixture_module",),
        ),) if configured else (),
    )
    original = configuration.profile_catalog
    catalog = RuntimeProfileCatalog(
        agents=tuple(original.agents.values()), prompts=tuple(original.prompts.values()),
        models=tuple(original.models.values()), schemas=tuple(original.schemas.values()),
        capabilities=tuple(profile.model_copy(update={
            "capability_allowlist": ("artifact_query", "execution_submit"),
        }) for profile in original.capabilities.values()),
    )
    assemblies = tuple(replace(
        spec, capability_phase_enabled=True,
        capability_prompt_values=spec.finalization_prompt_values,
        capability_allowlist=(("artifact_query", "execution_submit") if spec.stage_id is WorkflowStage.EXECUTE
                              else () if spec.stage_id is WorkflowStage.LEARN else ("artifact_query",)),
    ) for spec in configuration.stage_assemblies)
    application = LabBioApplication(replace(configuration, profile_catalog=catalog, stage_assemblies=assemblies))
    factory = _MessageCaptureFactory(catalog)
    application.runtime_factory = factory
    principal = Principal(user_id="user-c5", lab_id="lab-c5")
    workspace = WorkspaceContext(user_id="user-c5", project_id="project-c5", lab_id="lab-c5")
    raw_file = configuration.allowed_input_roots[0] / "fixture.dat"
    raw_file.write_text("PRIVATE_RAW_SENTINEL")
    raw = application.register_input_file(raw_file, principal=principal, workspace=workspace, artifact_type="fixture")
    handle = application.create_run(ApplicationRunRequest(
        task_text="Inspect the supplied data under the configured capabilities.",
        principal=principal, workspace=workspace, input_artifact_ids=(raw.artifact_id,),
    ))
    return application, handle, factory, raw


@pytest.mark.asyncio
@pytest.mark.parametrize("configured", (True, False))
async def test_early_stage_actual_messages_separate_configuration_from_permission(tmp_path, monkeypatch, configured):
    application, handle, factory, raw = _application(tmp_path, configured=configured)
    submission_calls = []

    async def submit(*_args, **_kwargs):
        submission_calls.append(True)
        raise AssertionError("UNDERSTAND must not enter execution submission")

    monkeypatch.setattr(application.execution_submission, "submit", submit)
    result = await application.run(handle)
    assert result.status is RunStatus.WAITING_FOR_USER
    assert [(mode, value["stage_id"]) for mode, value in factory.messages] == [
        (RuntimeInvocationMode.CAPABILITY, "INTAKE"), (RuntimeInvocationMode.FINALIZE, "INTAKE"),
        (RuntimeInvocationMode.CAPABILITY, "UNDERSTAND"), (RuntimeInvocationMode.FINALIZE, "UNDERSTAND"),
    ]
    for _, presented in factory.messages:
        assert presented["allowed_capabilities"] == ["artifact_query"]
        usage = presented["input_artifact_usage"][0]
        assert usage["artifact_id"] == str(raw.artifact_id)
        assert usage["exposure_class"] == ArtifactExposureClass.RAW.value
        assert usage["remote_view_types"] == ["METADATA"]
        assert usage["execution_input_eligible"] is configured
        if configured:
            execution = presented["execution_capability"]
            assert execution is not None
            assert execution["scope"] == "RUN_CONFIGURATION"
            assert execution["authority"] == "CONTROL_STATE"
            assert execution["image_key"] == "configured-python"
            assert execution["available_python_modules"] == ["fixture_module"]
            assert execution["mountable_input_artifact_ids"] == [str(raw.artifact_id)]
            assert execution["resources"] == application.execution_capability.resources.model_dump(mode="json")
        else:
            assert presented["execution_capability"] is None
        encoded = json.dumps(presented)
        assert all(secret not in encoded for secret in ("PRIVATE_RAW_SENTINEL", str(tmp_path), "storage_locator", "sha256:"))
    assert submission_calls == []
    assert all("execution_submit" not in toolset.tool_functions for toolset in factory.toolsets)
    assert factory.illegal_call_results[0]["error"]["error_code"] == "AUTHORIZATION_DENIED"
    assert all("execution_submit" not in item["allowed_capabilities"] for _, item in factory.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ("omission", "image_key", "scope"))
async def test_early_stage_configuration_tampering_fails_before_model_construction(tmp_path, tamper):
    application, handle, factory, _ = _application(tmp_path, configured=True)
    session = application._session(handle)
    application.workflow_engine.start(session.run)
    stage_input = session.coordinator.build_stage_input(session.run, instruction="Fixture input")
    application._checkpoint(
        session, recovery_state=RunRecoveryState.STAGE_IN_FLIGHT,
        inflight_stage=WorkflowStage.INTAKE, inflight_invocation_id=stage_input.invocation_id,
        inflight_operation=RunInflightOperation.RUNTIME_STAGE,
    )
    trusted = application.execution_capability.with_mountable_inputs(session.request.input_artifact_ids)
    forged = None if tamper == "omission" else trusted.model_copy(update={
        "image_key" if tamper == "image_key" else "scope": "forged-value",
    })
    invoker = session.coordinator.registry.get(WorkflowStage.INTAKE).invoker
    with pytest.raises(RuntimeProfileConfigurationError, match="trusted configuration"):
        await invoker.invoke(stage_input.model_copy(update={"execution_capability": forged}))
    assert factory.messages == []
    assert factory.toolsets == []
    assert factory.created_modes == []
