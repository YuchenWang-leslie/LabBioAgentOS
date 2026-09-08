"""Bounded source assembly for local curation, without replaying an analysis."""

from uuid import UUID

from .artifacts import ArtifactConsumer, ArtifactExposureClass, ArtifactQuery, ArtifactViewType
from .contracts import WorkflowStage
from .governance import AuthorizationDenied
from .skills.models import SkillStageContext
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


def source_artifact_views(application, run_id, principal, workspace):
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
    for ref in sorted(application.artifact_store.list_refs(), key=lambda item: str(item.artifact_id)):
        if ref.run_id != run_id or ref.exposure_class is ArtifactExposureClass.RAW:
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
