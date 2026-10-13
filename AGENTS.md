# AGENTS.md — LabBioAgentOS Engineering Guardrails

## Project Objective

This repository builds a governed bioinformatics multi-agent platform.

The user-approved direction from 2026-10-10 is to replace PantheonOS as the
in-stage Agent runtime with a LabBio-owned native kernel. The end state has no
Pantheon runtime or package dependency. Pantheon remains only the explicit
legacy implementation and behavior baseline during the bounded migration.

Preserve LabBio's existing workflow, safe execution, Artifact, trace, Memory,
Gold Skill, identity, and recovery boundaries. Reimplement only the provider,
Agent loop, tool transport, Leader-Specialist delegation, child-context, and
typed-finalization behavior that LabBio actually uses. Do not vendor, rename,
copy, or monkey-patch Pantheon into the new kernel.

The current migration authority is
`docs/NATIVE_KERNEL_MIGRATION_PLAN.md`. `docs/ARCHITECTURE.md` identifies the
active architecture documents. Older Pantheon reuse plans are historical
implementation evidence, not forward design authority.

---

## Critical Architecture Boundary

The system has three major planes:

1. Workflow Control Plane

   * owns run state
   * owns stage transitions
   * owns retry, branch, pause, resume, user gates
   * deterministic where possible

2. Stage Agent Runtime

   * performs reasoning inside workflow stages behind `StageRuntimeInvoker`
   * target implementation is the LabBio Native Kernel
   * current Pantheon implementation remains an explicit legacy backend until
     the migration gates pass
   * Leader and Specialist agents may collaborate dynamically subject to
     structural delegation policy
   * a run freezes backend, backend version, and contract revision at creation
   * a native failure never falls back to Pantheon or another provider/model

3. Data / Execution Plane

   * executes code locally inside Docker
   * owns raw biological data
   * raw or observation-level biological data must not be exposed directly to remote LLMs

Derived biological results may be exposed to the runtime LLM according to artifact exposure policy, including:

* top DEGs
* pathway enrichment results
* CellChat ligand-receptor results
* trajectory-associated genes
* marker results
* QC summaries
* user-approved existing biological results
* model-generated derived biological results

---

## Runtime Intelligence Sovereignty

This is a HARD RULE.

Codex is building infrastructure for another runtime LLM.

Codex MUST NOT replace the future runtime LLM's reasoning with hard-coded engineering logic.

Do NOT hard-code:

* which bioinformatics analysis should be chosen for a user request
* which specialist agent should be called for a scientific question
* whether CellChat, trajectory, DEG, enrichment, etc. should be run
* biological interpretation
* scientific hypotheses
* task-specific reasoning
* task-specific analysis plans
* analysis code that should instead be generated dynamically by the runtime model
* fixed agent collaboration sequences except structural safety constraints
* heuristics that pretend to perform the runtime model's reasoning

Allowed deterministic logic includes:

* security policy
* permission checks
* workflow state transitions
* schema validation
* path validation
* Docker restrictions
* artifact contracts
* event recording
* retry limits
* delegation permissions
* structured protocol validation
* deterministic technical validation

The runtime LLM should later decide:

* analysis strategy
* method selection
* agent delegation within allowed boundaries
* whether a Gold Skill should be adapted
* task-specific execution plan
* dynamically generated analysis code
* biological interpretation

Codex may implement the interfaces that allow those decisions to occur, but must not make those decisions on behalf of the runtime model.

---

## Codex Role In This Repository

Codex acts as:

* software architect implementing an already-approved architecture
* framework programmer
* test writer
* debugger
* integration engineer

Codex does NOT act as:

* the future bioinformatics reasoning agent
* the future PlannerAgent
* the future scRNAAgent
* the future BioInterpretationAgent
* the future runtime code-generation agent
* the scientific decision maker

Framework code is allowed.

Task-specific scientific intelligence is not.

---

## Anti-Local-Loop Rule

Codex frequently over-optimizes local implementation details. Do not do that in this repository.

Before changing code:

1. identify the current roadmap phase
2. identify the acceptance criterion
3. identify the minimal files that need modification
4. confirm that the change preserves the architecture boundary

Do not expand scope merely because nearby code could be improved.

Do not perform unrelated refactors.

During the native-kernel migration, rewrite only the behavior named by the
approved kernel contracts. Do not redesign `WorkflowEngine`, governance,
Artifact, Docker, RunState, RunTrace, Gold, or persistent Memory merely because
nearby runtime code is changing.

If the same implementation issue fails twice after materially different fixes:

* stop patching
* summarize the root cause
* identify whether the problem is architectural, environmental, upstream Pantheon behavior, or implementation-specific
* propose at most 2 viable solutions
* do not continue an indefinite patch loop

If an architectural decision is missing:

* do not invent one silently
* record it in docs/OPEN_DECISIONS.md
* implement only what is architecture-neutral

---

## Legacy Pantheon And Clean Replacement Rule

Treat the pinned Pantheon checkout as a read-only legacy behavior baseline
during migration. Do not add new Pantheon patches to make the native migration
pass. Convert required behavior into implementation-independent contracts and
implement those contracts in LabBio.

Do not:

* vendor or copy Pantheon source into LabBio
* monkey-patch Pantheon at LabBio import time
* infer that every Pantheon feature must be recreated
* keep a hidden Pantheon fallback after selecting the native backend
* continue a Pantheon-era run with the native backend

Rollback uses the previous immutable release and a fresh run identity. It does
not change the backend of an existing run.

---

## Native Kernel Contracts To Preserve

Preserve semantics, not Pantheon classes:

* exact provider/model binding with no silent transport switch
* bounded provider-turn and no-progress observation
* strict tool schemas and strict JSON-object arguments without repair
* canonical replay only after successful parsing
* foreground governed tool execution
* Leader-selected `list_specialists` / `delegate` behavior
* parent/child invocation and tool-call identities
* delegation chain, depth, self, ancestor, and loop protection
* isolated child working context
* typed child results and actor-attributed evidence
* capability evidence followed by tool-free typed finalization
* private reasoning isolation from trace, Memory, evidence, and reports

Do not recreate unused Pantheon MCP, RemoteAgent, Swarm, notebook, background
task, UI, or learning subsystems unless a later user-approved phase adds an
independent requirement.

---

## Workflow Architecture

Default workflow backbone:

INTAKE
→ UNDERSTAND
→ PLAN
→ PREFLIGHT
→ EXECUTE
→ VALIDATE
→ INTERPRET
→ REPORT
→ LEARN

Auxiliary capabilities:

* SEARCH
* DEBUG
* USER_GATE

`WorkflowEngine` owns run state.

The runtime configured behind `StageRuntimeInvoker` owns reasoning and
collaboration within one stage. During migration this may be the explicit
legacy Pantheon backend or the native candidate, but never both for one run.

The stage runtime may return typed proposals but may not directly mutate
workflow run state.

---

## Gold Skill Principle

Gold Skill is a user-approved validated procedural abstraction derived from a successful RunTrace.

Gold Skill is NOT a rigid pipeline.

First-time task:
normal workflow
→ validated successful Run
→ RunTrace
→ user optionally saves
→ SkillCurator abstraction
→ user approval
→ Gold Skill

Later related task:
understand current context
→ search relevant Gold Skills
→ ask user whether to use one
→ reuse / adapt / reference depending on similarity

Unless tasks are nearly identical, Gold Skills remain starting procedures that the runtime model may adapt.

Codex must build the infrastructure for this behavior, not decide adaptation scientifically itself.

---

## RunTrace Principle

RunTrace should record reproducible high-value execution history, especially:

* workflow stages
* stage transitions
* agent invocation graph
* caller/callee relationships
* kernel run and Agent invocation IDs
* parent_tool_call_id
* chain_path
* important rendered instructions/prompts
* Planner outputs
* Executor instructions
* generated scripts
* Docker execution
* parameters
* errors
* Debug actions
* retries
* validation
* artifacts
* final successful path

Do NOT attempt to store or reconstruct hidden chain-of-thought.

Store explicit decisions, reasons, structured outputs, calls, results, and execution artifacts.

---

## Data Boundary

Remote LLMs must not receive raw biological matrices or unrestricted file contents.

Default prohibited exposure:

* raw count/expression matrices
* FASTQ/BAM contents
* unrestricted h5ad contents
* arbitrary dataframe rows
* arbitrary file reads

Allowed controlled exposure:

* file type
* dimensions
* schema
* column names
* dtypes
* aggregate QC
* derived biological results
* user-approved result artifacts

All LLM-visible biological results should eventually pass through an Artifact/Exposure interface.

---

## Environment Rules

Development environment:

* local macOS
* Python-first
* use the user's existing conda installation
* Python 3.11 preferred
* do not install or introduce uv, Poetry, pyenv, pixi, virtualenv managers, or other environment managers
* do not create an R environment
* do not install R
* do not introduce rpy2
* R support is out of scope until explicitly requested

pip may only be used inside the active conda environment when needed for Python package installation.

Do not modify the user's base conda environment.

Do not install system software without explicit user instruction.

Do not install or reconfigure Docker automatically.

---

## Bioinformatics Scope At This Stage

Do NOT implement production scRNA-seq or bulk RNA-seq pipelines yet.

Do NOT download large datasets.

Do NOT create task-specific demo logic.

Use generic deterministic mock/example tasks when execution infrastructure needs testing.

Bioinformatics intelligence will be added after the core architecture is stable.

---

## Testing Philosophy

Every architecture phase must have acceptance tests.

Prefer testing contracts and boundaries over implementation trivia.

Important test areas:

* WorkflowEngine state ownership
* backend-neutral stage-runtime invocation
* provider/tool/Agent/team native-kernel contracts
* delegation policy
* RunTrace correctness
* artifact exposure policy
* raw-data leakage prevention
* Docker command construction
* user/project isolation
* Gold Skill lifecycle
* failure propagation

Avoid brittle tests tied to exact LLM prose.

Use mocked model responses whenever possible.

Live LLM calls should not be required for normal unit tests.

---

## Phase Discipline

Only work on the currently requested roadmap phase.

At the end of a phase:

1. run relevant tests
2. report changed files
3. report architecture impact
4. report unresolved issues
5. stop

Do not automatically begin the next roadmap phase.

Do not spend time polishing non-blocking details after acceptance criteria pass.

Working and architecturally correct is more important than locally perfect.

For Native Kernel P0 specifically, work is limited to exact baseline evidence,
authority/document reconciliation, and implementation-independent
characterization tests. Do not begin P1 production kernel code, switch the
default backend, remove `pantheon-agents`, run live provider/data tasks, deploy,
or activate profiles/Skills until P0 is reviewed and the user authorizes the
next phase.
