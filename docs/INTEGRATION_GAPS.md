# Integration Gaps

## Overall finding

The architecture is structurally suitable but the repository is not yet ready
to execute a governed real-LLM vertical slice. The blockers below require
LabBio integration work, not a redesign and not a PantheonOS core patch.

## BLOCKING

| Gap | Evidence in current code | Why it blocks the slice | Required boundary (not implementation here) |
|---|---|---|---|
| Scope-bound run creation is missing | `WorkflowRun` ownership fields are frozen, while `WorkflowEngine.create_run()` accepts only `retry_limit` and constructs local default IDs | A real authenticated/project run cannot be created with its true immutable scope | Accept a trusted `WorkspaceContext`/scope at run creation and validate it before storing the run |
| No runtime coordinator or stage registry | Tests manually build context, invoke the adapter, record results, and transition; no application service composes these operations | There is no end-to-end loop from current stage to configured team/result/proposal while preserving WorkflowEngine ownership | Add a non-intelligent coordinator and configuration registry; never route on scientific content |
| No real agent/profile/provider assembly | LabBio has no versioned stage prompts, agent profiles, response schemas, team factory, model selection configuration, or credential wiring | `PantheonStageAdapter` can call a supplied team, but no real governed team can currently be assembled | Configure Pantheon `Agent`/`PantheonTeam` through LabBio profiles and external credentials; use existing Pantheon model/provider mechanisms |
| Current stage result boundary is too generic | `StageContext.metadata` and `AgentStageResult.payload` are arbitrary JSON; `NextActionProposal` is separate | The coordinator would need convention/prose inference, and arbitrary payload can carry unsafe content into trace | Introduce a bounded discriminated stage input/result envelope with an explicit typed proposal and stage-specific bodies |
| Model-visible capability adapters are not wired | `PantheonArtifactQueryAdapter` is a plain adapter, and there are no runtime ToolSets for artifact discovery, execution, Skill, Memory, or search | A real model has no safe way to inspect inputs, submit execution, or use governed context | Add narrow per-stage ToolSets/adapter DTOs and inject them using Pantheon's existing plugin/provider extension points |
| Execution input authorization and scope propagation are absent | `ExecutionPlan` has no workspace ownership; `MountResolver.resolve_inputs()` calls `ArtifactStore.get_ref()` directly; executor/output registration omits owner/project/lab and falls back to local defaults | A known cross-project UUID could reach mount resolution, and produced artifacts would be mis-scoped | Govern execution submission with a bound Principal/WorkspaceContext, authorize each input before resolution, and propagate exact scope to every registered artifact |
| Execution result is not safe for model serialization | `ExecutionResult` contains full `ArtifactRef` values, whose `storage_locator` is a host path | Returning the current object through a Pantheon tool would disclose internal host paths | Define a model-safe execution receipt containing IDs and bounded status/issues only; keep full refs internal |
| Approval-capable runtime graph is not configured | `default_workflow_definition()` contains only the nine linear stages and no USER_GATE/SEARCH/DEBUG nodes or edges | Skill use, Memory update, and policy approval cannot be represented in the default run | Provide a reviewed runtime workflow definition with only the required gate/auxiliary edges; keep general graph validation unchanged |

The arbitrary-payload issue is also a data-safety issue: both
`PantheonStageAdapter` and `WorkflowEngine.record_stage_result()` currently emit
the payload into RunTrace. A typed and bounded result must replace that path
before real artifact-derived content is used.

## IMPORTANT

| Gap | Current effect | Integration direction |
|---|---|---|
| No scoped artifact discovery tool | A model must already know an artifact UUID | Add paginated `artifact_list` returning authorized metadata only |
| No common safe tool-error contract | Provider/tool exceptions may expose inconsistent prose or internal details | Map capability errors to bounded typed envelopes at each adapter boundary |
| No explicit model-facing result/view DTOs for Gold and Memory | Governed services return trusted domain records suitable for application code, not necessarily bounded prompt use | Add authorized, bounded candidate/view DTOs; stores remain internal |
| Workflow gate and domain approval decisions are not coordinated | Workflow `UserDecision`, Skill decisions, and Memory decisions are separate correct contracts | Coordinator must correlate a gate ID with one pending domain proposal, apply the external decision, and resume only after success |
| Prompt/instruction versioning is not assembled into runtime profiles | RunTrace supports InstructionRecord, but no production templates create it | Profiles should render sanitized, versioned/hashable instructions and record references without provider conversation dumps |
| Provider-native structured output is not configured | Adapter performs post-return validation only | Prefer Pantheon `response_format`/profile schema where supported, while retaining LabBio validation as authoritative |
| Report submission/registration boundary is unspecified | REPORT can return prose but has no narrow governed registration capability | Define a bounded report DTO and deterministic artifact registration path |
| Search provider/citation contract is absent | Runtime cannot perform governed literature retrieval | Add later only when a selected provider and bounded citation result contract are approved; not needed by the synthetic slice |
| Docker is unavailable on the inspected command path | The execution implementation cannot run locally in the current environment | User/environment must provide a working Docker installation and daemon; repository code must not install it |

## DEFERRED

These are concrete future integrations but do not block the first synthetic
slice:

- semantic/vector retrieval for Memory or Gold Skill candidates; eligibility
  filtering and runtime-model judgment are sufficient initially;
- a real SkillCurator agent; optional Gold save can be omitted or use a typed
  mock proposal while validating the approval boundary;
- literature/search capability for a task that does not require external
  evidence;
- asynchronous execution status, background queues, and remote schedulers; the
  current executor is synchronous;
- production persistence/transactions for workflow, trace, artifact, Skill,
  Memory, and governance records;
- production authentication, API/UI, approval UX, and ACL administration;
- production bioinformatics agent roster, images, output contracts, and
  scientific validation behavior.

## Decisions requiring user approval before implementation

1. **Stage result shape:** approve a common envelope with a discriminated
   stage-specific body and explicit `NextActionProposal` (recommended), or
   separate unrelated result models per stage.
2. **First-slice team topology:** approve the minimal coordinator + execution +
   reviewer profiles (recommended), recognizing that model-selected delegation
   remains dynamic.
3. **Runtime workflow graph:** approve which stages may enter USER_GATE and the
   allowed resume edges for Skill use, Memory updates, and execution policy.
4. **Execution submission contract:** approve host injection of
   run/stage/invocation/workspace identity into a model-supplied plan draft
   (recommended); those fields must not be model-controlled.
5. **Live model/provider:** choose the Pantheon-supported model and credential
   source for the opt-in integration test. Credentials remain external.
6. **Docker prerequisite:** provide/approve a working local Docker environment
   before the execution portion of the slice; this project must not install or
   reconfigure it.

## PantheonOS assessment

No blocking gap is located in PantheonOS. Local ToolSets, LocalProvider,
TeamPlugin injection, Agent model configuration, response formats, and native
delegation provide the required extension points. All identified fixes belong
in LabBioAgentOS. No Pantheon core file is proposed for modification.

