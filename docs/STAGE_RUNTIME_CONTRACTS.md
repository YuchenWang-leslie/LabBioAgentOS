# Stage Runtime Contracts

## Outer boundary

`StageRuntimeInvoker` is the only runtime port used by the application. It
receives a trusted `RuntimeStageInput` and returns a typed
`RuntimeStageResult`. The invoker may reason, use allowed capabilities, and
delegate inside the stage, but it cannot mutate `WorkflowRun`.

`RuntimeCoordinator` validates the returned proposal, evidence identity,
grounding, and legal next action before `WorkflowEngine` changes state.

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

`ModelProvider.generate(ModelRequest) -> ModelTurn` uses an exact trusted
provider/model binding. Requests contain bounded public messages, strict tool
schemas, response mode, limits, deadline, and correlation identity. Turns
contain public content, normalized tool calls, finish reason, usage when known,
and safe provider observations.

The runtime does not silently switch provider, model, protocol, endpoint, or
thinking mode. Missing usage is unknown rather than zero. Truncation, filtering,
malformed messages, and binding mismatch are explicit failures.

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

Repeated non-retryable requests may converge only after the canonical current
validator runs and the unchanged failure has already been delivered to the
Agent. A changed request or changed authority is evaluated normally.

## Collaboration

The Leader may call `list_specialists` and `delegate`. Trusted configuration
defines eligible peers and output ownership; the model chooses whether and whom
to delegate to. The host enforces self, ancestor, loop, depth, target, task,
budget, interruption, and child-failure rules.

A child receives isolated working context and only the capabilities allowed for
that invocation. Its typed result, public events, and actor-attributed evidence
return to the parent; private reasoning does not.

## Finalization mode

After capability mode ends, the host freezes the exact evidence bundle and
starts a separate tool-free finalizer. The finalizer returns the stage body,
next-action proposal, reasons, unresolved items, and evidence references under
the configured response schema.

Finalization cannot replay capabilities or invent evidence. One bounded
correction may be allowed for a clearly truncated structured response; it uses
the same frozen input and schema and does not repair JSON locally.

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
