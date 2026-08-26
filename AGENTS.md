# AGENTS.md — LabBioAgentOS Engineering Guardrails

## Project Objective

This repository is a controlled extension of PantheonOS for building a bioinformatics multi-agent platform.

PantheonOS remains the underlying multi-agent runtime. Preserve and reuse its existing Agent, PantheonTeam, call_agent delegation, execution context, child-memory isolation, delegation-chain tracking, plugin lifecycle, and provider abstractions wherever practical.

The project adds a higher-level workflow, safe execution, artifact, trace, memory, and Gold Skill layer around PantheonOS.

The goal is NOT to rewrite PantheonOS.

---

## Critical Architecture Boundary

The system has three major planes:

1. Workflow Control Plane

   * owns run state
   * owns stage transitions
   * owns retry, branch, pause, resume, user gates
   * deterministic where possible

2. Pantheon Agent Runtime

   * performs reasoning inside workflow stages
   * uses PantheonTeam and call_agent()
   * agents may collaborate dynamically subject to delegation policy

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

Do not rewrite a Pantheon subsystem when an adapter, subclass, wrapper, plugin, or extension point can solve the problem.

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

## Upstream Preservation Rule

Prefer:

LabBio extension
↓
adapter / plugin / wrapper
↓
PantheonOS

Avoid:

large invasive changes inside Pantheon core.

Pantheon core modifications require a clear reason.

For every direct modification under existing Pantheon core modules, record:

* why an extension was insufficient
* exact behavior changed
* upstream compatibility risk

Keep the diff against upstream small.

---

## Primary Pantheon Components To Reuse

Preserve unless a roadmap phase explicitly requires otherwise:

* pantheon.agent.Agent
* pantheon.team.PantheonTeam
* list_agents()
* call_agent()
* execution_context_id
* parent_tool_call_id
* chain_path
* delegation depth protection
* delegation loop protection
* child memory isolation
* TeamPlugin lifecycle
* provider/tool abstractions

SequentialTeam and SwarmTeam are not the primary LabBio workflow engine.

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

WorkflowEngine owns run state.

PantheonTeam owns reasoning and collaboration within a stage.

PantheonTeam may return structured proposals but may not directly mutate workflow run state.

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
* Pantheon execution_context_id
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
* PantheonTeam stage invocation
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
