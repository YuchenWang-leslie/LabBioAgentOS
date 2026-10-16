# LabBioAgentOS — PantheonOS Modification Roadmap

## Architectural Goal

Extend PantheonOS rather than replace it.

Target architecture:

User
→ WorkflowEngine
→ PantheonTeam
→ Runtime capabilities
→ Docker / Search / Artifact providers

with cross-cutting:

* RunTrace
* EventBus
* ArtifactStore
* ExposurePolicy
* User / Project / Lab scope
* Gold Skill lifecycle

PantheonTeam remains responsible for agent collaboration inside workflow stages.

WorkflowEngine remains responsible for deterministic run state.

---

# Phase 0 — Upstream Baseline And Architecture Mapping

## Goal

Understand the cloned PantheonOS version before modification and establish a stable baseline.

## Tasks

Inspect, do not redesign:

* pantheon/agent.py
* pantheon/team/
* pantheon/providers.py
* pantheon/internal/memory*
* existing skill/learning modules
* background execution support
* plugin lifecycle
* relevant tests

Create:

* docs/LABBIO_ARCHITECTURE.md
* docs/PANTHEON_REUSE_MAP.md
* docs/OPEN_DECISIONS.md
* docs/UPSTREAM_MODIFICATIONS.md

PANTHEON_REUSE_MAP must classify important components as:

* REUSE
* WRAP
* EXTEND
* MODIFY
* IGNORE FOR NOW

## Required conclusion

Document exactly how:

* PantheonTeam.call_agent works
* execution_context_id propagates
* child memory isolation works
* chain_path works
* plugins receive lifecycle events
* tool results return to the model

## Acceptance

* original Pantheon imports correctly
* baseline tests are recorded
* no architectural code changes
* no R installation
* no bioinformatics workflows
* no speculative refactors

STOP after Phase 0.

---

# Phase 1 — LabBio Extension Skeleton

## Goal

Create a clean extension layer without deeply modifying Pantheon core.

Preferred structure:

labbio/
workflow/
context/
policy/
artifacts/
trace/
execution/
skills/
memory/
teams/

Do not move existing Pantheon modules.

Create core typed contracts only:

* StageContext
* WorkflowRun
* WorkflowStage
* AgentStageResult
* ArtifactRef
* ExecutionPlan
* ValidationResult
* NextActionProposal

No scientific reasoning logic.

## Acceptance

A minimal Python test can:

1. create a WorkflowRun
2. enter a mock stage
3. invoke an existing PantheonTeam
4. receive a structured stage result
5. update workflow state outside PantheonTeam

Prove:

PantheonTeam cannot directly own WorkflowRun state.

STOP.

---

# Phase 2 — WorkflowEngine

## Goal

Implement the deterministic outer workflow runtime.

States/stages:

INTAKE
UNDERSTAND
PLAN
PREFLIGHT
EXECUTE
VALIDATE
INTERPRET
REPORT
LEARN

Auxiliary:
SEARCH
DEBUG
USER_GATE

Implement:

* stage registry
* stage transitions
* pause/resume
* failure
* retry accounting
* conditional branches
* user-gate state
* stage input/output artifact references

The WorkflowEngine must not contain scientific method-selection logic.

Dynamic workflow proposals may be accepted later through typed interfaces.

## Acceptance

Using mock stages only:

* successful run reaches completion
* failure enters correct state
* retry works
* USER_GATE pauses
* resumption works
* invalid transitions are rejected

No real bioinformatics tasks.

STOP.

---

# Phase 3 — PantheonTeam Integration And Delegation Policy

## Goal

Reuse PantheonTeam as the reasoning runtime inside Workflow stages.

Do NOT replace call_agent().

Add the smallest possible extension necessary for:

DelegationPolicy.can_call(
caller,
target,
StageContext
)

and:

list_allowed_agents(...)

Preserve:

* execution_context_id
* parent_tool_call_id
* chain_path
* child memory
* depth protection
* loop protection

Prefer wrapper/plugin/subclass approaches before editing pantheon/team/pantheon.py directly.

## Acceptance

Test a small mock team:

Planner
→ Specialist
→ Reviewer

and verify:

* allowed delegation succeeds
* forbidden delegation fails
* parent/child execution IDs remain intact
* WorkflowEngine remains owner of stage state
* agent failure is propagated structurally

STOP.

---

# Phase 4 — RunTrace And Event Infrastructure

## Goal

Make every important run reconstructable before adding real bioinformatics execution.

Implement RunTrace recording:

Workflow:

* stage entry/exit
* status
* transitions

Agent:

* invocation_id
* parent invocation
* caller
* callee
* stage
* execution_context_id
* parent_tool_call_id
* chain_path
* prompt/instruction reference
* structured result
* error

Important prompts:

* template/version/hash
* sanitized rendered instruction

Execution placeholders:

* command
* script reference
* parameters
* status

Events:

* stage started/completed/failed
* agent started/completed/failed
* debug requested
* retry started
* user action required

Do not record hidden chain-of-thought.

## Acceptance

A mock multi-agent run must generate a RunTrace from which the delegation graph and workflow path can be reconstructed.

STOP.

---

# Phase 5 — Artifact And Safe Exposure Layer

## Goal

Create the boundary between data-plane outputs and LLM-visible context.

Implement:

ArtifactRef
ArtifactStore
ArtifactSchema
ExposurePolicy
ArtifactQuery interface

Exposure categories:

RAW
STRUCTURAL
AGGREGATE
DERIVED
USER_APPROVED

RAW must not be returned to a remote LLM.

Allow:

* schema
* dimensions
* dtypes
* aggregate QC
* derived result tables/summaries
* top-N biological results
* user-approved result artifacts

Do NOT implement arbitrary file-reading tools for agents.

## Acceptance

Tests prove:

* raw artifact content cannot be exposed
* structural metadata can be exposed
* derived results can be exposed
* top-N querying works
* arbitrary path access is rejected

STOP.

---

# Phase 6 — Docker Execution Boundary

## Goal

Add local Docker sandbox execution without allowing runtime LLM direct raw-data access.

Implement deterministic infrastructure:

ExecutionPlan
DockerExecutor
execution result → ArtifactRef
technical validation
execution trace

Docker security design:

* no privileged containers
* no Docker socket mount
* controlled mounts
* project workspace only
* read-only references where appropriate
* no arbitrary Docker flags from model output
* network disabled by default
* resource limits represented in ExecutionPlan

Important:

The runtime LLM may later generate analysis code or execution plans.

Codex must NOT pre-write the scientific analysis logic that future runtime agents should generate.

For this phase use only generic Python execution examples.

Example:

* read a synthetic local file inside Docker
* generate a simple output artifact

Do not install R.

## Acceptance

A mock ExecutionPlan:

WorkflowEngine
→ DockerExecutor
→ output
→ ArtifactStore
→ safe exposure

works end-to-end.

Failures appear in RunTrace.

STOP.

---

# Phase 7 — Gold Skill Infrastructure

## Goal

Implement validated procedural memory derived from successful RunTrace.

Do NOT implement scientific Gold Skills yet.

Core lifecycle:

successful Run
→ user chooses save
→ SkillCandidate
→ abstraction interface
→ user approval
→ GoldSkill

Scopes:

* personal
* project
* lab

Gold Skill must retain lineage:

* source run_id
* successful workflow path
* important agent invocation pattern
* important Planner/Executor instructions
* execution method
* parameters
* validation
* useful failure/debug knowledge
* artifact contracts

Retrieval result must support:

* reuse
* adapt
* reference

But similarity classification should remain a pluggable/runtime intelligence capability.

Do not hard-code biological similarity rules.

On future tasks:

GoldSkillRetriever returns candidate skills and evidence.

The runtime model later decides how the skill should influence planning.

User must be asked before reuse by default.

## Acceptance

Using synthetic runs:

1. complete successful RunTrace
2. save as personal Gold Skill
3. new task retrieves it
4. user accepts use
5. adaptation can be represented without overwriting original Gold Skill
6. modified successful run can propose a new version

STOP.

---

# Phase 8 — User / Project Isolation And Persistent Memory

## Goal

Add multi-user/project boundaries after the core execution and skill abstractions are stable.

Scopes:

User
Project
Lab

Implement:

* workspace ownership
* project ownership
* collaborator read-only policy
* personal/project/lab Gold Skill visibility
* user/project/lab memory storage interfaces
* proposal-based memory changes
* permission-aware Artifact access

Do not yet build a large UI.

## Acceptance

Tests prove:

* user A cannot write to user B
* collaborator can read but not write
* project Gold Skill respects project scope
* personal Gold Skill respects user scope
* lab Gold Skill can be discovered
* memory scope rules are enforced

STOP.

---

# C13 — Apptainer Cluster Execution

Authorized by MOHAN CUI on 2026-10-15 (`docs/OPEN_DECISIONS.md` DECIDED-C13-0).
Gap analysis and numbered acceptance criteria:
`docs/C13_APPTAINER_EXECUTION_GAP_MEMO.md`.

## Goal

Run the same constrained execution on an HPC cluster that Docker provides
locally. The login node runs the control plane, LLM calls, and job submission;
compute nodes run analysis through the site scheduler inside Apptainer.
Cluster support must be generic across most site types.

Container runtime and launcher are separate axes. Configuration allows only:

* Local + Docker (control-plane host)
* Scheduler + Apptainer (the only cluster mode)

Execution is always network-disabled by the runtime itself.

Phases:

1. Runtime abstraction: extract `ContainerExecutor`; Docker behavior unchanged.
2. Apptainer backend: SIF store (sha256 identity), command builder, capability
   probe, runner.
3. Scheduler launcher (3a): `JobLauncher` with a configuration-driven
   scheduler template adapter (Slurm, PBS and LSF as bundled templates);
   generated job scripts; job ledger; non-blocking execution.
   Jobs are awaited inside one tool call; suspending runs in a new workflow
   state (3b) is a recorded fallback that needs its own decision.
4. Apptainer environment builds: wheels are downloaded inside the base SIF
   (on the login node, or in a job on a network-capable partition) and pinned
   by sha256; locked installation, SIF packing, and the import probe run in a
   network-disabled scheduler job.

Important:

C13 is execution infrastructure only. It adds no method selection, scientific
routing, or analysis logic. Backend, launcher, scheduler, and SIF details never
enter model-visible content. No Pantheon core change.

Do not install Apptainer, a scheduler, or Docker. Use generic mock tasks only.

## Acceptance

* existing Docker tests pass unchanged after Phase 1
* Docker and Apptainer command builders pass one shared sandbox contract
* invalid runtime/launcher combinations are rejected at configuration load
* LabBio passes no credentials to the runtime process, scheduler command, job
  script, or container; the container starts from a cleared environment even
  when the scheduler loads the user's login environment
* each execution's scratch directory is removed after the execution
* a crash during a job never resubmits it and still fails closed
* gated real runs pass: Apptainer on Linux, then a Slurm template end to end,
  then a cluster environment build end to end

Full list: the numbered criteria in the gap memo.

STOP.

---

# Later Phases — Not Yet Authorized

Do NOT implement these until explicitly requested:

* production scRNA-seq workflows
* bulk RNA-seq workflows
* CellChat implementation
* pseudotime implementation
* DEG method-selection logic
* biological interpretation prompts
* automatic literature-analysis workflows
* R support
* production server deployment
* FastAPI/UI
* Redis/RQ unless architecture later demonstrates a need
* automatic Gold Skill promotion

These are future layers built on the validated architecture.
