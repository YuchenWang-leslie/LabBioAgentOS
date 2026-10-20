# Known Limitations

## Research status

LabBioAgentOS is a research-stage platform. Workflow completion proves that the
governed lifecycle closed; it does not establish that a scientific method,
result, interpretation, or report is correct. Independent review remains
required.

Current real-task evidence includes annotated-H5AD and GOLDDEMO stress runs,
with both failed attempts and Agent-authored deliveries retained. Engineering
delivery is not scientific approval; same-version repeatability and independent
scientific acceptance are still outstanding.

## Provider behavior

The current adapter supports the required OpenAI-compatible Chat transport, not
every provider dialect. Exact endpoint, model, thinking mode, tool-schema mode,
response mode, deadline, and output limits are frozen in trusted configuration.
Unsupported behavior fails explicitly rather than switching transport.

Provider latency can be high and variable. Structured response truncation has
one bounded correction path using the same frozen evidence; it does not repair
JSON locally or replay tools. Missing token usage remains unknown.

Schema-valid output can still be scientifically or semantically poor. Host
validation enforces authority, structure, lineage, and release policy—not
scientific usefulness.

## Data exposure

Raw matrices and unrestricted files remain local, but explicitly enabled
bounded previews and derived results can contain biological values or
identifiers. Secret/path redaction is best effort and is not a general privacy
or de-identification guarantee. Deployment owners must review provider and data
use policy before enabling remote visibility.

## Local execution and storage

Docker limits CPU, memory, PIDs, time, network policy, read-only inputs/root,
and declared output collection. The writable workspace does not have an
independent filesystem quota. An untrusted program can create many undeclared
bounded files until cleanup. Full storage isolation requires filesystem- or
daemon-level quotas.

Each captured stdout/stderr stream is limited to 16 MiB in control-plane memory.
An overflow stops execution and retains explicitly incomplete prefixes; it
does not truncate declared scientific output files. Scheduler log files remain
on disk and are only read in bounded portions by the control plane. Docker
environment-build logs use a 16 MiB polling threshold, not a hard disk quota.
An interrupted Docker build/probe may continue in the daemon after its client
is killed; such attempts are marked `UNCONFIRMED` and require operator
reconciliation rather than automatic replay.

The platform trusts the local Docker engine, host kernel, configured images,
and local administrator. It does not defend against their compromise.

The source Scheduler + Apptainer backend has not been validated on a real
cluster with the Native Kernel or deployed. In a narrow crash window
between scheduler acceptance and persistence of the returned job ID, orphan
reconciliation records `UNCONFIRMED` and requires operator inspection; it
cannot guarantee automatic cancellation. See
[C13 integration review](C13_APPTAINER_EXECUTION_ACCEPTANCE.md).
Local Apptainer remains unvalidated on this host because its executable and
CPU-controller delegation are unavailable; resource limits were not bypassed.

## Persistence and concurrency

SQLite state, JSONL trace, Artifact storage, Docker operations, and provider
calls are not one distributed transaction. Uncertain external side effects
block recovery instead of being silently replayed. There is no high-availability
writer coordination or automatic multi-host reconciliation. Environment builds
serialize the same request across local processes and retain intent/terminal
receipts, but these files do not provide power-loss fsync guarantees or prove
that an interrupted Docker daemon operation has ended.

## Gold and Memory

Gold Skills and persistent Memory are governed model context, not scientific
evidence. Retrieval is bounded and deterministic rather than semantic/vector
search. Agent proposals still require appropriate user review and approval;
the service does not self-grade scientific quality.

## Release status

The deterministic suite is green and immutable P8 release
`native-p8-20261016T085718Z` passed clean-install, exact source/package comparison, manifest,
rollback, and active-current verification. Production points to that release.
The active-current task is a bounded runtime validation, not independent
scientific review or evidence of general repeatability. No profile or Skill
was activated or promoted with the release.
Fresh bioinformatics validation on this release remains outstanding. Failed
candidate runs and inactive uncertain source checkpoints are retained as audit
evidence; they are not resumable production work or active leases.
