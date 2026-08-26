# PantheonOS Reuse Map

## Baseline identity

| Item | Recorded value |
|---|---|
| Source repository | `/Users/wangyuchen/Coding/PantheonOS` |
| Git branch | `labbioagent-dev` |
| Git commit | `5d3d459ac5752ed9d39432232d76ad1581296012` |
| Pantheon version | `0.6.4` |
| Source worktree before/after Phase 0 | Clean; no Pantheon source changes |
| Conda environment | `labbioagent` (`/opt/anaconda3/envs/labbioagent`) |
| Python | `3.11.16` |
| pytest / pytest-asyncio | `9.1.1` / `1.4.0` |
| Import result | `import pantheon` passed and resolved to the inspected source checkout |

## Classification table

`MODIFY` below means a possible future direct upstream change, not a Phase 0 change. The approved Phase 0 upstream modification count is zero.

| Component | Current role | LabBio action | Reason | Core modification required? |
|---|---|---|---|---|
| `pantheon.agent.Agent` | LLM run loop, tool schema construction/dispatch, memory callbacks, streaming, response construction | REUSE | This is the core runtime intelligence and tool integration surface | No |
| `Agent.run` / `AgentRunContext` | Prepares filtered context, establishes a task-local run context, executes the LLM/tool loop, and returns `AgentResponse`/`AgentTransfer` | REUSE | Preserves runtime behavior and supplies callbacks/metadata needed by adapters | No |
| `Agent._pre_tool_hooks`, `_post_tool_hooks`, `_tool_tracking_hooks`, `_ephemeral_hooks` | Plugin-registered gates, decorators, tool tracking, and ephemeral prompt additions | EXTEND | Suitable for LabBio policy/trace adapters without hard-coding scientific choices | No, but these are semi-private surfaces |
| `ToolProvider` | Abstract `list_tools`, `call_tool`, `initialize`, and `shutdown` contract | EXTEND | Natural boundary for safe search, Docker execution, artifacts, and exposure-controlled results | No |
| `MCPProvider` | Remote MCP discovery/invocation with prefix routing and caching | REUSE | Useful for future approved external capabilities; not a raw-data channel | No |
| `LocalProvider` / `ToolSetProvider` | In-process and proxied ToolSet adapters | REUSE | Existing provider normalization should be preserved | No |
| Tool-result conversion in `Agent._handle_tool_calls` and `pantheon.utils.llm.process_tool_result` | Builds tool messages, removes declared hidden fields, truncates model content, retains `raw_content`, then feeds `content` into the next LLM turn | WRAP | Phase 3 catches delegation exceptions at the decorated `call_agent` boundary; future artifact exposure must still filter before this conversion | No for Phase 3 |
| `pantheon.team.base.Team` | Minimal agent registry/event aggregation abstraction | REUSE | Stable base contract; no reason to replace it | No |
| `PantheonTeam` | Team setup, active agent, team run, transfers, plugins, and delegation tool injection | EXTEND | Use a LabBio team adapter/subclass for stage context and policy while keeping collaboration internals | Prefer no |
| `list_agents` injected tool | Discovers peers, excludes self, and excludes ancestors for delegated children | WRAP | Phase 3 decorates the registered function and intersects Pantheon's visible candidates with policy-allowed candidates | No |
| `call_agent` injected tool | Validates target/instruction, constructs delegation metadata/context, creates child memory, runs target, forwards child events, and returns child content | WRAP | Phase 3 validates the runtime-selected edge, then calls the original closure unchanged; denial/failure becomes a typed LabBio record | No |
| `execution_context_id` | Identifies one delegated invocation and filters memory/messages | REUSE | Required for context isolation and RunTrace linkage | No |
| `parent_tool_call_id` | Links streamed child messages/chunks to the immediate parent `call_agent` tool call | REUSE | Required to reconstruct parallel/nested delegation | No |
| `chain_path`, depth, and loop checks | Tracks agent/call ancestry, rejects self/ancestor calls and excess depth | REUSE | Existing structural delegation safety must remain intact | No |
| `pantheon.internal.memory.Memory` | Flat message store with metadata, persistence backends, and `execution_context_id` filtering | REUSE / WRAP | Reuse for team short-term memory; add LabBio scope/persistence outside it | No |
| `MemoryManager`, JSON/JSONL backends | Conversation-memory persistence | WRAP | May back scoped short-term conversations, but must not own WorkflowRun/artifacts/Gold Skills | No |
| `TeamPlugin` | Declares toolsets and lifecycle hooks | EXTEND | Phase 3/4 use a LabBio plugin wrapper for policy and trace correlation while keeping Pantheon execution unchanged | No |
| Plugin registry | Creates enabled plugins by priority from settings | REUSE | Existing plugin lifecycle/configuration is sufficient for optional LabBio adapters | No |
| Memory/compression plugins | Retrieval injection and context compression | REUSE / WRAP | Useful for runtime conversation context; must obey artifact exposure and LabBio scope | No |
| `LearningRuntime`, `SkillStore`, `SkillInjector` | Layered project/global/factory skill discovery, parsing, indexing, and file operations | WRAP | Reuse mechanics, add Gold Skill provenance/approval/validation/scope outside upstream | No |
| `SkillToolSet` | LLM tools for list/view/manage plus marketplace operations | IGNORE FOR NOW | Gold Skill lifecycle is later and user-approved; Phase 0 must not enable runtime-created Gold Skills | No |
| `LearningPlugin` auto-extraction | Optionally extracts skills after runs; disabled by default | IGNORE FOR NOW | Automatic extraction does not satisfy validated RunTrace + explicit approval | No |
| `BackgroundTaskManager` and Agent background tools | In-process asyncio task launch/adoption, status, cancellation, and notifications | IGNORE FOR NOW | Not durable workflow state and must not bypass WorkflowEngine; reassess for non-critical capabilities later | No |
| `pantheon.internal.background_agent` | Creates a generic temporary background Agent | IGNORE FOR NOW | Not the deterministic outer workflow and not required by Phase 0 | No |
| `SequentialTeam`, `SwarmTeam`, AAT, MoA | Alternative team orchestration strategies | IGNORE FOR NOW | `PantheonTeam` is the approved stage-level runtime | No |
| `RemoteAgent` | Remote agent transport | IGNORE FOR NOW | Initial design is local Pantheon agents with local Docker data execution | No |
| ChatRoom, REPL, desktop, Claw, UI integrations | User interfaces and application transport | IGNORE FOR NOW | Explicitly outside Phase 0 architecture mapping | No |

## Required runtime traces

### 1. `Agent.run` at the team/tool integration level

1. `_prepare_execution_context` selects the supplied memory or the Agent's memory, converts input, and filters history by `execution_context_id`.
2. `run` wraps step/chunk callbacks. Every emitted child message receives the execution context ID and is optionally written to memory.
3. `AgentRunContext` is installed in a `ContextVar`; injected tools can retrieve the active agent, memory, callbacks, and execution context with `get_current_run_context()`.
4. `_run_stream` renders instructions, calls the model, appends the assistant message, dispatches tool calls in parallel through `_handle_tool_calls`, appends tool messages, and repeats until a final response.
5. `run` converts the final stream result into `AgentResponse`, or returns `AgentTransfer` for team hand-off.

### 2. How `PantheonTeam` adds `list_agents` and `call_agent`

`PantheonTeam.async_setup()` is idempotent. For teams with more than one agent it dynamically registers identically named closures on every agent using `agent.tool`. It adds optional transfer tools first, then `list_agents`, then unified `call_agent`; plugin toolsets and `on_team_created` follow.

`list_agents` always excludes the caller. During a delegated run it derives ancestor names from `_metadata.chain_path` and excludes those ancestors as well.

### 3. Child memory and message projection

`call_agent` creates `Memory(name=f"{target_agent.name}-{execution_context_id}")` and invokes the target with that memory, `use_memory=False`, and `update_memory=True`. This isolates the child's working history from the target Agent's default memory and from sibling invocations.

The target's normal step callback writes to child memory first. The wrapped parent callback then forwards the same child message into the parent's callback, so the parent conversation/event stream also retains a tagged projection. Pantheon therefore combines an isolated child working memory with a flat, tagged parent trace.

### 4. `execution_context_id` creation and propagation

For a first delegation, `_build_execution_context_id` derives a root token from the caller memory ID plus randomness. Nested delegations reuse the root portion of the parent's ID. The format is:

```text
<root>|d<depth>|<target-agent-slug>|<random-4>
```

The ID is placed at the top level of child context variables, passed explicitly to `target_agent.run`, stamped onto child step messages/chunks, and returned in `ResponseDetails`. The parent run context also maps the parent's tool call ID to the child execution ID so the eventual `call_agent` tool-result message is stamped with the child ID.

Root/team messages normally have no execution context ID. `Memory.get_messages(None)` selects only those root messages; `Memory.get_messages(child_id)` selects one child invocation; omitting the argument returns all contexts.

### 5. `parent_tool_call_id` propagation

Agent tool dispatch injects the current tool-call ID into the tool's `context_variables`. The `call_agent` closure captures it and its wrapped callbacks add it as `parent_tool_call_id` to every child step message and chunk. Each nested delegation uses its own immediate `call_agent` ID, while `chain_path` preserves the full ancestry.

### 6. Chain, depth, and loop prevention

The first chain entry is the root agent name. Each delegation appends `<target-agent-name>:<tool-call-id>`. `call_agent` rejects an ancestor target before resolution, `get_target_agent` rejects self-delegation, and `_build_child_context_metadata` performs a second loop/depth check. A root caller encountering a chain left from an earlier turn is treated as a stale chain and reset; a non-root recurrence raises a delegation-loop error.

The configured `max_delegate_depth` is checked from the inherited chain length before the child entry is appended. These semantics should be preserved and covered by LabBio contract tests rather than reimplemented.

### 7. Child result return

After `target_agent.run` completes, `call_agent` submits a child run result to team `on_run_end` hooks, extracts `response.content`, and returns it as the ordinary tool result. Parent `Agent._handle_tool_calls` converts it into a tool message; the current parent LLM loop sees that tool message and can continue reasoning.

The upstream return contract is content-oriented, not a typed child invocation
result. Phase 3's LabBio decorator catches delegation exceptions before
`_handle_tool_calls` converts them to `repr(exception)`, and Phase 4 emits typed
delegation/agent failure events from that record. No Pantheon change is needed.

### 8. Where tool output becomes LLM-visible

`Agent.call_tool` returns the provider/tool result to `_handle_tool_calls`. That method:

- removes `_metadata` from a result dict and merges it into message metadata;
- uses `process_tool_result` to remove fields named by `hidden_to_model` and apply per-tool/global truncation;
- stores the original value as `raw_content` and the processed value as tool-message `content`.

The tool message is appended to `_run_stream` history. On the next model call, `process_messages_for_model` removes `raw_content` and UI/internal fields, so the LLM receives `content`. The unfiltered `raw_content` can still be stored or observed elsewhere. LabBio exposure policy must therefore filter before `Agent.call_tool` returns.

### 9. TeamPlugin lifecycle in actual code

Actual `PantheonTeam` execution order is:

1. `plugin.get_toolsets(team)` during `async_setup`; exceptions are logged and skipped.
2. Declared toolsets are injected into selected agents.
3. `plugin.on_team_created(team)` after setup.
4. `plugin.on_run_start(team, msg, context)` before the active Agent; a non-`None` result replaces input.
5. `plugin.on_run_end(team, result)` after a main-agent response and separately after a successful delegated child response.

`pre_compression` is invoked by `CompressionPlugin`, not directly by the team. Although `post_compression`, `on_tool_call`, and `on_shutdown` are declared on `TeamPlugin`, no direct PantheonTeam dispatch for them was found in the inspected source. Per-LLM/tool behavior is instead explicitly supported by agent hook lists registered during `get_toolsets`. Future code must not assume a declared hook fires unless its caller is verified.

### 10. Existing skill index/view/update path

`LearningRuntime.initialize` creates a layered `SkillStore` for project, global, and packaged factory skills. `scan_headers` reads frontmatter and applies first-layer-wins shadowing; `SkillInjector.build_skill_index` formats and caches an index. `LearningPlugin.on_team_created` appends bounded guidance/index text to each agent's instructions.

`SkillToolSet` exposes:

- `skill_list` -> `SkillStore.scan_headers`;
- `skill_view` -> `load_skill` or controlled supporting-file reads;
- `skill_manage` -> validated atomic create/update/patch/delete operations followed by index-cache invalidation.

Optional automatic extraction runs from `LearningPlugin.on_run_end` only when configured. It is not an approval workflow and will not be treated as Gold Skill publication.

## Baseline test record

### Commands and results

1. Import/version:

```bash
conda run -n labbioagent python -c 'import pantheon; print(pantheon.__version__)'
```

Result: passed, `0.6.4`.

2. Collection:

```bash
conda run -n labbioagent python -m pytest --collect-only -q
```

Result: `1925 tests collected in 1.28s`; warnings included an unregistered `timeout` mark because `pytest-timeout` is not installed in this environment.

3. Full suite:

```bash
conda run -n labbioagent python -m pytest -q
```

Result: no final pass/fail summary. The process remained alive with output buffered for more than five minutes and was manually interrupted. The full collection contains live API, E2E, remote, gateway, and language-tool tests; no effort was made to repair or reconfigure them.

4. Representative offline architecture subset: background, memory properties, learning system, memory system, plugin registry, local provider, tool pairing, seven mocked/non-live Agent tests, and five non-live delegation tests.

Result: `364 passed, 6 failed, 1 warning in 8.95s`.

All six failures were in `TestAgentBackgroundIntegration` schema tests. The shared synchronous helper called `asyncio.get_event_loop()` after preceding async tests had closed/unset the current loop, causing `RuntimeError: There is no current event loop in thread 'MainThread'` before product assertions ran. Running that class alone produced `9 passed, 1 warning in 0.74s`, confirming test-order/event-loop isolation rather than a demonstrated background-tool behavior failure.

### Baseline interpretation

- Pantheon package import and the selected architecture contracts are usable in `labbioagent`.
- The six representative-subset failures predate all LabBio changes; Phase 0 changed documentation only.
- The full suite is not currently a bounded offline baseline because it mixes local tests with live/integration tests and lacks the configured timeout plugin in this environment.
- No dependency, environment, upstream source, Docker, or R changes were made.
