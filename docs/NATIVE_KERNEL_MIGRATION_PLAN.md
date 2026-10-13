# LabBioAgentOS Native Kernel Migration Plan

## Document status

- Status: Native Kernel P8 real validation is complete through one bounded
  ordinary-user bioinformatics task and final report; immutable P8 release,
  rollback rehearsal, and any separately authorized activation remain
- Recorded: 2026-10-10; updated: 2026-10-13
- Objective: replace PantheonOS as the LabBioAgentOS in-stage Agent runtime with
  a LabBio-owned native kernel
- Scope boundary: this document records the approved direction and proposed
  execution gates. It does not by itself authorize production deployment,
  profile activation, Gold promotion, or modification of an active release.

This plan should be revalidated against live source, release, database, worker,
Docker, provider, and profile state before each implementation or deployment
stage. Snapshot counts and commit identities below are evidence from the time
this document was written, not permanent authority.

## 1. Goal and definition of kernel removal

The target is not to rename or vendor PantheonOS. The target is a small,
LabBio-owned reasoning and collaboration kernel with no runtime or package
dependency on Pantheon.

"Pantheon removal complete" means:

1. Active LabBio source and tests no longer import Pantheon.
2. The LabBio wheel and clean installation environment do not require
   `pantheon-agents`.
3. Agent, provider, tool-loop, delegation, context-isolation, observation, and
   typed-finalization behavior are implemented through native LabBio contracts.
4. The current safety, authority, evidence, recovery, and trace boundaries are
   preserved.
5. Generic live, Leader-Specialist live, real Docker, and fresh ordinary-user
   bioinformatics validation have passed at their respective acceptance levels.
6. There is no hidden fallback to Pantheon and no cross-backend continuation of
   an existing run.

The native kernel may depend directly on a maintained model SDK or HTTP client.
"Native" means that LabBio owns the runtime semantics and contracts; it does not
mean reimplementing HTTP, TLS, or a provider SDK.

## 2. Current architecture and migration boundary

### 2.1 LabBio-owned components to retain

The following are already LabBio control, governance, execution, or persistence
components and are not part of the kernel rewrite:

- `WorkflowEngine`, `WorkflowRun`, stage transitions, retries, branches, and
  user gates;
- `RuntimeCoordinator` and the `StageRuntimeInvoker` port;
- Principal, Workspace, access control, scope, and authority composition;
- Artifact, ExposurePolicy, model-visible views, and reference validation;
- Docker execution, environment construction, governed submission, and result
  registration;
- RunState, Conversation, clarification, successor, and delivery state;
- RunTrace and append-only audit events;
- governed Gold Skill and persistent Memory services;
- typed stage inputs, results, capability evidence, and grounding validators.

The outer nine-stage workflow remains unchanged during the first kernel
migration. Changing the workflow graph and the stage runtime simultaneously
would make regressions difficult to attribute and would enlarge the safety
surface unnecessarily.

### 2.2 Pantheon responsibilities to replace

The native kernel must replace six semantic clusters:

1. model-provider transport and message projection;
2. the Agent model/tool loop;
3. tool schema construction, strict argument handling, and dispatch;
4. Leader-Specialist discovery, delegation, and child-context isolation;
5. strict typed finalization;
6. provider-turn observation, budgets, interruption, and convergence handling.

The existing `StageRuntimeInvoker` protocol is the main substitution seam. The
new implementation should be connected behind that port rather than coupled
directly into `WorkflowEngine`.

### 2.3 Current dependency snapshot

At the time of this plan:

- repository branch: `fix/gold-approval-handoff`;
- source HEAD: `9608bc1`;
- working tree: 37 tracked modifications and 16 untracked files;
- deterministic suite observed in this conversation: 1553 passed, 34 skipped,
  1 existing warning;
- Pantheon package range: `pantheon-agents>=0.6.4,<0.7`;
- required fork revision:
  `07675c45b538f7d27b9b16b1b7d8b72f37365293`;
- eight production files directly import or dynamically load Pantheon;
- 61 test files contain Pantheon identifiers or coupling.

The exact source identity, path inventory, and content fingerprints for this
snapshot are recorded in `NATIVE_KERNEL_P0_BASELINE.md`; the counts here are
only a summary. The dirty working tree includes active changes in runtime, tooling,
finalization, Gold handoff, checkpoint, and execution feedback code. These
changes belong to an existing root-cause cluster. They must be preserved and
converged into a separate baseline before broad kernel replacement begins.

Direct external-package consumers include:

- `src/labbioagentos/runtime/pantheon.py`;
- `src/labbioagentos/runtime/tooling.py`;
- `src/labbioagentos/teams/delegation.py`;
- `src/labbioagentos/teams/pantheon.py`;
- `src/labbioagentos/skills/curator.py`;
- `src/labbioagentos/local_config.py`;
- `src/labbioagentos/cli.py`;
- `src/labbioagentos/local_workspace_cli.py`.

`runtime/assembly.py`, `application.py`, public exports, configuration, tests,
and documentation also depend semantically on Pantheon-facing types even where
they do not import the external package directly.

### 2.4 Resolved P0 authority conflict and remaining documentation gap

At P0 entry, repository `AGENTS.md` still stated that Pantheon must remain the
underlying runtime and that the goal was not to rewrite it. P0 reconciled that
conflict without rewriting the historical acceptance record: `AGENTS.md`,
`ARCHITECTURE.md`, `LABBIO_ARCHITECTURE.md`, `OPEN_DECISIONS.md`, and this plan
now distinguish the implemented legacy backend from the approved Native Kernel
target. Older Pantheon roadmaps and integration maps are explicitly historical
behavior/provenance references.

`docs/ARCHITECTURE.md` was added as the canonical index. The expected source
document `docs/PRODUCTIONIZATION_DEBUG_GUIDE.md` remains absent, and no verified
production `current` tree was found during P0 preflight. That gap and production
status `NOT VERIFIED` are recorded rather than inferred from historical files;
production `current` was not edited as a substitute.

## 3. Target architecture

```text
User / API
  |
  v
WorkflowEngine                         retained control-plane authority
  |
  v
StageRuntimeInvoker                    stable backend port
  |
  v
NativeKernelRun                        one stage invocation
  +-- Leader AgentLoop
  |     +-- bounded Task Board
  |     +-- ToolRegistry / Dispatcher
  |     +-- list_specialists
  |     +-- delegate(target, instruction)
  |             `-- isolated Specialist AgentLoop
  |
  +-- ModelProvider
  +-- Budget / Interrupt / Timeline adapters
  |
  v
Frozen CapabilityEvidenceBundle
  |
  v
Tool-free Typed Finalizer
  |
  v
RuntimeStageResult                     validated by existing LabBio contracts
  |
  v
RuntimeCoordinator -> WorkflowEngine
```

The architecture has three explicit layers.

### Core runtime

- provider-neutral model request/turn contracts;
- Agent loop and invocation-local working memory;
- Leader-Specialist registry and delegation;
- task board and invocation identity;
- budget, interruption, convergence, and event contracts;
- strict typed finalization.

The core must remain domain-neutral. It must not know about single-cell,
spatial-omics, GSEA, Seurat, a particular file suffix, or a scientific method.

### Tool layer

- governed LabBio capabilities;
- Docker and environment services;
- Artifact and exposure services;
- report, Skill, Memory, and literature capabilities;
- safe model-visible result projection;
- authoritative capability evidence collection.

Tool authority is host-bound. A model must never be able to override Principal,
Workspace, run, stage, actor, consumer, Artifact authorization, or execution
policy through tool arguments.

### External behavior layer

- team profiles;
- Agent role and output ownership;
- user-level Skill Markdown;
- project-level Skill Markdown;
- domain workflows, defaults, and reusable practices.

Opinionated or domain-specific behavior belongs here, not in the kernel. The
model chooses which applicable Skill to read and use; the runtime must not
replace full Skill documents with hidden summaries.

## 4. Native kernel contracts

### 4.1 Identity model

The implementation must keep these identities distinct:

| Identity | Meaning |
|---|---|
| `thread_id` | Long-lived user/project conversation identity |
| `workflow_run_id` | One complete task attempt owned by WorkflowEngine |
| `kernel_run_id` | One stage-level native-kernel execution |
| `task_id` | One Leader-created task-board entry |
| `invocation_id` | One Leader or Specialist model loop |
| `tool_invocation_id` | One governed capability call |

All child activity must retain parent invocation, parent tool-call, root run,
depth, and chain identity. IDs are correlation data, not authorization.

### 4.2 Model-provider contract

The minimum provider protocol is conceptually:

```text
generate(ModelRequest) -> ModelTurn
```

`ModelRequest` must carry normalized public messages, an exact provider/model
binding, provider-visible tool schemas, an optional response schema, output
limits, deadline, and correlation identity.

`ModelTurn` must carry only bounded public content, normalized tool calls,
finish reason, token usage, provider request identity, and safe observation
metadata. Missing usage is unknown/NA, never zero.

The first production adapter should support only the currently required
OpenAI-compatible Chat transport. Unsupported transport or schema features must
fail during preflight. The runtime must not silently switch provider, model,
Chat/Responses protocol, or thinking mode.

Private reasoning continuity, where required by a compatible provider, must be
run-local and bound to the exact provider, model, endpoint, and tool-call
sequence. It must never enter RunTrace, persistent Memory, public model context,
Artifact evidence, or reports.

### 4.3 Tool schema and dispatch

The native tool system should separate:

- `ToolSpec` and provider-visible JSON Schema;
- `ToolRegistry` and the allowed tool set for an invocation;
- strict `ToolCall` parsing and Pydantic argument validation;
- `LabBioCapabilityDispatcher` for governed business operations;
- `ToolResult` / `ToolError` safe projections;
- evidence collection and trace emission.

Required behavior:

- top-level tool arguments must be a JSON object;
- duplicate keys, non-finite values, malformed JSON, truncated responses, and
  filtered responses are rejected before side effects;
- arguments are never repaired or guessed;
- a successfully parsed argument object has one canonical representation for
  history replay;
- the complete tool-call batch is structurally, schematically, and
  budget-validated before any item executes;
- an over-limit or structurally invalid batch is not partially executed;
- tool results pass through one authoritative model-transport projector;
- a result that would be silently altered or become incomplete must be rejected
  or explicitly paged, not presented as complete.

The first native implementation should use foreground-only execution. It may
accept multiple tool calls in one provider turn, but effectful calls should run
in documented deterministic order. Detached background tools are out of scope.

### 4.4 Agent loop

A native Agent loop must:

1. derive the actual tool set from trusted stage, profile, and assignment
   bindings;
2. reserve budget before a provider call;
3. record a safe provider-turn observation even if structured parsing later
   fails;
4. validate the entire returned tool batch before effects;
5. dispatch governed tools without allowing model-supplied authority fields;
6. append canonical assistant tool calls and exact paired tool results;
7. preserve completed and failed capability evidence;
8. stop on model completion, interrupt, provider-turn limit, evidence limit,
   wall-clock/no-progress limit, or repeated non-retryable request;
9. return a bounded `CapabilityEvidenceBundle`.

Model prose remains `MODEL_CONTEXT`. It does not become
`AUTHORITATIVE_EVIDENCE` merely because it was emitted by a Leader or
Specialist.

### 4.5 Leader-Specialist and task board

The Leader owns orchestration. Specialists own explicit tools or output types,
not merely different prompt labels.

Registry and delegation rules:

- membership comes from trusted team configuration;
- discovery returns policy-allowed candidates in configuration order, without
  framework ranking;
- the Leader model explicitly chooses the target Specialist;
- the framework never uses keywords, file suffixes, UI state, errors, or hidden
  heuristics to choose a Specialist;
- denied or failed delegation returns a typed result and never auto-selects an
  alternative;
- self, ancestor, repeated-chain, and excessive-depth delegation fail closed;
- each child gets isolated working messages and a bounded explicit instruction;
- a child does not inherit the parent's complete history or private reasoning;
- the parent receives a typed child result, evidence references, and status;
- child tool evidence retains its exact actor identity;
- child prose remains `MODEL_CONTEXT`.

The task board is run-scoped and model-authored. The framework validates task
identity, dependency acyclicity, ownership, legal state transitions, and
budgets; it does not create scientific tasks or choose their order.

Recommended task states are:

```text
PENDING -> READY -> RUNNING -> SUCCEEDED
                          |-> FAILED
                          |-> BLOCKED
                          `-> CANCELLED
```

Task completion is not equivalent to WorkflowRun completion, successful
analysis, or scientific validity. A BLOCKED task is not automatically retried
without new persistent evidence or explicit input.

The current default local composition has no capability peers, so a single
Agent vertical path can migrate first. Native dynamic delegation remains a
mandatory final acceptance surface and must not be removed merely because the
default profile currently does not exercise it.

### 4.6 Capability and typed-finalization boundary

The current two-mode structure must remain:

```text
CAPABILITY: tools and delegation allowed
    -> freeze exact capability evidence
FINALIZE: no tools or delegation; emit strict RuntimeStageResult
```

The finalizer may see only the bounded stage input, frozen evidence, task/child
model context, and the exact response schema. Existing stage-specific Skill,
execution, report, and evidence-grounding validators remain authoritative.

One bounded format-correction attempt may be retained when it uses the same
schema and frozen evidence and is explicitly observed. It must consume budget,
must not replay tools, and must not invent missing evidence.

The finalizer cannot mutate `WorkflowRun`. Only the existing coordinator and
WorkflowEngine may validate and apply a returned stage result.

### 4.7 Budget, timeline, interruption, and recovery

Budget accounting must cover at least provider turns, tokens, wall time, tool
calls, evidence entries/bytes, delegation count/depth, and finalization attempts.
Child consumption counts against the root grant. Budgets are not silently
increased, borrowed from siblings, or replenished after provider errors.

RunTrace remains the append-only audit timeline and does not become workflow
state authority. Native events should cover kernel/task lifecycle, model turns,
tool batches/calls, delegation, budgets, interrupts, checkpoints, and
finalization. Events must not store provider bodies, raw biological data,
credentials, host paths, complete conversations, or hidden reasoning.

The first implementation should preserve the current conservative recovery
model rather than introduce arbitrary mid-turn replay:

- complete capability evidence may continue through FINALIZE_ONLY;
- a persisted final result may continue through APPLY_RESULT validation;
- a tool with a trusted terminal receipt may consume that receipt rather than
  rerun;
- unknown model, tool, or delegation completion remains blocked and requires
  reconciliation;
- a runtime/profile/tool-schema revision mismatch rejects recovery;
- terminal runs are never reopened;
- Pantheon-era nonterminal runs are never silently resumed by the native
  backend.

### 4.8 Skill loading

- User-level promoted Skills are available to new threads for that user.
- Project-level promoted Skills become available only after exact user and
  project grounding.
- Metadata may be shown first, but the model must explicitly read the complete
  Markdown for a selected Skill.
- Material Skill use should be visible in the run timeline or result metadata.
- Draft and reviewed-but-unpromoted Skills must not silently guide ordinary
  runs.
- Project guidance must never be silently upgraded into a global user rule.

## 5. Pantheon behavior that becomes native acceptance contracts

The current fork's nine-patch history is a regression inventory, not an
implementation to copy. Native tests must cover:

1. provider-turn accounting and bounded no-observable-progress behavior;
2. primitive, `Annotated`, enum, bounds, and nested Pydantic schema fidelity;
3. strict tool arguments, including duplicate-key, non-finite, malformed, and
   truncated input rejection before execution;
4. canonical replay only after successful argument parsing;
5. omission of invalid empty assistant messages after private-reasoning
   redaction;
6. safe provider-turn observation before structured-response parsing failure;
7. explicit compatible-provider thinking transport;
8. strict provider tool schemas and foreground-only completion;
9. private tool-reasoning continuity without persistence or public leakage.

Additional LabBio contracts to preserve include:

- complete-batch evidence-limit checking before effects;
- exact failure and rejected-attempt retention;
- `list_specialists` policy/ancestor filtering;
- self, ancestor, loop, and depth rejection;
- isolated child messages and typed parent-child correlation;
- model-context versus authoritative-evidence separation;
- host-owned identity, scope, and authority bindings;
- explicit rejection instead of silent output truncation;
- one finalization correction without tool replay;
- runtime revision mismatch rejection.

## 6. Deliberate non-goals

The first native kernel will not recreate unused Pantheon features:

- MCPProvider or RemoteAgent;
- SequentialTeam, SwarmTeam, AAT, or MoA;
- Pantheon learning or automatic Skill extraction;
- Pantheon persistent conversation Memory;
- background Agent/task systems;
- transfer tools;
- notebook-first execution requirements;
- Pantheon UI, chat room, desktop, or Claw integrations;
- a second generic event bus alongside RunTrace;
- provider auto-selection or a multi-provider fallback zoo;
- arbitrary mid-turn recovery or exactly-once claims across provider, SQLite,
  Docker, and Artifact storage.

Historical documentation may continue to mention Pantheon as provenance. The
zero-dependency criterion applies to active runtime, package metadata, and
active tests; it does not require erasing history.

## 7. Red lines

- No keyword, file-suffix, UI-state, or error-text routing.
- No automatic Specialist selection or replacement.
- No fixed scientific tool order in the runtime.
- No scientific method, package, parameter, comparison, covariate, or result
  interpretation embedded in kernel code.
- No hidden fallback from native to Pantheon or another provider/model.
- No JSON repair or inferred tool arguments.
- No weakening of authority, Artifact, exposure, validator, recovery, or
  runtime-revision checks to make migration tests pass.
- No double execution of effectful tools for backend comparison.
- No copying Pantheon source wholesale into LabBio.
- No direct modification of a production `current` tree.
- Codex must not perform scientific decisions or reporting that belong to the
  LabBio Agent during real acceptance.

## 8. Migration phases and gates

### P0: Re-authorize architecture and freeze the baseline

Work:

- update repository `AGENTS.md`, active architecture, open decisions, and this
  workplan to make the native-kernel direction authoritative;
- freeze, preserve, and validate the current dirty root-cause cluster without
  silently assigning, cleaning, rebasing, committing, pushing, or overwriting
  pre-existing changes;
- record source, test, profile, provider, image, and runtime-manifest identity;
- convert each required Pantheon behavior into an implementation-independent
  characterization or contract test;
- adopt the legacy-run policy: no cross-backend continuation.

Gate:

- authoritative documents no longer contradict one another;
- existing changes have a reproducible baseline and are not overwritten;
- the full deterministic suite passes at the agreed baseline;
- migration scope, legacy-run policy, provider scope, and deployment boundary
  have been reviewed.

P0 outcome recorded on 2026-10-10:

- active authority documents agree on the legacy/current versus native/target
  distinction;
- the 53-path pre-P0 candidate has exact content and diff fingerprints and was
  preserved; it remains a mixed dirty source candidate, not an immutable
  commit or release;
- the public execution-revision feedback contradiction found during baseline
  audit has a focused regression and correction;
- the portable intent of all nine required legacy patch behavior groups maps to
  backend-neutral contracts whose shared test modules import only the neutral
  interface; Pantheon's exact hidden three-idle-turn silent guard is isolated as
  legacy-only evidence and is explicitly excluded from native parity, while the
  sole Pantheon package dependency stays in the legacy test adapter;
- the current backend remains Pantheon, legacy runs were not resumed or
  modified, and P1 production kernel code has not started;
- final P0 verification passed 37 contract/focused cases and the full
  deterministic suite at 1589 passed, 34 skipped, with one existing dependency
  warning. Exact commands and limits are recorded in the P0 baseline and
  convergence workplan.

P0 did not authorize or perform dependency removal, default-backend switching,
live provider/data/Docker execution, release/deployment, production mutation,
profile activation, or Skill promotion. P0 stops for user review before P1.

### P1: Introduce backend-neutral kernel contracts

Work:

- define provider, tool, Agent, team, delegation, budget, and event contracts;
- rename Pantheon-specific generic errors and comments where required;
- implement a deterministic FakeProvider;
- preserve `StageRuntimeInvoker` as the outer port;
- add contract tests without switching the application default.

Gate:

- the native contract package imports no Pantheon;
- schema, state transition, budget, interrupt, and error contracts pass;
- no domain terms or routing heuristics exist in the kernel package.

P1 outcome recorded on 2026-10-12:

- `src/labbioagentos/kernel/` now owns strict backend-neutral contracts for
  provider requests/turns, public message and tool history, Agent invocation
  modes, ordered team membership, delegation ancestry, task graphs, run/task
  transitions, root budget accounting, interrupts, and safe timeline events;
- provider, model, endpoint, transport, and thinking mode form one exact frozen
  binding; missing token usage remains unknown rather than becoming zero;
- `FakeProvider` consumes a deterministic scripted turn sequence, records only
  matching requests, and fails explicitly on binding mismatch or exhaustion;
- finalization-mode invocations cannot expose tools or delegation, configured
  Specialists require explicit tool or output ownership, and the framework does
  not rank or auto-select them;
- `StageRuntimeInvoker` remains the unchanged outer application seam apart from
  replacing its obsolete Pantheon-specific docstring with a backend-neutral
  description;
- the production kernel package has no Pantheon import or identifier and no
  domain vocabulary or routing heuristic; the current application default and
  package dependency remain Pantheon for the later migration gates;
- P1/native plus P0 legacy-contract and stage-port regressions passed 87 cases;
  the full deterministic suite passed 1606 tests with 34 skips and the one
  existing Uvicorn dependency warning.

P1 did not implement capability dispatch, an Agent loop, a production provider
adapter, application backend selection, dependency removal, live execution,
release, deployment, or activation. It stops for user review before P2.

### P2: Replace ToolSet and transport policy

Work:

- separate governed LabBio capability dispatch from Pantheon `ToolSet/@tool`;
- implement native ToolSpec/ToolRegistry and schema generation;
- implement one authoritative model-visible result projector;
- migrate direct-dispatch, schema, authority, evidence, and leakage tests;
- retain a temporary explicit Pantheon adapter only if needed during migration.

Gate:

- all LabBio capability tools can be registered and called without Pantheon;
- provider-visible schemas preserve supported constraints;
- malformed or unsafe calls fail before effects;
- existing authority and evidence semantics are unchanged.

P2 outcome recorded on 2026-10-12:

- `kernel.tooling` now owns an ordered `ToolRegistry`, Pydantic-derived inline
  JSON Schema, strict JSON-object parsing, canonical argument replay,
  whole-batch validation and foreground-only deterministic dispatch;
- malformed JSON, duplicate keys, non-finite numbers, non-object arguments,
  incomplete provider responses, duplicate call IDs, unknown or disallowed
  tools, invalid UUIDs and schema violations fail before host effects;
- `ModelResultProjector` is the single native JSON result boundary: it never
  silently truncates, rejects non-JSON or oversized results, accepts a
  host-owned content validator, and emits bounded failures without leaking
  unknown exception text;
- the 19 existing LabBio capabilities have strict native argument models and a
  host-operation protocol. Trusted stage allowlists, actor identity and
  information authority are bound outside model-supplied arguments;
- the capability ceiling and information-authority catalog moved to a neutral
  LabBio module shared by legacy and native dispatch, so P2 did not duplicate or
  reinterpret those governance decisions;
- item-level success/failure evidence retains existing LabBio authority and
  actor attribution, while unsafe result fields are rejected at the native
  projector before evidence or model transport;
- P2 focused tests passed 20 cases; Native Kernel plus P0 contract tests passed
  73 cases; the full deterministic suite passed 1626 tests with 34 skips and
  the one existing Uvicorn dependency warning.

P2 did not implement the provider/Agent loop, connect native dispatch to the
application default, remove the Pantheon dependency, run live provider/data or
Docker acceptance, build a release, deploy, activate profiles, or promote
Skills. It stops for user review before P3.

### P3: Implement the native single-Agent provider loop

Work:

- implement one OpenAI-compatible Chat adapter;
- implement strict message/tool-call parsing and canonical replay;
- implement provider-turn, finish-reason, usage, no-progress, and budget
  observation;
- implement foreground tool execution and bounded evidence collection;
- implement the capability/finalization split and format correction.

Gate:

- deterministic FakeProvider suites cover every loop termination;
- all nine Pantheon-derived behavior contracts pass natively;
- one controlled real-provider protocol smoke passes;
- there is no background execution, JSON repair, or provider fallback.

P3 outcome recorded on 2026-10-12:

- `kernel.loop.AgentLoop` now owns one isolated capability or finalization
  invocation, exact provider binding, provider-turn observation, finish/usage
  handling, explicit no-progress/deadline/interrupt/budget termination, strict
  whole-batch tool handling, canonical replay, paired tool feedback and safe
  timeline events;
- `kernel.openai_chat.OpenAICompatibleChatProvider` is the sole native
  production adapter. It emits strict OpenAI-compatible Chat schemas, performs
  one request with no retry or fallback, preserves missing usage as unknown,
  rejects malformed responses, and keeps compatible-provider private reasoning
  invocation-local and bound to the exact canonical tool-call sequence;
- capability execution remains foreground-only. Finalization exposes no tools,
  accepts only one complete duplicate-free JSON object under the requested
  Pydantic schema, and may issue only a bounded generic format correction;
- all nine portable legacy behavior groups run through the native backend
  adapter. Focused loop/provider plus dual-backend contract verification passed
  89 cases, and the full deterministic suite passed 1679 tests with 34 skips
  and the one existing Uvicorn dependency warning;
- the current-code real-provider smoke used `mimo-v2.6-pro` through the explicit
  Chat adapter and one synthetic `echo(PING)` capability. It completed in two
  provider turns with exactly one tool effect and one paired result, then ended
  as `MODEL_COMPLETED`;
- the first protocol probes exposed two provider wire variants:
  `tool_calls: null` on a no-tool turn and empty assistant content on a tool
  turn. Both failed closed first, then received narrow normalization rules and
  deterministic regressions before the final smoke was rerun successfully.

P3 did not connect the native loop to the application default, implement team
or delegation behavior, remove the Pantheon dependency, run application-level
generic live/data/Docker acceptance, build a release, deploy, activate
profiles, or promote Skills. It stops for user review before P4.

### P4: Implement native Leader-Specialist collaboration

Work:

- add a trusted Agent registry and unranked discovery;
- add a bounded task board;
- implement native `list_specialists` and `delegate`;
- implement child context isolation, actor binding, depth/loop protection,
  hierarchical budget, interrupt propagation, and typed child results;
- externalize domain Specialist profiles and practices into team profile/Skill
  files.

Gate:

- self, ancestor, loop, depth, invalid-target, and child-failure tests pass;
- sibling and parent messages do not leak into a child;
- child prose is never upgraded to authoritative evidence;
- a generic live task demonstrates that the model, not host code, selects a
  Specialist.

P4 outcome recorded on 2026-10-12:

- `kernel.collaboration` now owns one trusted ordered `AgentRegistry`, a bounded
  insertion-ordered `TaskBoard`, and a single-use `CollaborationRuntime`. Team
  providers must share one exact binding; profile membership and order must
  exactly match trusted `TeamSpec` configuration;
- any structurally eligible Agent receives strict `list_specialists` and
  `delegate` tools. Discovery returns configured metadata in original order and
  contains no score, rank, keyword route, or automatic target choice; the model
  supplies the target and bounded instruction;
- delegation binds the actual foreground `tool_invocation_id` to a fresh child
  invocation/task, reserves the shared root delegation/depth budget before task
  creation, and records READY/RUNNING/terminal task states plus globally ordered
  actor-attributed timeline events;
- every child receives only its complete external profile instructions and the
  current assignment. Parent and sibling message histories are absent. The
  shared deadline, budget ledger and interrupt token propagate through nested
  loops, while self, ancestor, repeated-chain, invalid-target, output-ownership,
  depth, task-limit and delegation-budget violations fail closed;
- child results are typed as `DelegationResult`. Model prose is permanently
  labelled `AGENT_PROSE`; authoritative evidence references remain a separate
  typed field derived only from child tool results. An oversized child envelope
  becomes a typed failed delegation/task before parent transport, rather than a
  false successful task whose result the parent cannot receive;
- full external instructions enter only the selected child. The core contains
  no domain profile, method, preset or scientific selection logic; application
  profile/Skill wiring remains a P5 concern;
- P4-focused tests passed 10 cases, Native Kernel plus legacy behavior contracts
  passed 136 cases, and the full deterministic suite passed 1689 tests with 34
  skips and the one existing Uvicorn dependency warning;
- the current-code generic live used two unordered generic Specialists. The
  Leader first called `list_specialists`, then selected `text_transformer` over
  `character_counter`, completed one successful delegation/task, and produced a
  final response. Host code supplied only trusted metadata and policy; it did
  not inspect the request or select the target.

P4 did not connect the team runtime to `StageRuntimeInvoker`, migrate current
application profiles/Skills, switch the application default, remove Pantheon,
run application-level bioinformatics/Docker/final-report acceptance, release,
deploy, activate profiles, or promote Skills. It stops for user review before
P5.

### P5: Connect the native kernel to the full application

Status: complete on 2026-10-12. The user explicitly amended the earlier stage
boundary and required Native to become the default for new runs after P5
debugging. That default switch is therefore part of P5 rather than deferred to
P8. Explicit Pantheon selection remains a temporary fresh-run rollback path;
cross-backend continuation and hidden fallback remain forbidden.

Work:

- implement `NativeKernelStageInvoker` behind `StageRuntimeInvoker`;
- connect existing stage schemas, grounding validators, checkpoints, and
  coordinator behavior;
- migrate the Skill curator to a generic typed model-invocation port;
- replace Pantheon logging and source-digest assumptions;
- record native kernel, provider adapter, tool schema, and team/profile revision
  in the runtime manifest;
- migrate public exports and tests away from Pantheon type names.

Gate:

- a complete fake-provider WorkflowRun passes;
- FINALIZE_ONLY and APPLY_RESULT never replay tools;
- invalid native results cannot mutate WorkflowRun;
- Artifact, identity, scope, authority, and leakage suites remain green.

### P6: Run an explicit dual-backend migration window

Status: complete on 2026-10-12. The user explicitly authorized deployment and
P6. The active immutable CLI release remains the P5 runtime snapshot; P6 added
comparison tests and evidence without changing runtime product code.

Temporary configuration may support:

```text
runtime_backend = pantheon | native
```

Rules:

- backend, backend version, and contract version are frozen at run creation;
- one run uses exactly one backend;
- native failure never falls back to Pantheon;
- differential tests compare schema, call/evidence shape, authority, trace,
  delegation graph, final control state, and recovery behavior;
- differential tests do not require identical prose, scientific decisions,
  Specialist choices, or tool ordering;
- effectful live tasks are never run twice against shared mutable state;
- A/B live runs use fresh identities, isolated Artifacts, and isolated effects.

Gate:

- scripted-provider and transcript-based structural parity passes;
- all new candidate runs can explicitly use native;
- rollback means starting a separate fresh legacy run/release, not changing the
  backend of an existing run.

### P7: Remove Pantheon and validate from a clean environment

Status: complete in source on 2026-10-12. This phase did not deploy or activate
the P7 source; production remains the sealed P5 CLI release.

Work:

- migrate all active source and tests to native contracts;
- remove `pantheon-agents` from `pyproject.toml`;
- remove `constraints/pantheon-runtime.txt`;
- list the actually used model SDK/transport as a direct dependency;
- remove or archive the legacy Pantheon adapters;
- rebuild generated packaging artifacts instead of manually editing them;
- archive Pantheon reuse/modification documents as historical provenance;
- update active architecture and convergence documentation.

Gate:

- no active `from pantheon` or `import pantheon` remains in source/tests;
- clean environment installation succeeds without Pantheon installed;
- wheel metadata contains no Pantheon `Requires-Dist`;
- `pip check`, import, CLI help, status/export, and the complete deterministic
  suite pass;
- legacy nonterminal runs fail explicitly rather than switching runtime.

P7 outcome recorded on 2026-10-12:

- active application composition now accepts only `runtime_backend = "native"`;
  the legacy adapters and backend plugin path were removed rather than wrapped;
- LabBio capability operations no longer inherit a third-party ToolSet or use
  third-party decorators; strict native schemas and dispatch call the existing
  governed host services directly;
- `pantheon-agents` and its constraints file were removed, and the actual
  OpenAI-compatible transport dependency is declared directly as `httpx`;
- legacy reuse and upstream-modification inventories moved to
  `docs/archive/pantheon/` and remain historical provenance only;
- the rebuilt wheel has no Pantheon member or `Requires-Dist`; a clean Python
  3.11 Conda environment installed it without Pantheon, passed `pip check`,
  import and CLI help, and completed provider-free `status`/`export` checks;
- the full deterministic suite passed 1326 tests with 10 skips. A persisted
  legacy runtime revision fails with `RUNTIME_REVISION_MISMATCH` and cannot
  select another backend.

P7 does not satisfy P8 live, Docker, ordinary-user bioinformatics, scientific,
final-report, cleanup/rollback, release, or activation gates.

### P8: Perform real validation and release

Status: items 1-7 completed by 2026-10-13, including one bounded PBMC3k
ordinary-user task through successful execution, governed Agent validation and
interpretation, final report export, and owned-resource cleanup. This is not an
independent external scientific peer review or general reliability claim. Item
8 remains: no immutable P8 release, rollback rehearsal, deployment, production
activation, profile activation, or Skill promotion has occurred.

Validation order:

1. generic native single-Agent live task;
2. generic native Leader-Specialist live task;
3. real-provider interruption/recovery exercise;
4. real Docker execution and cleanup;
5. fresh ordinary-user bioinformatics task in which the Agent chooses its own
   methods, tools, parameters, and program;
6. governed review, interpretation, claim checking, and final-report export;
7. process, lease, container, sandbox, and Artifact cleanup verification;
8. immutable release, rollback rehearsal, and—only with separate
   authorization—production activation.

Gate:

- acceptance levels are reported separately;
- no abnormal loop, writer, lease, container, or sandbox remains;
- production activation and removal of the explicit legacy rollback path occur
  only after all required validation levels pass. The source/local new-run
  default was changed to native in P5 under the user's explicit instruction.

## 9. Parity rules

### Must match exactly

- model-visible authoritative input and tool schemas;
- required/enum/bounds/nested-schema constraints;
- ToolResult/ToolError and CapabilityEvidence authority classification;
- validator acceptance/rejection and legal workflow transitions;
- host-bound identity/scope/actor fields;
- secret, path, raw-data, and hidden-reasoning exclusion;
- strict parse, truncation rejection, finish reason, usage, turn limits, and
  no-progress observation;
- runtime revision and recovery rejection behavior.

### Must match structurally

- visible Specialist set after policy/ancestor filtering;
- parent/child invocation and delegation graph;
- child-context isolation;
- invocation/tool/evidence/trace correlations;
- stage result schema and legal next state.

### Must not be forced to match

- natural-language wording;
- scientific method, parameter, code, or conclusion;
- whether and which Specialist the model chooses;
- tool order where the contract does not prescribe one;
- biological interpretation.

Forcing these items to match would turn the old backend into an oracle for
Agent-owned scientific reasoning and would invalidate the migration test.

## 10. Acceptance ladder

Every report must distinguish:

1. unit/schema tests;
2. service integration and deterministic fixtures;
3. generic live single-Agent and multi-Agent tasks;
4. real bioinformatics data and execution contracts;
5. statistical review, biological interpretation, claim limits, and final
   report;
6. user-visible delivery and resource cleanup;
7. immutable release and, if authorized, production activation.

Passing an earlier level does not imply a later level. A checkpoint, provider
response, completed tool loop, or completed workflow stage does not by itself
prove scientific validity or final-report acceptance.

## 11. Completion checklists

### P0 closeout

- [x] Forward architecture authority permits a LabBio-owned Native Kernel and
      marks Pantheon reuse documents as historical.
- [x] The exact pre-P0 dirty candidate and its limitations are recorded without
      cleaning or overwriting it.
- [x] The portable semantics of all nine required legacy behavior groups have
      backend-neutral contract coverage through a separately isolated legacy
      adapter; the hidden legacy idle allowance is recorded but not inherited.
- [x] The discovered execution-revision feedback contradiction has a focused
      correction and regression.
- [x] Deterministic contract, focused, and full-suite results are recorded in
      the P0 baseline and convergence workplan.
- [x] P1, backend switching, dependency removal, live work, deployment, and
      profile/Skill activation remain outside this checkpoint.

### P1 closeout

- [x] Provider, tool, Agent, team, delegation, budget, interrupt, task, event,
      and deterministic FakeProvider contracts exist in a LabBio-owned package.
- [x] Exact binding, unknown usage, legal state transitions, atomic budget
      reservation, explicit interruption, task acyclicity, delegation ancestry,
      and tool-free finalization-mode boundaries have focused tests.
- [x] The Native Kernel package contains no Pantheon import, copied runtime, or
      domain/routing policy.
- [x] `StageRuntimeInvoker` remains the outer port and the application still
      uses the explicit legacy backend.
- [x] Focused and full deterministic regressions pass at the P1 checkpoint.
- [x] P2 tool dispatch/transport work, backend switching, dependency removal,
      live work, deployment, and profile/Skill activation remain outside P1.

### P2 closeout

- [x] All 19 LabBio capabilities register through native `ToolSpec` and
      `ToolRegistry` without importing Pantheon.
- [x] Generated provider schemas are closed, inline, and retain supported
      enum, range, length, UUID, tuple and nested-object constraints.
- [x] The complete call batch is parsed, schema-checked and budget-reserved
      before the first effect; successful calls run sequentially in input order.
- [x] One complete, non-truncating result projector enforces strict JSON,
      transport size and host-owned model-visible content policy.
- [x] Trusted stage, actor and information-authority bindings cannot be supplied
      or overridden through provider arguments; item-level evidence is retained.
- [x] Focused, contract and full deterministic regressions pass at the P2
      checkpoint.
- [x] P3 Agent/provider loop work, backend switching, dependency removal, live
      work, deployment, and profile/Skill activation remain outside P2.

### P3 closeout

- [x] One exact-binding OpenAI-compatible Chat adapter exists with strict wire
      parsing, explicit thinking transport, unknown usage preservation, and no
      retry, provider switch, or fallback.
- [x] The native capability loop records provider observations before later
      settlement failures, validates complete tool batches before effects,
      executes tools only in the foreground, and canonically pairs every call
      with one result.
- [x] Deadline, interrupt, history, provider-turn, token, evidence, idle,
      incomplete-response, malformed-response, and scripted-provider exhaustion
      paths terminate explicitly with typed failures and safe events.
- [x] Tool-free strict finalization and its bounded generic format correction
      have focused coverage; duplicate JSON and incomplete responses fail
      closed without repair or tool replay.
- [x] All nine portable legacy behavior groups pass through the native backend,
      focused P3 verification passes, and the full deterministic suite is green
      at the P3 checkpoint.
- [x] A controlled real-provider synthetic tool loop passes on the final source;
      its earlier `null` tool-call and empty tool-turn content failures are
      retained as protocol evidence rather than hidden.
- [x] P4 collaboration, application wiring, backend switching, dependency
      removal, application-level live/data/Docker work, deployment, and
      profile/Skill activation remain outside P3.

### P4 closeout

- [x] Trusted Agent registration preserves configured order, exact membership,
      exact provider binding, explicit tool/output ownership and complete
      externally supplied profile instructions.
- [x] `list_specialists` exposes unranked metadata and `delegate` executes only
      the model-selected valid target; no keyword, suffix, error-text or host
      automatic-selection route exists.
- [x] The bounded task board, fresh child identities, exact parent tool link,
      shared root budget/depth, shared interrupt, and actor-attributed global
      timeline have focused coverage.
- [x] Self, ancestor, invalid target, ownership mismatch, depth, task limit,
      delegation budget, child failure, interruption and oversized child-result
      paths fail closed without starting unauthorized child work.
- [x] Child context contains only full external profile instructions and the
      explicit assignment; parent and sibling histories remain isolated.
- [x] Child prose stays `AGENT_PROSE`, separate from tool-derived evidence
      references, and typed outcomes reach the parent without silent truncation.
- [x] Focused, Native Kernel/contract and full deterministic regressions pass,
      and a generic current-code live proves model-selected Specialist use.
- [x] P5 application wiring, existing profile/Skill migration, backend
      switching, dependency removal, application-level data/Docker work,
      deployment and activation remain outside P4.

### P5 closeout

- [x] `NativeKernelStageInvoker` implements the existing application port and
      preserves stage, workspace, input-usage and capability authority checks.
- [x] Native capability collaboration uses per-Agent governed registries and
      actor-bound evidence; typed finalization remains tool-free.
- [x] Existing Skill, execution and report grounding controls constrain the
      Native response schema and post-response validation.
- [x] `FINALIZE_ONLY` uses persisted evidence without replaying tools, and
      malformed Native output cannot enter accepted result history.
- [x] Skill curation depends on a generic typed invocation port and uses the
      Native finalizer in local composition.
- [x] Runtime manifests include backend, contract, kernel, provider, tool-schema
      and team/profile identity; backend changes alter runtime revision.
- [x] Native is the default for new application/local runs under the user's
      explicit P5 condition; Pantheon requires explicit selection and there is
      no hidden fallback.
- [x] A complete fake-provider WorkflowRun and the full deterministic suite
      pass at the P5 checkpoint.
- [x] P6 differential/live migration, Pantheon dependency removal, real
      application/Docker/bioinformatics validation, deployment and activation
      remain outside P5.

### P6 closeout

- [x] Scripted transcripts compare canonical schema constraints, successful
      call/feedback/effect shape, provider-turn observations and strict invalid
      argument rejection across explicit legacy and Native backends.
- [x] Isolated Artifact roots preserve actor, scope, authority and evidence
      shape without duplicating an effect against shared mutable state.
- [x] Model-selected successful delegation has distinct parent/child identities,
      a correlated parent tool call, isolated child context and a typed terminal
      outcome in both implementations.
- [x] Native and explicit legacy runtime revisions differ; cross-backend
      recovery is rejected in both directions and rollback creates a fresh run.
- [x] New application/local runs still default to Native with no hidden
      fallback. Provider-specific prose, timestamps, prefixes and private object
      formats were not made into parity requirements.
- [x] The focused P6 suite passes against source and the active sealed release;
      the complete deterministic suite passes 1698 tests with 34 skips.
- [x] P7 dependency/adapter removal and P8 real-provider, Docker, scientific and
      final-report validation remain outside P6.

### P7 closeout

- [x] Active source and tests have no Pantheon import; only negative removal
      assertions and archived historical provenance retain the name.
- [x] Legacy adapters, the backend branch, dependency constraint, and package
      requirement were removed without copying Pantheon into LabBio.
- [x] The direct HTTP transport dependency is declared and generated package
      artifacts were rebuilt from current source.
- [x] The wheel contains no legacy adapter and no Pantheon `Requires-Dist`.
- [x] A clean Python 3.11 environment has no Pantheon module and passes install,
      `pip check`, import, CLI help, status, and export.
- [x] The complete deterministic suite passes 1326 tests with 10 skips.
- [x] Legacy nonterminal runtime identity fails explicitly; no fallback or
      cross-runtime continuation exists.
- [x] The active P5 release was verified unchanged; P7 was not deployed.
- [x] P8 live/provider/Docker/bioinformatics/scientific/report/release work
      remains outside P7.

### Final migration completion

- [x] Repository governance authorizes the native kernel.
- [ ] Current dirty changes are preserved and converged independently.
- [x] Native provider, tool, Agent, team, delegation, and finalizer contracts
      exist without Pantheon imports.
- [x] All Pantheon-derived behavior contracts pass.
- [x] Domain profiles and practices are external to core runtime code.
- [x] Temporary backend selection has no hidden fallback.
- [x] No run can change backend after creation.
- [x] Legacy nonterminal runs have an explicit, fail-closed policy.
- [x] Active source and tests contain no Pantheon imports.
- [x] Package metadata and clean install contain no Pantheon dependency.
- [x] Full deterministic and security suites pass.
- [x] Generic native single-Agent live passes.
- [x] Generic native Leader-Specialist live passes.
- [x] Real-provider interruption/recovery and real-Docker validation pass.
- [x] Fresh ordinary-user bioinformatics execution reaches final report without
      Codex performing Agent-owned scientific work.
- [x] Technical completion and governed Agent review are reported separately
      from independent external scientific review.
- [x] Owned live resources are cleaned after the accepted ordinary-user run.
- [ ] Immutable release rollback is rehearsed.
- [x] Active architecture and convergence documents reflect the current state.
- [x] Production activation was separately reviewed and authorized for the
      current CLI surface.

## 12. Main risks and controls

| Risk | Control |
|---|---|
| Losing behavior hidden in the Pantheon fork | Convert every fork patch into native contract tests before cutover |
| Provider/schema incompatibility | One explicit adapter, strict preflight, no protocol/provider fallback |
| Duplicate external effects during comparison | Fixtures/transcripts first; isolated fresh runs only; no effectful shadow double-run |
| Child-context or actor leakage | Isolated invocation memory and typed correlation/evidence tests |
| Unsafe recovery across kernels | Freeze backend/revision at run creation; reject mismatch |
| Scientific routing enters the kernel | External profiles/Skills; no keywords, suffixes, or error heuristics |
| Current dirty work is overwritten or mixed | P0 baseline convergence and separate release/root-cause cluster |
| Scope grows into a full Pantheon rewrite | Implement only the features used by LabBio and listed in this contract |
| Tests are mistaken for live/scientific acceptance | Use the explicit seven-level acceptance ladder |

## 13. Unique continuation point

P8 real validation is complete through one bounded ordinary-user PBMC3k task.
Fresh run `5cc1c236-a9fc-41c1-8991-af746919b29a` is
`COMPLETED / LEARN / STABLE`, with selected successful execution
`c6c8d2bb-0692-466c-85b4-19db4c8b1f91`, a 38-record selected summary and an
Agent-authored exported Chinese report. Codex did not select or author the
method, tools, parameters, program, interpretation, or report. Earlier runs
remain preserved failure evidence and must not be resumed. The deterministic
suite passes 1338 tests with 10 intentional skips, and owned live resources are
clean.

The unique continuation is the immutable P8 release and rollback rehearsal.
Independent external scientific peer review and repeat-run reliability remain
separate claims; this single descriptive task does not establish either.

Do not activate production, activate/promote profiles or Skills, or remove the
current immutable rollback release without the corresponding separate
authorization and evidence. Do not describe the completed generic levels as
scientific or final-report acceptance.
