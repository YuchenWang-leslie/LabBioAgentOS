# PantheonOS Upstream Modifications

## Current LabBio runtime revision

The earlier Phase 1-8 conclusion below is historical. C7 produced two generic
Pantheon runtime defects that could not be repaired faithfully in a LabBio
adapter, prompt, or monkey patch. The required source identities are:

| Identity | Commit |
|---|---|
| Pantheon upstream 0.6.4 baseline | `5d3d459ac5752ed9d39432232d76ad1581296012` |
| Reasoning-only idle convergence patch | `ba7f0e4b13a312e954fcb96df8b1a7a3f1510d44` |
| Primitive schema-constraint patch | `45ef598f8d79bd98e9befc7c549980b731476662` |
| Nested provider-schema fidelity patch | `02ba577abd41d8b180a0dbb79fd057d2ca15ae42` |
| Canonical tool-call history replay patch | `0ebbb8bac68b8fa88e2d16439ed9ecae2a746815` |
| Reasoning-only replay omission patch | `e2db6289a3daa0b42814c2ab02ad12c038e4428f` |
| Provider-turn progress and parameter reuse patch | `381146326e58720db2fcaf5f47419ae271a5d058` |
| Compatible-provider thinking transport patch | `93ec465c2f4cbbf44d594c4e142971de017ab232` |
| Governed tool-request integrity patch | `7b02bcba6402eb67d498101d5ad7ba3ae5ac47d7` |
| Private reasoning, strict schemas, foreground feedback and finalization observation patch | `07675c45b538f7d27b9b16b1b7d8b72f37365293` |
| Required LabBio Pantheon revision | `07675c45b538f7d27b9b16b1b7d8b72f37365293` |

The nine required patch commits form one linear history:

```text
5d3d459ac5752ed9d39432232d76ad1581296012
  -> ba7f0e4b13a312e954fcb96df8b1a7a3f1510d44
  -> 45ef598f8d79bd98e9befc7c549980b731476662
  -> 02ba577abd41d8b180a0dbb79fd057d2ca15ae42
  -> 0ebbb8bac68b8fa88e2d16439ed9ecae2a746815
  -> e2db6289a3daa0b42814c2ab02ad12c038e4428f
  -> 381146326e58720db2fcaf5f47419ae271a5d058
  -> 93ec465c2f4cbbf44d594c4e142971de017ab232
  -> 7b02bcba6402eb67d498101d5ad7ba3ae5ac47d7
  -> 07675c45b538f7d27b9b16b1b7d8b72f37365293
```

`ba7f0e4b` bounds reasoning-only idle convergence in Pantheon's generic Agent
loop. `45ef598f` preserves supported primitive constraints in provider-visible
tool JSON Schema and restores standard `typing` annotations across funcdesc
JSON serialization. `02ba577a` resolves postponed annotations before schema
generation, carries nested Pydantic schemas across the descriptor transport,
and inlines local references with bounded cycle handling. `0ebbb8ba`
canonicalizes only tool argument objects that Pantheon has already parsed
successfully before replaying them to a provider; unparseable arguments remain
failures and no nested application value is decoded or repaired. `e2db6289`
omits a provider-private reasoning-only turn when redaction would otherwise
leave an invalid empty assistant message; idle accounting is unchanged and no
reasoning or invented placeholder content is replayed. `38114632` copies
provider parameters before translating Pantheon's thinking shorthand, exposes
a content-free provider-turn observation, and accepts a caller-bounded
no-progress wall-clock. The observation contains only agent/context identity,
turn number, progress class, elapsed milliseconds, token count, and tool names;
it never contains provider bodies, message content, or hidden reasoning.
`93ec465c` sends an explicitly configured structured thinking extension through
the OpenAI SDK's `extra_body` for compatible base URLs; direct/native provider
behavior remains unchanged. None of the patches selects scientific methods,
routes a LabBio stage, repairs model output, or contains a provider/PBMC special
case.

`7b02bcba` preserves Chat Completions finish reason and completion-token usage
through extraction/statistics cleanup. Before tool dispatch it observes only
finite finish/parse/rejection categories and bounded tool identities. An opt-in
`strict_tool_arguments` mode rejects known truncated/filtered responses and
non-object, duplicate-key, non-finite or otherwise malformed JSON without repair,
before capability hooks or tool execution. LabBio enables it for capability
agents. Default Pantheon parsing/replay remains compatible for other callers;
strict callers retain the original failed request in conversation history.

A LabBio-only hook is insufficient: provider metadata was dropped, and argument
repair occurred, before that hook could run. The narrow upstream changes are in
`pantheon/agent.py`, `pantheon/utils/llm.py`, and
`pantheon/utils/llm_providers.py`. Compatibility risk is confined to opted-in
callers rejecting inputs previously repaired. This is syntax/transport integrity,
not proof that a syntactically valid program is complete or correct. Missing or
unknown finish reasons remain unknown. The separate Responses API terminal
status is not normalized by this patch, so its truncation detection is not claimed.
No provider bodies, arguments, source, raw streams, or reasoning enter the audit.

The integration source is the user-controlled fork
`https://github.com/YuchenWang-leslie/PantheonOS`, branch
`fix/private-tool-reasoning-continuity`. The earlier focused branches remain
available. The fork remains traceable to upstream
`https://github.com/aristoteleo/PantheonOS`. These commits are not claimed to be
part of an official Pantheon release. Do not copy their source into LabBio or
monkey-patch Pantheon at LabBio import time.

LabBio's public package declaration remains `pantheon-agents>=0.6.4,<0.7` so a
future official compatible release can replace the fork deliberately. That
range alone currently resolves vanilla 0.6.4 and is insufficient for current
runtime acceptance. For a reproducible development or acceptance environment,
install with the repository-owned constraint:

```bash
python -m pip install \
  -c constraints/pantheon-runtime.txt \
  -e '.[test]'
```

On 2026-09-09, the authorized fork push published `07675c45` on
`fix/private-tool-reasoning-continuity`, extending the previously published
`7b02bcba` history. The development constraint now includes all four runtime
extensions documented below. Their earlier "local/unpublished" sections record
the state at implementation time, not the current publication status.
Pantheon fork `main` remains the upstream baseline. An existing sibling
checkout at that exact SHA can alternatively be installed with
`python -m pip install -e ../PantheonOS`. Do not silently fall back to vanilla
0.6.4 or the earlier `7b02bcba`: neither supplies all the current Agent APIs.
No Git credential or proxy setting was changed, and no official Pantheon release
is claimed.

Then verify both source identity and import location; a `0.6.4` version string
alone is not sufficient:

```bash
python - <<'PY'
import pantheon
print(pantheon.__version__)
print(pantheon.__file__)
PY
```

## Result through Phase 8

No PantheonOS core file was modified through Phase 8. At that historical
boundary, no direct core modification was required or proposed.

Phase 3 demonstrated that a `TeamPlugin` can decorate the already-registered
`list_agents` and `call_agent` functions. Allowed calls still execute the
original Pantheon closure, while the decorator can reject policy violations and
catch child exceptions before `Agent._handle_tool_calls` converts them to prose.
The Phase 3 tests preserve execution IDs, parent tool-call IDs, chain paths,
depth protection, and ancestor protection, so neither watchlist file requires a
patch for controlled delegation.

Phase 4 reuses the adapter boundary, the Phase 3 decorated delegation function,
and Pantheon's existing step/chunk metadata. LabBio task-local invocation IDs
and append-only sinks provide workflow/agent correlation without changing
`pantheon/agent.py`, `pantheon/team/pantheon.py`, memory, or plugin contracts.

The default implementation strategy remains LabBio extension ->
adapter/plugin/provider/subclass -> PantheonOS. The eight current generic patches
above are the documented exceptions. The following is a deliberately small
conditional watchlist, not a request to edit additional files now.

Phase 5 adds a LabBio-owned local store, deterministic exposure service, and a
narrow Pantheon-facing query adapter. Because controlled views are generated
before any value reaches Pantheon's tool-result conversion, no change to
`pantheon/providers.py`, `pantheon/agent.py`, or team code is required.

Phase 6 remains entirely in the LabBio execution/artifact plane. Typed plans,
approved image resolution, mount validation, Docker argv construction, process
execution, output collection, and trace emission need no Pantheon hook. A later
authorized ToolProvider can wrap `DockerExecutor` and return `ExecutionResult`
references without changing Pantheon core.

Phase 7 keeps successful-trace projection, curator proposals, explicit approval,
immutable versions, scoped candidate retrieval, and usage evidence in LabBio.
Pantheon's learning system is not changed or used to auto-publish Gold Skills;
future integration can wrap its read/index mechanics without changing core.

Phase 8 adds Principal/project policy, frozen WorkflowRun ownership, governed
Artifact/Gold Skill service entry points, proposal-only persistent Memory, and
ID-only workspace resolution entirely within LabBio. Authorization services and
stores are never supplied to Pantheon agents. A future Pantheon-facing adapter
can pin a Principal exactly as the artifact consumer is pinned today, so no
Pantheon memory, provider, agent, or team modification is needed.

## Conditional watchlist

| Core file | Potential future need | Why wrapper/plugin/subclass may be sufficient | When a minimal core change would be justified | Compatibility risk |
|---|---|---|---|---|
| `pantheon/agent.py` | A future generic typed failure event for non-delegation tools might be useful | The Phase 3 `call_agent` decorator is sufficient for delegation failure; future tools can return their own typed envelopes | Only if a later authorized phase proves a generic tool failure cannot be observed at its capability boundary | High: central tool dispatch, streaming, background adoption, and all providers pass through this code |
| `pantheon/team/pantheon.py` | A future public policy hook could reduce reliance on decorating registered functions | The Phase 3 TeamPlugin wrapper preserves all required delegation mechanics and passes its contract tests | Only if an upstream API change makes registered-function decoration unavailable or demonstrably unsafe | Medium-high: delegation, child memory, chain safety, and plugin behavior are concentrated here |

## Files explicitly not expected to require direct changes

- `pantheon/team/base.py`: the LabBio adapter can implement stage invocation outside it.
- `pantheon/team/plugin.py`: the existing plugin contract and documented per-agent hook registration are sufficient for the first extension attempt.
- `pantheon/providers.py`: new safe execution/artifact providers can implement `ToolProvider` without altering existing providers.
- `pantheon/internal/memory/*`: Phase 8 governed persistent Memory remains outside Pantheon conversation memory.
- `pantheon/internal/learning_system/*`: Gold Skill approval and RunTrace provenance belong in a LabBio wrapper/store, not in automatic upstream extraction.
- `pantheon/background.py`: background tasks are ignored for the initial deterministic workflow.

## Decision rule for future modification

Before changing either watchlist file, the responsible phase must record:

1. the acceptance criterion that cannot be met through an adapter/plugin/provider/subclass;
2. a failing contract test demonstrating the gap;
3. the smallest new generic hook or typed metadata needed;
4. why the change contains no scientific method selection or task-specific reasoning;
5. upstream compatibility and fallback behavior when the LabBio extension is absent.

If a change is approved, append the exact commit/diff behavior and compatibility impact here. Do not silently turn a conditional watch item into a core patch.

## 2026-09-09 — Opt-in private tool reasoning continuity (local, unpublished)

The user authorizes general repair/novel-task testing. The published baseline
remains `7b02bcba6402eb67d498101d5ad7ba3ae5ac47d7`; the current local branch is
`fix/private-tool-reasoning-continuity`. No official release or remote publication
of this development change is claimed; existing package constraints still pin the
published baseline. Reproduction of the opt-in currently requires this local diff,
whose content digest is frozen in each local runtime manifest.

Some compatible Chat APIs require their original `reasoning_content` on later
assistant tool-call messages when thinking mode is enabled. Pantheon deliberately
strips private reasoning from public history and also strips it at model-message
processing. A LabBio ToolSet or public callback therefore cannot retain it safely:
the field is already absent there, and injecting it into public history would
violate the non-disclosure boundary. The narrow implementation is in
`pantheon/agent.py` and `pantheon/utils/llm.py`, with synthetic final-SDK-wire tests.

`Agent(private_tool_reasoning_continuity=True)` is explicitly opt-in and requires
strict tool arguments. A run-local private store binds the exact provider field
to model/endpoint, host assistant-message identity and unchanged tool-call
fingerprint. It is reattached only at the final compatible Chat adapter boundary.
No provider-name routing, reasoning synthesis, argument repair, additional model
turns or scientific policy is added. Non-strict mode is rejected because its
legacy argument rewriting would break the exact binding; no compatibility layer
is silently inserted.

Public histories, events, callbacks, memory, cache-safe requests and returned
results remain stripped. Child/new runs and another model/endpoint do not inherit
the private state; normal and exceptional exit clear it. Native/Responses APIs
do not receive this Chat-only field. Default-off behavior remains unchanged.
LabBio enables it only for explicitly configured thinking-enabled capability
agents; no-tool finalizers need no tool-history continuity. Broader token
accounting/history-pairing findings remain separate, unmodified limitations.

Provider contract reference: [MiMo deep-thinking tool-call requirements](https://platform.xiaomimimo.com/docs/en-US/usage-guide/passing-back-reasoning_content).

## 2026-09-09 — Explicit provider tool schema strictness (unpublished)

The current compatible-Chat adjustment removes function.strict and top-level
additionalProperties even though a selected endpoint may support them. Local
strict_tool_arguments only rejects malformed JSON and cannot enable constrained
provider generation. A local opt-in Agent.provider_tool_schema_strict flag now
sets strict=true on a per-request deep copy of existing tool definitions and
preserves parameters unchanged. It does not modify LocalProvider defaults,
arguments, defaults, required fields, fallback behavior or scientific decisions.
Native, Responses and the dedicated Codex adapter reject this tool option before
requesting generation; this is not a change to Codex services or tunnel settings.

Implementation is confined to pantheon/agent.py and a focused SDK-wire test file,
on the same unpublished development branch as private reasoning continuity.
The compatibility and privacy regressions pass 81 tests / 90 provider-live skips;
an independent read-only review passes 67 relevant tests. Actual endpoint schema
acceptance remains a separate bounded smoke, not a scientific-success claim.
MiMo documents strict support and a limited schema subset at
https://mimo.mi.com/docs/en-US/api/chat/openai-api . No official Pantheon release
or existing published dependency constraint is claimed to include this change.

## 2026-09-09 — Observe structured responses before parsing (unpublished)

A real REPORT finalization failed with root json_invalid. Pantheon parsed the
Response wrapper before its existing bounded provider-turn callback, so even an
attached observer could not see rejected structured responses. The local fix
moves that unchanged parse after the metadata callback and before any public
message publication. LabBio now attaches the callback in FINALIZE as well as
CAPABILITY. No response repair, additional attempt, schema change, or raw/hidden
content logging is introduced; rejection without an observer remains rejection.
Four new tests cover malformed JSON, schema rejection, valid response ordering,
and the default no-observer path. Related Pantheon regression: 62 passed,
one existing warning. This remains an unpublished local change, not a new pin.

## 2026-09-09 — Invocation-bound tool completion (local, unpublished)

The repeat-query audit reproduced a transport/lifecycle gap, independently of
any scientific task. Pantheon adds a required `_background` argument to ordinary
LocalProvider tools. Explicit background execution or timeout/steering adoption
can return a task handle before the governed result exists. Later native
notifications truncate its string representation to 500 characters (status
polling to 2000). A failed tool envelope can therefore lose its query constraints;
an inspected program page can lose source while retaining its earlier page
metadata. The capability invocation can finish while that task is still pending.
Historical LabBio audits did not retain the background choice, so this mechanism
is not asserted to be the cause of any particular earlier model decision.

Existing hooks cannot establish invocation-bound completion: pre-tool hooks see
arguments only after `_background` is removed; result hooks run after wrapping;
removing a schema field or changing the timeout does not prevent both dispatch
paths. Copying the dispatcher into a LabBio subclass would duplicate core code.
The minimal generic extension is `Agent(allow_background_tools=False)`, selected
by LabBio's factory. It omits the native background control from tool schemas,
rejects an explicit unsupported background request without executing it, and
disables automatic background adoption. Ordinary asynchronous waiting, parallel
tool calls, cancellation and the tool's own execution deadline remain available.
No scientific tool is selected, argument corrected, or retry/turn budget raised.
Default Pantheon callers retain background behavior.

This is a local change in `pantheon/agent.py`, with focused offline dispatch and
model-history regression tests, not a monkey-patch or a change to background.py.
LabBio's existing schema/authorization/query validators remain authoritative.
The published constraint still points to `7b02bcba`; it does **not** contain
this API or the other local changes above. Reproduction currently requires the
local checkout/diff and recorded source digest. Publishing an immutable revision
and updating that pin require a separate authorized Git checkpoint; no official
Pantheon release, fork push, or production deployment is claimed here.
