# Stage Runtime Contracts

## Outer boundary

Native reports now require an explicit `delivery_artifact_ids` list, independent
of observed-scope citations. The Agent selects registered execution outputs;
the host derives an `ARTIFACT_SET_V1` manifest with byte hashes, producer identity,
and exact original programs. Selection grants no reading credit, remote release,
or scientific approval. `[]` intentionally selects no files. `report_check`
and `report_submit` validate the same sources; failures identify the selection
index and a safe reason, never substitute a file. The selected saved report owns
the immutable result set used by local export and default `revise`; both recheck
integrity. Explicit imports keep their original producer and require the original
program among authorized inputs. The primary execution receipt remains a separate
summary, not the complete delivery set. Legacy reports without a manifest retain
their original single-execution interpretation and are not rewritten.

`StageRuntimeInvoker` is the only runtime port used by the application. It
receives a trusted `RuntimeStageInput` and returns a typed
`RuntimeStageResult`. The invoker may reason, use allowed capabilities, and
delegate inside the stage, but it cannot mutate `WorkflowRun`.

`RuntimeCoordinator` validates the returned proposal, evidence identity,
grounding, and legal next action before `WorkflowEngine` changes state.

`execution_submit` accepts exactly one program source: literal `script_content`,
or `source_revision` containing an explicitly imported original program's
`source_artifact_id`, `expected_script_hash`, and ordered exact `old`/`new` edits.
The host rechecks the current input scope, workspace, original-program identity,
stored bytes and whole-source hash. Each old string must match exactly once at
that edit's position in the sequence; no fuzzy edit or repair is inferred.
Source mode requires explicit current image, resources, input IDs, parameters
and output declarations. Adopting source does not mount it implicitly or inherit
its historical settings or receipt. The materialized complete program enters
the normal submission, syntax, preflight, execution and output-validation path
with a fresh execution identity, even when the edit list is empty.

The durable binding and planned trace retain only verified source IDs, hash and
edit count as `source_revision_origin`; original and new programs stay separate.
Source and replacement text are not copied into public evidence or traces.
`execution_inspect` can read the exact authorized imported original using its
`producer_execution_id`; imported inspection may have null receipt/contract.
`execution_revise` keeps its same-run original-submission semantics. Neither
source inspection nor a historical successful receipt supplies a current
execution or scientific validation; the existing fresh-receipt rules remain.

## Identity

Keep these identities distinct:

| Identity | Scope |
|---|---|
| `thread_id` | long-lived user/project conversation |
| `workflow_run_id` | one complete task attempt |
| `kernel_run_id` | one stage runtime execution |
| `task_id` | one Leader-authored collaboration task |
| `invocation_id` | one Leader or Specialist Agent loop |
| `tool_invocation_id` | one governed capability call |

Child events retain root run, parent invocation, parent tool call, depth, and
chain path. Correlation identity never grants authority.

## Provider contract

Completed capability calls are not necessarily successful program executions.
`retained_metadata` preserves host-derived receipt IDs/status/exit codes and
observation-page state without scientific content. Working-view directories use
`capability_request_succeeded`, not `business_success`, and distinguish actual
current-invocation delivery from an unread reference. Archiving does not revoke
prior delivery; directory metadata grants no new reading or whole-Artifact credit.
New actors/invocations still require authorized delivery of the original sources.

Agent-selected handoffs are aggregates, not single tool responses. Selections up
to the defensive 1 MiB aggregate bound can be delivered in ordered, SHA256-bound
exact fragments, each below the existing message bound. No selected body is
dropped or summarized. Actual request admission still counts every fragment,
prompt, schema, control and wire framing against the unchanged configured limit;
`READY` for a saved selection is not whole-request admission.

Execution metadata also preserves the complete host-verified output UUID/filename
inventory within 4096 bytes. If too large, it explicitly marks the full inventory
archived and retains the count/status, without selecting a subset. Scoped
`artifact_list` remains optional and explicitly granted, including for result
owners and read-only peers; it grants no content-reading credit. Repeat guards
revalidate current state, and compare only the delivered no-result episode since
the last completed delivered operation. Undelivered same-batch results do not
reset that episode. Consecutive unchanged hard errors still stop; returning to a
local error after intervening delivered work does not invalidate that work.

When an Agent-authored handoff is mandatory, the unchanged shared total of 64
tool/evidence slots includes two reserved completion-control slots. Ordinary
work stops admitting new calls at its remaining-work boundary; only actually
granted closeout tools remain exposed. Admission is atomic and rejected work
does not consume the reserved slots. The Agent still selects its own evidence,
limitations and legal next action. Nothing is auto-filled or retried. Token,
time and provider-turn bounds remain independently enforced; a reserve is not
a guarantee of scientific or overall completion.

Children reserve a bounded public return within their remaining investigation
allocation: one provider turn and one third of available model time/known-token
headroom above the inherited ancestor boundary. Return mode exposes no tools;
invalid calls execute nothing and cannot consume the parent's completion reserve.
The child authors its partial assessment and unread scope. A returned assessment
is not a scientific approval or inherited source-reading credit.

Mandatory Native handoff also allocates completion capacity within the same
shared totals: one third of model time and known-token allowance, and at most
three provider turns. A 900-second phase has at most 600 seconds of investigation
and 300 seconds to complete; governed tool duration remains separately bounded.
Work requests cannot consume the reserved model time. An incomplete timed-out
generation dispatches no tools, its unknown usage stays unknown, and completion
uses only existing history and actually granted control tools. Root closeout and
child public-return requests use an independent output ceiling of
min(8192, the profile output ceiling). Normal investigation and program generation
retain their profile output allowance (32768 in the default local profile).
The smaller closeout bound is sent before generation, not applied by trimming a
returned response. A smaller explicit model ceiling is never enlarged.
This changes per-response admission, not shared time/token/turn budgets or retries.
Token headroom is admission control, not a prediction of
the next request's unknown input usage; incurred over-limit usage is never erased.
Children stop with their exact reason rather than spending the parent's reserve.
REPORT can still use its granted `report_submit`; required execution/submission
is not waived. The finalizer remains tool-free and cannot invent a missing handoff.

A provider's typed request-deadline signal can precede the exact monotonic
boundary because the HTTP layer reserves diagnostic cleanup time. If this is a
reduced work request and the original total deadline still permits completion,
the loop enters the existing reserve without adding time or replaying tools.
An early unrelated transport/protocol error is not a deadline signal and does
not admit another generation. Ancestor completion boundaries remain protected.

Safe Kernel events are projected as they occur, including usage, request size,
closeout and delegation metadata. Final projection remains mandatory: original
kernel event identity prevents duplicate accounting and preserves delegation
receipt IDs. Failed live writes are not marked saved. Projection state is scoped
to a stage invocation and released on completion, failure or cancellation.

`ModelProvider.generate(ModelRequest) -> ModelTurn` uses an exact trusted
provider/model binding. Requests contain bounded public messages, strict tool
schemas, response mode, limits, deadline, and correlation identity. Turns
contain public content, normalized tool calls, finish reason, usage when known,
and safe provider observations.

The runtime does not silently switch provider, model, protocol, endpoint, or
thinking mode. Missing usage is unknown rather than zero. Truncation, filtering,
malformed messages, and binding mismatch are explicit failures.

Tool-argument and finalizer validation share schema-derived field-path
projection. Declared fields/array positions are retained when identifiable;
unknown input keys are labelled `unknown_field`, not echoed or converted into
declared parameters. The complete path must satisfy the feedback consumer's
Identifier bound, so malformed input cannot crash its own rejection feedback.

## Capability mode

Capabilities are assembled from a trusted stage profile. Before dispatch:

1. the complete tool-call batch is parsed;
2. top-level arguments must be one strict JSON object;
3. duplicate keys, non-finite values, unknown fields, and schema violations are
   rejected;
4. batch count, policy, budget, and interruption are checked;
5. only then may foreground side effects begin.

Trusted identity and scope fields never come from model arguments. Each result
is projected once into a bounded observation and a typed evidence item. Failed
calls remain evidence and are not rewritten as successes.

Kernel batch rejection happens before a governed capability is invoked. Its
value-free reason, schema field paths and original event IDs are retained
separately in host-owned `tool_rejections`, including stages without a prepared
handoff. Identical rejection shapes are grouped; later working views carry
exact rejected-batch counts and source result IDs, not invalid arguments or
repeated error prose. Original event IDs remain readable in the archived result.
These are `CONTROL_STATE`, not scientific failures or executed processes. They
participate in the same finalization capacity check as the actual finalizer.

Repeated non-retryable requests may converge only after the canonical current
validator runs and the unchanged failure has already been delivered to the
Agent. A changed request or changed authority is evaluated normally.

An authorized `TOP_N` reread is a legal query, not a pagination failure.
Each call revalidates access, parameters and transport bounds and creates its
own actual page receipt. The returned cursor describes that page, not enforced
monotonic progress. Repeated partial pages do not establish full-table coverage;
all ordinary invocation budgets still apply.

Completion feedback has two different meanings. Ordinary text is a follow-up
to an accepted answer and retains its public/private provider continuity.
`CompletionRejection` is an explicit host admission failure: its candidate is
not committed as an accepted Assistant answer. The original task and exact host
feedback remain, without a task rewrite, keyword filter or automatic tool call.

Three consecutive identical rejected candidates with identical feedback and
unchanged tool-result count stop with `COMPLETION_NO_PROGRESS`. Different
assessments, changed controls or new tool work do not satisfy that condition.
This does not reduce normal investigation/code-generation budgets. The last
provider turn still checks completion; rejected work at that boundary retains
`PROVIDER_TURN_LIMIT`, not a fabricated successful completion. Actual observed
usage and the stopped public candidate remain recorded; unknown usage stays
unknown. Unconfirmed effects remain blocked and take precedence.

## Collaboration

The Leader may call `list_specialists` and `delegate`. Trusted configuration
defines eligible peers and output ownership; the model chooses whether and whom
to delegate to. The host enforces self, ancestor, loop, depth, target, task,
budget, interruption, and child-failure rules.

A child receives isolated working context and only the capabilities allowed for
that invocation. Its typed result, public events, and actor-attributed evidence
return to the parent; private reasoning does not.

Local profile `peers` explicitly supplies each peer's `profile_key`,
`capabilities` and `capability_protocol`. A peer may have its own prompt values;
omitting them at the native assembly boundary preserves the legacy shared prompt.
The bundled local-v13 profile exposes an optional read-only methods reviewer in
VALIDATE and REPORT. Discovery and delegation remain Agent decisions. No peer
receives execution, environment-build, report-submit or workflow-handoff authority.
New computational checks return through the existing VALIDATE→EXECUTE transition;
the execution owner chooses, writes and runs them, followed by another review.

Original typed public delegation outcomes are retained by the host in the evidence
bundle, stage result and prior-result history, including status, exact public
content and stop/cancellation identity. These fields are not writable by the
finalizer. Working views and tool-free finalization carry only a directory;
`stage_context_read` can later read the original public advice by source result ID.
Advice is not scientific evidence, cannot override current user/control input,
and never transfers the peer's source-reading credentials to the parent. Private
reasoning is not retained. Old profiles without peers remain root-only.

## Finalization mode

Finalization token capacity is independent of its two-attempt limit. Trusted
profiles may set `max_finalization_tokens`; omitted values retain 65,536.
Input and output usage from every attempt, including rejected responses, is
charged. The bundled local-v12 profile reallocates 65,536 tokens from capability
to finalization in EXECUTE/VALIDATE/INTERPRET/REPORT: 2,031,616 + 131,072 still
equals the former 2,097,152 + 65,536. Other stage/resource limits are unchanged.
Budget failures retain the dimension, limit and actual usage, not just a
generic invocation failure. Token limits are not byte admission or reservations.

`handoff_status` exposes exact per-item handoff bytes before selection, for
already-delivered originals that remain authorized. The catalog carries no
bodies or additional read credit and does not select evidence. Its sizes omit
the Agent's assessment, limitations, decision and framing; final admission is
still checked against the complete request. Revoked sources expose no size.

After capability mode ends, the host freezes the exact evidence bundle and
starts a separate tool-free finalizer. The finalizer returns the stage body,
next-action proposal, reasons, unresolved items, and evidence references under
the configured response schema.

For a new controlled capability invocation with `prepare_handoff`, the
investigating Agent submits its own typed `next_action` with the selected
observations. The tool schema and host reuse the current legal-action format.
The host binds the proposal to the run, stage, invocation and authoritative
user/control input; it does not infer an action from prose or choose one.

When that context is unchanged, the finalizer must preserve the exact action,
including its reason and question. Both the response schema and local validator
enforce this. Disagreement uses the existing correction budget (at most two
finalizer attempts) with safe error type and field paths, never local replacement
or default continuation. A missing decision in a new-protocol bundle is
`HANDOFF_REQUIRED` before a finalizer request, not permission to choose again.
The proposal still requires Coordinator/WorkflowEngine authorization.

New authoritative instructions, clarification answers or workflow control
supersede the archived proposal; finalization uses the current legal-action
contract without replaying capabilities or rewriting the original evidence.
Historical bundles without the new context marker retain their old semantics;
this compatibility path is not used for new controlled handoffs.

Finalization cannot replay capabilities or invent evidence. Bounded schema or
eligible truncation corrections use the frozen input and current schema; the
host never repairs JSON or fills scientific decisions locally. Required user
questions and Agent-reported blockers remain legal actions, not filtered prose.

## Execution reference identity

EXECUTE `references` and `body.issue_references` retain resource identity in both
directions: an execution ID must use `EXECUTION`, and an `EXECUTION` reference
must be one of the supplied real execution IDs. Capability observation UUIDs
are not execution UUIDs. The response schema enumerates actual execution IDs;
the host rechecks the binding without substituting IDs. Control also carries
the exact mechanical receipt issue codes, not model-written issue messages.

## Bounded working context

The kernel may replace old, completed and durably archived tool batches with
explicit observation references only when `evidence_read` is available. The
latest batch and non-rereadable content remain visible. Original observations
are never rewritten, summarized or deleted by this operation. Automatic batch
replacement is triggered only by measured public/provider-wire request size
above the declared byte limit and stops once the request fits.

Observed input-token cost across the remaining provider-turn allowance remains
a budget-risk signal in request audit; it does not trigger batch replacement.
It is not a tokenizer, a promise to use all remaining turns, or an estimate of
actual future charges. Unknown usage is not filled in. Actual usage, deadlines,
permissions and hard limits remain unchanged; the Agent chooses what to reread
and what scientific action to take. Request audit records the observed input,
remaining turns, risk flag and count of replaced batches.

## Cross-stage working context

The complete stage results remain persisted. A `RuntimeStageResult` may also
carry host-only `handoff_context`: the investigating Agent's original summary,
limitations and historical action, its capability stop reason, and the exact
selected observation identities/hashes. This is not a provider-writable field
in `RuntimeStageProposal`. Model-authored intent remains `MODEL_CONTEXT`.

Profiles explicitly opting into `stage_context_read` receive a bounded working
projection: current user/control/execution state, the latest original Agent
handoff, and an exact-ID directory of old stage results instead of their
repeated prose. `stage_context_read` pages the unchanged prior model context;
it does not read source evidence or grant report credit. Legacy profiles retain
their old prose projection, without the new host-only metadata.

Selected original observations are loaded from the existing immutable store,
checked against their hashes and current user/project/run/source permissions,
and supplied as separate messages. `evidence_read` is still required. Execution
history uses current `execution_inspect` read authority, not permission to submit
or revise. A changed Artifact-list exposure snapshot is not silently reused.
Unavailable bodies are explicitly unavailable; summaries never substitute for
them. Original per-item authority and historical source identities remain.

Loading or preparing messages grants no read credit. Only a completed provider
turn consuming those exact messages can acknowledge them to the current
invocation. Partial table views retain only their actual scope; no previous
Agent's full-read mark is inherited. Cancellation and uncertain provider
completion grant no credit. Handoff selection itself remains Agent-owned.

With a current prepared handoff, tool-free finalization uses that current
assessment/decision and its selected originals, not the preceding stage's
superseded prose. Earlier history is not destroyed, and missing evidence never
causes a default continue or an inferred scientific conclusion.

`working_context` records preparation sizes/IDs; `context_delivery` records
acknowledged IDs. These are different events. Neither logs private reasoning nor
proves scientific validity or zero-error performance.

## Structured output rejection feedback

`output_issues[].validation`, when present, carries the output validator's own
mechanical constraint: `SCALAR_REQUIRED` or `STRING_TOO_LONG`, a bounded safe
host-declared field name and the applicable string-length limit. It propagates
unchanged through registration, execution receipts, trace and archived public
observations. It contains no rejected values or unknown record keys; a field
name that cannot satisfy this diagnostic contract is omitted, not truncated.
Missing diagnostics do not prove conformity. Rejected outputs retain RAW
classification, and legacy issues without this optional object remain valid.
The host does not coerce, truncate or repair the Agent's output.

## Stage responsibilities

| Stage | Agent responsibility | Host responsibility |
|---|---|---|
| INTAKE | understand the request and missing facts | bind identity and registered inputs |
| UNDERSTAND | form a task understanding | enforce exposure and validate structure |
| PLAN | choose scientific approach and collaboration | validate proposal and permissions |
| PREFLIGHT | identify required execution assumptions | enforce trusted environment and policy |
| EXECUTE | author and revise the analysis program | execute in Docker and register evidence |
| VALIDATE | assess produced evidence and limitations | preserve immutable receipts and checks |
| INTERPRET | interpret allowed derived results | enforce evidence scope and claim boundary |
| REPORT | author the report and citations | validate references and export exact content |
| LEARN | propose reusable knowledge | require governed review before persistence |

`USER_GATE`, workflow retry, run completion, and production release are host
control operations, never model-owned stage transitions.

## Recovery invariants

- Runtime revision is frozen for a run.
- A terminal predecessor is never reopened or mutated.
- In-flight external effects fail closed until reconciled.
- Capability evidence and finalization results persist before the next action.
- A successful execution is selected by persisted identity, not recency.
- Workflow completion is not proof of scientific correctness.
