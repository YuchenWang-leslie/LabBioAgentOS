"""Small deterministic readiness check before governed execution."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from labbioagentos.artifacts import ArtifactExposureClass, ArtifactStore
from labbioagentos.governance import (
    AccessAction,
    AccessService,
    AuthorizationDenied,
    Principal,
    WorkspaceContext,
)
from labbioagentos.contracts import WorkflowStage
from labbioagentos.trace import RunTraceRecorder, TraceEventType

from .images import ApprovedImageRegistry, ExecutionPolicy
from .models import ExecutionRuntime, RequestedResources
from .registration import ArtifactRegistrationPolicy


class PreflightInputRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    artifact_id: UUID
    exposure_class: ArtifactExposureClass


class ExecutionPreflightRequest(BaseModel):
    """Script-free execution envelope proposed to deterministic policy."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    runtime: ExecutionRuntime = ExecutionRuntime.PYTHON
    image_key: str = Field(min_length=1, max_length=128)
    input_requirements: tuple[PreflightInputRequirement, ...] = Field(
        default=(), max_length=128
    )
    resources: RequestedResources = Field(default_factory=RequestedResources)
    network_required: bool = False
    output_contract_ids: tuple[str, ...] = Field(default=(), max_length=128)

    @model_validator(mode="after")
    def reject_duplicates(self) -> "ExecutionPreflightRequest":
        artifact_ids = [item.artifact_id for item in self.input_requirements]
        if len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError("Preflight input Artifact IDs must be unique")
        if len(set(self.output_contract_ids)) != len(self.output_contract_ids):
            raise ValueError("Preflight output contract IDs must be unique")
        return self


class ExecutionPreflightReceipt(BaseModel):
    """Bounded model-safe proof that host policy completed its checks."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    structurally_valid: bool = True
    runtime: ExecutionRuntime
    image_key: str
    input_artifact_ids: tuple[UUID, ...]
    resources: RequestedResources
    network_required: bool
    approved_contract_ids: tuple[str, ...]
    approved_schema_ids: tuple[str, ...]


class ExecutionPreflightError(RuntimeError):
    """A deterministic readiness check failed before model finalization."""


class ExecutionPreflightService:
    def __init__(
        self,
        *,
        artifact_store: ArtifactStore,
        access_service: AccessService,
        image_registry: ApprovedImageRegistry,
        execution_policy: ExecutionPolicy,
        registration_policy: ArtifactRegistrationPolicy,
        trace_recorder: RunTraceRecorder | None = None,
    ):
        self.artifact_store = artifact_store
        self.access_service = access_service
        self.image_registry = image_registry
        self.execution_policy = execution_policy
        self.registration_policy = registration_policy
        self.trace_recorder = trace_recorder

    def require_ready(
        self,
        request: ExecutionPreflightRequest,
        *,
        principal: Principal,
        workspace: WorkspaceContext,
        run_id: UUID,
    ) -> ExecutionPreflightReceipt:
        """Run structural checks only; no data rows or script are inspected."""

        try:
            if (
                workspace.user_id != principal.user_id
                or workspace.lab_id != principal.lab_id
            ):
                raise AuthorizationDenied(
                    "Trusted principal and workspace do not match"
                )
            self.access_service.require_project(
                principal,
                workspace.project_id,
                AccessAction.WRITE_PROJECT,
                run_id=run_id,
            )
            for requirement in request.input_requirements:
                ref = self.artifact_store.get_ref(requirement.artifact_id)
                if (
                    ref.project_id != workspace.project_id
                    or ref.lab_id != workspace.lab_id
                ):
                    raise AuthorizationDenied(
                        "Preflight input is outside the bound workspace"
                    )
                self.access_service.require_artifact(
                    principal, ref, AccessAction.READ_ARTIFACT
                )
                if ref.exposure_class is not requirement.exposure_class:
                    raise ExecutionPreflightError(
                        "Preflight input exposure class does not match the request"
                    )
            image = self.image_registry.resolve(
                request.image_key, runtime=request.runtime
            )
            self.execution_policy.validate_request(
                request.resources,
                network_required=request.network_required,
                image=image,
            )
            contracts = tuple(
                self.registration_policy.resolve_contract(contract_id)
                for contract_id in request.output_contract_ids
            )
            receipt = ExecutionPreflightReceipt(
                runtime=request.runtime,
                image_key=request.image_key,
                input_artifact_ids=tuple(
                    item.artifact_id for item in request.input_requirements
                ),
                resources=request.resources,
                network_required=request.network_required,
                approved_contract_ids=tuple(
                    contract.contract_id for contract in contracts
                ),
                approved_schema_ids=tuple(contract.schema_id for contract in contracts),
            )
        except Exception as exc:
            self._emit(
                run_id,
                TraceEventType.PREFLIGHT_FAILED,
                "FAILED",
                request,
                error_code=type(exc).__name__,
            )
            if isinstance(exc, ExecutionPreflightError):
                raise
            raise ExecutionPreflightError(
                "Deterministic execution preflight rejected the request"
            ) from exc
        self._emit(
            run_id,
            TraceEventType.PREFLIGHT_COMPLETED,
            "COMPLETED",
            request,
            schema_ids=receipt.approved_schema_ids,
        )
        return receipt

    def _emit(
        self,
        run_id: UUID,
        event_type: TraceEventType,
        status: str,
        request: ExecutionPreflightRequest,
        *,
        error_code: str | None = None,
        schema_ids: tuple[str, ...] = (),
    ) -> None:
        if self.trace_recorder is None:
            return
        payload = {
            "image_key": request.image_key,
            "runtime": request.runtime.value,
            "input_artifact_ids": [
                str(item.artifact_id) for item in request.input_requirements
            ],
            "output_contract_ids": list(request.output_contract_ids),
            "schema_ids": list(schema_ids),
            "network_required": request.network_required,
        }
        if error_code is not None:
            payload["error_code"] = error_code
        self.trace_recorder.emit(
            run_id,
            event_type,
            stage_id=WorkflowStage.PREFLIGHT,
            status=status,
            payload=payload,
        )
