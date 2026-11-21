"""Bounded source assembly for local curation, without replaying an analysis."""

from uuid import UUID

from .artifacts import ArtifactConsumer, ArtifactExposureClass, ArtifactExposureDenied, ArtifactNotFoundError, ArtifactQuery, ArtifactViewType
from .contracts import WorkflowStage
from .governance import AuthorizationDenied
from .model_safety import UnsafeModelContentError
from .skills.models import (
    SkillOutcomeEvidence, SkillSelectedDelivery, SkillSelectedReport, SkillSourceClarification, SkillSourceText,
    SkillSourceUserRequest, SkillStageContext,
)
from .trace import TraceEventType


# These are typed model-authored stage fields, not generic tool/provider payloads.
_PROCEDURAL_FIELDS = {
    WorkflowStage.PLAN: {"procedure_steps", "required_input_references",
                         "requested_capabilities", "validation_expectations"},
    WorkflowStage.EXECUTE: {"execution_status", "execution_reference",
                            "output_artifact_references", "issue_references"},
    WorkflowStage.VALIDATE: {"technical_status", "runtime_assessment",
                             "evidence_references", "limitations"},
    WorkflowStage.LEARN: {"learning_summary", "source_bundle_reference"},
}


def stage_context_from_results(results, events, run_id):
    """Preserve declared plans/assessments, explicitly not proof of execution."""
    identities = []
    for event in events:
        if event.run_id != run_id:
            raise ValueError("Curation trace must belong to exactly one run")
        if event.event_type is TraceEventType.RESULT_RECORDED:
            payload = event.payload.get("result", {}).get("payload", {})
            if "runtime_result_id" in payload:
                result_id = UUID(payload["runtime_result_id"])
                identities.append((result_id, event.stage_id, UUID(payload["invocation_id"])))
    if len(results) != len(identities):
        raise ValueError("Curation results do not match the persistent result sequence")
    context = []
    # The durable runtime contract preserves result order but does not require a
    # model-supplied result UUID to be globally unique, even across retry attempts.
    for result, identity in zip(results, identities, strict=True):
        if (result.result_id, result.stage_id) != identity[:2]:
            raise ValueError("Curation result has no matching persistent invocation")
        fields = _PROCEDURAL_FIELDS.get(result.stage_id)
        if fields is None:
            continue
        context.append(SkillStageContext(
            result_id=result.result_id, invocation_id=identity[2], stage_id=result.stage_id,
            model_summary=result.summary,
            model_body=result.body.model_dump(mode="json", include=fields),
        ))
    if len(context) > 64:
        raise ValueError("Curation stage context exceeds its explicit bound")
    return tuple(context)


def source_artifact_views(application, run_id, principal, workspace, *, selected_delivery=None):
    """Project source-run outputs under existing exposure rules, never RAW files.

    Format-neutral views are source material, not a selection of scientific results.
    TOP_N retains the exposure policy's default and explicit completeness metadata.
    """
    queries = {
        ArtifactExposureClass.STRUCTURAL: (ArtifactViewType.SCHEMA,),
        ArtifactExposureClass.AGGREGATE: (ArtifactViewType.SUMMARY,),
        ArtifactExposureClass.DERIVED: (ArtifactViewType.SUMMARY, ArtifactViewType.TOP_N),
        ArtifactExposureClass.USER_APPROVED: (ArtifactViewType.SUMMARY, ArtifactViewType.TOP_N),
    }
    requests = []
    selected_ids = {item.artifact_id for item in selected_delivery.outputs} if selected_delivery is not None else None
    for ref in sorted(application.artifact_store.list_refs(), key=lambda item: str(item.artifact_id)):
        if ref.exposure_class is ArtifactExposureClass.RAW:
            continue
        if selected_ids is not None and ref.artifact_id not in selected_ids:
            continue
        if selected_ids is None and ref.run_id != run_id:
            continue
        if (ref.owner_user_id, ref.project_id, ref.lab_id) != (
            principal.user_id, workspace.project_id, principal.lab_id
        ):
            raise AuthorizationDenied("Curation Artifact does not belong to the source scope")
        requests.extend((ref.artifact_id, view) for view in queries[ref.exposure_class])
    if len(requests) > 32:
        raise ValueError("Curation Artifact views exceed their explicit bound")
    return tuple(application.artifact_exposure.artifact_query(
        artifact_id, ArtifactQuery(view_type=view), ArtifactConsumer.REMOTE_LLM,
        principal=principal,
    ) for artifact_id, view in requests)


def source_outcome_evidence(application, record, run_id, principal, workspace):
    from .local_delivery import selected_delivery_manifest, selected_report_id
    from .runtime.delivery import build_delivery_manifest
    from .runtime.reporting import read_report_page

    clarifications = tuple(SkillSourceClarification(
        agent_question=SkillSourceText.from_record(item.question.prompt),
        user_answer=SkillSourceUserRequest.from_record(item.answer_text),
    ) for item in getattr(record.workflow_run, "clarifications", ())
        if item.status in {"ANSWERED", "RESOLVED"} and item.answered_by == principal.user_id)
    report_id = selected_report_id(record)
    report = None
    if report_id is not None:
        try:
            if application.artifact_store.get_ref(report_id).run_id != run_id:
                raise AuthorizationDenied("Selected report does not belong to the reviewed run")
            page = read_report_page(application.artifact_store, application.artifact_exposure, report_id,
                                    principal=principal, workspace=workspace, limit=8000)
            pages = [page["content"]]
            while page["next_offset"] is not None and page["end_offset"] < 32_000:
                page = read_report_page(
                    application.artifact_store, application.artifact_exposure, report_id,
                    principal=principal, workspace=workspace, offset=page["next_offset"],
                    limit=8000, expected_sha256=page["sha256"],
                )
                pages.append(page["content"])
            report = SkillSelectedReport.from_prefix(report_id, page["sha256"], page["total_characters"],
                                                    "".join(pages))
        except ArtifactNotFoundError:
            report = SkillSelectedReport(artifact_id=report_id, excerpt=SkillSourceText())
        except (ArtifactExposureDenied, UnsafeModelContentError):
            report = SkillSelectedReport(artifact_id=report_id, excerpt=SkillSourceText(status="WITHHELD"))
    selected_delivery = None
    if report is not None:
        saved = selected_delivery_manifest(application.artifact_store, record)
        if saved is not None:
            verified = build_delivery_manifest(application.artifact_store, application.access_service,
                tuple(item.artifact_id for item in saved.outputs), principal=principal, workspace=workspace,
                run_id=run_id, imported_artifact_ids=(*record.input_artifact_ids, *record.context_artifact_ids),
                trace_recorder=application.trace_recorder)
            if verified != saved.model_dump(mode="json"):
                raise ValueError("Selected delivery differs from its registered producers")
            selected_delivery = SkillSelectedDelivery(
                outputs=tuple(item.model_dump(exclude={"filename"}) for item in saved.outputs),
                programs=tuple(item.model_dump() for item in saved.programs))
    return SkillOutcomeEvidence(selected_report=report, selected_delivery=selected_delivery,
                                answered_clarifications=clarifications)
