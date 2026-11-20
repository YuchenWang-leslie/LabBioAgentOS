# R2 DBOS recovery prototype

## Current scope: minimal prototype accepted, not enabled in App

The 2026-11-03 final freeze passes22joint Native/Apptainer tests and71existing
prototype tests, source3307passed/82skipped,6real execution regressions and80
read-only audit checks. Production does not import this directory or require
DBOS. No scientific program, live model API, new worktree or release was used.

`joint_round.py` connects a synthetic provider, the actual Native tool/schema
boundary, submission service, Executor, user-local Apptainer, JobLedger and scoped
ObservationStore. A later fake-provider turn receives the original Native public
feedback projection. Stable call/Plan binding precedes effects; DBOS stores only
Native references. Receipt/observation consistency is checked on recovery and
cached completion; uncertain outcomes refuse replay. Lost stable mappings cannot
be guessed from existing completed jobs, and missing Native authority storage
cannot be recreated as a substitute. Native Workflow still owns business state.

Same-chain faults cover response/intent/acceptance/receipt/observation/checkpoint/
feedback gaps, evidence loss/tampering, batch stop, Native Specialist cancellation
and abrupt controller loss. Cancellation uses the existing resource owner and
confirms the old tree before handover. The original unsafe direct-DBOS cancellation
counterexample remains intentionally passing; it is a risk witness, not safety.

Use the existing isolated DBOS3.3.0 interpreter and explicitly ready non-setuid
user installation/SIF. From the repository root:

```bash
LABBIO_RUN_LOCAL_SYSTEMD=1 LABBIO_RUN_APPTAINER=1 \
LABBIO_APPTAINER_BINARY=/absolute/user-prefix/bin/apptainer \
LABBIO_APPTAINER_SIF=/absolute/verified/python.sif \
PYTHONPATH=src /path/to/experimental/python -m pytest prototypes/dbos_r2 -q
```

Defaults stay Local+user Apptainer; no sudo, silent Docker fallback, extra mandatory
control service or generic recovery engine. Resource mode is explicitly
systemd-affinity with existing memory/pids/lifecycle bounds, not CPU quota.
Physical power loss, shared-filesystem faults, multi-node takeover, real-provider
private continuation and full Native-stage cold recovery are unaccepted. R3/R5
and App/production enablement remain separate. Preserve failed versions/results.

Final evidence: WYC `result/r2-recovery-20261103/joint-chain/RESULT.md`,
`FREEZE-FINAL.json`, `AUDIT.json` and archived frozen source.

## Historical first fake-provider/tool probe and intermediate gates

This directory is an independent experiment. Production does not import it and
does not require DBOS. It makes no model API requests and launches no analysis.
The fake provider returns a synthetic `ModelTurn`; the fake tool writes through
the real JobLedger and ObservationStore. It is not a ToolExecutor/schema or
Docker/Apptainer integration test.

The target is DBOS 3.3.0, Python 3.11, MIT. The separate experimental environment
uses `dbos[aiosqlite]==3.3.0`. DBOS's PostgreSQL driver remains a package dependency
even when the experiment uses a local SQLite database; no database server is
started. See [release metadata](https://pypi.org/pypi/dbos/3.3.0/json),
[tagged dependencies](https://github.com/dbos-inc/dbos-transact-py/blob/3.3.0/pyproject.toml),
and the [license](https://github.com/dbos-inc/dbos-transact-py/blob/3.3.0/LICENSE).

Run from the repository root with an interpreter containing that experimental
dependency and the project's normal test dependencies:

```bash
python -m pytest prototypes/dbos_r2/test_round_trip.py -q
```

DBOS checkpoints only Native receipt IDs and hashes. Synthetic provider intent,
response and binding records reuse LocalArtifactStore with INTERNAL_ONLY release
basis; tool observations use ObservationStore. Replays revalidate Native evidence
outside the workflow as well, because a fully cached DBOS workflow skips its
original body. Uncertain provider results retain unknown usage and stop. Open
execution intents stop without resubmission; this experiment does not implement
execution-side reconciliation.

The existing process-scoped `run_writer` lease is held before DBOS is launched and
until its process exits. A replacement cannot take over from a live lease owner.
There is no distributed lease, epoch fencing or live-worker takeover. The test
named `test_dbos_cancellation_alone_does_not_revoke_native_sync_writer` is an
intentional counterexample: it passes when a cancelled synchronous DBOS step can
still commit a Native receipt. Passing that test identifies an unmet integration
requirement; it does not validate cancellation safety.

Fault tests simulate controller process death with `os._exit(75)`, not machine
power failure. The existing LocalArtifactStore does not fsync its JSON envelope,
so these tests cannot establish power-loss durability for provider receipts.
Version tests reject a changed declared experimental version; they do not verify
the full production source/schema/provider/environment manifest.

The experiment must remain unintegrated until real invocation permissions,
idempotent execution-side reconciliation, cancellation and process-tree cleanup,
post-takeover write rejection, complete version binding and durable provider
attempt receipts are separately established. No Coordinator or stage loop was
rewritten to make these tests work.

## Local controller ownership probe (2026-11-03)

`controlled_round.py` adds an isolated process boundary using the existing
SystemdLocalLauncher, not a new recovery engine. It holds `run_writer` in the
controller, gives the worker a stable service identity derived from the canonical
run directory, and retires any previous service before dispatching a replacement.
Acquiring the controller's lease alone is **not** proof that its worker exited.
The existing user service manager owns all worker descendants. Local integer-core
affinity, memory/pids limits and lifecycle enforcement remain required; this
does not install system software, require sudo, or fall back to Docker.

The launcher now distinguishes an explicit absent unit from a failed/unknown
lookup. A terminal service must have MainPID=0 and either no cgroup or a removed/
unpopulated cgroup before it is confirmed. A populated or unreadable cgroup keeps
the result unconfirmed. Cleanup is issued once; repeated cancellation waits for
that same operation before releasing the lease and propagating cancellation.
Failed cleanup propagates, and the next controller must still retire the old
stable service before any dispatch. There is no distributed fencing or takeover
from a live lease owner.

Run the opt-in prototype suite with the separate DBOS 3.3.0 interpreter:

```bash
LABBIO_RUN_LOCAL_SYSTEMD=1 PYTHONPATH=src python -m pytest prototypes/dbos_r2 -q
```

`blocking_worker.py` is a synthetic host-side test fixture, not an Agent analysis
program. It demonstrates that a sync DBOS writer and a detached SIGTERM-ignoring
writer can both remain alive after DBOS cancellation. Tests stop the entire unit,
verify both writers are gone, then allow a replacement and verify no old receipt
appears. A separate abrupt-controller-death test checks orphan retirement before
replacement. These tests exercise process ownership, **not** permission to resume
a cancelled Native Workflow, nor a real provider/tool integration. Existing
unknown-side-effect refusal and the original cancellation counterexample remain.

Additional fault tests exercise pre/post-acceptance uncertainty, Native completion
before DBOS checkpoint, missing/tampered Native observations, and changed declared
request/version. Lexical path aliases must identify the same worker service.
They do not establish complete production source/schema/model/environment binding
or power-loss durability. R2 production integration remains unaccepted; no DBOS
dependency or entry point was added to the application.

Results and all failure-first/intermediate records are under WYC
`result/r2-recovery-20261103/`. Native controller and real user-level Apptainer
regressions are recorded separately from this fake provider/tool experiment.

## Receipt, identity and acceptance gate (2026-11-03 continuation)

New runs use format-2 internal receipts. `native_record` validates Native identity,
owner/project/lab/run/type/release basis and a canonical payload hash. Before it
acknowledges either a newly written or an existing receipt, it syncs the envelope
and its ancestor directories. Failed synchronization propagates; a readable file
left by a failed sync cannot bypass the next barrier. An intent is acknowledged
before a fake external request; a lost response remains unknown and is not
replayed. The generic ArtifactStore was not silently upgraded or duplicated into
a second business journal. Old experimental envelopes are not migrated/resealed.
This verifies the local fsync protocol, **not** actual machine power-loss recovery.

`runtime_binding.py` reuses Native `runtime_manifest` plus the actual schemas,
prototype/source identities, dependency versions and Python executable identity.
It never loads provider credentials. The binding is stored before DBOS launch;
the DBOS application version is content-derived rather than only a caller label.
Real local environment bindings hash the SIF and the complete receipted non-setuid
user installation (including the executable behind a shell wrapper and helpers).
An environment hash is labelled HASH_BOUND_NOT_PREFLIGHT. Host kernel/system
libraries are not sealed, and the fake provider/tool branch claims no container.

`execution_reconciliation.py` checks that binding, the exact Native execution
request and JobLedger identity before querying the existing launcher. It never
submits or promotes launcher completion to a result. Known live jobs return
WAITING; absent/unknown jobs, state conflicts and terminal jobs without a Native
receipt stay BLOCKED with replay_allowed=false. Missing ledger/SIF storage is
not recreated. A present Native receipt still requires the existing authorized
inspection path; the diagnostic query does not fabricate an observation/result.

Opt-in `test_execution_reconciliation_real.py` executes infrastructure-only
programs through the real Native execution_submit/SubmissionService/Executor and
user-level Apptainer. It tests lost acceptance acknowledgement, completed process
without a Native receipt, failed receipt commit, and normal receipt restoration
using a fresh service object. Failed cases reject a second submission and keep
the actual single-start marker. Normal inspection reads the persisted original
binding without another execution. These are not Agent-authored analyses, full
OS restarts or a DBOS-backed production-stage integration.

See WYC `result/r2-recovery-20261103/receipt-binding/RESULT.md` for the new freeze,
preserved negative results and acceptance boundary. Real provider continuation,
joint DBOS/real-tool fault windows and runtime child/batch cancellation remain
separate gates; R3 and production activation are not released by these probes.
