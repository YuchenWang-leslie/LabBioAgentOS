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

Cumulative phase token limits are checked against reported input and output
usage. A request can cross the remaining allowance before that usage is known;
its result is then rejected. This is not a prepaid provider quota. An explicit
supported `FINALIZE_ONLY` recovery is a separately audited model operation that
retains completed capabilities and the original limit, and can incur additional
provider usage. Unreported usage remains unknown.

Low-level HTTP progress callbacks may arrive after a successful turn and retain
the then-current phase instead of the original HTTP phase. A progress event
labelled `HTTP_PHASE_FAILED` with reason `COMPLETED` is not sufficient evidence
of a failed model request. The recorded event does not retain enough information
to reconstruct its original phase or exception. Match invocation and turn IDs
and use the provider turn/attempt terminal events for request accounting;
`scripts/runtime_stress_report.py` uses these terminal outcomes. This telemetry
limitation does not change model acceptance or scientific execution status.

A deployment selects exactly one provider API format in trusted configuration
(`[provider] api_format`):

- OpenAI Chat Completions (`openai`), in a vendor dialect for DeepSeek- and
  MiMo-style services or in the official dialect;
- the OpenAI Responses API;
- the Anthropic Messages API;
- the Gemini `generateContent` API;
- the Gemini Interactions API.

Exact endpoint, model, thinking settings, tool-schema mode, response mode,
deadline, retry bound and output limits are frozen in trusted configuration.
Nothing switches format, model or provider automatically, and unsupported
behavior fails explicitly.

Every format is used statelessly. Each request carries the full history, and
nothing is kept on the provider side (`store: false` where the API offers
it). Provider-hosted tools, such as web search, code execution, file search
or MCP servers, are never offered, because they would act outside the
governed execution plane.

Parsing is strict:

- An unknown content type fails the turn with a code that names the type.
- Unknown event types and metadata are ignored.
- An unknown finish reason is recorded as `OTHER`.
- A provider request id that does not have LabBio's identifier form is
  recorded as unknown; the id only links a turn to the provider's records.

A provider that adds a new kind of content therefore stops working until
LabBio supports it, instead of silently losing that content.

Format-specific limits:

- **OpenAI Chat Completions.** The official service returns no reasoning, so
  reasoning does not continue across tool calls; the Responses API does
  continue it.
- **OpenAI Responses.** A reasoning model needs both reasoning efforts
  configured. Without them its reasoning cannot be replayed statelessly.
- **Schema modes.** The official OpenAI and Anthropic strict modes cannot
  represent LabBio's stage schemas, because of optional fields, unions and
  maps; Anthropic also caps optional fields and unions. Gemini accepts small
  schemas but rejects a full stage schema as too complex for constrained
  decoding. DeepSeek's OpenAI-compatible endpoint rejects schema mode, and
  its Anthropic-compatible endpoint accepts the schema but does not apply
  it. With these providers use `provider_tool_schema_strict = false`
  and `finalization_json_object = true`. Local validation stays
  authoritative in every mode.
- **Anthropic thinking.** How a model stops thinking differs by model, so
  `thinking_off` has no default.
- **Gemini.** Only Gemini 3 or later is supported, because earlier models may
  omit function-call IDs. The Interactions API is used on `v1beta`. Under
  `store: false` it assigns no interaction id, so its turns carry no
  provider request id, and its overload error code is not yet confirmed.
  The free tier allows only a few requests per model per day (20 when
  checked), too few for a workflow run.
- **Vendor compatibility layers.** A vendor's Anthropic- or OpenAI-compatible
  endpoint may ignore fields such as thinking settings. Such an endpoint
  validates the wire format only, not the official service's behavior. On
  DeepSeek's Anthropic-compatible endpoint, `thinking_off = "disabled"`
  works, `between-tools` is rejected, and `omit` with a low effort still
  thinks. Kimi's `kimi-k3` stops thinking only with `reasoning_effort =
  "none"` on its OpenAI-compatible endpoint, a value its documentation does
  not list. On its Anthropic-compatible endpoint, `disabled` and
  `between-tools` both stop thinking.
- **Authentication.** Only API keys are supported. Cloud-platform
  authentication (Amazon Bedrock, Google Vertex AI, Claude Platform on AWS,
  Microsoft Foundry) is not, and consumer chat subscriptions are not API keys.

Rate limits, overloads, server errors and connection failures are retried a
bounded number of times: twice by default, at most ten. Each retry is
recorded in the run trace. A retried request can be billed again when the
failed attempt had already produced output. Request writes have a separate
60-second timeout, clamped to the remaining deadline; `NETWORK_WRITE_TIMEOUT`
uses the same retry bound. Model-response reads retain their original deadline;
read/connect/pool timeouts remain final. Failed-attempt usage stays unknown
unless supplied by the provider. This bounds a broken upload, not slow model
generation or the underlying network failure. Each response is capped, after
decompression, at 8 MiB plus 1 KiB per requested output token. A provider
failure that outlasts the retry bound fails the stage. The run stays in
flight and can be resumed only by an operator, with `reconcile` and
`continue`.

Live checks of the formats against real services are recorded in the
[C14 gap memo](C14_API_COMPAT_GAP_MEMO.md), criterion 20, and summarized in
[C14 acceptance](C14_API_COMPAT_ACCEPTANCE.md). A first live run of
the Gemini formats found three defects, which are fixed. After the fixes,
both Gemini formats passed the kernel-level checks and completed the full
workflow on Scheduler + Apptainer. With a lite model, no stage read the
output values. Both reports stated numbers that appear nowhere in the run,
and the host accepted them. With `gemini-3.6-flash`, the same workflow read
the result records, and both reports matched them. The host does not check
a report's numbers against its evidence; see criterion 20. DeepSeek's
OpenAI- and Anthropic-compatible endpoints passed the kernel-level checks in
JSON-object mode, but no full DeepSeek workflow was run. Kimi's two
compatible endpoints passed the kernel-level checks. Its one workflow
submitted a correct report and then stopped, when the provider returned
HTTP 502 to the REPORT finalization. The OpenAI
Responses API, the official Chat dialect and the official Anthropic service
are validated by deterministic contract tests only.

Provider latency can be high and variable. Structured response truncation has
one bounded correction path using the same frozen evidence; it does not repair
JSON locally or replay tools. Missing token usage remains unknown.

Native tool calls have a hard limit of 64,000 Unicode characters for the complete
serialized argument JSON, including escaping and combined fields; backend
source-field maxima do not override this channel limit. OpenAI-compatible Chat
can reject a complete over-capacity response before any dispatch and offer one
Agent-chosen correction within the original budgets. Safe diagnostics identify
only the field, call index and measured/allowed lengths. This correction is not
implemented or validated for the other provider wire formats, which retain their
existing fail-closed behavior; malformed/unsupported protocol is not retried.

Archived, rereadable old tool batches can leave the active model context when
the measured public/provider-wire request exceeds the request-byte cap;
unarchived evidence and the newest batch remain visible. The forecast based on
observed input tokens and all remaining turn slots is audit telemetry only,
not an exact next-request estimate or guaranteed budget admission. Keeping a
fitting working context can still consume the actual phase budget before the
Agent prepares a sufficient evidence handoff.

Schema-valid output can still be scientifically or semantically poor. Host
validation enforces authority, structure, lineage, and release policy—not
scientific usefulness.

A structured response with one field the stage schema does not allow fails
local validation (`extra_forbidden`), and the run fails at that stage; there
is no correction turn for it. This was seen once with `gemini-3.6-flash` in
JSON-object mode during the C15 cluster validation and is recorded for a
separate decision.

## Data exposure

Raw matrices and unrestricted files remain local, but explicitly enabled
bounded previews and derived results can contain biological values or
identifiers. Secret/path redaction is best effort and is not a general privacy
or de-identification guarantee. Deployment owners must review provider and data
use policy before enabling remote visibility.

Choosing a provider format and endpoint chooses who receives model input:

- **Relay gateways.** A relay gateway sees every prompt, derived result and
  the API key. Use official endpoints or a gateway your institution approves.
- **Provider terms.** Retention and training terms differ between providers
  and tiers; some free tiers allow the provider to use submitted content.
  Check them before sending unpublished or sensitive data.
- **Run identity.** `RUNTIME.json` records the endpoint origin (scheme, host
  and port), so a shared run directory reveals an internal gateway's
  hostname.

Provider keys are read from a private env file only when a command needs
them. The file must belong to the user and be mode 0600. The key is held in
memory and never written to the process environment, so child processes,
scheduler commands and containers do not inherit it. Two cases remain
outside LabBio's control:

- A key exported in a shell startup file can still reach a job shell.
- Mode 0600 does not stop a container that runs as the same user from
  reading the env file if it lies under a mounted path. Keep it outside input
  roots, result roots and workspaces.

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
is killed. An environment build attempt holds a lease for as long as it runs;
an attempt whose lease is free and that has no final receipt (none, or
`UNCONFIRMED`) is cleaned up before the next environment build, by
`image-reconcile` and after a run's `reconcile`, and marked `ABANDONED`. It
is never rebuilt automatically. On a cluster, an attempt is cleaned up only
once its run's ledger has no open build job; an attempt started before this
rule, or whose run directory and ledger were removed, is kept, since its jobs
cannot be confirmed stopped.

The platform trusts the local Docker engine, host kernel, configured and
admitted images, and local administrator. It does not defend against their
compromise; admission checks an image's source, architecture, entry points
and inventory, not its honesty.

The Scheduler + Apptainer backend has been validated on one Slurm test
cluster (C14 full workflows; C15 executions, admissions, environment builds,
image lifecycle and GPUs), not on PBS or LSF, and is not deployed. In a narrow
crash window
between scheduler acceptance and persistence of the returned job ID, orphan
reconciliation records `UNCONFIRMED` and requires operator inspection; it
cannot guarantee automatic cancellation. See
[C13 integration review](C13_APPTAINER_EXECUTION_ACCEPTANCE.md).
The earlier quota-mode Local Apptainer probe was blocked by missing runtime
and CPU-controller prerequisites; retain that historical refusal. The current
primary `systemd-affinity` mode instead requires enforced integer-core affinity
and delegated memory/pids, without CPU-controller delegation. See
[Local Apptainer](LOCAL_APPTAINER.md) for the separate current host checks and
evidence boundary; these checks do not establish scientific or release acceptance.

## Lab area and images

- If an Apptainer admission process dies after its SIF is registered but
  before its record is written, the SIF stays in the member's or the lab's
  SIF store without a record. Crash cleanup stops the admission's jobs and
  deletes its attempt, but files in a SIF store are never deleted
  automatically (DECIDED-C15-H).

- Members may write the lab area's `requests/` drop directory, so a member can
  fill the shared filesystem with request payloads. LabBio limits each member
  to a few pending requests and checks sizes when an administrator approves,
  but only a site disk quota bounds the writes themselves.
- An admitted image supplies its own interpreter, so a malicious image can
  falsify results; container isolation cannot prevent that. Personal images
  serve only their owner, lab images need a LAB_ADMIN, and trace records which
  image and source ran.
- Image admission pulls on the host where the command runs, which on a cluster
  is the login node; only the Apptainer conversion runs in a job.
- Apptainer's `/tmp` lives in the execution's scratch directory and is
  checked every few seconds, not enforced as a quota: a program can exceed
  `tmpfs_size_mb` between two checks before it is stopped with
  `SCRATCH_LIMIT`.
- Conda environments are solved on the host where the command runs. The
  solver's repodata cache is not kept, so each conda build fetches the channel
  metadata again.
- Permissions a conda package carries itself are kept (for example 775 on a
  program); a SIF is read-only, so others can read and run it. Every other
  file in an image's conda environment and Python site-packages must be
  readable by others, or the build fails with `IMAGE_NOT_SHAREABLE`.
- Derived and lock-built images built on a cluster before 2026-11-18 may be
  readable only by the building account (the build then ran with umask 077).
  They are deleted and restored, which rebuilds them under the current rule.
- A live Agent has chosen existing non-default images by itself, but has
  never chosen a SHELL image: in the cluster validation, an equally valid
  Python environment with the same tool was listed. The SHELL path, including
  the refusal of a SHELL image without an explicit runtime, is covered by
  tests and by real executions with a named image.
- That another account can run a lab image rebuilt by the holding account was
  checked with two real OS accounts on Docker only. On the test cluster, with
  one account, only the file permissions inside the SIF were checked.
- An Apptainer conversion of the same OCI source is not byte-reproducible
  (D4): a restored or separately converted SIF can differ from the original.
  Identity is the source and the inventory, which are checked on every
  restore, not the SIF's own hash.
- Registry credentials are static (username and password or token). Tokens
  that expire, such as AWS ECR's, need the file refreshed by hand, and the
  Docker credential-helper protocol is not supported.
- A member's conda lock is checked by solving exactly its builds against the
  current channel metadata. A lock naming a file the channel no longer serves,
  or whose metadata changed, is refused with `CONDA_LOCK_MISMATCH`, even if
  the member still has the file.
- A lab image's references from members are seen only through the `usage/`
  markers. A member can forge a marker, which only blocks a deletion, or
  remove their own marker, which only puts their own runs at risk. Markers
  are brought in line with a member's live references when that member runs
  `image-references`, so a stale marker blocks deletion until then.
- On Docker, a lab image and a member's personal image of the same manifest
  are one Docker image. Deleting the lab image keeps the file when the lab
  image was promoted from a member's image, but not when a member admitted
  the same manifest separately; that member restores it with image-restore.
- A personal image admitted from a SIF file is deleted on its owner's own
  confirmation (`--confirm-no-upstream`); only a lab image's deletion needs an
  administrator.
- The reference index finds runs that existed before it only under the
  member's result roots; a run directory placed elsewhere with `--output`
  under another root is not found until it executes again.
- Environments the Agent built before C15 have no recorded size, so they do
  not count against the member's quota until they are rebuilt.

## GPUs

- On Docker, each execution is given the first N GPUs of the host's
  inventory. Docker has no scheduler, so concurrent runs on the same host can
  share a GPU and its memory.
- An image's GPU flag is the operator's or member's declaration. Admission
  checks that an image declared for GPUs contains a CUDA runtime library, but
  not that its CUDA version suits the hosts' drivers, and configured images
  are not checked at all.
- AMD ROCm and multi-node GPU jobs are not supported.
- Under a scheduler, only the scheduler confines a job to its allocated GPUs
  (for example Slurm `ConstrainDevices=yes`); `--nv` itself binds every GPU
  device node of the node. On the Slurm test cluster `--nv` worked together
  with LabBio's isolation options, `CUDA_VISIBLE_DEVICES` reached the
  container, and with `ConstrainDevices` only the allocated GPU could be used.
  The PBS and LSF GPU directives are tested only against fake scheduler
  output.
- Some Slurm sites refuse `--gpus` and require `--gres`; such a site needs a
  site template, and `backend-check` reports the refusal.
- Local Apptainer under systemd does not support GPUs; a configuration with
  `gpus > 0` is refused at load.

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
