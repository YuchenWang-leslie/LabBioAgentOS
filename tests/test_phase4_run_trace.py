"""Phase 4 acceptance tests for append-only unified RunTrace events."""

from __future__ import annotations

import json
from contextlib import contextmanager
from types import MethodType
from uuid import UUID, uuid4

import pytest
from pantheon.agent import Agent, AgentResponse, AgentRunContext, _RUN_CONTEXT
from pantheon.team import PantheonTeam
from pydantic import ValidationError

from labbioagentos import (
    InMemoryDelegationPolicy,
    InMemoryTraceSink,
    InstructionKind,
    InstructionRecord,
    JsonlTraceSink,
    PantheonStageAdapter,
    RunStatus,
    RunTraceRecorder,
    StageContext,
    StageInvocationError,
    TraceEventType,
    TraceSink,
    TraceSinkError,
    UserDecision,
    WorkflowDefinition,
    WorkflowEngine,
    WorkflowStage,
    WorkflowTransition,
    default_workflow_definition,
    project_run_trace,
)


def _trace_pair():
    sink = InMemoryTraceSink()
    return sink, RunTraceRecorder(sink)


def _mock_agent(name: str) -> Agent:
    return Agent(
        name=name,
        description=f"Mock {name} capability",
        instructions="Use deterministic mock behavior only.",
        model="openai/mock-model",
    )


@contextmanager
def _agent_context(agent: Agent, kwargs: dict, execution_context_id=None):
    token = _RUN_CONTEXT.set(
        AgentRunContext(
            agent=agent,
            memory=kwargs.get("memory"),
            execution_context_id=execution_context_id,
            process_step_message=kwargs.get("process_step_message"),
            process_chunk=kwargs.get("process_chunk"),
        )
    )
    try:
        yield
    finally:
        _RUN_CONTEXT.reset(token)


async def _run_traced_stage(
    recorder: RunTraceRecorder,
    run_id: UUID,
    *,
    nested: bool = False,
    deny_specialist: bool = False,
    fail_reviewer: bool = False,
    instruction_record: InstructionRecord | None = None,
):
    planner = _mock_agent("planner")
    specialist = _mock_agent("specialist")
    reviewer = _mock_agent("reviewer")
    team = PantheonTeam(
        agents=[planner, specialist, reviewer],
        use_summary=False,
    )
    observed: dict = {
        "specialist_calls": 0,
        "reviewer_calls": 0,
    }

    async def reviewer_run(self, message, **kwargs):
        observed["reviewer_calls"] += 1
        observed["reviewer_kwargs"] = kwargs
        if fail_reviewer:
            raise RuntimeError("mock reviewer execution failure")
        await kwargs["process_step_message"](
            {"role": "assistant", "content": "mock reviewer step"}
        )
        return AgentResponse(
            agent_name=self.name,
            content="reviewer completed",
            details=None,
        )

    async def specialist_run(self, message, **kwargs):
        observed["specialist_calls"] += 1
        observed["specialist_kwargs"] = kwargs
        if nested:
            with _agent_context(
                self,
                kwargs,
                execution_context_id=kwargs["execution_context_id"],
            ):
                child_context = dict(kwargs["context_variables"])
                child_context["tool_call_id"] = "call-reviewer"
                observed["reviewer_result"] = await self.functions["call_agent"](
                    agent_name="reviewer",
                    instruction="Perform the nested mock review.",
                    context_variables=child_context,
                )
        await kwargs["process_step_message"](
            {"role": "assistant", "content": "mock specialist step"}
        )
        return AgentResponse(
            agent_name=self.name,
            content="specialist completed",
            details=None,
        )

    async def planner_run(self, message, **kwargs):
        observed["planner_kwargs"] = kwargs
        with _agent_context(self, kwargs):
            call_context = dict(kwargs["context_variables"])
            call_context["tool_call_id"] = "call-specialist"
            observed["specialist_result"] = await self.functions["call_agent"](
                agent_name="specialist",
                instruction="Perform the selected mock specialist task.",
                context_variables=call_context,
            )
        return AgentResponse(
            agent_name=self.name,
            content={
                "stage": "PLAN",
                "summary": "Mock traced stage completed.",
                "payload": {"result_ref": "mock-result-1"},
            },
            details=None,
        )

    planner.run = MethodType(planner_run, planner)
    specialist.run = MethodType(specialist_run, specialist)
    reviewer.run = MethodType(reviewer_run, reviewer)
    policy = InMemoryDelegationPolicy(
        {
            "planner": set() if deny_specialist else {"specialist"},
            "specialist": {"reviewer"},
        }
    )
    adapter = PantheonStageAdapter(
        team,
        delegation_policy=policy,
        trace_recorder=recorder,
    )
    context = StageContext(
        run_id=run_id,
        stage=WorkflowStage.PLAN,
        instruction="Execute the sanitized mock stage instruction.",
        metadata={"fixture": "phase-4"},
    )
    result = await adapter.run_stage(
        context,
        instruction_record=instruction_record,
    )
    return result, observed


def test_workflow_run_emits_ordered_events_and_reconstructs_stage_path():
    sink, recorder = _trace_pair()
    engine = WorkflowEngine(
        default_workflow_definition(),
        trace_recorder=recorder,
    )
    run = engine.create_run()
    engine.start(run)
    for target in (
        WorkflowStage.UNDERSTAND,
        WorkflowStage.PLAN,
        WorkflowStage.PREFLIGHT,
        WorkflowStage.EXECUTE,
        WorkflowStage.VALIDATE,
        WorkflowStage.INTERPRET,
        WorkflowStage.REPORT,
        WorkflowStage.LEARN,
    ):
        engine.transition(run, target)
    engine.complete(run)

    events = sink.read(run.run_id)
    assert [event.sequence for event in events] == list(range(len(events)))
    assert all(event.timestamp.utcoffset().total_seconds() == 0 for event in events)
    assert events[0].event_type is TraceEventType.RUN_CREATED
    assert events[-1].event_type is TraceEventType.RUN_COMPLETED
    projection = project_run_trace(events)
    assert projection.stage_path == (
        WorkflowStage.INTAKE,
        WorkflowStage.UNDERSTAND,
        WorkflowStage.PLAN,
        WorkflowStage.PREFLIGHT,
        WorkflowStage.EXECUTE,
        WorkflowStage.VALIDATE,
        WorkflowStage.INTERPRET,
        WorkflowStage.REPORT,
        WorkflowStage.LEARN,
    )
    assert run.status is RunStatus.COMPLETED


def test_user_gate_resume_and_retry_are_traceable():
    definition = WorkflowDefinition(
        workflow_id="trace-gate-fixture",
        nodes=frozenset(
            {WorkflowStage.INTAKE, WorkflowStage.USER_GATE, WorkflowStage.PLAN}
        ),
        allowed_transitions=frozenset(
            {
                WorkflowTransition(
                    source=WorkflowStage.INTAKE,
                    target=WorkflowStage.USER_GATE,
                ),
                WorkflowTransition(
                    source=WorkflowStage.USER_GATE,
                    target=WorkflowStage.PLAN,
                ),
            }
        ),
        initial_stage=WorkflowStage.INTAKE,
        terminal_stages=frozenset({WorkflowStage.PLAN}),
    )
    sink, recorder = _trace_pair()
    engine = WorkflowEngine(definition, trace_recorder=recorder)
    run = engine.create_run(retry_limit=1)
    engine.start(run)
    engine.retry(run)
    engine.pause_for_user(run, "Approve the mock continuation?")
    gate_id = run.pending_user_gate.gate_id
    engine.resume(
        run,
        UserDecision(gate_id=gate_id, target_stage=WorkflowStage.PLAN),
    )

    events = sink.read(run.run_id)
    event_types = [event.event_type for event in events]
    assert TraceEventType.RETRY_STARTED in event_types
    assert TraceEventType.USER_GATE_ENTERED in event_types
    assert TraceEventType.USER_GATE_RESUMED in event_types
    projection = project_run_trace(events)
    assert len(projection.retries) == 1
    assert projection.stage_path == (
        WorkflowStage.INTAKE,
        WorkflowStage.USER_GATE,
        WorkflowStage.PLAN,
    )


def test_workflow_failure_and_cancellation_emit_terminal_events():
    sink, recorder = _trace_pair()
    engine = WorkflowEngine(
        default_workflow_definition(),
        trace_recorder=recorder,
    )
    failed = engine.create_run()
    engine.start(failed)
    engine.fail(failed, "mock workflow failure")
    cancelled = engine.create_run()
    engine.start(cancelled)
    engine.cancel(cancelled, "mock cancellation")

    assert [event.event_type for event in sink.read(failed.run_id)][-2:] == [
        TraceEventType.STAGE_FAILED,
        TraceEventType.RUN_FAILED,
    ]
    assert sink.read(cancelled.run_id)[-1].event_type is TraceEventType.RUN_CANCELLED


@pytest.mark.asyncio
async def test_unified_workflow_and_stage_trace_reconstructs_parent_child():
    sink, recorder = _trace_pair()
    engine = WorkflowEngine(
        default_workflow_definition(),
        trace_recorder=recorder,
    )
    run = engine.create_run()
    engine.start(run)
    engine.transition(run, WorkflowStage.UNDERSTAND)
    engine.transition(run, WorkflowStage.PLAN)
    result, observed = await _run_traced_stage(recorder, run.run_id)
    engine.record_stage_result(run, result)

    projection = project_run_trace(sink.read(run.run_id))
    root = next(item for item in projection.invocations if item.agent_name == "planner")
    specialist = next(
        item for item in projection.invocations if item.agent_name == "specialist"
    )
    assert root.parent_invocation_id is None
    assert specialist.parent_invocation_id == root.invocation_id
    assert projection.invocation_children(root.invocation_id) == (specialist,)
    assert projection.delegations[0].caller == "planner"
    assert projection.delegations[0].target == "specialist"
    assert projection.delegations[0].invocation_id == specialist.invocation_id
    assert observed["specialist_calls"] == 1
    assert any(
        event.event_type is TraceEventType.RESULT_RECORDED
        and event.payload["result"]["payload"]["result_ref"] == "mock-result-1"
        for event in sink.read(run.run_id)
    )


@pytest.mark.asyncio
async def test_nested_delegation_tree_and_pantheon_metadata_are_preserved():
    sink, recorder = _trace_pair()
    run_id = uuid4()
    result, observed = await _run_traced_stage(
        recorder,
        run_id,
        nested=True,
    )

    projection = project_run_trace(sink.read(run_id))
    by_agent = {item.agent_name: item for item in projection.invocations}
    assert by_agent["specialist"].parent_invocation_id == by_agent["planner"].invocation_id
    assert by_agent["reviewer"].parent_invocation_id == by_agent["specialist"].invocation_id
    assert [item.target for item in projection.delegations] == [
        "specialist",
        "reviewer",
    ]
    specialist = by_agent["specialist"]
    assert specialist.execution_context_id == result.delegations[-1].execution_context_id
    assert specialist.parent_tool_call_id == "call-specialist"
    assert specialist.chain_path == (
        "planner",
        "specialist:call-specialist",
    )
    reviewer = by_agent["reviewer"]
    assert reviewer.execution_context_id == result.delegations[0].execution_context_id
    assert reviewer.parent_tool_call_id == "call-reviewer"
    assert reviewer.chain_path[-1] == "reviewer:call-reviewer"
    assert observed["reviewer_calls"] == 1


@pytest.mark.asyncio
async def test_delegation_denial_is_structural_and_has_no_child_agent_start():
    sink, recorder = _trace_pair()
    run_id = uuid4()
    result, observed = await _run_traced_stage(
        recorder,
        run_id,
        deny_specialist=True,
    )

    projection = project_run_trace(sink.read(run_id))
    assert observed["specialist_calls"] == 0
    assert len(projection.delegations) == 1
    assert projection.delegations[0].status == "DENIED"
    assert projection.delegations[0].target == "specialist"
    assert [item.agent_name for item in projection.invocations] == ["planner"]
    assert result.delegations[0].invocation_id == projection.delegations[0].invocation_id


@pytest.mark.asyncio
async def test_nested_child_failure_is_structural_in_projection():
    sink, recorder = _trace_pair()
    run_id = uuid4()
    result, observed = await _run_traced_stage(
        recorder,
        run_id,
        nested=True,
        fail_reviewer=True,
    )

    projection = project_run_trace(sink.read(run_id))
    failed_delegation = next(
        item for item in projection.delegations if item.target == "reviewer"
    )
    failed_invocation = next(
        item for item in projection.invocations if item.agent_name == "reviewer"
    )
    assert failed_delegation.status == "FAILED"
    assert failed_invocation.status == "FAILED"
    assert failed_delegation.execution_context_id is not None
    assert failed_delegation.parent_tool_call_id == "call-reviewer"
    assert any(
        event.event_type is TraceEventType.DELEGATION_FAILED
        and event.payload["error_type"] == "RuntimeError"
        and event.payload["error_message"] == "mock reviewer execution failure"
        for event in projection.failures
    )
    assert observed["reviewer_result"]["labbio_delegation"]["outcome"] == "FAILED"
    assert any(record.target == "reviewer" for record in result.delegations)


@pytest.mark.asyncio
async def test_stage_invocation_failure_emits_agent_and_stage_failures():
    sink, recorder = _trace_pair()
    failing = _mock_agent("planner")

    async def fail_run(self, message, **kwargs):
        raise RuntimeError("mock root agent failure")

    failing.run = MethodType(fail_run, failing)
    adapter = PantheonStageAdapter(
        PantheonTeam(agents=[failing]),
        trace_recorder=recorder,
    )
    context = StageContext(
        run_id=uuid4(),
        stage=WorkflowStage.PLAN,
        instruction="Raise the mock stage failure.",
    )

    with pytest.raises(StageInvocationError):
        await adapter.run_stage(context)

    projection = project_run_trace(sink.read(context.run_id))
    assert projection.invocations[0].status == "FAILED"
    assert [event.event_type for event in projection.failures] == [
        TraceEventType.AGENT_FAILED,
        TraceEventType.STAGE_FAILED,
    ]


@pytest.mark.asyncio
async def test_instruction_record_links_template_metadata_to_root_invocation():
    sink, recorder = _trace_pair()
    run_id = uuid4()
    record = InstructionRecord(
        run_id=run_id,
        stage_id=WorkflowStage.PLAN,
        kind=InstructionKind.STAGE,
        template_id="mock-stage-template",
        template_version="1.0",
        template_hash="sha256:mock",
        sanitized_rendered_instruction="Sanitized mock rendered instruction.",
    )

    await _run_traced_stage(
        recorder,
        run_id,
        instruction_record=record,
    )

    projection = project_run_trace(sink.read(run_id))
    traced = next(
        item for item in projection.instructions if item.instruction_id == record.instruction_id
    )
    root = next(item for item in projection.invocations if item.agent_name == "planner")
    assert traced.stage_id is WorkflowStage.PLAN
    assert traced.invocation_id == root.invocation_id
    assert traced.template_id == "mock-stage-template"
    assert traced.sanitized_rendered_instruction == (
        "Sanitized mock rendered instruction."
    )


def test_jsonl_sink_is_append_only_valid_json_and_reloads_order(tmp_path):
    path = tmp_path / "trace" / "mock-run.jsonl"
    sink = JsonlTraceSink(path)
    recorder = RunTraceRecorder(sink)
    run_id = uuid4()
    first = recorder.emit(run_id, TraceEventType.RUN_CREATED, status="CREATED")
    second = recorder.emit(run_id, TraceEventType.RUN_STARTED, status="RUNNING")

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert all(isinstance(json.loads(line), dict) for line in lines)
    reloaded = JsonlTraceSink(path).read(run_id)
    assert [event.event_id for event in reloaded] == [first.event_id, second.event_id]
    assert [event.sequence for event in reloaded] == [0, 1]

    reopened = RunTraceRecorder(JsonlTraceSink(path))
    reopened.emit(run_id, TraceEventType.RUN_COMPLETED, status="COMPLETED")
    assert [event.sequence for event in reopened.events(run_id)] == [0, 1, 2]


@pytest.mark.parametrize(
    "payload",
    [
        {"raw_matrix": [[1, 2], [3, 4]]},
        {"nested": {"dataframe_rows": [{"value": 1}]}},
        {"file_contents": "unrestricted mock file"},
        {"h5ad_contents": "mock binary representation"},
    ],
)
def test_trace_payload_rejects_obvious_raw_data_fields(payload):
    sink, recorder = _trace_pair()

    with pytest.raises(ValidationError, match="reserved for raw data"):
        recorder.emit(
            uuid4(),
            TraceEventType.RESULT_RECORDED,
            payload=payload,
        )

    assert sink.read() == ()


class _FailingTraceSink(TraceSink):
    def append(self, event):
        raise TraceSinkError("mock trace persistence failure")

    def read(self, run_id=None):
        return ()


def test_trace_sink_failure_is_explicit_and_not_swallowed():
    recorder = RunTraceRecorder(_FailingTraceSink())

    with pytest.raises(TraceSinkError, match="mock trace persistence failure"):
        recorder.emit(uuid4(), TraceEventType.RUN_CREATED, status="CREATED")
