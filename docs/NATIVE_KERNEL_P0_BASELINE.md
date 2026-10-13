# Native Kernel P0 Baseline

## Status

- Recorded: 2026-10-10, before P0 authority/test edits
- Classification: exact pre-P0 dirty candidate, not an immutable release
- Authorized phase: Native Kernel P0
- Active runtime backend at capture: Pantheon legacy
- Production release and health: `NOT VERIFIED`
- Live provider, data, Docker execution, deployment, profile activation, Skill
  promotion, commit, and push performed by this checkpoint: none

This file freezes the evidence needed to preserve the existing working tree
while P0 introduces documentation and characterization tests. It does not claim
that the mixed dirty tree is a release baseline or that its scientific/live
behavior has been accepted.

## Source identity

| Item | Captured value |
|---|---|
| Repository | `/media/desk16/iy1982/WYC/projects/LabBioAgentOS` |
| Branch | `fix/gold-approval-handoff` |
| HEAD | `9608bc1887f97683f14c1a39ac7068352a4958e7` |
| Local `main` | same commit as HEAD at capture |
| Tracked modified paths | 37 |
| Untracked paths | 16 |
| Total dirty paths | 53 |
| Original dirty paths excluding the already-created migration plan | 52 |
| `git diff --check` | PASS |

The sixteenth untracked path is
`docs/NATIVE_KERNEL_MIGRATION_PLAN.md`, created immediately before P0 at the
user's request. It is not part of the earlier Gold/checkpoint/finalization
root-cause cluster.

### Exact pre-P0 fingerprints

| Fingerprint | SHA256 |
|---|---|
| All 53 dirty path contents, sorted path/hash manifest | `fa55aef96502b7528d55e82c083578092fa042df7520edda3710d78bb638868a` |
| Original 52 dirty path contents, excluding the migration plan | `289bc34441d28b6ab04f95d3e7cc9fab4996e767b8efab8f8dba1bde7d9b8669` |
| Tracked binary diff from HEAD | `b59f22ad6ff12517eb1c8c77f15670e34bce58286af09ae1f203dcae3fbefb5c` |
| Tracked changed-path list | `f3f17702dca1d24784069b6247e5a0c124eb88f070cefc839c25c8ce91b0dcaf` |
| Original untracked-path list, excluding migration plan | `15515e526a2ef343388c5bca8ad352e6c7b235f7fbca219cf97e6ef74a642067` |

The path-content manifest was computed from the sorted dirty path list and each
file's SHA256. P0 edits intentionally change the live working-tree digest; the
values above identify the preserved pre-P0 candidate and must not be recomputed
and presented as though they were the same snapshot.

## Runtime and dependency identity

| Item | Captured value |
|---|---|
| Python | `3.11.16` |
| Interpreter | `/media/desk16/iy1982/miniconda3/envs/labbioagent/bin/python` |
| Pantheon distribution | `pantheon-agents 0.6.4` |
| Pantheon import root | `/media/desk16/iy1982/WYC/projects/PantheonOS/pantheon` |
| Pantheon sibling HEAD | `07675c45b538f7d27b9b16b1b7d8b72f37365293` |
| Pantheon sibling worktree | clean |
| Pantheon constraint SHA256 | `4911c8525630dbe78a33e273c2e62d1bf736b8d3578c44858c4081201268459b` |
| Built-in `local-default.json` SHA256 | `1423644a3a204327f32ee2083f06d9e6bf8ba84223524a2cdb2a70ad8d6f102f` |
| Active provider/model | `NOT VERIFIED` — no external settings or credentials were loaded |
| Repository example transport/model | `openai-compatible` / `mimo-v2.6-pro`; example only, not accepted as active |
| Approved execution image | `NOT VERIFIED`; the example uses key `scientific-python` with an all-zero placeholder reference |
| Active runtime manifest/revision | `NOT GENERATED`; P0 created no run and composed no live runtime |

The built-in profile still declares version `local-v3` despite changed content;
the content hash above, not the label alone, is the exact identity for this
candidate.

The example model change is the unowned working-tree item noted below. Recording
its literal value does not approve it or establish the provider used by any
run. P0 deliberately did not read an external provider environment file. The
all-zero image reference is a template placeholder, not an available immutable
image identity. No runtime manifest was invented from those example values.

The legacy patch history contains nine commits between the upstream baseline
and the required fork revision. P0 verified that the full SHA for the
provider-turn progress commit is
`381146327a6b8e55f616ba2c174b118efc274625`; the former architecture document
contained an incorrect expanded SHA and an eight-patch count.

## Host preflight

- No production `LabBioAgentOS-production/current` tree was found at either
  checked WYC location.
- `docs/PRODUCTIONIZATION_DEBUG_GUIDE.md` was absent.
- Docker was queryable and reported no running container at capture.
- No LabBio API, LabBio worker, RQ worker, or Redis process was found by the
  bounded process-name check. An unrelated Uvicorn service outside this project
  existed and was not touched.
- Because there is no verified production pointer/service set, no production
  version, database, queue, worker health, or deployment claim is made.

## Pre-P0 dirty cluster inventory

The candidate contains several related but distinct root-cause clusters.

### Gold approval and cross-stage handoff

Proposal, gate, authorization, actual Skill access, original guidance, and
section paging are bound explicitly. Main paths include `runtime/assembly.py`,
`runtime/contracts.py`, `runtime/skill_grounding.py`, `runtime/tooling.py`,
`runtime/gold_handoff.py`, `skills/curator.py`, the built-in profile, and the
Gold handoff/retrieval tests.

### Execution resource feedback

Preflight request, canonical submission defaults, host policy ceilings, actual
receipt resources, and bounded rejection feedback are kept distinct. The
framework does not infer OOM or automatically increase resources.

### Explicit execution revision and checkpoint reuse

The Agent may submit exact source edits and explicitly replace inputs/parameters
using same-live-run registered outputs. Failed processes still collect declared
outputs without becoming successful. Reviews may cite only real feedback and
approved/read Gold context.

### Report reference feedback

`report_check` returns complete bounded citation/coverage problems without
requiring report prose. `report_submit` rechecks the same contract.

### Finalization, handoff, and host-owned result identity

The model returns a proposal; the host creates/validates result identity.
Finalization supports a JSON-object wire mode, one bounded schema correction,
longer capability handoff, and exact duplicate safe-result references without
replaying tools.

### Repeated non-retryable query convergence

Current authorization and policy are rechecked first. Only a repeated unchanged
non-retryable request after delivered feedback stops the next provider turn;
both failures remain visible to finalization and no success is synthesized.

### Items not silently assigned to those clusters

- `examples/local-runtime.toml` changes the example model from
  `mimo-v2.5-pro` to `mimo-v2.6-pro`; no current root-cause note owns that
  choice. P0 preserves it but does not approve, revert, or attribute it.
- `docs/ENGINEERING_DEEP_DIVE.md` is an untracked pre-Native-decision source
  walkthrough.
- `docs/NATIVE_KERNEL_MIGRATION_PLAN.md` belongs to the new migration.

## Baseline defect disposition and remaining limitations

1. **Resolved in P0:** `runtime/tooling.py` told an
   `ExecutionRevisionError` caller that a different input selection required
   `execution_submit`, while the same candidate intentionally added explicit
   `execution_revise.input_artifact_ids` replacement. P0 changed only that safe
   feedback to state the actual replacement/clearing semantics and added a
   regression at the real `ExecutionRevisionError` path. The first assertion
   was deliberately placed on a hash-mismatch case, failed because that case
   has a different safe error, and was then moved to the correct focused path;
   that failed attempt was not hidden.
2. The example provider-model update is unowned and remains preserved rather
   than silently accepted or reverted.
3. New behavior still relies on Pantheon callbacks/private transport semantics;
   Native Kernel removal cannot begin until their behavior is captured by
   backend-neutral contracts.
4. The checkpoint registry remains process-local. Registered files may persist,
   but automatic post-restart checkpoint discovery is not claimed.
5. The tree contains no immutable commit/release for these changes.
6. Full deterministic tests do not establish real provider, real Docker/data,
   scientific, final-report, production, or recovery acceptance.

## Test evidence

Pre-P0 verification used the project interpreter and did not change the source
fingerprints.

| Scope | Result |
|---|---|
| Ten new test files | 60 passed |
| All 27 changed/new test files | 345 passed |
| 2026-09-21 query/checkpoint pair | 20 passed |
| Full deterministic suite | 1553 passed, 34 skipped, 1 existing Uvicorn warning |

Fresh full-suite command run by the primary P0 session:

```bash
/media/desk16/iy1982/miniconda3/envs/labbioagent/bin/python -m pytest -q
```

Result: `1553 passed, 34 skipped, 1 warning in 36.83s`.

The skipped/live tests were not treated as passes for provider, Docker/data,
science, reporting, deployment, or production health.

## Backend-neutral legacy behavior contracts

P0 added a neutral `KernelContractBackend` protocol and DTOs under
`tests/kernel_contracts/contracts.py`. Contract test modules depend on that
interface rather than importing Pantheon. Current legacy execution is isolated
in `tests/kernel_contracts/backends/pantheon_legacy.py`; a future native adapter
must satisfy the same observable outcomes without copying its private seams.

| Legacy patch behavior | Backend-neutral contract modules |
|---|---|
| Reasoning-only idle convergence (`ba7f0e4b`) | Shared intent: explicit typed no-progress tests in `test_turn_progress_contract.py`; the exact hidden three-idle-turn silent stop is isolated in `backends/test_pantheon_legacy_idle_characterization.py` and is not native parity |
| Primitive schema constraints (`45ef598f`) | `test_tool_schema_contract.py` |
| Nested schema fidelity (`02ba577a`) | `test_tool_schema_contract.py`, `test_foreground_dispatch_contract.py` |
| Canonical parsed argument replay (`0ebbb8ba`) | `test_tool_history_contract.py` |
| Reasoning-only replay omission (`e2db6289`) | `test_reasoning_redaction_contract.py` |
| Provider-turn observation and parameter reuse (`38114632`) | `test_turn_progress_contract.py`, `test_structured_observation_contract.py`, `test_thinking_transport_contract.py` |
| Compatible thinking transport (`93ec465c`) | `test_thinking_transport_contract.py` |
| Strict governed tool-request integrity (`7b02bcba`) | `test_strict_tool_call_contract.py` |
| Private reasoning, strict wire schemas, foreground dispatch, and finalization observation (`07675c45`) | `test_private_reasoning_contract.py`, `test_foreground_dispatch_contract.py`, `test_structured_observation_contract.py` |

These are characterization contracts for the retained behavior boundary, not
P1 native implementation. Their success does not show that a native provider,
Agent loop, tool registry, team runtime, or finalizer exists.

The legacy three-turn idle streak is intentionally not a shared Native Kernel
acceptance rule. Native P1 must use explicit provider-turn and/or wall-clock
budgets with observable typed termination; it must not reproduce Pantheon's
hidden consecutive-idle allowance or silent normal return.

## P0 closeout evidence

| Check | Final P0 result |
|---|---|
| Branch / HEAD | `fix/gold-approval-handoff` / `9608bc1887f97683f14c1a39ac7068352a4958e7` (unchanged) |
| Closeout working tree | 47 tracked modifications, 33 untracked paths, 80 dirty paths including preserved pre-P0 work and the P0 overlay |
| Shared neutral contracts | 35 collected behavior cases |
| Legacy-only characterization | 1 hidden-idle-streak case, excluded from native parity |
| Contracts plus focused feedback regression | 37 passed, 1 existing Uvicorn dependency warning |
| Full deterministic suite | 1589 passed, 34 skipped, 1 existing Uvicorn dependency warning in 37.22 s |
| Text patch integrity | `git diff --check` PASS |
| Active runtime/provider/image | no runtime composed; provider/model and approved image `NOT VERIFIED` |
| Active runtime manifest | `NOT GENERATED` |
| Live/deployment state | no provider/data/Docker live, release, deployment, activation, or legacy-run mutation |

Closeout found no running container and no LabBio API, worker, RQ worker, or
Redis process. It did find two pre-existing orphaned `git push` process trees,
both started on 2026-09-12 for branch
`fix/gold-evidence-crosscheck-20260911`. They were not launched by P0, are not
evidence of a P0 push, and were left untouched; their remote outcome was not
inferred.

Final commands used the project Python 3.11.16 interpreter with bytecode and
pytest cache writes disabled:

```bash
PYTHONDONTWRITEBYTECODE=1 \
/media/desk16/iy1982/miniconda3/envs/labbioagent/bin/python -B \
-m pytest -q -p no:cacheprovider \
tests/kernel_contracts \
tests/test_checkpoint_revision_inputs.py::test_execution_review_requires_real_feedback_and_remains_model_context
```

```bash
PYTHONDONTWRITEBYTECODE=1 \
/media/desk16/iy1982/miniconda3/envs/labbioagent/bin/python -B \
-m pytest -q -p no:cacheprovider
```

## Freeze classification

This snapshot is a reproducible **pre-P0 dirty candidate**, not an immutable
release baseline. P0 may preserve, document, characterize, and run deterministic
tests against it. P0 does not grant permission to clean, reset, rebase, commit,
push, deploy, or overwrite the existing changes.

After P0 closeout this snapshot is the exact pre-migration **behavior
candidate** for P1 review: the focused defect is corrected, architecture
authority is consistent, and all nine legacy groups have neutral contract
coverage. It is still not an immutable commit/release baseline. The unowned
example-model change, mixed dirty tree, absent production identity, and lack of
immutable Git identity remain explicitly reported rather than approved.

P1 may begin only after user review and explicit authorization. P0 did not
switch the backend, remove a dependency, resume a legacy run, perform live
acceptance, or deploy.
