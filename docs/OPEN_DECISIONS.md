# Open Architecture Decisions

This file records unresolved architecture decisions and decisions resolved when
they directly determine the current repository boundary.

## Resolved — Source/package topology

Resolved by the user before Phase 1: LabBioAgentOS is an independent Python
repository/package beside PantheonOS and uses PantheonOS as its runtime dependency.
LabBio code must not be moved into the PantheonOS repository.

Phase 1 initializes `/Users/wangyuchen/Coding/LabBioAgentOS` as its own git
repository and uses the distribution/package name `labbioagentos` with a `src`
layout. PantheonOS remains in `/Users/wangyuchen/Coding/PantheonOS`.

## OPEN-002 — Structured child-failure contract before Phase 3

**Decision deadline:** before Phase 3 implementation, not before Phase 1 contracts.

**Observed state:** delegated child success returns only `response.content`. A tool-task exception is caught in `Agent._handle_tool_calls` and converted to `repr(exception)` as ordinary tool content, so a stage adapter cannot reliably distinguish failure from prose by type alone.

**Question:** Which generic, non-scientific envelope should represent delegated invocation success/failure to the LabBio stage adapter while preserving the text result visible to the parent runtime LLM?

**Why a decision is required:** Phase 3 acceptance requires structural failure propagation. Inferring failure from strings is unsafe; changing Pantheon core prematurely is also unsafe.

**Allowed resolution space:** first test a LabBio wrapper/subclass with an explicit invocation record; consider the conditional `pantheon/agent.py` hook in `UPSTREAM_MODIFICATIONS.md` only if the wrapper cannot meet the contract.

## Not open in Phase 0

The following are intentionally deferred to their roadmap phases and must not be invented now: workflow persistence backend, Docker resource limits, artifact type taxonomy, exposure approval UX, long-term memory schema, Gold Skill similarity/adaptation policy, and bioinformatics agent roster. None is needed to complete the Phase 0 mapping.
