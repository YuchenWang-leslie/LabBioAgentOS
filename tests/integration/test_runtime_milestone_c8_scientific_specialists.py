"""Opt-in bounded real-provider acceptance for the C8 specialist layer."""

from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from labbioagentos import (
    AccessService,
    ArtifactExposureClass,
    ArtifactExposureService,
    AuthorizationPolicy,
    CapabilityEvidenceStatus,
    CapabilityProfile,
    DelegationPolicyPlugin,
    InMemoryDelegationPolicy,
    InMemoryProjectStore,
    JsonlTraceSink,
    LocalArtifactStore,
    ModelProfile,
    PantheonRuntimeFactory,
    PerInvocationPantheonStageInvoker,
    Principal,
    Project,
    PromptProfile,
    ProviderConfigRef,
    ProviderTransport,
    ReportSubmissionService,
    ResponseSchemaRef,
    RunTraceRecorder,
    RuntimeAgentCapabilitySpec,
    RuntimeCapabilityServices,
    RuntimeEvidenceReference,
    RuntimeEvidenceRole,
    RuntimeInvocationMode,
    RuntimeProfileCatalog,
    RuntimeReferenceKind,
    RuntimeStageAssemblySpec,
    RuntimeStageInput,
    RuntimeWorkspaceIdentifiers,
    TraceEventType,
    WorkflowStage,
    WorkspaceContext,
    default_agent_profiles,
    scientific_specialist_profiles,
)
from labbioagentos.artifacts import ExposurePolicy


pytestmark = pytest.mark.skipif(
    os.environ.get("LABBIO_RUN_LIVE_C8") != "1",
    reason="set LABBIO_RUN_LIVE_C8=1 for the bounded C8 provider acceptance",
)

SOURCE_STORE = Path(
    ".local/c7-final-closeout/test_c7_c_d_real_runtime_selec0/artifacts"
)
SOURCE_ARTIFACT_ID = UUID("eb3a70a2-2f2e-42f2-9d57-6e5de31390f8")
SPECIALIST_KEYS = {
    "single-cell-analysis-specialist",
    "scientific-methods-reviewer",
}

CAPABILITY_PROTOCOL = """
CAPABILITY MODE for a bounded scientific REPORT stage. The user requests an
independent specialist assessment where useful. Discover the available peers
and choose any scientifically appropriate target at runtime; no peer is
preselected. The current INPUT_EVIDENCE ARTIFACT reference identifies the only
governed PBMC3k-derived QC summary for this task. Ground factual content by
using artifact_query. A delegated peer must inspect that governed Artifact
through its own available artifact_query rather than relying only on parent
prose. Specialist prose is advice, not authoritative evidence. Compose a short
methods-oriented report, then call report_submit once with the governed input
Artifact as evidence. Do not emit RuntimeStageResult in capability mode. Never
request or reproduce RAW records, host paths, credentials, provider messages,
scripts, process streams, or hidden reasoning.
""".strip()

FINALIZATION_PROTOCOL = """
FINALIZE MODE for exact stage REPORT. LabBio capabilities are unavailable.
Treat capability_evidence as AUTHORITATIVE_EVIDENCE and any Agent prose as
MODEL_CONTEXT. Return exactly one strict REPORT RuntimeStageResult. Use the
registered Report Artifact from the completed report_submit evidence as
report_reference, cite the governed input Artifact, and use action='transition'
with target_stage='LEARN'. Do not include raw records, paths, credentials,
provider messages, scripts, process streams, or hidden reasoning.
""".strip()


def _catalog() -> RuntimeProfileCatalog:
    profiles = (*default_agent_profiles(), *scientific_specialist_profiles())
    allowlists = {
        "coordinator-capabilities": ("artifact_query", "report_submit"),
        "execution-capabilities": (),
        "reviewer-capabilities": ("artifact_query",),
        "single-cell-analysis-specialist-capabilities": ("artifact_query",),
        "scientific-methods-reviewer-capabilities": ("artifact_query",),
    }
    return RuntimeProfileCatalog(
        agents=profiles,
        prompts=(
            PromptProfile(
                template_id="runtime-generic",
                version="c8-live-1",
                template_text="{protocol}",
                max_value_length=8_000,
            ),
        ),
        models=(
            ModelProfile(
                profile_key="runtime-default",
                version="c8-live-1",
                model_identifier=os.environ.get("MIMO_MODEL", "mimo-v2.5-pro"),
                provider_config=ProviderConfigRef(
                    config_id="staging-mimo-c8", provider="mimo"
                ),
                transport=ProviderTransport.OPENAI_CHAT_COMPLETIONS,
                thinking_enabled=False,
                max_output_tokens=4_096,
            ),
        ),
        schemas=(ResponseSchemaRef(),),
        capabilities=tuple(
            CapabilityProfile(
                profile_key=profile.capability_profile_key,
                version="c8-live-1",
                capability_allowlist=allowlists[profile.capability_profile_key],
            )
            for profile in profiles
        ),
    )


def _copy_governed_source(store: LocalArtifactStore, run_root: Path):
    source_root = Path(
        os.environ.get("LABBIO_C8_SOURCE_STORE", str(SOURCE_STORE))
    ).resolve()
    source_store = LocalArtifactStore(source_root)
    source_ref = source_store.get_ref(SOURCE_ARTIFACT_ID)
    assert source_ref.exposure_class is ArtifactExposureClass.DERIVED
    source_view = source_store.load_for_view(source_ref.artifact_id)
    return store.register(
        artifact_type=source_ref.artifact_type,
        exposure_class=ArtifactExposureClass.DERIVED,
        representation=source_view.representation,
        owner_user_id="user-c8-live",
        project_id="project-c8-live",
        lab_id="lab-c8-live",
        run_id=source_ref.run_id,
        stage_id=source_ref.stage_id,
        producer_invocation_id=source_ref.producer_invocation_id,
        metadata={
            "source_artifact_id": str(source_ref.artifact_id),
            "source_store_kind": "accepted-c7-local-evidence",
            "c8_run_root": run_root.name,
        },
    )


@pytest.mark.asyncio
async def test_real_provider_selects_specialist_with_own_governed_evidence():
    assert os.environ.get("OPENAI_API_KEY"), "OPENAI_API_KEY must be mapped externally"
    assert os.environ.get("OPENAI_API_BASE"), "OPENAI_API_BASE must be mapped externally"
    live_root = Path(os.environ.get("LABBIO_C8_LIVE_ROOT", ".local/c8-live")).resolve()
    run_root = live_root / str(uuid4())
    run_root.mkdir(parents=True, exist_ok=False)
    store = LocalArtifactStore(run_root / "artifacts")
    source = _copy_governed_source(store, run_root)

    projects = InMemoryProjectStore()
    projects.register(
        Project(
            project_id="project-c8-live",
            lab_id="lab-c8-live",
            owner_user_id="user-c8-live",
        )
    )
    access = AccessService(projects, AuthorizationPolicy())
    principal = Principal(user_id="user-c8-live", lab_id="lab-c8-live")
    workspace = WorkspaceContext(
        user_id="user-c8-live",
        project_id="project-c8-live",
        lab_id="lab-c8-live",
    )
    recorder = RunTraceRecorder(JsonlTraceSink(run_root / "run-trace.jsonl"))
    exposure = ArtifactExposureService(
        store,
        ExposurePolicy(),
        access_service=access,
        trace_recorder=recorder,
    )
    reporting = ReportSubmissionService(store, access, trace_recorder=recorder)
    observed = {}

    def observe(kind: str, value: object) -> None:
        observed[kind] = value
        with (run_root / "runtime-boundaries.jsonl").open(
            "a", encoding="utf-8"
        ) as handle:
            handle.write(
                json.dumps(
                    {"kind": kind, "payload": value.model_dump(mode="json")},
                    sort_keys=True,
                )
            )
            handle.write("\n")

    run_id, invocation_id = uuid4(), uuid4()
    stage_input = RuntimeStageInput(
        run_id=run_id,
        stage_id=WorkflowStage.REPORT,
        invocation_id=invocation_id,
        instruction=(
            "Prepare a concise methods-oriented assessment of the governed PBMC3k "
            "QC summary and obtain an independent specialist assessment where useful."
        ),
        workspace=RuntimeWorkspaceIdentifiers(
            user_id=workspace.user_id,
            project_id=workspace.project_id,
            lab_id=workspace.lab_id,
        ),
        authoritative_evidence_references=(
            RuntimeEvidenceReference(
                reference_id=str(source.artifact_id),
                kind=RuntimeReferenceKind.ARTIFACT,
                label="Governed PBMC3k derived QC summary",
                evidence_role=RuntimeEvidenceRole.INPUT_EVIDENCE,
                producer_invocation_id=source.producer_invocation_id,
            ),
        ),
        allowed_capabilities=("artifact_query", "report_submit"),
    )
    invoker = PerInvocationPantheonStageInvoker(
        assembly=RuntimeStageAssemblySpec(
            stage_id=WorkflowStage.REPORT,
            root_profile_key="coordinator",
            prompt_template_key="runtime-generic",
            capability_allowlist=("artifact_query", "report_submit"),
            capability_peer_specs=(
                RuntimeAgentCapabilitySpec(
                    profile_key="single-cell-analysis-specialist",
                    capability_allowlist=("artifact_query",),
                ),
                RuntimeAgentCapabilitySpec(
                    profile_key="scientific-methods-reviewer",
                    capability_allowlist=("artifact_query",),
                ),
            ),
            capability_prompt_values={"protocol": CAPABILITY_PROTOCOL},
            finalization_prompt_values={"protocol": FINALIZATION_PROTOCOL},
            required_capabilities=("report_submit",),
            max_capability_turns=20,
        ),
        factory=PantheonRuntimeFactory(_catalog()),
        principal=principal,
        workspace=workspace,
        services=RuntimeCapabilityServices(
            artifact_store=store,
            artifact_exposure=exposure,
            report_submission=reporting,
            trace_recorder=recorder,
        ),
        trace_recorder=recorder,
        plugin_factory=lambda: [
            DelegationPolicyPlugin(
                InMemoryDelegationPolicy(
                    {
                        "coordinatoragent": {
                            "singlecellanalysisspecialist",
                            "scientificmethodsreviewer",
                        }
                    }
                )
            )
        ],
        boundary_observer=observe,
    )

    result = await invoker.invoke(stage_input)
    bundle = observed["capability_evidence"]
    events = recorder.events(run_id)
    delegated = [
        event
        for event in events
        if event.event_type is TraceEventType.DELEGATION_COMPLETED
    ]
    assert delegated
    assert all(event.target in {
        "singlecellanalysisspecialist",
        "scientificmethodsreviewer",
    } for event in delegated)
    assert all(event.parent_invocation_id == invocation_id for event in delegated)
    assert all(event.execution_context_id for event in delegated)
    assert all(event.parent_tool_call_id for event in delegated)
    assert all(event.chain_path for event in delegated)

    child_queries = [
        item
        for item in bundle.items
        if item.actor_profile_key in SPECIALIST_KEYS
        and item.capability_name == "artifact_query"
        and item.status is CapabilityEvidenceStatus.COMPLETED
        and item.artifact_query_request is not None
        and str(item.artifact_query_request.artifact_id) == str(source.artifact_id)
    ]
    assert child_queries
    assert any(
        item.actor_profile_key == "coordinator"
        and item.capability_name == "report_submit"
        and item.status is CapabilityEvidenceStatus.COMPLETED
        for item in bundle.items
    )
    assert bundle.delegation_trace_event_ids

    reports = tuple(
        ref
        for ref in store.list_refs()
        if ref.run_id == run_id and ref.artifact_type == "report"
    )
    assert len(reports) == 1
    report = store.load_for_view(reports[0].artifact_id)
    assert str(source.artifact_id) in report.representation.summary[
        "evidence_artifact_ids"
    ]
    assert result.body.report_reference is not None
    assert result.body.report_reference.reference_id == str(reports[0].artifact_id)

    trace_text = (run_root / "run-trace.jsonl").read_text(encoding="utf-8")
    boundary_text = (run_root / "runtime-boundaries.jsonl").read_text(
        encoding="utf-8"
    )
    report_text = report.representation.stored_content or ""
    surfaces = trace_text + boundary_text + report_text
    for forbidden in (
        "storage_locator",
        "provider_raw_body",
        "reasoning_content",
        "script_content",
        "authorization_payload",
    ):
        assert forbidden not in surfaces
    for secret_name in ("OPENAI_API_KEY", "MIMO_API_KEY"):
        secret = os.environ.get(secret_name)
        if secret:
            assert secret not in surfaces

    acceptance = {
        "run_id": str(run_id),
        "invocation_id": str(invocation_id),
        "source_c7_artifact_id": str(SOURCE_ARTIFACT_ID),
        "c8_input_artifact_id": str(source.artifact_id),
        "selected_specialists": sorted({event.target for event in delegated}),
        "child_capability_invocation_ids": [
            str(item.capability_invocation_id) for item in child_queries
        ],
        "report_artifact_id": str(reports[0].artifact_id),
        "result_id": str(result.result_id),
        "pantheon_mode": RuntimeInvocationMode.CAPABILITY.value,
        "leak_audit": "PASS",
    }
    (run_root / "acceptance.json").write_text(
        json.dumps(acceptance, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
