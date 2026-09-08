"""Opt-in curation over preserved safe evidence; no analysis, approval or replay."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from labbioagentos.cli import _close
from labbioagentos.local_config import build_application, load_settings
from labbioagentos.local_gold_source import stage_context_from_results
from labbioagentos.local_workspace_cli import configured_curator
from labbioagentos.runtime.contracts import RuntimeStageResult
from labbioagentos.skills.models import SkillCurationSourceView
from labbioagentos.trace import TraceEvent, TraceEventType


@pytest.mark.skipif(os.environ.get("LABBIO_GOLD_GUIDANCE_LIVE") != "1",
                    reason="Explicit provider curation and preserved safe source required")
@pytest.mark.asyncio
async def test_real_agent_curation_from_preserved_successful_source():
    source_path = Path(os.environ["LABBIO_GOLD_SOURCE_BOUNDARIES"])
    results_path = Path(os.environ["LABBIO_GOLD_SOURCE_RESULTS"])
    trace_path = Path(os.environ["LABBIO_GOLD_SOURCE_TRACE"])
    output = Path(os.environ["LABBIO_GOLD_GUIDANCE_OUTPUT"])
    rows = [json.loads(line) for line in source_path.read_text().splitlines()]
    sources = [row["payload"] for row in rows if row["kind"] == "curator_source"]
    assert len(sources) == 1
    source = SkillCurationSourceView.model_validate_json(json.dumps(sources[0]))
    events = tuple(TraceEvent.model_validate_json(line)
                   for line in trace_path.read_text().splitlines())
    terminal = [event for event in events if event.event_type in {
        TraceEventType.RUN_COMPLETED, TraceEventType.RUN_FAILED, TraceEventType.RUN_CANCELLED,
    }]
    assert terminal and terminal[-1].event_type is TraceEventType.RUN_COMPLETED
    assert {event.run_id for event in events} == {source.source_run_id}
    results = tuple(RuntimeStageResult.model_validate_json(json.dumps(value))
                    for value in json.loads(results_path.read_text()))
    source = SkillCurationSourceView.model_validate({
        **source.model_dump(mode="python"),
        "stage_context": stage_context_from_results(results, events, source.source_run_id),
    })
    assert source.stage_context and source.artifact_evidence_views
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    settings = load_settings(Path(os.environ["LABBIO_GOLD_GUIDANCE_CONFIG"]))
    application = build_application(settings, output / "runtime")
    try:
        # The real configured draft/audit/revision agents own all procedural text.
        draft = await configured_curator(application).propose(source)
        (output / "candidate.json").write_text(draft.model_dump_json(indent=2) + "\n")
        assert draft.proposed_name and draft.procedure.workflow_outline
        assert draft.procedure.tags and draft.procedure.adaptation_points
        assert all(point.modifiable for point in draft.procedure.adaptation_points)
        (output / "verification.json").write_text(json.dumps({
            "scope": "CURATION_ONLY_NOT_APPROVED_NOT_ANALYSIS_ACCEPTANCE",
            "source_run_id": str(source.source_run_id),
            "source_sha256": hashlib.sha256(source.model_dump_json().encode()).hexdigest(),
            "source_stage_count": len(source.stage_context),
            "source_view_count": len(source.artifact_evidence_views),
            "workflow_step_count": len(draft.procedure.workflow_outline),
            "adaptation_point_count": len(draft.procedure.adaptation_points),
            "runtime_revision": application.configuration.runtime_revision,
        }, indent=2) + "\n")
    finally:
        _close(application)
