# Native Runtime reliability: implementation and acceptance

This work independently implements the approved runtime contracts. It does not
copy an Agent framework, add scientific routing, choose analysis methods, repair
Agent-authored programs, or activate a production release.

## Source and comparison boundary

The frozen baseline is `d7b1000251ccc00656b30a96e37785dcec07e4a2`.
Implementation uses a separate `feat/native-runtime-reliability` worktree.
Existing configuration changes and `sandbox-docker` remain outside this work.
Source acceptance, Docker task delivery, Local Apptainer acceptance, real cluster
acceptance and production activation are separate outcomes.

The benchmark uses six task classes: autonomous annotated-H5AD analysis,
annotation uncertainty, a minimal environment requiring Agent-selected packages,
multi-file GOLDDEMO analysis, complete result delivery with scoped reporting, and
durable execution-checkpoint interruption/recovery. Each class has one baseline
and three candidate attempts, with no more than two active tasks.
The first candidate exposed a prompt-composition defect before analysis. It is
retained as a failed attempt on `324c66e`; the user authorized one extra attempt,
so the complete record contains six baselines and nineteen candidates, including
eighteen attempts on the next frozen revision. Failed history is not replaced.

`scripts/runtime_stress.py` submits ordinary tasks through the CLI. It contains no
analysis programs or method presets. Each attempt records input hashes, task,
preferences, configuration, runner hash, source identity and process result.
The application's `RUNTIME.json` binds the actual source hashes and profile, not
only the Git commit. Freeze candidate source before launching its comparison arm.
Do not overwrite a failed attempt, silently enlarge budgets, or count execution
replay as a fresh Agent success.

The configured comparison model is `mimo-v2.6-pro`, with output and phase limits
held fixed. Analysis execution uses 4 CPU / 4 GiB for H5AD and 4 CPU / 16 GiB for
GOLDDEMO. The historical Docker environment builder does not enforce a CPU quota:
these analysis limits must not be represented as a limit on all task processes.
Unknown provider token usage remains unknown; the old baseline did not expose
per-turn tokens in its trace.

## Contracts implemented

- Cancellation is checked after provider return and before each tool. Completed
  results remain, and calls never started have explicit reasons. A simultaneous
  usage overrun cannot hide an interruption; consumed usage remains recorded.
  Async child cancellation records a terminal delegation outcome and propagates.
- One repeated successful read no longer terminates an entire capability phase.
  It still consumes the explicit call/time/token budgets. Existing authorization
  failures and business pagination rules remain; no automatic retry is added.
- Actual provider usage is accounted separately from admission reservations.
  An overrun cannot erase already consumed tokens or time.
- Deployment prompts bind shared context, phase instructions and stage protocol
  as separate components, preserving their exact concatenated text. Each
  component retains its 8,000-character limit and the rendered prompt its
  16,000-character limit. Every configured phase is validated before credentials
  are loaded or state is created; a late-stage template error cannot silently
  leave an otherwise started workflow in flight.
- Finalization returns safe validation categories and field paths without
  echoing rejected values. Specialist termination preserves its specific reason.
- `provider.max_request_bytes` bounds the complete request, including tool and
  output schemas. UTF-8 bytes and token usage are separate measurements.
- Original public observations are archived outside the bounded model window.
  `evidence_read` reauthorizes and pages those observations; it does not rerun the
  original action. Provider-private reasoning is not part of this archive.
- Only feedback actually delivered to the model obtains read credit. Persisting
  a successful tool outcome, or merely loading old evidence, is insufficient.
  Repeated delegation to the same Specialist starts a new reading context:
  previous observations must be explicitly redelivered. Read receipts identify
  the actual model invocation while report production retains Workflow identity.
- `prepare_handoff` copies Agent-selected, already delivered observations with
  the Agent's own assessment and limitations. Oversized selections receive size
  feedback. The tool-free finalizer refuses oversized unprepared evidence rather
  than removing every result body and proceeding. The Agent's final assessment
  is delivered separately and losslessly, under the same total request bound.
- New report `evidence_references` bind artifact IDs to actual observation IDs.
  Host receipts retain source hashes and exact page/aggregate scope. Legacy
  `evidence_artifact_ids` retains its complete-reading requirement in the legacy
  API; new Native tool declarations expose only the scoped contract. Neither mode
  certifies scientific validity.
- Local Apptainer uses an explicit systemd/cgroup v2 boundary, without fallback
  to Docker or unbounded local execution. See [deployment and acceptance](LOCAL_APPTAINER.md).

Tests include 6,401 synthetic engineering records to separate complete artifact
retention from scoped reading. Synthetic fixtures are never scientific results.
Existing Workflow stage permissions and lifecycle gates remain outside the kernel.

## Reproducible evidence and recovery

Use the same runner and frozen parameters for both arms:

```bash
python scripts/runtime_stress.py \
  --source /absolute/frozen/source --root /absolute/evaluation/root \
  --config /absolute/private/config.toml \
  --arm baseline --case autonomous
python scripts/runtime_stress_report.py report --root /absolute/evaluation/root
```

The reporter reads all attempt directories, including failed and incomplete
attempts, validates selected delivery hashes and preserves unknown measurements.
It does not assign a scientific quality score.

The recovery case exits only after a durable EXECUTE capability checkpoint.
Resume requires its exact interruption receipt, frozen source/configuration/input
identities and a `FINALIZE_ONLY` recovery state. It holds the normal writer lease,
uses native reconciliation, and checks execution identities before and after:

```bash
python scripts/runtime_stress_report.py resume \
  --root /absolute/evaluation/root \
  --attempt baseline-recovery-1 --source /absolute/frozen/source
```

An uncertain submission is not permission to execute again. Recovery and ordinary
task delivery are evaluated separately.

## Acceptance status and remaining limitations

The corrected candidate source suite passes **1914 tests, 26 skipped**. Five opt-in
real Docker input-isolation and file-limit checks also pass. The skipped tests
are not counted as backend acceptance. No final benchmark acceptance is claimed
by this document. Current raw records are retained under
`result/native-runtime-reliability-20261020` in the parent workspace.
The corresponding records are `verification/full-regression-5.xml` and
`verification/docker-boundaries-3.xml`; earlier failing regression records remain.
The Docker checks used the first frozen candidate; the later correction changes
prompt assembly and diagnostic reporting, not the Docker execution boundary.

The first candidate's configured EXECUTE prompt contained 8,007 characters,
incorrectly packed into one variable with an 8,000-character limit. No analysis
executed. The repair preserves the text and existing limits by representing its
actual components separately and validating all phases at startup. The failed
run is not resumed or rewritten. Metrics distinguish its exited controller from
its persisted in-flight Workflow state and check residue without claiming a
Workflow terminal state.

Confirmed baseline observations include successful process exits rejected for
output record-limit violations, and Agent-driven Python correction attempts.
Those are failed execution/delivery attempts until the persisted receipts say
otherwise. Platform feedback can expose the actual count and permitted bound;
the platform must not select records, rewrite programs or raise limits for the Agent.

The autonomous H5AD baseline eventually produced a successful execution after
eight failed attempts, but its report phase stopped on a repeated successful
aggregate before submitting any report. The old failure code misleadingly said
`MALFORMED_RUNTIME_RESULT`; the finalizer had never started. Missing required
capability completion now has its own `REQUIRED_CAPABILITY_INCOMPLETE` code with
the preserved stop reason. This baseline remains failed; later success cannot
replace it in the comparison.

This host lacks Apptainer and CPU-controller delegation. Real Local Apptainer
execution/build/cancellation/replay and real cluster acceptance remain blocked.
No host administration or production switch is included in this source change.

Independent review also identified existing unbounded stdout/stderr collection
and incomplete cancellation/resource control during Docker environment builds.
These require a common runner lifecycle/output contract, not a systemd-only
silent truncation. Until repaired and verified, they remain explicit limitations.

The six baseline tasks support descriptive comparison only. Engineering
completion, valid artifact/report delivery, and independent scientific review
must be reported as distinct results.

## Design references

Mechanisms, not implementation code, informed the work:

- [Anthropic: separating execution from model context](https://www.anthropic.com/engineering/managed-agents)
- [Anthropic: evaluate real outcomes and repeatability](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)
- [ADK: function declaration contracts](https://adk.dev/tools-custom/function-tools/)
- [Apptainer: cgroup requirements and external systemd enforcement](https://apptainer.org/docs/user/latest/cgroups.html)
