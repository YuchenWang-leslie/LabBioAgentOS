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
failed attempt had already produced output. Each response is capped, after
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

Archived, rereadable old tool batches can leave the active model context when
the observed remaining token budget is below the previous request's input
tokens, even before the request-byte cap is reached. This is a risk signal,
not an exact next-request token estimate or guaranteed budget admission;
unarchived evidence and the newest batch remain visible. A model can still
exhaust its phase budget before preparing a sufficient evidence handoff.

Schema-valid output can still be scientifically or semantically poor. Host
validation enforces authority, structure, lineage, and release policy—not
scientific usefulness.

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
