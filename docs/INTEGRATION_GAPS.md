# Integration Gaps

## Overall finding

The historical integration blockers below were resolved through the C2-C4
runtime milestones. C4 was accepted at
`4c13e8d853f1b478b914992bdcb360c420966c4c` after a real MiMo, Pantheon, and
Docker nine-stage synthetic vertical slice. This is source/runtime acceptance,
not evidence of production deployment or production health.

C5 resolves the application-composition gap through a generic
`LabBioApplication` boundary: callers can admit trusted inputs, create a scoped
run, and drive the existing coordinator without reproducing the C4 integration
test wiring. The opt-in real MiMo, Pantheon, and Docker test completed the same
nine-stage synthetic path through this application boundary. C5 does not add a
biological workflow.

C6 adds one explicit trusted data-plane extension for AnnData `.h5ad`: local
format inspection produces bounded, scope-bound STRUCTURAL and AGGREGATE
Artifacts linked to the RAW source Artifact ID. A fresh real MiMo/Pantheon run
completed all nine stages using only those safe views. This remains
source/runtime acceptance, not production deployment or production health.

## RESOLVED FOR C4

| Historical gap | Current resolution |
|---|---|
| Scope-bound run creation | `RuntimeCoordinatorService.create_run()` validates trusted immutable `Principal` and `WorkspaceContext` through `AccessService`. |
| Runtime coordinator and stage registry | `RuntimeCoordinatorService` and `StageRuntimeRegistry` drive exact current-stage configuration without task-content routing. |
| Agent/profile/provider assembly | Versioned runtime profiles and `PantheonRuntimeFactory` assemble real Pantheon agents using external provider credentials. |
| Typed stage boundary | `RuntimeStageInput` and stage-bound `RuntimeStageResult` schemas carry explicit typed proposals and bounded bodies. |
| Model-visible capability adapters | Per-invocation `LabBioRuntimeToolSet` exposes exact-stage allowlists with bounded tool results/errors. |
| Execution scope propagation | `ExecutionSubmissionService` injects trusted run/workspace identity, authorizes inputs, and validates output provenance. |
| Safe execution receipt | Model-facing execution receives bounded `ExecutionReceipt`; internal `ArtifactRef.storage_locator` remains host-only. |
| Runtime workflow and gates | `runtime_workflow_definition()` includes reviewed source-resuming USER_GATE edges while `WorkflowEngine` owns state. |
| Artifact discovery and exposure | Paginated `artifact_list` and controlled `artifact_query` preserve absolute REMOTE_LLM denial for RAW. |
| Report registration | `ReportSubmissionService` registers a scoped report Artifact and returns only a receipt ID. |
| Real provider and Docker path | C4 completed all nine stages with MiMo, native Pantheon delegation, runtime-generated Python, and digest-pinned Docker. |

## C5 APPLICATION COMPOSITION AND AUDIT

| Classification | C4 test objects | C5 boundary |
|---|---|---|
| APPLICATION CONFIGURATION | projects, authorization/exposure policies, storage/workspace roots, approved images, execution ceilings, output contracts, runtime profile catalog, workflow definition, stage assembly, delegation plugin | Construct and wire once in the application composition root. |
| PER-RUN STATE | trusted Principal/WorkspaceContext, task text, WorkflowRun, input/context Artifact IDs, preflight receipt, accumulated results | Validate and bind inside one application-owned run session; callers receive an opaque handle and safe result. |
| PER-STAGE STATE | exact stage spec, invocation ID, fresh Pantheon team, ToolSet, capability evidence, stage input/result | Continue using `PerInvocationPantheonStageInvoker`; do not cache teams or let Pantheon mutate WorkflowRun. |
| TEST FIXTURE ONLY | synthetic CSV, grouped-mean truth, C4 prompts, inspecting Docker runner, boundary capture, expected A/B/C values | Keep in integration tests; never move into production application code. |

The C5 composition root remains configuration/orchestration only. C6 adds an
explicit call into the trusted `H5ADInspector`; no file suffix or task content
routes the runtime, and no model-visible path reader exists.

## C6 H5AD INSPECTION BOUNDARY

- The RAW h5ad file remains local and is absolutely denied to REMOTE_LLM.
- `H5ADInspector` runs in the trusted data plane and emits no matrix values,
  observation rows, axis index values, or complete gene list.
- STRUCTURAL output contains bounded shapes, dtypes, field/key names, storage
  representation, and raw/layer/key presence.
- AGGREGATE output contains bounded categorical counts and numeric summaries;
  near-unique fields are suppressed and category overflow is explicit.
- Both safe Artifacts retain exact owner/project/lab scope and an opaque
  `source_artifact_id` relationship to the RAW Artifact.
- C6.1 places format-specific Artifact construction behind an explicit trusted
  inspector registry and adds HDF5 metadata resource preflight before AnnData
  eager metadata loading.
- Cardinality suppression is not semantic sensitivity classification. Any
  future caller/user-approved field exposure policy must run inside the trusted
  inspection boundary before safe Artifact registration.
- C6 does not choose QC thresholds or implement downstream single-cell methods.

## DEFERRED

These are concrete future integrations but do not block the first synthetic
slice:

- semantic/vector retrieval for Memory or Gold Skill candidates; eligibility
  filtering and runtime-model judgment are sufficient initially;
- production SkillCurator hosting and approval UX; a local Pantheon-backed
  adaptive Curator, independent Agent audit/revision, exact human approval,
  SQLite restart, and later real approved use have been validated;
- literature/search capability for a task that does not require external
  evidence;
- asynchronous execution status, background queues, and remote schedulers; the
  current executor is synchronous;
- production persistence/transactions for workflow, trace, artifact, Skill,
  Memory, and governance records;
- production authentication, API/UI, approval UX, and ACL administration;
- production bioinformatics agent roster, images, output contracts, and
  scientific validation behavior.

## Historical decisions resolved by the accepted runtime

The accepted choices are a common discriminated result envelope, the minimal
coordinator/execution/reviewer profiles with model-selected delegation, the
reviewed runtime graph, host injection of execution authority, external MiMo
credentials, and an externally available digest-pinned Docker image. These
choices are runtime evidence, not production deployment approval.

## PantheonOS assessment

No blocking gap is located in PantheonOS. Local ToolSets, LocalProvider,
TeamPlugin injection, Agent model configuration, response formats, and native
delegation provide the required extension points. All identified fixes belong
in LabBioAgentOS. No Pantheon core file is proposed for modification.
