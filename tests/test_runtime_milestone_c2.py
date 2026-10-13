"""Milestone C2 tests for separated capability and typed-finalization turns."""

from __future__ import annotations

import json
from types import MethodType, SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from labbioagentos import (
    AccessService,
    AgentProfile,
    ArtifactExposureClass,
    ArtifactExposureService,
    ArtifactRepresentation,
    AuthorizationPolicy,
    CapabilityEvidenceBundle,
    CapabilityEvidenceItem,
    CapabilityEvidenceStatus,
    CapabilityProfile,
    InMemoryProjectStore,
    InMemoryTraceSink,
    InformationAuthority,
    IntakeStageBody,
    LabBioRuntimeToolSet,
    LocalArtifactStore,
    ModelProfile,
    NextAction,
    NextActionProposal,
    NativeRuntimeFactory,
    Principal,
    Project,
    PromptProfile,
    ProviderConfigRef,
    ProviderThinkingWireFormat,
    ProviderTransport,
    ResponseSchemaRef,
    RunTraceRecorder,
    RuntimeCapabilityContext,
    RuntimeCapabilityServices,
    RuntimeCoordinatorService,
    RuntimeGateDecisionView,
    RuntimeInvocationMode,
    RuntimeProfileCatalog,
    RuntimeProfileConfigurationError,
    RuntimeResultValidationError,
    RuntimeStageInput,
    RuntimeStageResult,
    RuntimeWorkflowControlView,
    RuntimeWorkspaceIdentifiers,
    StageRuntimeRegistry,
    StageRuntimeSpec,
    TraceEventType,
    ValidateStageBody,
    WorkflowEngine,
    WorkflowStage,
    WorkspaceContext,
    runtime_workflow_definition,
)
from labbioagentos.artifacts import ExposurePolicy


def _profile() -> AgentProfile:
    return AgentProfile(
        profile_key="coordinator",
        version="1",
        agent_name="CoordinatorAgent",
        role_description="Coordinate the current synthetic stage.",
        prompt_profile_key="runtime-generic",
        response_schema_key="runtime-stage-result",
        model_profile_key="runtime-default",
        capability_profile_key="coordinator-capabilities",
    )


def _catalog(*, model: ModelProfile | None = None) -> RuntimeProfileCatalog:
    return RuntimeProfileCatalog(
        agents=(_profile(),),
        prompts=(
            PromptProfile(
                template_id="runtime-generic",
                version="1",
                template_text="Use only the supplied synthetic context.",
            ),
        ),
        models=(
            model
            or ModelProfile(
                profile_key="runtime-default",
                version="1",
                model_identifier="mock/provider-model",
                provider_config=ProviderConfigRef(
                    config_id="external-mock", provider="mock"
                ),
            ),
        ),
        schemas=(ResponseSchemaRef(),),
        capabilities=(
            CapabilityProfile(
                profile_key="coordinator-capabilities",
                version="1",
                capability_allowlist=("artifact_list",),
            ),
        ),
    )

def _stage_input(stage: WorkflowStage = WorkflowStage.INTAKE) -> RuntimeStageInput:
    return RuntimeStageInput(
        run_id=uuid4(),
        stage_id=stage,
        instruction="Synthetic integration request.",
        workspace=RuntimeWorkspaceIdentifiers(
            user_id="user-c2", project_id="project-c2", lab_id="lab-c2"
        ),
        allowed_capabilities=("artifact_list",),
    )


def _toolset(tmp_path, stage_input, recorder=None):
    projects = InMemoryProjectStore()
    projects.register(
        Project(
            project_id="project-c2", lab_id="lab-c2", owner_user_id="user-c2"
        )
    )
    access = AccessService(projects, AuthorizationPolicy())
    principal = Principal(user_id="user-c2", lab_id="lab-c2")
    workspace = WorkspaceContext(
        user_id="user-c2", project_id="project-c2", lab_id="lab-c2"
    )
    store = LocalArtifactStore(tmp_path / str(stage_input.invocation_id))
    store.register(
        artifact_type="synthetic-c2-result",
        exposure_class=ArtifactExposureClass.DERIVED,
        representation=ArtifactRepresentation(summary={"safe_count": 2}),
        owner_user_id="user-c2",
        project_id="project-c2",
        lab_id="lab-c2",
        run_id=stage_input.run_id,
        stage_id=stage_input.stage_id,
        producer_invocation_id=stage_input.invocation_id,
    )
    exposure = ArtifactExposureService(
        store, ExposurePolicy(), access_service=access
    )
    binding = RuntimeCapabilityContext(
        principal=principal,
        workspace=workspace,
        run_id=stage_input.run_id,
        stage_id=stage_input.stage_id,
        invocation_id=stage_input.invocation_id,
        actor_profile_key="coordinator",
        actor_agent_name="CoordinatorAgent",
        capability_allowlist=("artifact_list",),
    )
    return LabBioRuntimeToolSet(
        binding,
        RuntimeCapabilityServices(
            artifact_store=store,
            artifact_exposure=exposure,
            trace_recorder=recorder,
        ),
    )


def _intake_result() -> RuntimeStageResult:
    return RuntimeStageResult(
        stage_id=WorkflowStage.INTAKE,
        summary="Synthetic typed finalization.",
        body=IntakeStageBody(interpreted_goal="Synthetic goal."),
        next_action=NextActionProposal(
            action=NextAction.TRANSITION,
            target_stage=WorkflowStage.UNDERSTAND,
        ),
    )
