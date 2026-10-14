# Known Limitations

## Research status

LabBioAgentOS is a research-stage platform. Workflow completion proves that the
governed lifecycle closed; it does not establish that a scientific method,
result, interpretation, or report is correct. Independent review remains
required.

Current real-task evidence covers one bounded PBMC3k descriptive task. It is not
a repeatability study or validation across data types, methods, providers, and
failure modes.

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

The platform trusts the local Docker engine, host kernel, configured images,
and local administrator. It does not defend against their compromise.

## Persistence and concurrency

SQLite state, JSONL trace, Artifact storage, Docker operations, and provider
calls are not one distributed transaction. Uncertain external side effects
block recovery instead of being silently replayed. There is no high-availability
writer coordination or automatic multi-host reconciliation.

## Gold and Memory

Gold Skills and persistent Memory are governed model context, not scientific
evidence. Retrieval is bounded and deterministic rather than semantic/vector
search. Agent proposals still require appropriate user review and approval;
the service does not self-grade scientific quality.

## Release status

The deterministic suite is green and immutable P8 release
`native-p8-20261014T025522Z` passed clean-install, packaged live, manifest,
rollback, and active-current verification. Production points to that release.
The active-current task is a bounded runtime validation, not independent
scientific review or evidence of general repeatability. No profile or Skill
was activated or promoted with the release.
