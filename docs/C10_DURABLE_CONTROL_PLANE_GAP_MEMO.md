# C10 Durable Control Plane Gap Memo

Status: pre-implementation architecture audit at LabBioAgentOS
`24867f267c4dc35c55a4971f04b0b3785842ecea` and Pantheon
`45ef598f8d79bd98e9befc7c549980b731476662`.

This memo records the current source behavior before C10 changes. It is not an
acceptance claim.

## Q1-Q7 source audit

### Q1 - WorkflowEngine ownership

Yes. `WorkflowEngine` owns runs only in its process-local
`self._runs: dict[UUID, WorkflowRun]`. `_require_run()` requires object identity
against that dictionary, so a serialized `WorkflowRun` cannot be used by a new
engine until an explicit validated attach operation exists.

### Q2 - application sessions

Yes. `LabBioApplication` owns sessions only in
`self._sessions: dict[UUID, _ApplicationRunSession]`. A restart loses the
trusted request scope and Artifact ID sets, reconstructed safe domain
references, the mutable `WorkflowRun`, the `RuntimeCoordinatorService`, and the
coordinator's prior `RuntimeStageResult` sequence. The provider, tools, access
service, callbacks, and other runtime objects are configuration-owned and must
be reconstructed, not persisted.

### Q3 - facts already durable elsewhere

- `LocalArtifactStore` persists Artifact metadata and payloads, including
  execution Artifacts and report Artifacts. It remains the authority for
  Artifact existence, scope, and store-private location.
- `JsonlTraceSink` can persist append-only `RunTraceEvent` observations. Trace
  is audit evidence and is not authoritative workflow state.
- `SQLiteSkillStore` persists Gold Skill, access, usage, and proposal data as
  validated Pydantic JSON in SQLite transactions.
- None of those stores persists authoritative `WorkflowRun` state or the
  application session reconstruction data.

### Q4 - WAITING_FOR_USER restart

No. A new `LabBioApplication` has empty `_sessions`, and its new
`WorkflowEngine` has empty `_runs`. A run UUID therefore cannot recover a
pending gate, even when its trace, Artifacts, and Gold data still exist.

### Q5 - stable boundary versus possible escaped side effect

No. The current application does not durably mark a runtime stage or domain
gate decision before invoking it. After process loss, `RUNNING` cannot prove
whether a provider, Docker execution, Artifact write, or domain decision
already escaped the process boundary. Automatic continuation would risk replay.

### Q6 - runtime contract drift

No. A run is not bound to a host-owned runtime revision. A reconstructed
application could therefore use changed workflow, profile, tool, or runtime
contracts without detecting the difference.

### Q7 - JSONL trace continuation

Yes for a single writer after an orderly single-process restart.
`RunTraceRecorder._sequence_for()` reads and validates the existing sink events
on first use, requires contiguous sequence values `0..N`, and selects `N+1`.
`JsonlTraceSink.append()` also checks the expected sequence before appending.
This is not a multi-process locking guarantee and does not make trace recovery
authority.

## Required C10 boundary

`RunStateStore` will be authoritative durable control state. `RunTrace` remains
append-only observational evidence. Recovery will deserialize only strict data:
request scope and Artifact IDs, safe domain references, a `WorkflowRun`
snapshot, prior runtime stage results needed to reconstruct coordinator
context, a host-owned runtime revision, recovery/in-flight markers, timestamps,
and an optimistic record version.

Pantheon agents, teams, provider clients, tool sets, coordinators, executors,
access services, callbacks, plugins, open handles, credentials, paths, and
Artifact storage locators will not be serialized. No pickle will be used.

`WorkflowEngine` remains the only component that starts, transitions, retries,
pauses, resumes, fails, or completes a run. A store only creates, retrieves,
version-checks, and replaces snapshots. A recovered run must be explicitly
validated and attached to a new engine before normal operations resume.

## Checkpoint ordering and crash boundary

The intended ordering is:

1. Persist a newly created run at `STABLE`.
2. Persist `STAGE_IN_FLIGHT` with the exact stage and invocation identity.
3. Invoke the runtime stage and allow existing LabBio-owned state mutations.
4. Persist the resulting workflow/coordinator state and clear the marker to
   `STABLE`.
5. Before a domain gate decision, persist `GATE_DECISION_IN_FLIGHT`.
6. Apply the domain decision through its existing handler and resume through
   `WorkflowEngine`.
7. Persist the updated workflow state and clear the marker to `STABLE`.

There is no shared transaction across SQLite, JSONL, Artifact files, Docker, or
the provider. If the process fails after an in-flight marker but before the
stable checkpoint, C10 must report a bounded operator-recovery condition. It
must not replay the stage, reapply the gate decision, or create a workflow
retry.

## Crash consistency matrix

| Durable condition | Recoverable into a session? | Automatic continuation allowed? | Required handling |
|---|---:|---:|---|
| Stable `CREATED` | Yes | Yes, after explicit recovery | Reauthorize, reconstruct, then normal `run()` |
| Stable `RUNNING` boundary | Yes | Yes, after explicit recovery | Continue current stage; do not duplicate prior results |
| Stable `WAITING_FOR_USER` | Yes | No stage call until a new typed decision | Preserve the exact pending gate and use normal resume semantics |
| `STAGE_IN_FLIGHT` | No | No | Fail closed; operator reconciliation required |
| `GATE_DECISION_IN_FLIGHT` | No | No | Fail closed; never reapply automatically |
| Stable `COMPLETED` | Yes | No | Return the terminal result; no stage replay |
| Stable `FAILED` | Yes | No | Return the terminal result; no stage replay |
| Stable `CANCELLED` | Yes | No | Return the terminal result; no stage replay |
| Runtime revision mismatch | No | No | Explicit revision-mismatch recovery issue |
| Required Artifact missing | No | No | Explicit missing-Artifact recovery issue |
| Authorization changed or identity mismatched | No | No | Reject using current trusted authorization checks |

## Scope and first-version limitations

C10 is generic control-plane infrastructure. It will not choose scientific
methods, infer recovery actions, add hidden retries, or alter C9 Skill semantics.
It does not claim multi-process writer coordination, distributed transactions,
high availability, automatic uncertain-side-effect reconciliation, or runtime
migration. Operator intervention for an uncertain in-flight operation is an
intentional fail-closed result.
