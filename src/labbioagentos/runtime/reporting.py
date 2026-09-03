"""Narrow report-to-artifact boundary with trusted provenance injection."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from labbioagentos.artifacts import (
    ArtifactExposureClass,
    ArtifactReleaseBasis,
    ArtifactRepresentation,
    ArtifactStore,
)
from labbioagentos.contracts import WorkflowStage
from labbioagentos.governance import (
    AccessAction,
    AccessService,
    AuthorizationDenied,
    Principal,
    WorkspaceContext,
)
from labbioagentos.trace import RunTraceRecorder, TraceEventType


class ReportReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    report_artifact_id: UUID
    status: StrictStr = "REGISTERED"


class ReportSubmissionService:
    def __init__(
        self,
        store: ArtifactStore,
        access_service: AccessService,
        *,
        trace_recorder: RunTraceRecorder | None = None,
        max_report_chars: int = 64_000,
    ):
        self.store = store
        self.access_service = access_service
        self.trace_recorder = trace_recorder
        self.max_report_chars = max_report_chars

    def submit(
        self,
        *,
        title: str,
        report_text: str,
        evidence_artifact_ids: tuple[UUID, ...],
        principal: Principal,
        workspace: WorkspaceContext,
        run_id: UUID,
        stage_id: WorkflowStage,
        invocation_id: UUID,
    ) -> ReportReceipt:
        title = title.strip()
        report_text = report_text.strip()
        if not title or len(title) > 256:
            raise ValueError("Report title must contain 1 to 256 characters")
        if not report_text or len(report_text) > self.max_report_chars:
            raise ValueError("Report text exceeds the bounded report contract")
        if len(evidence_artifact_ids) > 256 or len(set(evidence_artifact_ids)) != len(
            evidence_artifact_ids
        ):
            raise ValueError("Evidence artifact references are invalid")
        if workspace.user_id != principal.user_id or workspace.lab_id != principal.lab_id:
            raise AuthorizationDenied("Trusted principal and workspace do not match")
        self.access_service.require_project(
            principal, workspace.project_id, AccessAction.WRITE_PROJECT, run_id=run_id
        )
        for artifact_id in evidence_artifact_ids:
            ref = self.store.get_ref(artifact_id)
            if ref.project_id != workspace.project_id or ref.lab_id != workspace.lab_id:
                raise AuthorizationDenied("Evidence artifact is outside the bound workspace")
            self.access_service.require_artifact(principal, ref)
        ref = self.store.register(
            artifact_type="report",
            exposure_class=ArtifactExposureClass.DERIVED,
            release_basis=ArtifactReleaseBasis.MODEL_AUTHORED_REPORT,
            representation=ArtifactRepresentation(
                summary={
                    "title": title,
                    "character_count": len(report_text),
                    "evidence_artifact_ids": [str(item) for item in evidence_artifact_ids],
                },
                stored_content=report_text,
            ),
            owner_user_id=principal.user_id,
            project_id=workspace.project_id,
            lab_id=workspace.lab_id,
            run_id=run_id,
            stage_id=stage_id,
            producer_invocation_id=invocation_id,
            metadata={"title": title, "format": "markdown"},
        )
        if self.trace_recorder is not None:
            self.trace_recorder.emit(
                run_id,
                TraceEventType.REPORT_SUBMITTED,
                stage_id=stage_id,
                invocation_id=invocation_id,
                status="REGISTERED",
                payload={
                    "report_artifact_id": str(ref.artifact_id),
                    "evidence_artifact_ids": [str(item) for item in evidence_artifact_ids],
                },
            )
        return ReportReceipt(report_artifact_id=ref.artifact_id)
