# C10 Durable Control Plane Acceptance

Status: accepted on `c10-durable-control-plane` after deterministic recovery
and full non-live regression. No provider, Docker, scientific workflow, C11,
C12, or Pantheon change was used for acceptance.

## Accepted contract

- Authoritative control state: `RunStateStore`.
- Observational audit only: `RunTrace`.
- Durable formats: strict bounded Pydantic data serialized as JSON; SQLite uses
  transactional writes and explicit optimistic version conflicts.
- Safe continuation: explicit `recover_run(run_id, principal, workspace)` from
  a `STABLE` boundary after current identity/access, Artifact, workflow, and
  runtime-revision checks.
- Uncertain external effects: `STAGE_IN_FLIGHT` and
  `GATE_DECISION_IN_FLIGHT` fail closed with operator recovery required. Neither
  is automatically replayed.
- Workflow authority: only `WorkflowEngine` performs lifecycle transitions;
  attachment validates a recovered snapshot but emits no transition.
- Runtime reconstruction: provider/tool/coordinator/executor objects are rebuilt
  from trusted current configuration. Only prior typed result data is restored.

## Deterministic acceptance mapping

| Item | Result |
|---|---|
| D1 SQLite roundtrip | Pass; close/reopen returns the same validated JSON record |
| D2 version conflict | Pass for in-memory and SQLite stores |
| D3 CREATED recovery | Pass; same run and scope, no model call |
| D4 stable RUNNING recovery | Pass; continuation starts at the stored boundary |
| D5 WAITING recovery | Pass |
| D6 gate identity | Pass; gate ID, source stage, and domain reference remain exact |
| D7 C9 Skill gate | Pass; SQLite Skill authorization succeeds after reconstruction |
| D8 retry/results | Pass; retry count and prior typed results survive without duplication |
| D9 identity | Pass; run UUID alone cannot recover a run |
| D10 authorization change | Pass; current project access is rechecked |
| D11 missing Artifact | Pass; explicit `REQUIRED_ARTIFACT_MISSING` |
| D12 runtime drift | Pass; explicit `RUNTIME_REVISION_MISMATCH` |
| D13 stage in flight | Pass; no replay and no workflow retry |
| D14 EXECUTE effect | Pass; synthetic external counter remains exactly one |
| D15 gate decision in flight | Pass; handler is not reapplied |
| D16 COMPLETED | Pass; terminal result survives and no stage reruns |
| D17 FAILED | Pass; terminal result survives and no stage reruns |
| D18 CANCELLED | Pass; terminal result survives and no stage reruns |
| D19 JSONL continuity | Pass; next event continues at N+1 after application reconstruction |
| D20 trace authority | Pass; observational trace edits do not mutate control state |
| D21 Gold SQLite compatibility | Pass |
| D22 coordinator reconstruction | Pass; a new coordinator is built, never deserialized |
| D23 safe Artifact reference | Pass; recovered model-facing reference has no locator |
| D24 recovery call behavior | Pass; recovery alone invokes no provider/model |

The final deterministic suite result is `330 passed, 9 skipped`, plus the one
pre-existing Uvicorn websockets warning. The C9 baseline was `303 passed, 9
skipped`; all accepted C1-C9 contracts remain green.

## Actual crash-consistency behavior

| Durable condition | Session reconstruction | Automatic continuation | Outcome |
|---|---:|---:|---|
| Stable CREATED | Yes | Yes after explicit recovery | Normal start |
| Stable RUNNING | Yes | Yes after explicit recovery | Continue current stage |
| Stable WAITING_FOR_USER | Yes | No until typed decision | Normal gate resume |
| STAGE_IN_FLIGHT | No | No | Operator reconciliation required |
| GATE_DECISION_IN_FLIGHT | No | No | Operator reconciliation required |
| COMPLETED / FAILED / CANCELLED | Yes | No | Return terminal result |
| Runtime revision mismatch | No | No | Explicit mismatch issue |
| Missing required Artifact | No | No | Explicit missing-Artifact issue |
| Identity/access mismatch | No | No | Current authorization rejection |

There is no atomic commit spanning SQLite, JSONL, Artifact files, Docker, or a
provider. The durable pre-operation marker is the conservative replay barrier.

## Change classification and limitations

Production changes are generic infrastructure. Test invokers, synthetic side
effect counters, and the C9 Skill fixture are test fixtures. No scientific
runtime intelligence, keyword routing, preset workflow, prompt patch,
compatibility fallback, hidden retry, automatic recovery decision, or Pantheon
change was introduced.

The accepted local implementation does not provide multi-process writer
claims, distributed transactions, HA failover, automatic uncertain-effect
reconciliation, Redis/PostgreSQL/Kubernetes scheduling, or automatic runtime
migration. Those limitations are explicit and do not weaken the no-replay
invariant.
