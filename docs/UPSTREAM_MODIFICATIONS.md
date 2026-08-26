# PantheonOS Upstream Modifications

## Phase 0 result

No PantheonOS core file was modified in Phase 0. No direct core modification is currently approved.

The default implementation strategy remains LabBio extension -> adapter/plugin/provider/subclass -> PantheonOS. The following is a deliberately small conditional watchlist, not a request to edit these files now.

## Conditional watchlist

| Core file | Potential future need | Why wrapper/plugin/subclass may be sufficient | When a minimal core change would be justified | Compatibility risk |
|---|---|---|---|---|
| `pantheon/agent.py` | Preserve a structured delegation/tool failure instead of converting every tool-task exception to `repr(exception)` model content; optionally expose a stable per-tool lifecycle event | A LabBio team/tool wrapper can catch expected capability failures and return a typed envelope; existing pre/post/tracking hooks cover most tracing | Only if Phase 3 acceptance tests prove a child Agent exception cannot reach the stage adapter as structured failure without copying/replacing the large `_handle_tool_calls` implementation | High: central tool dispatch, streaming, background adoption, and all providers pass through this code |
| `pantheon/team/pantheon.py` | Add an explicit delegation-policy/list-filter extension point without replacing `call_agent` internals | A `PantheonTeam` subclass plus plugin-registered pre-tool gate can enforce policy and preserve the existing closure; stage context can travel in `context_variables` | Only if policy-filtered `list_agents` and `call_agent` cannot be implemented without duplicating substantial upstream delegation code or weakening execution/chain metadata | Medium-high: delegation, child memory, chain safety, and plugin behavior are concentrated here |

## Files explicitly not expected to require direct changes

- `pantheon/team/base.py`: the LabBio adapter can implement stage invocation outside it.
- `pantheon/team/plugin.py`: the existing plugin contract and documented per-agent hook registration are sufficient for the first extension attempt.
- `pantheon/providers.py`: new safe execution/artifact providers can implement `ToolProvider` without altering existing providers.
- `pantheon/internal/memory/*`: LabBio workflow/artifact/scope stores remain outside conversation memory.
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
