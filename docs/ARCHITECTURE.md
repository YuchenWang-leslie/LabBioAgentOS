# LabBioAgentOS Architecture Index

## Current authority

Read the active architecture in this order:

1. repository `AGENTS.md` for engineering and Agent-sovereignty guardrails;
2. [Native Kernel Migration Plan](NATIVE_KERNEL_MIGRATION_PLAN.md) for the
   approved target, migration phases, gates, and stop boundaries;
3. [LabBio Architecture](LABBIO_ARCHITECTURE.md) for the implemented platform,
   retained ownership boundaries, and historical acceptance evidence;
4. [Open Decisions](OPEN_DECISIONS.md) for resolved architecture choices;
5. [Real E2E Convergence Workplan](REAL_E2E_CONVERGENCE_WORKPLAN.md) for dated
   execution evidence and the current continuation point.

## 2026-10-13 status

Native Kernel P8 real validation has completed through one bounded ordinary-user
PBMC3k task, successful Docker execution, governed Agent validation and
interpretation, and an exported final report. This does not constitute an
independent external scientific peer review or repeat-run reliability claim.
Immutable P8 release, rollback rehearsal and separately authorized production
activation remain.
Active source
now has only the LabBio-owned Native Kernel; the Pantheon backend, adapters,
dependency, and constraint have been removed. The immutable CLI release
`native-p5-20261012T043811Z` remains active under
`LabBioAgentOS-production/current`; P7 was not deployed, so that older sealed
release still contains its temporary legacy wheel while defaulting to Native.
P0 produced authority reconciliation, exact baseline evidence, and
implementation-independent characterization tests. P1 added the
LabBio-owned backend-neutral contract package and deterministic FakeProvider
without connecting them to the application default. P2 added the independently
designed native tool registry, strict whole-batch argument transport, governed
LabBio capability binding, item-level evidence, and one non-truncating
model-visible result projector. P3 added the native single-Agent capability and
tool-free finalization loops plus one exact-binding OpenAI-compatible Chat
adapter. P4 added dynamic native Leader-Specialist discovery/delegation,
bounded task state, isolated child invocations, shared hierarchical controls,
typed child outcomes and one actor-attributed team timeline. P5 connected that
runtime behind `StageRuntimeInvoker`, preserved the existing grounding and
recovery contracts, migrated local Skill curation to a typed native invocation,
and made native the new-run default. P6 added isolated scripted/transcript
differential acceptance and two-way cross-backend recovery rejection. P7
removed the legacy implementation and dependency, rebuilt the wheel, and
validated it in a clean environment. P8 has validated generic Native live
behavior, the governed Docker boundary, and fresh ordinary-user run
`5cc1c236-a9fc-41c1-8991-af746919b29a` through
`COMPLETED / LEARN / STABLE`, a selected successful execution, a bounded
summary, and an Agent-authored Chinese report. Earlier invalid-output and
turn-boundary failures remain preserved evidence. P7/P8 source release and
deployment, rollback rehearsal, always-on service deployment, and profile/Skill
promotion are not complete.

The target retains `WorkflowEngine`, Principal/Workspace governance,
Artifact/Exposure, Docker, RunState, RunTrace, Gold, and persistent Memory while
replacing the in-stage provider/Agent/tool/team/finalization runtime. One run
uses one frozen backend and runtime revision. There is no hidden fallback and
no cross-backend recovery.

The P1-P3 source surface is `src/labbioagentos/kernel/`, plus the neutral
capability catalog and native LabBio binding in `src/labbioagentos/runtime/`.
The kernel contains strict public
contracts for exact provider binding, tool/message transport, Agent/team/task
identity, delegation ancestry, state transitions, budgets, interrupts, safe
events, the deterministic provider test double, native schemas, strict parsing,
foreground dispatch and complete result projection. The runtime binding keeps
trusted stage, actor and authority fields outside provider arguments. Domain
behavior and specialist selection are absent from the kernel by design. The P3
loop adds typed provider observations and termination, strict canonical tool
replay, bounded evidence, invocation-local private-reasoning continuity, and
strict tool-free structured finalization. `StageRuntimeInvoker` remains the
outer substitution port; the native team runtime is now application-wired.
Trusted full profile instructions are injected from the external behavior
layer. Specialist discovery preserves trusted configuration order and the
model selects targets; the core has no domain profiles, ranking, keyword route,
or automatic selection.

## Historical and implementation-reference documents

- `Roadmap.md` is the superseded Pantheon-extension roadmap.
- `archive/pantheon/PANTHEON_REUSE_MAP.md` and
  `archive/pantheon/UPSTREAM_MODIFICATIONS.md` are archived legacy behavior and
  provenance inventories.
- `RUNTIME_INTEGRATION_MAP.md` and `RUNTIME_TOOL_SURFACE.md` describe the
  pre-migration implementation and retained contracts.
- `STAGE_RUNTIME_CONTRACTS.md` retains active typed stage/evidence contracts;
  its Pantheon names describe legacy wiring. `LOCAL_TASK_ENTRYPOINT.md` is the
  current local entrypoint guide.
- `INTEGRATION_GAPS.md` and `VERTICAL_SLICE_PLAN.md` are historical
  Pantheon-backed integration and acceptance evidence.
- C8-C12 gap, threat-model, and acceptance documents remain historical evidence;
  they are not rewritten to pretend the native kernel already existed.

`PRODUCTIONIZATION_DEBUG_GUIDE.md` is not present in the source tree at this
checkpoint. P0 found no production tree; the P6-authorized CLI deployment now
provides one sealed `current` release. Historical files must not be
used to invent current production state.
