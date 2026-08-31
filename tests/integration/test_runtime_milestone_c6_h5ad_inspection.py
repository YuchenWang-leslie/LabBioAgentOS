"""Opt-in real MiMo/Pantheon nine-stage run over safe h5ad inspection."""

from __future__ import annotations

import json
import os

import anndata as ad
import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError
from scipy.sparse import csr_matrix

from labbioagentos import (
    ApplicationRunRequest,
    ApplicationRuntimeConfiguration,
    ArtifactConsumer,
    ArtifactExposureDenied,
    ArtifactQuery,
    ArtifactViewType,
    CapabilityProfile,
    InMemoryTraceSink,
    LabBioApplication,
    ModelProfile,
    PantheonRuntimeIntegrationError,
    Principal,
    Project,
    PromptProfile,
    ProviderConfigRef,
    ProviderTransport,
    ResponseSchemaRef,
    RunStatus,
    RuntimeProfileCatalog,
    RuntimeStageAssemblySpec,
    TraceEventType,
    WorkflowStage,
    WorkspaceContext,
    default_agent_profiles,
    project_run_trace,
)


pytestmark = pytest.mark.skipif(
    os.environ.get("LABBIO_RUN_LIVE_C6") != "1",
    reason="set LABBIO_RUN_LIVE_C6=1 for the real C6 h5ad inspection slice",
)

MAIN_PATH = (
    WorkflowStage.INTAKE,
    WorkflowStage.UNDERSTAND,
    WorkflowStage.PLAN,
    WorkflowStage.PREFLIGHT,
    WorkflowStage.EXECUTE,
    WorkflowStage.VALIDATE,
    WorkflowStage.INTERPRET,
    WorkflowStage.REPORT,
    WorkflowStage.LEARN,
)
CAPABILITIES = {
    WorkflowStage.INTAKE: ("artifact_list",),
    WorkflowStage.UNDERSTAND: ("artifact_list", "artifact_query"),
    WorkflowStage.PLAN: (),
    WorkflowStage.PREFLIGHT: (),
    WorkflowStage.EXECUTE: (),
    WorkflowStage.VALIDATE: (),
    WorkflowStage.INTERPRET: (),
    WorkflowStage.REPORT: ("artifact_query", "report_submit"),
    WorkflowStage.LEARN: (),
}
REQUIRED_CAPABILITIES = {
    WorkflowStage.INTAKE: ("artifact_list",),
    WorkflowStage.UNDERSTAND: ("artifact_list", "artifact_query"),
    WorkflowStage.REPORT: ("report_submit",),
}
PRIVATE_BARCODES = tuple(f"c6-private-cell-{index:03d}" for index in range(12))
PRIVATE_GENES = tuple(f"C6_PRIVATE_GENE_{index:03d}" for index in range(8))
USER_REQUEST = (
    "Inspect this single-cell dataset, explain its structure and available metadata, "
    "identify data-quality considerations visible from safe summaries, and propose a "
    "future analysis plan. Do not perform downstream single-cell analysis."
)


def _write_h5ad(path) -> None:
    obs = pd.DataFrame(
        {
            "cell_id": PRIVATE_BARCODES,
            "sample": pd.Categorical(["sample-a"] * 6 + ["sample-b"] * 6),
            "condition": pd.Categorical(["control", "treated"] * 6),
            "broad_cell_type": pd.Categorical(["T", "B", "myeloid"] * 4),
            "total_counts": np.arange(100, 220, 10, dtype=np.float64),
            "pct_counts_mt": np.linspace(1.0, 12.0, 12, dtype=np.float64),
        },
        index=PRIVATE_BARCODES,
    )
    obs.loc[PRIVATE_BARCODES[-1], "pct_counts_mt"] = np.nan
    var = pd.DataFrame(
        {"feature_type": pd.Categorical(["Gene Expression"] * 8)},
        index=PRIVATE_GENES,
    )
    data = ad.AnnData(
        X=csr_matrix(np.arange(96, dtype=np.float32).reshape(12, 8)),
        obs=obs,
        var=var,
    )
    data.layers["counts"] = data.X.copy()
    data.obsm["X_pca"] = np.arange(36, dtype=np.float32).reshape(12, 3)
    data.raw = data.copy()
    data.write_h5ad(path)


def _catalog() -> RuntimeProfileCatalog:
    profiles = default_agent_profiles()
    ceilings = {
        "coordinator-capabilities": (
            "artifact_list",
            "artifact_query",
            "report_submit",
        ),
        "execution-capabilities": ("artifact_query", "execution_submit"),
        "reviewer-capabilities": ("artifact_query",),
    }
    return RuntimeProfileCatalog(
        agents=profiles,
        prompts=(
            PromptProfile(
                template_id="runtime-generic",
                version="c6-1",
                template_text="{protocol}",
                max_value_length=8_000,
            ),
        ),
        models=(
            ModelProfile(
                profile_key="runtime-default",
                version="c6-1",
                model_identifier="mimo-v2.5-pro",
                provider_config=ProviderConfigRef(
                    config_id="staging-mimo-c6", provider="mimo"
                ),
                transport=ProviderTransport.OPENAI_CHAT_COMPLETIONS,
                thinking_enabled=False,
                max_output_tokens=8_192,
            ),
        ),
        schemas=(ResponseSchemaRef(),),
        capabilities=tuple(
            CapabilityProfile(
                profile_key=profile.capability_profile_key,
                version="c6-1",
                capability_allowlist=ceilings[profile.capability_profile_key],
            )
            for profile in profiles
        ),
    )


def _capability_protocol(stage: WorkflowStage) -> str:
    common = (
        f"CAPABILITY MODE for exact stage {stage.value}. Use only the exposed LabBio "
        "tools. Never request or reproduce RAW h5ad content, matrix values, observation "
        "rows, axis index values, complete gene lists, host paths, credentials, provider "
        "messages, or hidden reasoning. Do not perform downstream single-cell analysis. "
        "Do not emit RuntimeStageResult in this turn. "
    )
    directions = {
        WorkflowStage.INTAKE: (
            "Call artifact_list and establish which opaque RAW and safe inspection "
            "Artifact references are available."
        ),
        WorkflowStage.UNDERSTAND: (
            "Call artifact_list. Query h5ad-structural with SCHEMA and h5ad-aggregate "
            "with SUMMARY using artifact_query. Understand the dataset only from those "
            "safe views; RAW has no allowed view."
        ),
        WorkflowStage.REPORT: (
            "Query the safe h5ad inspection Artifacts as needed, compose a concise report "
            "that separates observed structure/summaries, limitations, and a future "
            "analysis plan, then call report_submit exactly once with the structural and "
            "aggregate Artifact IDs as evidence."
        ),
    }
    return common + directions[stage]


def _finalization_protocol(stage: WorkflowStage) -> str:
    if stage is WorkflowStage.LEARN:
        action = "action='finish' with no target_stage or other exclusive fields"
    else:
        target = MAIN_PATH[MAIN_PATH.index(stage) + 1]
        action = (
            f"action='transition' and target_stage='{target.value}', with no user_prompt, "
            "domain_reference_id, or failure reason"
        )
    requirement = {
        WorkflowStage.UNDERSTAND: (
            "Base requirements and evidence only on validated safe capability evidence."
        ),
        WorkflowStage.PLAN: (
            "Propose a future analysis plan selected by the runtime, but do not claim it "
            "was executed and do not include task-specific code."
        ),
        WorkflowStage.PREFLIGHT: (
            "Assess only whether safe inspection evidence is structurally sufficient for "
            "this inspection/report milestone."
        ),
        WorkflowStage.EXECUTE: (
            "No downstream analysis is authorized in C6. Set execution_status to a bounded "
            "not-executed inspection-only status and provide no execution/output reference."
        ),
        WorkflowStage.VALIDATE: (
            "Validate the inspection/report boundary and explicitly retain limitations."
        ),
        WorkflowStage.INTERPRET: (
            "State only structure and quality considerations supported by safe summaries; "
            "do not infer biological conclusions."
        ),
        WorkflowStage.REPORT: (
            "Include the ReportReceipt Artifact ID as report_reference."
        ),
    }.get(stage, "Preserve only useful safe references in the typed body.")
    return (
        f"FINALIZE MODE for exact stage {stage.value}. LabBio tools are unavailable. "
        "Use only bounded stage input, validated prior_results, and capability_evidence. "
        "Keep the summary under 500 characters, lists to at most eight items, and each "
        "item under 500 characters. Return exactly one strict RuntimeStageResult whose "
        f"stage_id and body kind are {stage.value}. {requirement} For NextActionProposal "
        f"use {action}. Do not include RAW values, rows, complete gene lists, paths, "
        "credentials, provider messages, or hidden reasoning."
    )


def _model_visible_json(value) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True)


@pytest.mark.asyncio
async def test_real_h5ad_safe_inspection_vertical_slice(tmp_path):
    assert os.environ.get("OPENAI_API_KEY"), "OPENAI_API_KEY must be mapped externally"
    assert os.environ.get("OPENAI_API_BASE"), "OPENAI_API_BASE must be mapped externally"
    principal = Principal(user_id="user-c6", lab_id="lab-c6")
    workspace = WorkspaceContext(
        user_id="user-c6", project_id="project-c6", lab_id="lab-c6"
    )
    sink = InMemoryTraceSink()
    visible_boundaries: list[str] = []

    def observe(_kind: str, value: object) -> None:
        visible_boundaries.append(_model_visible_json(value))

    assemblies = []
    for stage in MAIN_PATH:
        capabilities = CAPABILITIES[stage]
        assemblies.append(
            RuntimeStageAssemblySpec(
                stage_id=stage,
                root_profile_key=(
                    "reviewer" if stage is WorkflowStage.VALIDATE else "coordinator"
                ),
                prompt_template_key="runtime-generic",
                capability_allowlist=capabilities,
                capability_prompt_values=(
                    {"protocol": _capability_protocol(stage)} if capabilities else {}
                ),
                finalization_prompt_values={
                    "protocol": _finalization_protocol(stage)
                },
                capability_phase_enabled=bool(capabilities),
                required_capabilities=REQUIRED_CAPABILITIES.get(stage, ()),
            )
        )

    application = LabBioApplication(
        ApplicationRuntimeConfiguration(
            artifact_root=tmp_path / "artifacts",
            execution_workspace_root=tmp_path / "executions",
            allowed_input_roots=(tmp_path,),
            projects=(
                Project(
                    project_id="project-c6",
                    lab_id="lab-c6",
                    owner_user_id="user-c6",
                ),
            ),
            profile_catalog=_catalog(),
            stage_assemblies=tuple(assemblies),
            trace_sink=sink,
            boundary_observer=observe,
        )
    )
    source = tmp_path / "real-format-single-cell.h5ad"
    _write_h5ad(source)
    raw = application.register_input_file(
        source,
        principal=principal,
        workspace=workspace,
        artifact_type="h5ad",
        metadata={"format": "h5ad"},
    )
    inspected = application.inspect_h5ad(
        raw.artifact_id,
        principal=principal,
        workspace=workspace,
    )
    handle = application.create_run(
        ApplicationRunRequest(
            task_text=USER_REQUEST,
            principal=principal,
            workspace=workspace,
            input_artifact_ids=(raw.artifact_id,),
            context_artifact_ids=(
                inspected.structural_artifact.artifact_id,
                inspected.aggregate_artifact.artifact_id,
            ),
        )
    )
    try:
        outcome = await application.run(handle)
    except PantheonRuntimeIntegrationError as exc:
        cause = exc.__cause__
        if isinstance(cause, ValidationError):
            safe_errors = [
                {
                    "location": [str(item) for item in error.get("loc", ())],
                    "type": error.get("type"),
                    "message": error.get("msg"),
                }
                for error in cause.errors(
                    include_url=False,
                    include_context=False,
                    include_input=False,
                )[:16]
            ]
            print("safe_runtime_validation_errors=" + json.dumps(safe_errors))
        raise

    events = application.trace_events(handle)
    assert outcome.status is RunStatus.COMPLETED
    assert outcome.final_stage is WorkflowStage.LEARN
    assert project_run_trace(events, handle.run_id).stage_path == MAIN_PATH
    assert not any(
        event.event_type
        in {TraceEventType.EXECUTION_STARTED, TraceEventType.EXECUTION_COMPLETED}
        for event in events
    )
    for artifact_id in (
        inspected.structural_artifact.artifact_id,
        inspected.aggregate_artifact.artifact_id,
    ):
        assert str(artifact_id) in "\n".join(visible_boundaries)
    with pytest.raises(ArtifactExposureDenied):
        application.artifact_exposure.artifact_query(
            raw.artifact_id,
            ArtifactQuery(view_type=ArtifactViewType.SCHEMA),
            ArtifactConsumer.REMOTE_LLM,
            principal=principal,
        )

    report_refs = tuple(
        ref
        for ref in application.artifact_store.list_refs()
        if ref.run_id == handle.run_id and ref.artifact_type == "report"
    )
    assert len(report_refs) == 1
    assert outcome.report_artifact_ids == (report_refs[0].artifact_id,)
    report_text = application.artifact_store.load_for_view(
        report_refs[0].artifact_id
    ).representation.stored_content
    assert isinstance(report_text, str) and report_text.strip()
    assert "12" in report_text and "8" in report_text
    assert "plan" in report_text.lower() or "analysis" in report_text.lower()

    trace_json = json.dumps([event.model_dump(mode="json") for event in events])
    visible_json = "\n".join(visible_boundaries)
    for private_value in (*PRIVATE_BARCODES, *PRIVATE_GENES):
        assert private_value not in visible_json
        assert private_value not in report_text
        assert private_value not in trace_json
    for forbidden in (
        str(tmp_path),
        "storage_locator",
        "h5ad_contents",
        "raw_matrix",
        "provider_raw_body",
        "reasoning_content",
    ):
        assert forbidden.lower() not in visible_json.lower()
        assert forbidden.lower() not in report_text.lower()
        assert forbidden.lower() not in trace_json.lower()
    for secret_name in ("OPENAI_API_KEY", "MIMO_API_KEY"):
        secret = os.environ.get(secret_name)
        if secret and len(secret) >= 8:
            assert secret not in visible_json
            assert secret not in report_text
            assert secret not in trace_json
