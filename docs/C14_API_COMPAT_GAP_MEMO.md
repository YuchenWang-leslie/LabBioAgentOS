# C14 Provider API Format Compatibility Gap Memo

Date: 2026-10-21

Status: pre-implementation architecture audit. This memo records current
source behavior before any change. It is not an acceptance claim. C14 was
requested by MOHAN CUI on 2026-10-21; its decisions are recorded in
[API compatibility decisions](C14_API_COMPAT_DECISIONS.md) (DECIDED-C14-0 to
DECIDED-C14-10). The source audit below describes the starting revision.
Implementation proceeds one step at a time, each started on MOHAN CUI's
instruction, and the acceptance criteria are refined as the decisions are.

Starting LabBio revision: `d7b1000251ccc00656b30a96e37785dcec07e4a2`
(branch `feat/api-compat`, created from `main`).

## Target

A deployment owner selects one provider wire format in trusted configuration,
and every stage calls the model through that format:

- `openai`: OpenAI Chat Completions, including compatible vendors and
  self-hosted servers;
- `openai-responses`: the OpenAI Responses API;
- `anthropic`: the Anthropic Messages API;
- `gemini`: the Google Gemini `generateContent` API;
- `gemini-interactions`: the Google Gemini Interactions API, stateless only.

Each format must reach parity with the current OpenAI-compatible path: tool
calls including parallel calls, structured finalization, reasoning continuity
across tool turns, streaming, usage, bounded error classification, and the
model-phase deadline.

## Existing capability

- The kernel depends on one provider port: `ModelProvider` with `binding` and
  `generate(ModelRequest) -> ModelTurn` (`kernel/contracts.py:365`), plus an
  optional `clear_invocation` that the loop calls in `finally`
  (`kernel/loop.py:401`). A new format needs a new adapter, not loop changes.
- `ModelRequest` already carries the exact binding, bounded public messages,
  tools, an optional response schema, an output limit, and an absolute
  `deadline_monotonic` (`contracts.py:267-286`).
- Conversation history is append-only. The loop extends it only as
  `(*self.messages, new)` (`loop.py:280`, `:360`, `:372`, `:556`). This
  satisfies the Anthropic requirement that replayed thinking blocks keep an
  unchanged history prefix.
- A team shares one exact binding (`kernel/collaboration.py:78-80`), so a
  format choice cannot diverge between Leader and Specialists.
- The current transport (`kernel/openai_chat.py:54-191`) already rejects
  duplicate JSON keys and non-finite numbers, disables redirects
  (`:64`), applies `trust_env` only to the model HTTP client (`:65`), and maps
  failures to bounded categories without response bodies (`:82-99`).
- Private reasoning replay exists for one dialect: `reasoning_content` is kept
  in memory per invocation, bounded at 64,000 characters (`:367`), keyed by
  normalized tool calls (`:447`), and cleared by `clear_invocation` (`:241`).
- The CLI hides exception messages because they can contain provider bodies or
  credentials, and emits fixed, value-free details only for known error types
  (`cli.py:380-394`).
- `tests/kernel_contracts/` is a backend-neutral harness that can run one
  contract suite against more than one provider adapter.

## Source audit

### Q1 - where is the wire format coupled?

Five places:

1. `ProviderBinding.transport` is `Literal["OPENAI_COMPATIBLE_CHAT"]`
   (`contracts.py:210`).
2. `environment_native_provider` (`runtime/native_stage.py:85-106`) ignores
   `ModelProfile.transport` and always builds `OpenAICompatibleChatProvider`.
   `ProviderTransport.AUTO` has no effect in the Native Kernel
   (`runtime/profiles.py:36-40`).
3. Local configuration builds every `ModelProfile` with provider
   `openai-compatible` and `ProviderTransport.OPENAI_CHAT_COMPLETIONS`
   (`local_config.py:606-620`).
4. The adapter itself (`openai_chat.py:194` onward).
5. The runtime manifest hashes only `kernel/openai_chat.py` as
   `native_provider_sha256` (`local_config.py:395`).

### Q2 - which dialect does the current adapter speak?

A vendor dialect used by DeepSeek- and MiMo-style services: it sends
`thinking: {type: ...}` (`openai_chat.py:261`) and `max_tokens` (`:258`) and
reads `reasoning_content`. Whether official OpenAI reasoning models reject
`thinking` and require `max_completion_tokens` is unverified and must be
checked against the live API.

### Q3 - how are tool and response schemas sent?

As full Pydantic JSON Schemas (`kernel/tooling.py:184-187`), including
keywords such as `maxLength` and `minimum`. Provider-side constrained modes
accept only a subset. Local configuration defaults
`provider_tool_schema_strict = false` (`local_config.py:43`), and
`finalization_json_object` (`openai_chat.py:279-302`) already provides a
JSON-object finalization path for providers without schema support.

### Q4 - how are credentials loaded, and where do they travel?

- `_load_provider` (`local_config.py:415-426`) reads the env file, accepts
  `http` or `https` without userinfo, and writes the key and endpoint into the
  control-plane `os.environ` as `OPENAI_API_KEY` and `OPENAI_API_BASE`.
- `os.environ` is the late-binding channel. The application is built with
  `load_provider=False`, and `_load_provider` runs later, only when a model
  call is needed (`cli.py:259`, `:317`; `local_continuation.py:190`, `:225`,
  `:333`). The provider builder reads the environment at stage time
  (`local_config.py:706`).
- When `native_provider_builder` is not supplied, the application falls back to
  `environment_native_provider`, which reads the same variables
  (`application.py:815`, `native_stage.py:115`).
- No file-permission check is applied to the env file.
- Every child process inherits the key unless its caller filters the
  environment. The Docker runner does not pass `env=`
  (`execution/docker.py:52`), so the docker CLI and its helpers inherit the
  key. The container receives only explicit `NAME=value` flags
  (`docker.py:187-203`). Environment builds, Apptainer and scheduler
  submission use allowlists (`execution/environment_builder.py:86-90`,
  `execution/apptainer.py:303`, `execution/launchers/scheduler.py:43`).

### Q5 - what does the runtime identity record?

`runtime_manifest` (`local_config.py:347-412`) hashes the configuration, the
effective profile, all LabBio sources, the whole `kernel/` directory, and the
single adapter file. By design it does not read the env file
(`local_config.py:348`), so the endpoint is not part of the identity. A
revision mismatch blocks recovery and continuation
(`application.py:1268`, `:2028`). Any source change in this work changes the
revision, so runs started earlier cannot continue afterwards. This is existing
behavior and must be stated in upgrade notes, not worked around.

### Q6 - how are provider failures handled?

There is no retry. A transport or protocol failure raises a typed kernel error.
A capability invocation that already holds tool results stops with
`PROVIDER_TRANSPORT_STOP` or `PROVIDER_PROTOCOL_STOP`; otherwise the error
propagates (`loop.py:168-209`). One HTTP 429 or 529 therefore ends the model
call.

### Q7 - how is an untrusted response bounded?

JSON parsing is strict, and the public `ModelTurn` content is bounded. The
stream reassembly in `_stream_response` (`openai_chat.py:102-191`) has no
total byte cap, so an endpoint can keep sending data until the deadline.

### Q8 - how are configuration errors shown?

Unknown exceptions are reported as `LOCAL_COMMAND_FAILED` with only the
exception type (`cli.py:393`). New configuration checks that must tell the
operator how to fix them need their own fixed, value-free error codes, as
`RUN_WRITER_ACTIVE` and `GOLD_EXPORT_CONFLICT` already have.

### Q9 - which documents bound this work?

- `KNOWN_LIMITATIONS.md`, "Provider behavior": the adapter supports only the
  OpenAI-compatible Chat transport; unsupported behavior fails explicitly
  rather than switching transport.
- `AGENTS.md`: the Native Kernel owns the exact provider binding and must not
  contain hidden fallback.
- `REAL_E2E_CONVERGENCE_WORKPLAN.md`, 2026-10-19: `[provider].trust_env`
  applies only to the model HTTP client and also disables that client's host
  certificate environment. New formats must keep this meaning.

## Confirmed gaps

1. **One wire format.** The binding, builder, configuration and manifest admit
   only OpenAI-compatible Chat.
2. **One dialect.** Official OpenAI parameters are not expressible.
3. **No shared transport.** HTTP, SSE and error handling live inside the one
   adapter.
4. **No stream byte cap** (Q7).
5. **No transient-failure retry** (Q6).
6. **No schema projection** for provider-side constrained modes (Q3).
7. **Private continuation is dialect-specific.** It is keyed by tool calls only
   and stores only `reasoning_content`.
8. **Credentials travel through `os.environ`**, are inherited by child
   processes, and are read back from the environment by default (Q4).
9. **Plain http is accepted for remote endpoints, and the env file
   permissions are unchecked** (Q4).
10. **The endpoint is absent from the runtime identity** (Q5).
11. **No actionable error codes** for provider configuration problems (Q8).

## Required boundary

### Layering

```text
WorkflowEngine / AgentLoop / collaboration   unchanged; they see ModelProvider only
        ModelRequest -> ModelTurn
format adapters (kernel/)
  openai_chat.py          existing, dialect made configurable
  openai_responses.py     new
  anthropic_messages.py   new
  gemini_content.py       new
shared transport
  http_stream.py          new; extracted from openai_chat.py
```

### Shared transport

Owns POST, SSE framing, strict JSON, bounded error categories without bodies,
`trust_env`, no redirects, the per-response byte cap of DECIDED-C14-8, and the
bounded retry of DECIDED-C14-3. httpx strips only `Authorization` on cross-origin redirects,
not `x-api-key` or `x-goog-api-key`, so redirects stay disabled for every
format.

### Adapters

Each adapter builds the request, reassembles the stream, normalizes to
`ModelTurn`, keeps private continuation, and raises typed kernel errors. Each
adapter:

- always streams;
- fails on incomplete streams and on unknown content-bearing block or delta
  types, ignores unknown event types and metadata, maps unknown finish reasons
  to `OTHER` (DECIDED-C14-2), and never guesses a completion;
- never repairs tool-call JSON. `arguments_json` stays the raw string for
  the kernel's strict parser;
- keeps provider reasoning, signatures and encrypted reasoning in memory only,
  bounded and scoped to one invocation, and cleared by `clear_invocation`;
- maps provider error payloads to bounded categories and never records their
  messages.

### Data recipients

- Format and endpoint come only from trusted configuration. Task text and tool
  arguments cannot change them.
- No provider built-in or server tools are sent: web search or fetch, code
  execution, file search, URL context, MCP connectors, server-side fallbacks.
  They would act outside the governed execution plane.
- No server-side conversation state: `store: false` on the Responses API, no
  `previous_response_id`, no `cachedContent`. Every request carries the full
  public history. Anthropic prompt caching stays off unless decided later.

### Runtime intelligence sovereignty

This is transport infrastructure only. Adapters do not guess parameters from
model names. Every dialect choice is explicit configuration. Nothing selects a
model, format, provider or scientific method on the Agent's behalf, and nothing
switches automatically after a failure.

## Non-goals

- Changes to WorkflowEngine state, AgentLoop control flow, or collaboration
  policy, beyond recording retry attempts.
- Mixing formats within one runtime, or automatic format, model or provider
  fallback.
- Official vendor SDKs (DECIDED-C14-2).
- Cloud-platform authentication: AWS SigV4 for Amazon Bedrock and Claude
  Platform on AWS, and Google Cloud credentials for Vertex AI.
- Provider built-in tools, server-side state, prompt caching, and batch APIs.
- Model-specific parameter inference.
- Scientific behavior of any kind.

## Acceptance criteria

Step 1 - shared transport

1. `kernel/http_stream.py` owns POST, SSE framing, strict JSON, bounded error
   categories, `trust_env` and disabled redirects. The OpenAI Chat adapter uses
   it, and all existing provider tests pass without assertion changes.
2. A per-response byte cap of `8 MiB + 1 KiB x max_output_tokens`
   (DECIDED-C14-8) applies to the decoded bytes of streamed and non-streamed
   bodies. Exceeding it is a transport error, and no partial turn is returned.
   Requests accept only gzip, which the transport inflates in steps bounded by
   the remaining cap; a test shows that a 64 MiB gzip bomb never raises memory
   use far above a 1 MiB cap.
3. Any 3xx response is a transport error. Tests prove that no request reaches
   the redirect target for each credential header form.
4. Retry follows DECIDED-C14-3. It applies only to HTTP 429, 529 and 5xx,
   connection failures, and provider stream error events mapped to overload or
   rate limiting. It honors `retry-after`, caps attempts, and never waits past
   `deadline_monotonic`. It resends the identical request to the identical
   endpoint and model. Other statuses (400, 401, 403, 404 and similar) fail
   at once. Until criterion 8 holds, the default is zero retries. Each
   attempt has one total limit that ends 0.5 seconds before the deadline, so
   a slow stream cannot outlast it.

Step 2 - neutral contract

5. `ProviderBinding.transport` admits exactly `OPENAI_COMPATIBLE_CHAT`,
   `OPENAI_RESPONSES`, `ANTHROPIC_MESSAGES`, `GEMINI_GENERATE_CONTENT` and
   `GEMINI_INTERACTIONS`; the last was added with its adapter in step 6b.
   A per-format dialect sub-model must match its transport. The team-wide
   single-binding check covers it.
6. A generic private-continuation store is keyed by kernel run, invocation,
   and a normalized fingerprint of the public assistant message. It is
   size-bounded, memory-only, and cleared by `clear_invocation`. The OpenAI Chat
   replay uses it, and its behavior is unchanged.
7. Per-format schema projection removes unsupported keywords for
   provider-side constraints. Local Pydantic validation stays authoritative:
   a test shows a value that the projected schema accepts and local validation
   rejects, and local rejection wins.
8. Retry attempts (category and wait) are returned with the turn or the
   final error. The loop records each one as a public kernel event, and the
   stage projects these events into the run trace. `model-boundaries.jsonl`
   holds no per-turn provider facts, so retries are not written there.
   Retries are also recorded when the request deadline ends the final
   attempt. Retry records never enter `PublicMessage`. With recording in
   place, the default becomes two retries (DECIDED-C14-3).

Step 3 - OpenAI Chat dialects

9. The existing dialect stays the default, and its request snapshots are
   unchanged. The official dialect sends `reasoning_effort`,
   `max_completion_tokens` and `stream_options.include_usage`. Neither dialect
   sends `store: true` or built-in tools. The reasoning effort is configured
   for thinking on and for thinking off, because omitting it would leave a
   reasoning model at its own default effort. A non-empty `refusal` becomes
   the `CONTENT_FILTER` finish reason, and its text is not kept
   (DECIDED-C14-9).

Step 4 - Anthropic Messages adapter

10. Request snapshots cover:
    - the top-level `system`;
    - `tool_result` blocks grouped into one user message;
    - strict tools and `output_config.format`;
    - adaptive thinking with optional effort;
    - the configured thinking-off form: `disabled`, `between_tools`, or
      omitted with a stated effort, because some models cannot stop thinking.

    Stream reassembly handles text, thinking, signature and tool-input deltas.
    Each assistant turn is replayed exactly as it was received, including
    `thinking` and `redacted_thinking` blocks and the model's own tool-input key
    order, so the history sent to the provider only grows. A refusal keeps its
    declared category (DECIDED-C14-9).
    Unknown block and delta types fail with `unsupported_type`, which names
    them; unknown event types are ignored, and unknown stop reasons become
    `OTHER` (DECIDED-C14-2). A failed tool result carries `is_error: true`
    (DECIDED-C14-10). The key appears only in `x-api-key`.

Step 5 - Gemini adapter

11. Requests use `streamGenerateContent?alt=sse`, with the key only in
    `x-goog-api-key` and never in the URL. All function calls of a turn precede
    their responses. Thought signatures are replayed in their original
    positions, including a signature delivered on a final empty part.
    `finishReason` and `promptFeedback.blockReason` map to `FinishReason`; a
    filter reason becomes `CONTENT_FILTER` with the reason as category
    (DECIDED-C14-9). No `candidateCount`, `cachedContent`, `store` or built-in
    tools are sent. Model parts are replayed with their original boundaries,
    and a failed result is sent under the `error` key. Only Gemini 3 or later
    is supported, since earlier models may omit function-call IDs. Field
    names, the call-ID rule and defaults were checked against the official
    reference on 2026-10-23; the ID format and stream error shape are
    confirmed in step 9.

Step 6 - OpenAI Responses adapter

12. Every request sends `store: false` and never `previous_response_id`,
    `conversation`, `background`, `truncation` or built-in tools, and resends
    the full history. A leading system message becomes `instructions`; later
    system messages stay in place as input items. The reasoning effort is
    configured for thinking on and for thinking off, or for neither; when it
    is configured, `include: ["reasoning.encrypted_content"]` is sent as
    well. Each assistant turn's output items (reasoning, message and
    function-call items) are replayed exactly as received, ids included, from
    the in-memory store. Function tools always state `strict` explicitly,
    because the Responses API otherwise treats them as strict. Structured
    output uses `text.format` with the full schema, or `json_object` with the
    schema in `instructions`. Only the full response of the terminal event
    (`response.completed`, `response.incomplete` or `response.failed`) builds
    a turn, and its status must match the event. `max_output_tokens` maps to
    `LENGTH`, `content_filter` and any `refusal` part to `CONTENT_FILTER`
    without a category, and other incomplete reasons to `OTHER`. Only
    `rate_limit_exceeded` in a stream error or failed response is retried.
    Unknown output item and content part types fail with `unsupported_type`,
    which names them. The key appears only in `Authorization`. Event names,
    status values and fields were checked against the official reference on
    2026-10-23. Step 9 confirms that item ids are accepted under
    `store: false`. A reasoning model configured without an effort returns
    reasoning items without encrypted content, which the service reportedly
    cannot resolve under `store: false`, so step 8 documents that reasoning
    models need a configured effort.

Step 6b - Gemini Interactions adapter

12b. Every request goes to `{endpoint}/interactions` with the key only in
     `x-goog-api-key`. It sends `store: false` and `stream: true`. It never
     sends `previous_interaction_id`, `background`, an agent, an
     environment, `service_tier`, a webhook, `safety_settings`, labels or
     built-in tools, and it resends the full history as steps. Leading
     system messages become `system_instruction`; a later system message is
     refused, because the API has no system step. Both thinking levels are
     configured. Strict tools use `tool_choice: validated`. Tools and
     `response_format` carry the full schema, or JSON-object mode puts the
     schema in `system_instruction`. Because the completed event carries no
     steps, the stream is reassembled from `step.start`, `step.delta` and
     `step.stop`: text, thought signatures and argument fragments.
     Thought, output and function-call steps are replayed exactly as
     reassembled. Function results reference `call_id` and carry
     `is_error` for a failed result (DECIDED-C14-10). The statuses map as
     follows:
     - `requires_action` maps to `TOOL_CALLS`;
     - `completed` maps to `STOP`, or to `TOOL_CALLS` when it has calls;
     - `incomplete` maps to `LENGTH`, since the documented case is the output
       limit (MOHAN CUI, 2026-10-23);
     - other statuses map to `OTHER`.
     A safety error code (`safety`, `jailbreak`, `prohibited_content`,
     `blocklist`) becomes `CONTENT_FILTER` with the code as category
     (DECIDED-C14-9). `unavailable` and `resource_exhausted` are retried.
     Unknown step and delta types fail with `unsupported_type`, which names
     them, and unknown events are ignored. Field names, step types, event
     names and statuses were checked against the official reference on
     2026-10-23 (v1beta; DECIDED-C14-1). Step 9 confirms:
     - the overload and rate-limit code strings;
     - whether a blocked input arrives as HTTP 400 or a stream error;
     - whether interaction ids fit the kernel identifier format;
     - that the replayed steps are accepted.

Step 7 - contract parity

13. The five adapters pass one contract suite through the real AgentLoop with
    a fake transport, covering:
    - a tool round trip and parallel calls;
    - reasoning continuity;
    - structured finalization in both modes;
    - finish reasons;
    - usage, where unknown values stay unknown;
    - error categories, retry and the byte cap;
    - the absence of private reasoning from events, trace and results.

    `tests/provider_contracts/` runs every scenario against six wire variants:
    Chat in its vendor and official dialects, Responses, Messages,
    generateContent and Interactions. Each variant uses the real adapter and
    the shared transport, and only the network is replaced. The variants
    encode one neutral script in each documented stream form. The format
    differences are declared and asserted, not skipped:
    - official Chat returns no reasoning to replay;
    - Chat has no retryable in-stream error;
    - the content-filter category is declared by Messages, generateContent
      and Interactions only;
    - a Chat `content_filter` finish keeps the turn's output, whereas only a
      refusal drops it.
    One application-level test per variant runs the full workflow and checks
    that the run trace and every written file hold no private continuation
    or key. A Chat chunk that carries only an error now fails as
    `STREAM_ERROR` without retry, matching the other formats (MOHAN CUI,
    2026-10-23).

Step 8 - configuration, credentials, identity

14. `[provider] api_format` selects exactly one adapter, and the default is
    `openai`. A per-format sub-table is accepted only with its format. Unknown
    or mismatched settings fail at configuration load.
15. Remote endpoints must use https, and plain http is accepted only for
    loopback hosts (DECIDED-C14-4). Rejection uses a fixed CLI error code with
    a value-free fix hint.
16. The env file must belong to the current user and grant no group or other
    access (DECIDED-C14-5). Rejection uses a fixed CLI error code and names
    `chmod 600` as the fix.
17. Credentials are never written to `os.environ` (DECIDED-C14-6):
    - the provider is built from an in-memory credential holder, filled at
      the existing late-loading points, with the key held as a secret value;
    - the default environment-reading builder is removed;
    - tests prove that a child process started after loading carries no
      provider key.
18. The runtime manifest records the endpoint scheme and host
    (DECIDED-C14-7), but no path, query, userinfo or key. It hashes every
    adapter and the shared transport. Changing the host changes
    `runtime_revision`.

Step 9 - documentation and live checks

19. `KNOWN_LIMITATIONS.md`, `LOCAL_TASK_ENTRYPOINT.md`, `ARCHITECTURE.md`,
    `CLUSTER_DEPLOYMENT.md` and `examples/` describe the formats, the
    migration (https, file mode, removed environment variables), data
    recipients, and relay gateways. `REAL_E2E_CONVERGENCE_WORKPLAN.md` records
    the state change at the top.
20. Gated live tests, requiring an explicit environment flag and one env file
    per provider, pass for all five formats: multi-turn tool use with thinking
    enabled, and structured finalization. Results are reported by acceptance
    level.

Across all steps

21. No model-name parameter guessing and no automatic switching of format,
    model or provider.
22. Keys, private reasoning, signatures and encrypted reasoning never appear
    in events, trace, Memory, model-boundary records, `RUNTIME.json`, error
    output or CLI output.
23. The full deterministic suite passes on macOS without network access.
