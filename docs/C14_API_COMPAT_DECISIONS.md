# C14 Provider API Format Compatibility Decisions

This file records the decisions for C14, provider API format compatibility. The
earlier `OPEN_DECISIONS.md` was removed from `main` during the Native Kernel
cleanup (`a8c0aa6`), so these decisions are kept here. Analysis and acceptance
criteria: [API compatibility gap memo](C14_API_COMPAT_GAP_MEMO.md).

All decisions below were made by MOHAN CUI on 2026-10-21. No item is open.

## DECIDED-C14-0 - Scope

MOHAN CUI requested on 2026-10-21 that a deployment owner be able to use
OpenAI-format, Anthropic-format or Gemini-format provider APIs, and allowed
changing the existing OpenAI-compatible adapter. MOHAN CUI numbered the
work as milestone C14 on the same day. The work follows the nine
steps of the gap memo. Each step starts only on MOHAN CUI's instruction, ends
with the full test suite, and is committed separately. WorkflowEngine, the
AgentLoop control flow and collaboration policy are not redesigned.

## DECIDED-C14-1 - Wire formats

Four formats, one per runtime, selected only in trusted configuration:

- `openai` - OpenAI Chat Completions. It is the default, and keeps the
  existing vendor dialect plus an explicit official OpenAI dialect.
- `openai-responses` - the OpenAI Responses API, as a separate adapter, so
  official reasoning models keep encrypted reasoning across tool turns.
- `anthropic` - the Anthropic Messages API.
- `gemini` - the Gemini `generateContent` API. Google labels it legacy, but it
  remains fully supported and is what gateways implement. The Interactions
  API is not used.

A format never changes automatically, and formats are never mixed within one
runtime.

## DECIDED-C14-2 - Raw HTTP, no vendor SDKs

Adapters call the provider HTTP APIs directly through one shared httpx
transport. Official SDKs are not used, for four reasons:

- they retry on their own by default;
- they read credentials and base URLs from the environment and from profile
  files;
- they parse responses leniently, which bypasses the strict parsing and byte
  cap;
- they add dependencies that handle credentials.

Accepted cost: LabBio maintains the wire mapping itself. A response element
that a provider adds later fails explicitly until the adapter is updated.

## DECIDED-C14-3 - Bounded transient retry

The shared transport retries only these failures:

- HTTP 429, 529 and 5xx;
- connection failures;
- provider stream error events that mean overload or rate limiting.

Retry rules:

- It resends the identical request to the identical endpoint and model.
- It honors `retry-after` and caps the number of attempts.
- It never waits past the request deadline.
- Every retry is recorded with its failure category and wait.

Statuses that a retry cannot fix (400, 401, 403, 404 and similar) fail at
once. A failed attempt is discarded in full and never enters the history.
Retry is not a fallback: nothing else changes between attempts.

The default is two retries, so at most three requests, as in the official
SDKs. The retry code lands in step 1 with zero retries by default. The
default becomes two in step 2, together with retry recording, so no commit
retries without recording it.

The transport enforces the request deadline itself. Each attempt has one
total limit that ends 0.5 seconds before the deadline, and no attempt starts
or retry waits inside that margin. The deadline outcome therefore reaches the
loop with the retries that preceded it. The loop still cancels a provider
call at the deadline, as a backstop for providers that ignore it. MOHAN CUI
decided this on 2026-10-21, after step 2 showed that a call cancelled by the
loop lost its retry records.

## DECIDED-C14-4 - Remote endpoints require https

A remote endpoint must use https. Plain http is accepted only for loopback
hosts (`localhost`, `127.0.0.1`, `::1`), such as a model server on the same
machine. Configuration load rejects other http endpoints with an actionable
message. The fixes are TLS on the server, usually through a reverse proxy, or
an SSH tunnel that exposes the remote server on localhost.

## DECIDED-C14-5 - Provider env file mode

The provider env file must belong to the current user and grant no group or
other access (0600 or stricter). Configuration load rejects other modes, and
the error names `chmod 600` as the fix.

## DECIDED-C14-6 - Credentials stay out of the process environment

Provider credentials are no longer written to `os.environ`. After loading,
the key is held in memory as a secret value and passed directly to the
adapter. Child processes therefore do not inherit it, and libraries that
read conventional variable names cannot find it.

Loading stays late: the key is still read only when a model call is needed.
The default builder that read `OPENAI_API_KEY` and `OPENAI_API_BASE` from the
environment is removed, so a Python caller must supply the provider
configuration explicitly.

## DECIDED-C14-7 - Endpoint in the runtime identity

The runtime manifest records the endpoint scheme and host in plain text, but
not its path, query, userinfo or key. Where model traffic went is then
auditable from `RUNTIME.json`. Changing the endpoint host changes
`runtime_revision`, so an unfinished run cannot continue against a different
recipient. An internal gateway's hostname is visible to anyone who receives
the run directory.

## DECIDED-C14-8 - Per-response byte cap

One response may hold at most `8 MiB + 1 KiB x max_output_tokens` bytes,
computed from each request's own output limit. A stream can frame every
generated token as a separate event, and each event carries roughly 140 to
700 bytes of framing depending on the format. The fixed 8 MiB covers
fixed-size content, such as the full response objects that the Responses API
echoes and encrypted reasoning.

| `max_output_tokens` | Cap |
|---|---|
| 8,192 (kernel default) | 16 MiB |
| 16,384 (local default) | 24 MiB |
| 32,768 (local maximum) | 40 MiB |
| 131,072 (contract maximum) | 136 MiB |

The cap applies to every attempt, and to streamed and non-streamed bodies
alike. It counts bytes after decompression. Exceeding it raises the transport
error `RESPONSE_TOO_LARGE`. No partial turn is returned, and the response is
not retried, because it is outside DECIDED-C14-3.

There is no separate setting; the cap follows `max_output_tokens`. Step 9
measures the bytes per output token of each format against real endpoints and
records them in the acceptance document. A setting is reconsidered only if
those measurements approach the allowance.

## Recorded trade-offs

- Strict parsing: unknown blocks, events or finish reasons fail rather than
  being dropped. Newly introduced provider output, such as the Anthropic stop
  reasons `refusal` and `model_context_window_exceeded`, needs an adapter update
  before use. Pinning the API version and enabling no opt-in features keeps
  this rare.
- A retried request whose failed attempt had already produced output may be
  billed twice.

## Not decided here

These remain outside this work. Each needs its own decision before any change:

- authentication for cloud platforms (Amazon Bedrock, Claude Platform on AWS,
  Vertex AI);
- Anthropic prompt caching;
- provider built-in tools;
- rejecting a provider env file located under a path that is mounted into
  execution;
- filtering the environment of the Docker CLI subprocess.
