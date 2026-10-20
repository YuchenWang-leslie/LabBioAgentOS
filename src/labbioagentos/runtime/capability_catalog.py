"""Backend-neutral LabBio capability names, stage ceilings, and authority."""

from labbioagentos.contracts import InformationAuthority, WorkflowStage


CAPABILITY_CEILINGS: dict[WorkflowStage, tuple[str, ...]] = {
    WorkflowStage.INTAKE: (
        "artifact_list",
        "artifact_query",
        "report_read",
        "file_read",
    ),
    WorkflowStage.UNDERSTAND: (
        "artifact_list",
        "artifact_query",
        "report_read",
        "file_read",
        "skill_search",
        "skill_view",
        "memory_search",
        "memory_view",
    ),
    WorkflowStage.PLAN: (
        "artifact_query",
        "report_read",
        "file_read",
        "skill_search",
        "skill_view",
        "skill_propose_use",
        "memory_search",
        "memory_view",
        "environment_list",
    ),
    WorkflowStage.PREFLIGHT: (
        "artifact_query",
        "report_read",
        "file_read",
    ),
    WorkflowStage.EXECUTE: (
        "artifact_query",
        "report_read",
        "file_read",
        "execution_submit",
        "execution_revise",
        "execution_inspect",
        "environment_list",
        "environment_build",
        "skill_view",
    ),
    WorkflowStage.VALIDATE: (
        "artifact_query",
        "artifact_aggregate",
        "report_read",
        "file_read",
        "execution_inspect",
        "skill_view",
    ),
    WorkflowStage.INTERPRET: (
        "artifact_query",
        "artifact_aggregate",
        "report_read",
        "file_read",
        "literature_search",
        "execution_inspect",
        "skill_view",
    ),
    WorkflowStage.REPORT: (
        "artifact_query",
        "artifact_aggregate",
        "report_read",
        "file_read",
        "report_check",
        "report_submit",
        "execution_inspect",
        "skill_view",
    ),
    WorkflowStage.LEARN: (
        "skill_search",
        "skill_view",
        "memory_search",
        "memory_view",
        "memory_propose_update",
    ),
}

# These observation operations do not grant any scientific/execution capability.
# Profiles must still opt in; this is a stage ceiling, not an automatic grant.
CAPABILITY_CEILINGS = {
    stage: (*names, "evidence_read", "prepare_handoff")
    for stage, names in CAPABILITY_CEILINGS.items()
}

ALL_CAPABILITIES = frozenset(
    capability
    for values in CAPABILITY_CEILINGS.values()
    for capability in values
)

CAPABILITY_INFORMATION_AUTHORITY: dict[str, InformationAuthority] = {
    "evidence_read": InformationAuthority.MODEL_CONTEXT,
    "prepare_handoff": InformationAuthority.CONTROL_STATE,
    "artifact_list": InformationAuthority.AUTHORITATIVE_EVIDENCE,
    "artifact_query": InformationAuthority.AUTHORITATIVE_EVIDENCE,
    "artifact_aggregate": InformationAuthority.AUTHORITATIVE_EVIDENCE,
    "execution_submit": InformationAuthority.AUTHORITATIVE_EVIDENCE,
    "execution_revise": InformationAuthority.AUTHORITATIVE_EVIDENCE,
    "execution_inspect": InformationAuthority.AUTHORITATIVE_EVIDENCE,
    "environment_list": InformationAuthority.AUTHORITATIVE_EVIDENCE,
    "environment_build": InformationAuthority.AUTHORITATIVE_EVIDENCE,
    "report_submit": InformationAuthority.AUTHORITATIVE_EVIDENCE,
    "report_check": InformationAuthority.CONTROL_STATE,
    "report_read": InformationAuthority.MODEL_CONTEXT,
    "file_read": InformationAuthority.MODEL_CONTEXT,
    "literature_search": InformationAuthority.MODEL_CONTEXT,
    "skill_search": InformationAuthority.MODEL_CONTEXT,
    "skill_view": InformationAuthority.MODEL_CONTEXT,
    "memory_search": InformationAuthority.MODEL_CONTEXT,
    "memory_view": InformationAuthority.MODEL_CONTEXT,
    "skill_propose_use": InformationAuthority.CONTROL_STATE,
    "memory_propose_update": InformationAuthority.CONTROL_STATE,
}

if frozenset(CAPABILITY_INFORMATION_AUTHORITY) != ALL_CAPABILITIES:
    raise RuntimeError("Capability information-authority mapping is incomplete")
