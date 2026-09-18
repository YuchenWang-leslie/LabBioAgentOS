"""Narrow report-to-artifact boundary with trusted provenance injection."""

from __future__ import annotations

from hashlib import sha256
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from labbioagentos.artifacts import (
    ArtifactExposureClass,
    ArtifactConsumer,
    ArtifactExposureDenied,
    ArtifactQuery,
    ArtifactReleaseBasis,
    ArtifactRepresentation,
    ArtifactStore,
    ArtifactViewType,
)
from labbioagentos.model_safety import validate_model_visible_json
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
            metadata={
                "title": title, "format": "markdown",
                "sha256": sha256(report_text.encode("utf-8")).hexdigest(),
                "size_bytes": len(report_text.encode("utf-8")),
            },
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


def read_report_page(
    store, exposure, artifact_id: UUID, *, principal, workspace,
    offset: int = 0, limit: int = 4_000, expected_sha256: str | None = None,
) -> dict:
    """Expose exact bounded model-authored prose, not arbitrary stored content."""
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 8_000:
        raise ValueError("Report pagination is outside the allowed bounds")
    ref = store.get_ref(artifact_id)
    if ref.artifact_id != artifact_id or exposure.store is not store:
        raise ValueError("Report identity does not match its governed store")
    if ((principal.user_id, workspace.project_id, principal.lab_id)
            != (workspace.user_id, workspace.project_id, workspace.lab_id)
            or (ref.owner_user_id, ref.project_id, ref.lab_id)
            != (workspace.user_id, workspace.project_id, workspace.lab_id)):
        raise AuthorizationDenied("Report is outside the current workspace")
    if (ref.artifact_type != "report" or ref.exposure_class is not ArtifactExposureClass.DERIVED
            or ref.release_basis is not ArtifactReleaseBasis.MODEL_AUTHORED_REPORT):
        raise ArtifactExposureDenied("Only governed model-authored report prose can be read")
    exposure.artifact_query(
        artifact_id, ArtifactQuery(view_type=ArtifactViewType.METADATA),
        ArtifactConsumer.REMOTE_LLM, principal=principal,
    )
    stored = store.load_for_view(artifact_id)
    content = stored.representation.stored_content
    if stored.ref != ref or not isinstance(content, str) or len(content) > 64_000 or offset > len(content):
        raise ValueError("Report content or offset is invalid")
    digest = sha256(content.encode("utf-8")).hexdigest()
    if ((expected_sha256 is not None and expected_sha256 != digest)
            or ("sha256" in ref.metadata and ref.metadata["sha256"] != digest)
            or ("size_bytes" in ref.metadata and ref.metadata["size_bytes"] != len(content.encode("utf-8")))):
        raise ValueError("Report content identity does not match")
    end = min(offset + limit, len(content))
    page = {
        "artifact_id": str(artifact_id), "source_run_id": str(ref.run_id) if ref.run_id else None,
        "sha256": digest, "content_authority": "MODEL_CONTEXT", "offset": offset,
        "end_offset": end, "total_characters": len(content), "content": content[offset:end],
        "next_offset": end if end < len(content) else None, "truncated": end < len(content),
    }
    validate_model_visible_json(page)
    return page
