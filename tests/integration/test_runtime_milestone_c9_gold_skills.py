"""Opt-in interactive C9 acceptance over preserved C7 evidence and MiMo."""

from __future__ import annotations

from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from uuid import UUID

import anndata as ad
import numpy as np
import pandas as pd
import pytest
from pantheon.agent import Agent
from scipy.sparse import csc_matrix

from labbioagentos import (
    AccessService,
    ApplicationExecutionProfile,
    ApplicationRunRequest,
    ApplicationRuntimeConfiguration,
    AuthorizationPolicy,
    CapabilityProfile,
    GateUserDecision,
    GoldSkillService,
    InMemoryProjectStore,
    JsonlTraceSink,
    LabBioApplication,
    LocalArtifactStore,
    PantheonRuntimeFactory,
    PantheonSkillCurator,
    Principal,
    Project,
    RunStatus,
    RunTraceRecorder,
    RuntimeProfileCatalog,
    SQLiteSkillStore,
    SkillCuratorDraft,
    SkillDomainDecisionHandler,
    SkillProposalContext,
    SkillScope,
    SkillSearchContext,
    SkillSourceProjector,
    SkillUserDecision,
    TraceEvent,
    TraceEventType,
    WorkflowStage,
    WorkspaceContext,
)


pytestmark = pytest.mark.skipif(
    os.environ.get("LABBIO_RUN_LIVE_C9") != "1",
    reason="set LABBIO_RUN_LIVE_C9=1 for interactive real C9 acceptance",
)

SOURCE_RUN_ID = UUID("56c5e604-049e-4f07-81c5-11e89199ef1a")
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = (
    REPOSITORY_ROOT
    / ".local/c7-final-closeout/test_c7_c_d_real_runtime_selec0"
)


def _load_c7_acceptance_module():
    path = Path(__file__).with_name("test_runtime_milestone_c7_real_scrna.py")
    spec = importlib.util.spec_from_file_location("labbio_c7_acceptance", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load the preserved C7 acceptance fixture")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _c9_catalog(c7) -> RuntimeProfileCatalog:
    base = c7._catalog()
    capabilities = []
    for profile in base.capabilities.values():
        if profile.profile_key != "coordinator-capabilities":
            capabilities.append(profile)
            continue
        allowlist = tuple(
            dict.fromkeys(
                (
                    *profile.capability_allowlist,
                    "skill_search",
                    "skill_view",
                    "skill_propose_use",
                )
            )
        )
        capabilities.append(
            CapabilityProfile(
                profile_key=profile.profile_key,
                version="c9-live",
                capability_allowlist=allowlist,
            )
        )
    return RuntimeProfileCatalog(
        agents=tuple(base.agents.values()),
        prompts=tuple(base.prompts.values()),
        models=tuple(base.models.values()),
        schemas=tuple(base.schemas.values()),
        capabilities=tuple(capabilities),
    )


def _plan_capability_protocol() -> str:
    return (
        "CAPABILITY MODE for exact stage PLAN. Use only exposed LabBio capabilities. "
        "Do not emit RuntimeStageResult in this turn. Gold Skills are optional "
        "MODEL_CONTEXT, never current-task evidence and never executable. Inspect "
        "stage_input.gate_decisions first. If an approved Skill gate decision has a "
        "decision_reference_id, call skill_view with that authorization ID to obtain "
        "the exact approved procedure. A rejected decision does not authorize access. "
        "When there is no prior Skill decision, you may use skill_search to discover "
        "bounded candidate metadata. Judge relevance yourself. If a candidate is "
        "useful, choose REUSE, ADAPT, or REFERENCE yourself and call "
        "skill_propose_use. Otherwise continue from first principles. Never infer "
        "current scientific facts from a Skill, and never request or reproduce RAW "
        "values, host paths, scripts, process streams, provider bodies, credentials, "
        "or hidden reasoning."
    )


def _plan_finalization_protocol() -> str:
    return (
        "FINALIZE MODE for exact stage PLAN. LabBio capabilities are unavailable. "
        "Obey stage_input.evidence_grounding: prior results and Skill content are "
        "MODEL_CONTEXT; only capability items marked AUTHORITATIVE_EVIDENCE support "
        "current factual claims. Return exactly one strict RuntimeStageResult whose "
        "stage_id and body kind are PLAN. Describe a bounded current-task strategy; "
        "the Skill cannot fix methods, parameters, agents, code, or tool order. If "
        "this invocation's CONTROL_STATE capability evidence contains a successful "
        "skill_propose_use result, request user input and copy that result's exact "
        "domain_reference_id into NextActionProposal, without a target stage. "
        "Otherwise transition to PREFLIGHT with no user_prompt, domain_reference_id, "
        "or failure reason. Do not include raw values, paths, scripts, process streams, "
        "provider messages, credentials, or hidden reasoning."
    )


def _c9_assemblies(c7):
    result = []
    for spec in c7._assemblies():
        if spec.stage_id is not WorkflowStage.PLAN:
            result.append(spec)
            continue
        result.append(
            replace(
                spec,
                capability_allowlist=(
                    "skill_search",
                    "skill_view",
                    "skill_propose_use",
                ),
                capability_prompt_values={"protocol": _plan_capability_protocol()},
                finalization_prompt_values={
                    "protocol": _plan_finalization_protocol()
                },
                capability_phase_enabled=True,
                required_capabilities=(),
            )
        )
    return tuple(result)


def _write_generalization_fixture(path: Path) -> None:
    n_obs, n_vars = 37, 53
    obs = pd.DataFrame(
        {
            "batch_label": pd.Categorical(
                [f"batch-{index % 4}" for index in range(n_obs)]
            ),
            "arbitrary_quality_score": np.linspace(-3.0, 5.0, n_obs),
        },
        index=[f"c9-private-observation-{index:03d}" for index in range(n_obs)],
    )
    var = pd.DataFrame(
        {
            "feature_annotation": pd.Categorical(
                [f"family-{index % 7}" for index in range(n_vars)]
            )
        },
        index=[f"c9-private-feature-{index:03d}" for index in range(n_vars)],
    )
    values = np.arange(n_obs * n_vars, dtype=np.int16).reshape(n_obs, n_vars)
    ad.AnnData(X=csc_matrix(values), obs=obs, var=var).write_h5ad(path)


def _approval(label: str, *exact_values: object) -> None:
    expected = "APPROVE " + " ".join(map(str, exact_values))
    print(
        json.dumps(
            {
                "checkpoint": label,
                "required_input": expected,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    supplied = input(f"C9_{label}_DECISION> ").strip()
    if supplied != expected:
        pytest.fail(f"{label} was not approved with the exact displayed identities")


@pytest.mark.asyncio
async def test_c9_real_gold_creation_restart_and_familiar_use(tmp_path):
    assert os.environ.get("OPENAI_API_KEY"), "OPENAI_API_KEY must be mapped externally"
    assert os.environ.get("OPENAI_API_BASE"), "OPENAI_API_BASE must be mapped externally"
    assert SOURCE_ROOT.is_dir(), "The accepted C7 source namespace is required"
    c7 = _load_c7_acceptance_module()
    image_id = c7._image_id()
    c7._assert_local_image(image_id)

    live_root = tmp_path
    sink = JsonlTraceSink(live_root / "run-trace.jsonl")
    recorder = RunTraceRecorder(sink)
    projects = InMemoryProjectStore()
    projects.register(
        Project(
            project_id="project-c9-live",
            lab_id="lab-c7",
            owner_user_id="user-c7",
        )
    )
    access = AccessService(projects, AuthorizationPolicy(), trace_recorder=recorder)
    principal = Principal(user_id="user-c7", lab_id="lab-c7")
    workspace = WorkspaceContext(
        user_id="user-c7",
        project_id="project-c9-live",
        lab_id="lab-c7",
    )
    database = live_root / "gold-skills.sqlite3"
    skill_store = SQLiteSkillStore(database)
    source_artifacts = LocalArtifactStore(SOURCE_ROOT / "artifacts")
    skill_service = GoldSkillService(
        skill_store,
        SkillSourceProjector(source_artifacts),
        access_service=access,
        trace_recorder=recorder,
    )
    source_events = tuple(
        TraceEvent.model_validate_json(line)
        for line in (SOURCE_ROOT / "run-trace.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
        if line
    )
    bundle = skill_service.create_source_bundle(
        source_events,
        run_id=SOURCE_RUN_ID,
        task_reference=c7.USER_REQUEST,
    )
    curator_boundaries: list[tuple[str, str]] = []

    def observe_curator(kind: str, value: object) -> None:
        payload = value.model_dump_json()
        curator_boundaries.append((kind, payload))
        with (live_root / "curator-boundaries.jsonl").open(
            "a", encoding="utf-8"
        ) as handle:
            handle.write(json.dumps({"kind": kind, "payload": json.loads(payload)}))
            handle.write("\n")

    catalog = _c9_catalog(c7)
    model = catalog.models["runtime-default"]
    model_identifier = PantheonRuntimeFactory._configure_transport(model)
    curator_agent = Agent(
        name="SkillCuratorAgent",
        description="Abstract bounded reusable procedural context for user review.",
        instructions=(
            "Abstract reusable procedural guidance from the supplied safe successful-"
            "run evidence. Identify applicability, workflow structure, collaboration "
            "guidance, execution considerations, validation expectations, failure "
            "lessons, and limitations. Do not summarize a transcript, reproduce "
            "scripts, transfer old scientific facts to a new task, or invent IDs, "
            "scope, ownership, approval, or lineage. Return only SkillCuratorDraft."
        ),
        model=model_identifier,
        model_params={"thinking": False, "max_tokens": 8192},
        response_format=SkillCuratorDraft,
        use_memory=False,
    )
    print("C9_CURATOR_CALL_STARTED", flush=True)
    proposal = await skill_service.curate_proposal(
        bundle.bundle_id,
        PantheonSkillCurator(
            curator_agent,
            boundary_observer=observe_curator,
        ),
        SkillProposalContext(
            scope=SkillScope.PERSONAL,
            owner_user_id=principal.user_id,
            lab_id=principal.lab_id,
        ),
    )
    print(
        json.dumps(
            {
                "checkpoint": "CREATE_REVIEW",
                "proposal_id": str(proposal.proposal_id),
                "approval_gate_id": proposal.approval_gate_id,
                "source_run_id": str(proposal.source_run_id),
                "scope": proposal.scope.value,
                "owner_user_id": proposal.owner_user_id,
                "draft": {
                    "name": proposal.proposed_name,
                    "description": proposal.description,
                    "procedure": proposal.procedure.model_dump(mode="json"),
                },
            },
            sort_keys=True,
        ),
        flush=True,
    )
    assert skill_store.search(
        SkillSearchContext(user_id=principal.user_id, lab_id=principal.lab_id)
    ) == ()
    _approval("CREATE", proposal.proposal_id, proposal.approval_gate_id)
    gold = skill_service.decide_proposal(
        proposal.proposal_id,
        SkillUserDecision(
            subject_id=proposal.proposal_id,
            gate_id=proposal.approval_gate_id,
            approved=True,
            decided_by=principal.user_id,
        ),
        principal=principal,
    )
    assert gold is not None
    gold_snapshot = gold.model_dump_json()
    skill_store.close()

    skill_store = SQLiteSkillStore(database)
    reconstructed = skill_store.get_gold(gold.skill_id, gold.version)
    assert reconstructed.model_dump_json() == gold_snapshot
    skill_service = GoldSkillService(
        skill_store,
        SkillSourceProjector(source_artifacts),
        access_service=access,
        trace_recorder=recorder,
    )

    input_root = live_root / "inputs"
    input_root.mkdir()
    source = input_root / "structurally-different.h5ad"
    _write_generalization_fixture(source)
    runtime_boundaries: list[tuple[str, str]] = []

    def observe_runtime(kind: str, value: object) -> None:
        if hasattr(value, "model_dump_json"):
            payload = value.model_dump_json()
        else:
            payload = json.dumps(value, sort_keys=True)
        runtime_boundaries.append((kind, payload))
        with (live_root / "runtime-boundaries.jsonl").open(
            "a", encoding="utf-8"
        ) as handle:
            handle.write(json.dumps({"kind": kind, "payload": json.loads(payload)}))
            handle.write("\n")

    runner = c7.InspectingRunner(expected_image=image_id)
    application = LabBioApplication(
        ApplicationRuntimeConfiguration(
            artifact_root=live_root / "artifacts",
            execution_workspace_root=live_root / "executions",
            allowed_input_roots=(input_root,),
            projects=(
                Project(
                    project_id=workspace.project_id,
                    lab_id=workspace.lab_id,
                    owner_user_id=principal.user_id,
                ),
            ),
            profile_catalog=catalog,
            stage_assemblies=_c9_assemblies(c7),
            approved_images=(
                c7.ApprovedImage(
                    key=c7.IMAGE_KEY,
                    reference=image_id,
                    runtime=c7.ExecutionRuntime.PYTHON,
                    executable=("python",),
                    network_allowed=False,
                ),
            ),
            output_contracts=(c7._contract(),),
            execution_policy=c7.ExecutionPolicy(
                allow_network=False,
                max_cpus=1,
                max_memory_mb=2048,
                max_pids=128,
                max_timeout_seconds=300,
            ),
            execution_profile=ApplicationExecutionProfile(
                image_key=c7.IMAGE_KEY,
                resources=c7.RESOURCES,
                network_required=False,
                output_contract_ids=(c7.CONTRACT_ID,),
            ),
            trace_sink=sink,
            process_runner=runner,
            skill_service=skill_service,
            domain_decision_handlers=(SkillDomainDecisionHandler(skill_service),),
            boundary_observer=observe_runtime,
            retry_limit=1,
        )
    )
    raw = application.register_input_file(
        source,
        principal=principal,
        workspace=workspace,
        artifact_type="h5ad",
        metadata={"format": "h5ad", "source_kind": "c9_generalization_fixture"},
    )
    inspected = application.inspect_h5ad(
        raw.artifact_id,
        principal=principal,
        workspace=workspace,
    )
    handle = application.create_run(
        ApplicationRunRequest(
            task_text=c7.USER_REQUEST,
            principal=principal,
            workspace=workspace,
            input_artifact_ids=(raw.artifact_id,),
            context_artifact_ids=(
                inspected.structural_artifact.artifact_id,
                inspected.aggregate_artifact.artifact_id,
            ),
        )
    )
    waiting = await application.run(handle)
    assert waiting.status is RunStatus.WAITING_FOR_USER
    gate = waiting.pending_user_gate
    assert gate is not None and gate.domain_reference_id is not None
    assert gate.domain_reference_id.startswith("skill-use:")
    use_proposal_id = UUID(gate.domain_reference_id.removeprefix("skill-use:"))
    use_proposal = skill_store.get_use_proposal(use_proposal_id)
    assert (use_proposal.skill_id, use_proposal.skill_version) == (
        gold.skill_id,
        gold.version,
    )
    print(
        json.dumps(
            {
                "checkpoint": "USE_REVIEW",
                "workflow_gate_id": gate.gate_id,
                "domain_reference_id": gate.domain_reference_id,
                "use_proposal_id": str(use_proposal.proposal_id),
                "approval_gate_id": use_proposal.approval_gate_id,
                "skill_id": str(use_proposal.skill_id),
                "skill_version": use_proposal.skill_version,
                "mode": use_proposal.proposed_mode.value,
                "reason": use_proposal.reason,
                "deviations": use_proposal.proposed_deviations,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    _approval(
        "USE",
        use_proposal.proposal_id,
        use_proposal.approval_gate_id,
        gate.gate_id,
    )
    completed = await application.resume_run(
        handle,
        GateUserDecision(
            gate_id=gate.gate_id,
            approved=True,
            decided_by=principal.user_id,
            domain_reference_id=gate.domain_reference_id,
        ),
    )
    assert completed.status is RunStatus.COMPLETED
    authorization = skill_store.get_authorization_for_proposal(
        use_proposal.proposal_id
    )
    assert authorization is not None and authorization.approved
    usage = skill_store.get_usage_for_authorization(authorization.authorization_id)
    assert usage is not None
    assert usage.run_id == handle.run_id
    assert (usage.skill_id, usage.skill_version) == (gold.skill_id, gold.version)

    events = application.trace_events(handle)
    completed_capabilities = {
        event.payload.get("capability")
        for event in events
        if event.event_type is TraceEventType.CAPABILITY_COMPLETED
    }
    assert {
        "skill_search",
        "skill_propose_use",
        "skill_view",
        "execution_submit",
        "report_submit",
    }.issubset(completed_capabilities)
    assert TraceEventType.SKILL_CONTEXT_ACCESSED in {
        event.event_type for event in events
    }
    assert runner.security_checks >= 1
    later_script_ids = {
        UUID(event.payload["script_artifact_id"])
        for event in events
        if event.event_type is TraceEventType.EXECUTION_PLANNED
        and event.payload.get("script_artifact_id")
    }
    assert later_script_ids
    assert later_script_ids.isdisjoint(gold.procedure.script_artifact_ids)
    for container_name in runner.container_names:
        assert subprocess.run(
            ["docker", "container", "inspect", container_name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        ).returncode != 0

    curator_text = "\n".join(payload for _, payload in curator_boundaries)
    runtime_text = "\n".join(payload for _, payload in runtime_boundaries)
    trace_text = json.dumps(
        [event.model_dump(mode="json") for event in sink.read()]
    )
    gold_text = gold.model_dump_json()
    private_values = (
        "c9-private-observation-000",
        "c9-private-feature-000",
    )
    for surface in (curator_text, runtime_text, trace_text, gold_text):
        for private_value in private_values:
            assert private_value not in surface
        for forbidden in (
            "storage_locator",
            "provider_raw_body",
            "reasoning_content",
            "authorization_secret",
        ):
            assert forbidden not in surface.lower()
        for secret_name in ("OPENAI_API_KEY", "MIMO_API_KEY"):
            secret = os.environ.get(secret_name)
            if secret and len(secret) >= 8:
                assert secret not in surface
    assert str(SOURCE_ROOT) not in curator_text
    assert str(source) not in runtime_text

    acceptance = {
        "source_run_id": str(SOURCE_RUN_ID),
        "source_bundle_id": str(bundle.bundle_id),
        "source_proposal_id": str(proposal.proposal_id),
        "gold_skill_id": str(gold.skill_id),
        "gold_version": gold.version,
        "later_run_id": str(handle.run_id),
        "use_proposal_id": str(use_proposal.proposal_id),
        "authorization_id": str(authorization.authorization_id),
        "usage_id": str(usage.usage_id),
        "use_mode": use_proposal.proposed_mode.value,
        "curator_call_count": 1,
        "restart_reconstruction": "PASS",
        "leak_audit": "PASS",
    }
    (live_root / "acceptance.json").write_text(
        json.dumps(acceptance, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("C9_ACCEPTANCE=" + json.dumps(acceptance, sort_keys=True), flush=True)
    skill_store.close()
