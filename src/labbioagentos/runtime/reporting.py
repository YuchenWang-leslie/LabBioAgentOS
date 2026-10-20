"""Narrow report-to-artifact boundary with trusted provenance injection."""

from __future__ import annotations

from hashlib import sha256
import json
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

from .contracts import ArtifactReadReceipt


def observation_digest(value) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")).hexdigest()


def artifact_representation_digest(store, artifact_id) -> str:
    return observation_digest(store.load_for_view(artifact_id).representation.model_dump(mode="json"))


def make_read_receipt(store, capability, data, *, observation_id, invocation_id):
    """Describe only the approved result actually returned by the read operation."""
    if capability not in {"artifact_query", "artifact_aggregate", "file_read", "report_read"}:
        return None
    artifact_id = UUID(data["artifact_id"])
    if capability == "artifact_query":
        view = data["view_type"]
        if view == "TOP_N":
            scope = {"kind": "TABLE_PAGE", "offset": data["offset"],
                     "end_offset": data["offset"] + data["returned_count"],
                     "record_count": data["available_count"]}
        else:
            scope = {"kind": f"ARTIFACT_{view}"}
        identity = "ARTIFACT_REPRESENTATION"
        source_hash = artifact_representation_digest(store, artifact_id)
    elif capability == "artifact_aggregate":
        scope = {"kind": "TABLE_AGGREGATE",
                 "query": {key: data[key] for key in
                           ("operation", "field", "match_field", "match_value") if key in data},
                 "record_count": data["source_record_count"],
                 "matched_count": data["matched_count"]}
        identity = "ARTIFACT_REPRESENTATION"
        source_hash = artifact_representation_digest(store, artifact_id)
    else:
        is_report = data.get("content_authority") == "MODEL_CONTEXT"
        identity = "REPORT_TEXT" if is_report else "GENERATED_FILE"
        source_hash = data["sha256"]
        if "end_offset" in data:
            scope = {"kind": "REPORT_TEXT_PAGE" if is_report else "FILE_TEXT_PAGE",
                     "offset": data["offset"], "end_offset": data["end_offset"],
                     "total_characters": data["total_characters"]}
            if "view_sha256" in data:
                scope["view_sha256"] = data["view_sha256"]
                scope["redacted"] = data["redacted"]
        else:
            scope = {"kind": "FILE_PREVIEW", "status": data["status"]}
    return ArtifactReadReceipt(
        artifact_id=artifact_id, observation_id=observation_id,
        source_invocation_id=invocation_id, delivered_invocation_id=invocation_id,
        source_sha256=source_hash, source_identity=identity,
        observation_sha256=observation_digest(data), scope=scope,
    )


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
        evidence_read_receipts: tuple[ArtifactReadReceipt, ...] | None = None,
        receipt_delivery_invocation_id: UUID | None = None,
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
        citation_summary = {}
        if evidence_read_receipts is not None:
            delivery_invocation_id = receipt_delivery_invocation_id or invocation_id
            if (len(evidence_read_receipts) > 64
                    or set(item.artifact_id for item in evidence_read_receipts) != set(evidence_artifact_ids)
                    or any(item.delivered_invocation_id != delivery_invocation_id for item in evidence_read_receipts)
                    or len({item.observation_id for item in evidence_read_receipts}) != len(evidence_read_receipts)):
                raise ValueError("Read receipts do not match report sources and invocation")
            citation_summary = {
                "evidence_contract": "OBSERVED_SCOPE_V1",
                "evidence_read_receipts": [item.model_dump(mode="json") for item in evidence_read_receipts],
                "model_invocation_id": str(delivery_invocation_id),
                "scientific_validation": "NOT_PERFORMED",
            }
        ref = self.store.register(
            artifact_type="report",
            exposure_class=ArtifactExposureClass.DERIVED,
            release_basis=ArtifactReleaseBasis.MODEL_AUTHORED_REPORT,
            representation=ArtifactRepresentation(
                summary={
                    "title": title,
                    "character_count": len(report_text),
                    "evidence_artifact_ids": [str(item) for item in evidence_artifact_ids],
                    **citation_summary,
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
                    **citation_summary,
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
