# PantheonOS Upstream Modifications

## Result through Phase 6

No PantheonOS core file was modified through Phase 6. No direct core
modification is currently required or proposed.

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

The default implementation strategy remains LabBio extension -> adapter/plugin/provider/subclass -> PantheonOS. The following is a deliberately small conditional watchlist, not a request to edit these files now.

Phase 5 adds a LabBio-owned local store, deterministic exposure service, and a
narrow Pantheon-facing query adapter. Because controlled views are generated
before any value reaches Pantheon's tool-result conversion, no change to
`pantheon/providers.py`, `pantheon/agent.py`, or team code is required.

Phase 6 remains entirely in the LabBio execution/artifact plane. Typed plans,
approved image resolution, mount validation, Docker argv construction, process
execution, output collection, and trace emission need no Pantheon hook. A later
authorized ToolProvider can wrap `DockerExecutor` and return `ExecutionResult`
references without changing Pantheon core.

## Conditional watchlist

| Core file | Potential future need | Why wrapper/plugin/subclass may be sufficient | When a minimal core change would be justified | Compatibility risk |
|---|---|---|---|---|
| `pantheon/agent.py` | A future generic typed failure event for non-delegation tools might be useful | The Phase 3 `call_agent` decorator is sufficient for delegation failure; future tools can return their own typed envelopes | Only if a later authorized phase proves a generic tool failure cannot be observed at its capability boundary | High: central tool dispatch, streaming, background adoption, and all providers pass through this code |
| `pantheon/team/pantheon.py` | A future public policy hook could reduce reliance on decorating registered functions | The Phase 3 TeamPlugin wrapper preserves all required delegation mechanics and passes its contract tests | Only if an upstream API change makes registered-function decoration unavailable or demonstrably unsafe | Medium-high: delegation, child memory, chain safety, and plugin behavior are concentrated here |

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
