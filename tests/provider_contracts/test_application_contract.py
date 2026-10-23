"""Criterion 13: the full workflow through each real adapter, trace included."""

from __future__ import annotations

import dataclasses
import json

from labbioagentos import (
    JsonlTraceSink,
    LabBioApplication,
    NextAction,
    NextActionProposal,
    RunStatus,
    RuntimeStageResult,
    TraceEventType,
    WorkflowStage,
)
from test_native_stage_application import MAIN_PATH, _body, _configuration, _request

from .wire import KEY, Turn

PRIVATE = "PRIVATE_CONTINUATION_WORKFLOW"


def _stage_reply(wire):
    """Answer one stage request with its typed result, as the stage tests do."""

    def reply(body):
        payload = json.loads(wire.last_user_text(body))
        stage = WorkflowStage(payload["stage_input"]["stage_id"])
        index = MAIN_PATH.index(stage)
        action = (
            NextActionProposal(action=NextAction.FINISH)
            if stage is WorkflowStage.LEARN
            else NextActionProposal(action=NextAction.TRANSITION,
                                    target_stage=MAIN_PATH[index + 1])
        )
        result = RuntimeStageResult(stage_id=stage, summary=f"Completed {stage.value}.",
                                    body=_body(stage), next_action=action)
        content = result.model_dump_json(exclude={"result_id"})
        return wire.encode(Turn(text=content, reasoning=PRIVATE), index + 1)

    return reply


async def test_workflow_completes_through_the_adapter_and_keeps_private_data_out(
    wire, server, tmp_path,
):
    server.script(*([_stage_reply(wire)] * len(MAIN_PATH)))
    configuration = dataclasses.replace(
        _configuration(tmp_path, lambda _model: wire.provider(wire.binding(thinking=False))),
        trace_sink=JsonlTraceSink(tmp_path / "run-trace.jsonl"),
    )
    application = LabBioApplication(configuration)
    handle = application.create_run(_request())

    outcome = await application.run(handle)

    assert outcome.status is RunStatus.COMPLETED
    assert len(server.requests) == len(MAIN_PATH)
    events = application.trace_events(handle)
    observed = [event for event in events
                if event.event_type is TraceEventType.PROVIDER_TURN_OBSERVED]
    assert [(event.stage_id, event.status) for event in observed] == [
        (stage, "STOP") for stage in MAIN_PATH
    ]
    assert PRIVATE not in "".join(event.model_dump_json() for event in events)
    written = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert tmp_path / "run-trace.jsonl" in written
    for path in written:
        data = path.read_bytes()
        assert PRIVATE.encode() not in data and KEY.encode() not in data, path.name
