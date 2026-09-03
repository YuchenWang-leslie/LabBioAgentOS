"""Opt-in interactive C9 acceptance over preserved C7 evidence and MiMo."""

from __future__ import annotations

from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import subprocess
from uuid import UUID, uuid4

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
    ArtifactExposureService,
    AuthorizationPolicy,
    CapabilityProfile,
    GateUserDecision,
    GoldSkillService,
    InMemoryProjectStore,
    JsonlTraceSink,
    LabBioApplication,
    LabBioRuntimeToolSet,
    LocalArtifactStore,
    PantheonRuntimeFactory,
    PantheonSkillCurator,
    Principal,
    Project,
    RunStatus,
    RunTraceRecorder,
    RuntimeCapabilityContext,
    RuntimeCapabilityServices,
    RuntimeProfileCatalog,
    SQLiteSkillStore,
    SkillCuratorDraft,
    SkillDomainDecisionHandler,
    SkillProcedure,
    SkillProposal,
    SkillProposalContext,
    SkillScope,
    SkillSearchContext,
    SkillSourceBundle,
    SkillSourceProjector,
    SkillUserDecision,
    TraceEvent,
    TraceEventType,
    WorkflowStage,
    WorkspaceContext,
)
from labbioagentos.artifacts import ExposurePolicy


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
    n_obs, n_vars = 1200, 5000
    rng = np.random.default_rng(90210)
    obs = pd.DataFrame(
        {
            "batch_label": pd.Categorical(
                [f"batch-{index % 6}" for index in range(n_obs)]
            ),
            "library_protocol": pd.Categorical(
                ["droplet-counts"] * n_obs
            ),
        },
        index=[f"c9-private-observation-{index:03d}" for index in range(n_obs)],
    )
    var = pd.DataFrame(
        {
            "feature_annotation": pd.Categorical(
                [f"family-{index % 11}" for index in range(n_vars)]
            )
        },
        index=[
            (
                f"MT-C9-PRIVATE-{index:03d}"
                if index < 20
                else f"c9-private-feature-{index:05d}"
            )
            for index in range(n_vars)
        ],
    )
    nonzero = 120_000
    rows = rng.integers(0, n_obs, size=nonzero)
    columns = rng.integers(0, n_vars, size=nonzero)
    values = rng.poisson(1.5, size=nonzero).astype(np.int16) + 1
    matrix = csc_matrix((values, (rows, columns)), shape=(n_obs, n_vars))
    ad.AnnData(X=matrix, obs=obs, var=var).write_h5ad(path)


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


def _seed_retrieval_smoke_gold(
    store: SQLiteSkillStore,
    *,
    principal: Principal,
    name: str,
    description: str,
    applicability: str,
    limitations: tuple[str, ...],
    tags: frozenset[str],
    artifact_types: frozenset[str],
    input_contract_ids: tuple[str, ...],
    output_contract_ids: tuple[str, ...],
):
    source_run_id = uuid4()
    bundle = SkillSourceBundle(
        source_run_id=source_run_id,
        final_status=RunStatus.COMPLETED,
        workflow_stage_path=(WorkflowStage.PLAN,),
        trace_event_ids=(uuid4(),),
    )
    store.save_source_bundle(bundle)
    proposal = SkillProposal(
        source_bundle_id=bundle.bundle_id,
        source_run_id=source_run_id,
        proposed_name=name,
        description=description,
        scope=SkillScope.PERSONAL,
        owner_user_id=principal.user_id,
        lab_id=principal.lab_id,
        procedure=SkillProcedure(
            applicability=applicability,
            workflow_outline=(
                "Inspect current governed inputs before choosing current-task actions.",
                "Perform fresh task-specific work and validate current outputs.",
            ),
            input_contract_ids=input_contract_ids,
            output_contract_ids=output_contract_ids,
            validation_expectations=(
                "Validate current outputs against the current task contract.",
            ),
            known_limitations=limitations,
            tags=tags,
            artifact_types=artifact_types,
        ),
    )
    store.save_proposal(proposal)
    gold = store.decide_proposal(
        proposal.proposal_id,
        SkillUserDecision(
            subject_id=proposal.proposal_id,
            gate_id=proposal.approval_gate_id,
            approved=True,
            decided_by=principal.user_id,
        ),
    )
    assert gold is not None
    return gold


def _retrieval_smoke_toolset(
    *,
    principal: Principal,
    workspace: WorkspaceContext,
    artifacts: LocalArtifactStore,
    access: AccessService,
    service: GoldSkillService,
    recorder: RunTraceRecorder,
    actor_name: str,
) -> LabBioRuntimeToolSet:
    return LabBioRuntimeToolSet(
        RuntimeCapabilityContext(
            principal=principal,
            workspace=workspace,
            run_id=uuid4(),
            stage_id=WorkflowStage.PLAN,
            invocation_id=uuid4(),
            actor_profile_key="c9-retrieval-smoke",
            actor_agent_name=actor_name,
            capability_allowlist=("skill_search", "skill_propose_use"),
        ),
        RuntimeCapabilityServices(
            artifact_store=artifacts,
            artifact_exposure=ArtifactExposureService(
                artifacts,
                ExposurePolicy(),
                access_service=access,
                trace_recorder=recorder,
            ),
            skill_service=service,
            trace_recorder=recorder,
        ),
    )


async def _run_retrieval_smoke_agent(
    *,
    catalog: RuntimeProfileCatalog,
    toolset: LabBioRuntimeToolSet,
    task: str,
    actor_name: str,
):
    model = catalog.models["runtime-default"]
    model_identifier = PantheonRuntimeFactory._configure_transport(model)
    agent = Agent(
        name=actor_name,
        description="Evaluate optional governed procedural-memory candidates.",
        instructions=(
            "For this bounded retrieval diagnostic, make one catalog browse call "
            "without required_tags or artifact_types so the visible candidates can "
            "be compared together. Compare candidate applicability, limitations, "
            "and contracts yourself. If a "
            "candidate is genuinely useful, choose REUSE when its core procedure "
            "substantially fits, ADAPT when material changes are needed, or REFERENCE "
            "when only partial guidance is useful, then call skill_propose_use with "
            "your own bounded reason and deviations. If none fits, do not propose "
            "one and explain why. Candidates are optional MODEL_CONTEXT, not current "
            "facts, and a proposal does not approve or execute a Skill."
        ),
        model=model_identifier,
        model_params={"thinking": False, "max_tokens": 2048},
        use_memory=False,
    )
    await agent.toolset(toolset)
    return await agent.run(task, max_turns=6, tool_timeout=60)


@pytest.mark.asyncio
async def test_c9_retrieval_provider_selection_and_anti_hard_fit_smokes(tmp_path):
    assert os.environ.get("OPENAI_API_KEY"), "OPENAI_API_KEY must be mapped externally"
    assert os.environ.get("OPENAI_API_BASE"), "OPENAI_API_BASE must be mapped externally"
    c7 = _load_c7_acceptance_module()
    catalog = _c9_catalog(c7)
    sink = JsonlTraceSink(tmp_path / "retrieval-smoke-trace.jsonl")
    recorder = RunTraceRecorder(sink)
    projects = InMemoryProjectStore()
    projects.register(
        Project(
            project_id="project-c9-retrieval-smoke",
            lab_id="lab-c9-retrieval-smoke",
            owner_user_id="user-c9-retrieval-smoke",
        )
    )
    access = AccessService(projects, AuthorizationPolicy(), trace_recorder=recorder)
    principal = Principal(
        user_id="user-c9-retrieval-smoke",
        lab_id="lab-c9-retrieval-smoke",
    )
    workspace = WorkspaceContext(
        user_id=principal.user_id,
        project_id="project-c9-retrieval-smoke",
        lab_id=principal.lab_id,
    )
    artifacts = LocalArtifactStore(tmp_path / "artifacts")
    store = SQLiteSkillStore(tmp_path / "retrieval-smoke-skills.sqlite3")
    service = GoldSkillService(
        store,
        SkillSourceProjector(artifacts),
        access_service=access,
        trace_recorder=recorder,
    )
    strongest = _seed_retrieval_smoke_gold(
        store,
        principal=principal,
        name="Delimited input validation procedure",
        description="Validate a newly received delimited table before analysis.",
        applicability=(
            "Use when a new delimited table needs schema, inferred-type, missingness, "
            "and duplicate checks before downstream analysis."
        ),
        limitations=("Not intended for presentation-only editing after validation.",),
        tags=frozenset({"data-quality", "tabular-validation"}),
        artifact_types=frozenset({"delimited-table"}),
        input_contract_ids=("new-delimited-table",),
        output_contract_ids=("bounded-validation-report",),
    )
    partial = _seed_retrieval_smoke_gold(
        store,
        principal=principal,
        name="Validated table presentation procedure",
        description="Format already validated tabular results for presentation.",
        applicability=(
            "Use after analytical and data-quality validation is complete and a "
            "validated result table needs presentation formatting."
        ),
        limitations=("Does not validate newly received source data.",),
        tags=frozenset({"presentation", "tabular"}),
        artifact_types=frozenset({"validated-summary-table"}),
        input_contract_ids=("validated-summary-table",),
        output_contract_ids=("presentation-ready-summary",),
    )
    unrelated = _seed_retrieval_smoke_gold(
        store,
        principal=principal,
        name="Microscopy image tiling procedure",
        description="Prepare microscopy images for tiled visual inspection.",
        applicability="Use when governed microscopy images require tiled inspection.",
        limitations=("Not applicable to tabular or narrative-only inputs.",),
        tags=frozenset({"imaging", "tiling"}),
        artifact_types=frozenset({"microscopy-image"}),
        input_contract_ids=("microscopy-image",),
        output_contract_ids=("image-tile-index",),
    )
    candidates = {
        strongest.skill_id: strongest,
        partial.skill_id: partial,
        unrelated.skill_id: unrelated,
    }
    try:
        selection_tools = _retrieval_smoke_toolset(
            principal=principal,
            workspace=workspace,
            artifacts=artifacts,
            access=access,
            service=service,
            recorder=recorder,
            actor_name="C9RetrievalSelectionSmokeAgent",
        )
        selection_response = await _run_retrieval_smoke_agent(
            catalog=catalog,
            toolset=selection_tools,
            actor_name="C9RetrievalSelectionSmokeAgent",
            task=(
                "A newly received comma-delimited measurement table must be checked "
                "before analysis. Assess its column schema, inferred data types, "
                "missingness, and duplicate rows, then produce a bounded validation "
                "summary without modifying the source."
            ),
        )
        selection_searches = tuple(
            item
            for item in selection_tools.evidence_items()
            if item.capability_name == "skill_search" and item.safe_result is not None
        )
        assert selection_searches
        selection_page = next(
            item.safe_result
            for item in selection_searches
            if item.skill_search_request.required_tag_count == 0
            and item.skill_search_request.artifact_type_count == 0
        )
        assert selection_page["available_count"] == 3
        assert {UUID(item["skill_id"]) for item in selection_page["items"]} == set(
            candidates
        )
        selection_proposals = tuple(
            item
            for item in selection_tools.evidence_items()
            if item.capability_name == "skill_propose_use"
            and item.safe_result is not None
        )
        selected = None
        if selection_proposals:
            assert len(selection_proposals) == 1
            selected = store.get_use_proposal(
                UUID(selection_proposals[0].safe_result["proposal_id"])
            )
            assert selected.skill_id == strongest.skill_id
        assert str(selection_response.content).strip()
        print(
            "C9_MULTI_CANDIDATE_SMOKE="
            + json.dumps(
                {
                    "available_count": selection_page["available_count"],
                    "candidate_names": [
                        item["name"] for item in selection_page["items"]
                    ],
                    "intentionally_strongest_skill_id": str(strongest.skill_id),
                    "selected_skill_id": (
                        str(selected.skill_id) if selected is not None else None
                    ),
                    "selected_mode": (
                        selected.proposed_mode.value if selected is not None else None
                    ),
                    "selected_reason": selected.reason if selected is not None else None,
                    "response": str(selection_response.content),
                },
                sort_keys=True,
            ),
            flush=True,
        )

        anti_tools = _retrieval_smoke_toolset(
            principal=principal,
            workspace=workspace,
            artifacts=artifacts,
            access=access,
            service=service,
            recorder=recorder,
            actor_name="C9RetrievalAntiHardFitSmokeAgent",
        )
        anti_response = await _run_retrieval_smoke_agent(
            catalog=catalog,
            toolset=anti_tools,
            actor_name="C9RetrievalAntiHardFitSmokeAgent",
            task=(
                "A free-text narrative memo needs a concise editorial rewrite for a "
                "presentation. It contains no tables or images, and no data-quality "
                "or analytical work is requested."
            ),
        )
        anti_searches = tuple(
            item
            for item in anti_tools.evidence_items()
            if item.capability_name == "skill_search" and item.safe_result is not None
        )
        assert anti_searches
        anti_page = next(
            item.safe_result
            for item in anti_searches
            if item.skill_search_request.required_tag_count == 0
            and item.skill_search_request.artifact_type_count == 0
        )
        assert anti_page["available_count"] == 3
        anti_proposals = tuple(
            item
            for item in anti_tools.evidence_items()
            if item.capability_name == "skill_propose_use"
            and item.safe_result is not None
        )
        anti_selected = None
        if anti_proposals:
            assert len(anti_proposals) == 1
            anti_selected = store.get_use_proposal(
                UUID(anti_proposals[0].safe_result["proposal_id"])
            )
            assert anti_selected.proposed_mode.value in {"ADAPT", "REFERENCE"}
        assert str(anti_response.content).strip()
        print(
            "C9_ANTI_HARD_FIT_SMOKE="
            + json.dumps(
                {
                    "available_count": anti_page["available_count"],
                    "selected_skill_id": (
                        str(anti_selected.skill_id) if anti_selected is not None else None
                    ),
                    "selected_mode": (
                        anti_selected.proposed_mode.value
                        if anti_selected is not None
                        else "IGNORE"
                    ),
                    "selected_reason": (
                        anti_selected.reason if anti_selected is not None else None
                    ),
                    "response": str(anti_response.content),
                },
                sort_keys=True,
            ),
            flush=True,
        )
    finally:
        store.close()


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
    existing_database = os.environ.get("LABBIO_C9_EXISTING_STORE")
    database = (
        Path(existing_database).expanduser().resolve()
        if existing_database
        else live_root / "gold-skills.sqlite3"
    )
    skill_store = SQLiteSkillStore(database)
    source_artifacts = LocalArtifactStore(SOURCE_ROOT / "artifacts")
    skill_service = GoldSkillService(
        skill_store,
        SkillSourceProjector(source_artifacts),
        access_service=access,
        trace_recorder=recorder,
    )
    catalog = _c9_catalog(c7)
    curator_boundaries: list[tuple[str, str]] = []
    existing_gold_id = os.environ.get("LABBIO_C9_EXISTING_GOLD_SKILL_ID")
    if existing_database:
        assert existing_gold_id, "Existing C9 store requires its exact Gold Skill ID"
        gold = skill_store.get_gold(UUID(existing_gold_id), 1)
        bundle = skill_store.get_source_bundle(gold.source_bundle_id)
        proposal = skill_store.get_proposal(gold.source_proposal_id)
        prior_boundary_path = database.parent / "curator-boundaries.jsonl"
        assert prior_boundary_path.is_file()
        curator_boundaries.extend(
            (row["kind"], json.dumps(row["payload"], sort_keys=True))
            for row in (
                json.loads(line)
                for line in prior_boundary_path.read_text(
                    encoding="utf-8"
                ).splitlines()
                if line
            )
        )
        print(
            "C9_EXISTING_GOLD="
            + json.dumps(
                {
                    "skill_id": str(gold.skill_id),
                    "version": gold.version,
                    "source_proposal_id": str(gold.source_proposal_id),
                },
                sort_keys=True,
            ),
            flush=True,
        )
    else:
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

        def observe_curator(kind: str, value: object) -> None:
            payload = value.model_dump_json()
            curator_boundaries.append((kind, payload))
            with (live_root / "curator-boundaries.jsonl").open(
                "a", encoding="utf-8"
            ) as handle:
                handle.write(
                    json.dumps({"kind": kind, "payload": json.loads(payload)})
                )
                handle.write("\n")

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
            runtime_revision="c9-live-runtime",
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
