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

## RESOLVED-002 — Structured child-failure contract

Resolved in Phase 3 without a Pantheon core modification.

**Observed state:** delegated child success returns only `response.content`. A tool-task exception is caught in `Agent._handle_tool_calls` and converted to `repr(exception)` as ordinary tool content, so a stage adapter cannot reliably distinguish failure from prose by type alone.

`DelegationPolicyPlugin` decorates the registered native `call_agent` function.
It records `SUCCEEDED`, `DENIED`, or `FAILED` as a typed
`DelegationRecord`. Denial and failure also return a `labbio_delegation`
envelope as ordinary tool content, so the parent runtime model can react while
the adapter receives the same outcome through
`AgentStageResult.delegations`.

Allowed calls execute Pantheon's original closure. The decorator catches child
exceptions before Pantheon's general tool dispatcher converts them to
`repr(exception)`, avoiding string inference while preserving all native
delegation metadata and safety checks.

No upstream patch is proposed. The full Phase 3 contract is covered by offline
tests.

## Deferred beyond Phase 3

The following are intentionally deferred to their roadmap phases and must not be invented now: RunTrace/EventBus, workflow persistence backend, Docker resource limits, artifact type taxonomy, exposure approval UX, long-term memory schema, Gold Skill similarity/adaptation policy, and bioinformatics agent roster. None is needed to complete Phase 3.
