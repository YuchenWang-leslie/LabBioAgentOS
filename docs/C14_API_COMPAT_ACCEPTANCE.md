# C14 Provider API Format Compatibility Acceptance

## Decision

```text
PROPOSED: C14 PROVIDER API FORMATS ACCEPTED, CRITERION 20 IN PART
```

Status: draft for MOHAN CUI's decision. The decision line is final only when
MOHAN CUI confirms it.

Criteria 1 to 19 and 21 to 23 are met. Criterion 20 asks for live checks of
all five formats and is met in part:

- **Gemini `generateContent` and Gemini Interactions:** acceptance levels
  3 to 6. They passed the kernel-level checks and the full workflow on
  Scheduler + Apptainer.
- **DeepSeek's OpenAI-compatible (Chat, vendor dialect) and
  Anthropic-compatible endpoints:** level 3 only. They passed the
  kernel-level checks in JSON-object mode. MOHAN CUI dropped the full DeepSeek
  workflow on the cluster from C14 acceptance on 2026-10-26.
- **Kimi's OpenAI-compatible and Anthropic-compatible endpoints**
  (`kimi-k3`, 2026-10-27): level 3 in both formats, in schema mode and in
  JSON-object mode. One full workflow on Scheduler + Apptainer submitted a
  correct report. The provider then failed the REPORT finalization with
  HTTP 502, so delivery (level 6) was not reached.
- **OpenAI Responses, the official Chat dialect and the official Anthropic
  service:** levels 1 and 2 only. No key for these services was available.
  The official Chat dialect's request form did run live, but against Kimi
  rather than OpenAI.

C14 changes how the Native Kernel calls a model. It adds no method
selection, scientific routing or analysis logic. Nothing switches format,
model or provider automatically. Workflow states, loop control and
collaboration policy are unchanged.

Design and criteria: [C14 gap memo](C14_API_COMPAT_GAP_MEMO.md). Decisions:
DECIDED-C14-0 to DECIDED-C14-10 in
[C14 decisions](C14_API_COMPAT_DECISIONS.md). Operator guide:
[local task entrypoint](LOCAL_TASK_ENTRYPOINT.md) (provider section) and
[cluster deployment](CLUSTER_DEPLOYMENT.md).

## Revisions and environment

| Item | Value |
| --- | --- |
| Starting LabBio SHA (`main`) | `d7b1000251ccc00656b30a96e37785dcec07e4a2` |
| Accepted code SHA (`feat/api-compat`) | `43d6d695517f8dc9a9550c7aac63df8b82ba2990`; this document is committed on top of it |
| Development machines | macOS with conda Python 3.11 until 2026-10-26; then a Linux workstation (WSL2) with conda Python 3.11 |
| Test cluster | Linux x86_64, Slurm 25.11.5, Apptainer 1.4.1; the same cluster as C13 |
| Local Docker | Docker Desktop 29.8.2 on macOS arm64 |
| Push state | `feat/api-compat` is not pushed, merged, released or deployed; the cluster received the code as git bundles |

## Test results

| Suite | Result |
| --- | --- |
| Full suite before C14 (`d7b1000`, macOS) | 1756 passed, 24 skipped |
| Full suite at `43d6d69` (macOS) | 2363 passed, 68 skipped; 44 of the skips are the opt-in live checks |
| Full suite at `43d6d69` (Linux workstation, 2026-10-26) | 2364 passed, 67 skipped; the extra test runs only on Linux |
| `tests/provider_contracts/` | 137 tests over six wire variants, included in the full suite |
| Live kernel checks, `test_provider_formats_live.py` (level 3) | See [Live results](#live-results) |
| Live workflow, `test_provider_workflow_live.py` (levels 4 to 6) | See [Live results](#live-results) |
| Existing gated Docker tests (no model calls, macOS) | 1 passed, 4 failed; they fail the same way on `d7b1000` (see [Findings outside C14](#findings-outside-c14)) |

Each remaining warning is the pre-existing third-party Uvicorn deprecation
warning.

## Live results

All live runs used the opt-in tests on the accepted code or on code that
differs from it only in the fixes listed under
[Live defects found and fixed](#live-defects-found-and-fixed). The tests write
content-free JSON records. They keep no prompt, answer, key or private
reasoning.

| Variant | Model and host | Kernel checks (level 3) | Full workflow (levels 4 to 6) |
| --- | --- | --- | --- |
| `gemini` | `gemini-3.6-flash` and `gemini-3.1-flash-lite`, test cluster, paid tier | All 14 checks passed with each model, no retries | Both models completed on Scheduler + Apptainer. Only the `gemini-3.6-flash` report matched the result table |
| `gemini-interactions` | as above | All 14 checks passed with each model, no retries | as above |
| `gemini`, `gemini-interactions` | `gemini-3.5-flash-lite`, macOS, free tier, after the fixes | All 11 checks passed in each format | Not run || `openai`, vendor dialect | `deepseek-flash`, Linux workstation | Passed in JSON-object mode; schema mode fails with HTTP 400 | Not run (dropped from C14) |
| `anthropic` | `deepseek-flash` through DeepSeek's Anthropic-compatible endpoint, Linux workstation | Passed in JSON-object mode; schema mode is accepted but not applied | Not run (dropped from C14) |
| `openai`, official-dialect settings | `kimi-k3` through Kimi's OpenAI-compatible endpoint | Passed in schema mode and JSON-object mode, including the stage schema in schema mode | Ran on Scheduler + Apptainer; correct report submitted; REPORT finalization failed with HTTP 502 from the provider |
| `anthropic` | `kimi-k3` through Kimi's Anthropic-compatible endpoint | Passed in schema mode and JSON-object mode | Not run |
| `openai`, official dialect against OpenAI | — | Not run (no key) | Not run |
| `openai-responses` | — | Not run (no key) | Not run |
| `anthropic`, official service | — | Not run (no key) | Not run |

The DeepSeek and Kimi rows are vendor compatibility endpoints, not the
official OpenAI or Anthropic services.

### Full workflow on Scheduler + Apptainer

The workflow test runs the real CLI on a synthetic expression table. Every
run below completed and met every check of the test:

- each of the eight model stages made provider turns, and the host
  completed PREFLIGHT;
- one execution failed and a revised program then succeeded;
- delivery was exported;
- `RUNTIME.json` held the format and the endpoint origin, without the raw
  endpoint;
- a scan of the run directory found no key and no private reasoning.

| Format | Model | Provider turns | Tokens in / out | Time | Report |
| --- | --- | ---: | --- | --- | --- |
| `gemini` | `gemini-3.1-flash-lite` | 37 | about 245k–318k / 7k–9k (both lite runs) | 156–228 s (both lite runs) | Names none of the expected top genes; its numbers appear nowhere in the run |
| `gemini-interactions` | `gemini-3.1-flash-lite` | 38 | as above | as above | Names only GENE_C; its numbers appear nowhere in the run |
| `gemini` | `gemini-3.6-flash` | 31 | 267k / 16.7k | 194 s | GENE_C 3.0, GENE_G 2.17, GENE_E -2.0; matches the result table |
| `gemini-interactions` | `gemini-3.6-flash` | 30 | 226k / 16.8k | about 200 s | as above |

Usage was known for every turn, and no request was retried. The two models
ran with the same code and configuration, so the report difference comes from
the model's choice of Artifact view. See
[Findings outside C14](#findings-outside-c14).

The Kimi run (`openai` format, `kimi-k3`, 2026-10-27) did not complete.
- **What went right:**
  - seven model stages made 23 provider turns, and the host completed
    PREFLIGHT;
  - the one execution succeeded;
  - every stage that read results used `TOP_N`;
  - the report submitted in REPORT gives GENE_C 3.0, GENE_G 2.17 and
    GENE_E -2.0, which match the result table.
- **Where it stopped.** The REPORT finalization received HTTP 502 from the
  provider on all three attempts, each after about 74 seconds. The stage
  failed with `NATIVE_INVOCATION_FAILED` (`PROVIDER_TRANSPORT_ERROR`).
- **How the platform handled it.** As designed, the run stayed in flight at
  REPORT. Recovery means repeating the finalization only, through an
  operator `reconcile` and `continue`; the run was not resumed. No delivery
  was exported, and LEARN did not start.
- **Totals:** 1650 s, 162k input and 10k output tokens, and five retries
  (HTTP 429, 502 and network). There were no leaks, and no scheduler job or
  scratch remained.

A read-only diagnosis found every LabBio layer correct up to the provider
failure: identity, configuration, model-visible context, tool calls and the
submitted report.

### Questions the live runs answered

| Criterion | Question | Answer |
| --- | --- | --- |
| 11 | Do `generateContent` ids fit the kernel identifier form? | No. Some response ids start with `-` or `_`; such ids are now recorded as unknown (`8509da8`). Function-call ids fit |
| 11 | What is the in-stream error shape? | Not observed. Every error seen arrived as an HTTP status |
| 12 | Are Responses item ids accepted under `store: false`? | Not checked; no OpenAI key was available |
| 12b | Does an interaction id fit the identifier form? | The service sends no id under `store: false`; the id is now optional (`1ef1363`) |
| 12b | Rate-limit and overload codes | A rate limit is HTTP 429 with code `too_many_requests`. The overload code was not observed |
| 12b | Is a blocked input an HTTP 400 or a stream error? | Not observed. A rejected schema is HTTP 400 whose body is an SSE `error` event with code `invalid_request` |
| 12b | Are replayed steps accepted? | Yes, in every tool round after the fixes |
| 9, 10 | Official OpenAI Chat and official Anthropic behavior | Not checked; no key was available. DeepSeek's and Kimi's compatible endpoints check the wire format only. Kimi accepted the official Chat dialect's request form |
| 2 | Is the byte cap large enough? | No live response was refused by the cap. In the DeepSeek and Kimi records the largest response was about 72 KB and the largest ratio about 1 KiB per output token, far inside 8 MiB plus 1 KiB per requested output token |
| 4 | Does retry behave against real services? | Yes. Kimi rate limits (HTTP 429, once with a 60 s `retry-after`), gateway errors (HTTP 502) and a network error were retried within the bound and recorded in the trace; when the bound was spent, the stage failed as a transport error and the run stayed recoverable |

## Criteria

| # | Criterion (summary) | Result and evidence |
| ---: | --- | --- |
| 1 | `http_stream.py` owns POST, SSE framing, strict JSON, error categories, `trust_env` and disabled redirects; Chat uses it with no assertion changes | Met. `fe9a081`; `tests/native_kernel/test_http_stream.py`. Existing provider tests passed unchanged. |
| 2 | Byte cap `8 MiB + 1 KiB x max_output_tokens` on decoded bytes; gzip only, inflated within the remaining cap | Met. `fe9a081`, `73ed81e` (DECIDED-C14-8). A 64 MiB gzip bomb stays near a 1 MiB cap in tests. |
| 3 | Any 3xx is a transport error; no request reaches the redirect target | Met. Tested for each credential header form. |
| 4 | Bounded retry of transient failures only, honoring `retry-after`, never past the deadline; each attempt ends 0.5 s before it | Met. `fe9a081`, `a2e3a49`, `1523ec7` (DECIDED-C14-3). Live: Gemini and Kimi rate limits, Kimi HTTP 502 and network errors were retried and recorded, a 60 s `retry-after` was honored, and a spent bound ended the stage as a transport error. |
| 5 | Five transports; dialect must match its transport; single binding per team | Met. `8c5c7d1`, `86d406a`. |
| 6 | Generic private-continuation store, bounded, memory-only, cleared per invocation | Met. `8c5c7d1`; Chat replay unchanged. Live: reasoning, thinking blocks and thought signatures were sent back in every format checked live. |
| 7 | Schema projection; local validation wins | Met. `8c5c7d1`. Live: DeepSeek's Anthropic-compatible endpoint ignored the schema, and local validation rejected the answer (`extra_forbidden`). |
| 8 | Retry records reach the run trace, also at the deadline; default two retries | Met. `8c5c7d1`, `a2e3a49`; trace type `PROVIDER_RETRY_OBSERVED`. |
| 9 | Vendor dialect unchanged; official dialect; refusal is `CONTENT_FILTER` | Met at levels 1 and 2. `d5f7bb0`, `d7938ec`. Vendor request bodies were compared byte for byte with the old code. Vendor dialect live with DeepSeek; the official dialect's request form live with Kimi; official OpenAI not live. |
| 10 | Anthropic Messages adapter with verbatim replay | Met at levels 1 and 2. `ef933f2`, `cf5627a`. Live only through DeepSeek's and Kimi's compatible endpoints: two tool rounds with thinking blocks sent back passed; Kimi also applied `output_config.format`. |
| 11 | Gemini `generateContent` adapter | Met. `c257dc0`, `8509da8`. Live at levels 3 to 6. |
| 12 | OpenAI Responses adapter, stateless | Met at levels 1 and 2. `5dad087`. Item ids under `store: false` are not live-checked. |
| 12b | Gemini Interactions adapter, stateless | Met. `86d406a`, `1ef1363`. Live at levels 3 to 6; the overload code and a blocked input are not observed. |
| 13 | One contract suite through the real AgentLoop for every wire variant | Met. `0928feb`, `fffda5d`, `ba8d45c`; `tests/provider_contracts/`, 137 tests. |
| 14 | `[provider] api_format` and per-format tables; mismatches fail at load | Met. `11190b8`, `8bd68c8`. |
| 15 | https for remote endpoints; endpoint in TOML; fixed error codes | Met. `8bd68c8` (DECIDED-C14-4, DECIDED-C14-7 amended). |
| 16 | Env file owned by the user, mode 0600, checked at load and on the open file | Met. `8bd68c8` (DECIDED-C14-5). |
| 17 | Credentials never in `os.environ`; child processes carry no key | Met. `8bd68c8` (DECIDED-C14-6); `tests/test_local_configuration.py`. Live: no key found in any run directory. |
| 18 | Runtime identity records the endpoint origin and digest and hashes every provider module | Met. `91e8c27`. Live: every workflow `RUNTIME.json` held the origin and no raw endpoint. |
| 19 | Living documents describe formats, migration, data recipients and relay gateways | Met. `acfb9e6`, `7b4409f` and the commit that adds this document. |
| 20 | Gated live tests pass for all five formats | **Met in part.** See [Decision](#decision) and [Live results](#live-results). |
| 21 | No model-name parameter guessing; no automatic switching | Met. Dialect settings are configured, and Anthropic and Gemini thinking settings have no default. `src/` contains no model-name literal. |
| 22 | Keys and private reasoning never reach events, trace, Memory, model-boundary records, `RUNTIME.json`, error or CLI output | Met. Contract and application tests per wire variant; fixed error codes carry no values. Live: every workflow scan was clean. |
| 23 | Full deterministic suite passes without network access | Met on macOS and on Linux. |

Kimi's first kernel run returned HTTP 404 for every request until the account
was set up in the provider console; the unchanged configuration passed
afterwards. It is not counted as a defect.

## Live defects found and fixed

| Found in | Defect | Fix |
| --- | --- | --- |
| First Gemini run | `generateContent` response ids outside the kernel identifier form failed the turn | `8509da8`: every adapter records such an id as unknown |
| First Gemini run | Interactions sends no interaction id under `store: false`, so every request failed | `1ef1363`: the id is optional; a duplicate created event is still refused |
| First Gemini run | Full stage schemas are rejected in Gemini schema mode (HTTP 400) | `7b4409f`: Gemini requires `finalization_json_object = true`; documented |
| First cluster workflow run | The live test expected model turns in PREFLIGHT, which the host owns when an execution profile is configured | `08518c6`: the test expects eight model stages and a host-completed PREFLIGHT |
| DeepSeek kernel run | Schema mode is rejected by the OpenAI-compatible endpoint and ignored by the Anthropic-compatible one | Documented with this acceptance: DeepSeek requires `finalization_json_object = true` |

The scripted services of the offline rehearsal followed the published
documentation, so they could not reveal the first three defects.

## Findings outside C14

These were found during C14 live work. They predate C14 and concern code that
C14 does not change. By MOHAN CUI's decision of 2026-10-26, they are recorded
and left for discussion with the code owner. No code was changed.

- **Reports not checked against results.** With `gemini-3.1-flash-lite`, the
  result tables were correct, but no stage ever received an output value.
  Every Artifact view except `TOP_N` reports `available_count` 0, and the
  stages used only `SUMMARY`, which is empty for these records. Both reports
  stated numbers that appear nowhere in the run. `report_submit` checks
  citation coverage only, so it accepted them. A completed workflow is not a
  correct result.
- **Gated Docker tests.**
  - The file-limit test asserts that `/sys/class/net` holds only `lo`, which
    fails on Docker Desktop's VM kernel even though networking is off.
  - The C3 test grants no release authorization, so its output stays `RAW`;
    it fails on any host.
  - The C12 and input-identity tests pin image IDs from the original Linux
    host.

## Limitations carried forward

- The OpenAI Responses API, the official Chat dialect and the official
  Anthropic service have no live evidence. Their first live use may expose
  defects of the kind the Gemini run found.
- DeepSeek has no full-workflow evidence. The one Kimi workflow stopped at
  the REPORT finalization on provider HTTP 502 and was not resumed.
- A provider outage longer than the retry bound stops the stage. The run
  stays recoverable, but resuming it is an operator action.
- The Interactions overload code and the arrival form of a blocked input are
  unknown.
- The official OpenAI, Anthropic, Gemini and DeepSeek schema modes cannot be
  used with LabBio's stage schemas; deployments must set
  `provider_tool_schema_strict = false` and `finalization_json_object = true`.
- Strict parsing can stop a provider that adds a new content type until
  LabBio supports it.
- Only API keys are supported; cloud-platform authentication is not.
- A retried request can be billed twice.
- The runtime revision changes with C14, so runs started before the upgrade
  cannot resume after it. A changed endpoint also changes the revision.
- Existing configurations need migration: https endpoints, mode 0600 env
  files, and the endpoint moved from the env file into `[provider] endpoint`.
- The Docker runner does not filter the environment it passes on, so a key
  the user exports in the shell still reaches the Docker client. An env file
  under a mounted path can be read by the container despite mode 0600.

## Acceptance levels

| Level | Status |
| --- | --- |
| 1. Unit and schema tests | Passed |
| 2. Service integration and fixtures | Passed, including the contract suite for six wire variants |
| 3. Generic live Agent behavior | Passed for both Gemini formats and for the DeepSeek- and Kimi-compatible endpoints of both other formats; not run against OpenAI or Anthropic themselves |
| 4. Real execution | Passed for both Gemini formats and for Kimi (`openai` format) on Scheduler + Apptainer, on a synthetic table |
| 5. Validation, interpretation, claims and report export | Completed for both Gemini formats. Reports are correct with `gemini-3.6-flash` and wrong with `gemini-3.1-flash-lite`. The Kimi run submitted a correct report but did not finish REPORT |
| 6. User-visible delivery and cleanup | Delivery exported in every completed Gemini run. The Kimi run exported none; it left no scheduler job or scratch. Cleanup was not checked separately for the Gemini runs |
| 7. Release, rollback rehearsal and activation | Not started; the branch is not merged |

## Commits

| Step | Commits |
| --- | --- |
| 0 Documents | `1daf203`, `b97bc90` |
| 1 Shared transport | `fe9a081` |
| 2 Neutral contract | `8c5c7d1`, `7d4294e`, `a2e3a49`, `73ed81e` |
| 3 OpenAI Chat dialects | `d5f7bb0`, `1d3b819`, `1523ec7`, `d7938ec`, `cf5627a` |
| 4 Anthropic Messages | `ef933f2` |
| 5 Gemini `generateContent` | `c257dc0` |
| 6 OpenAI Responses | `5dad087` |
| 6b Gemini Interactions | `86d406a` |
| 7 Contract parity | `fffda5d`, `6b09c17`, `0928feb`, `ba8d45c` |
| 8 Configuration, credentials, identity | `11190b8`, `8bd68c8`, `91e8c27` |
| 9 Documents and live checks | `5c82efc`, `acfb9e6`, `cc966b6`, `86f7621`, `8509da8`, `1ef1363`, `7b4409f`, `6deb379`, `08518c6`, `b3bd446`, `4699f0c`, `43d6d69` |

Live-run records and cluster archives were kept outside the repository and
are not part of this record.
