"""Only bounded original model-authored report pages cross the report boundary."""

import hashlib
import json
from uuid import uuid4

import pytest
from pantheon.providers import LocalProvider

from labbioagentos import (
    ArtifactExposureClass, ArtifactReleaseBasis, ArtifactRepresentation,
    LabBioRuntimeToolSet, RuntimeCapabilityContext, WorkflowStage,
)
from test_local_revision import revision_apps, _import


def _tools(fixture, *, allow=True, stage=WorkflowStage.REPORT):
    source, target, handle, principal, workspace, report_id = fixture
    _import(fixture)
    binding = RuntimeCapabilityContext(
        principal=principal, workspace=workspace, run_id=uuid4(),
        stage_id=stage, invocation_id=uuid4(),
        actor_profile_key="fixture", actor_agent_name="FixtureAgent",
        capability_allowlist=("report_read",) if allow else (),
    )
    return LabBioRuntimeToolSet(binding, target.capability_services), report_id


@pytest.mark.asyncio
async def test_original_report_pages_survive_handoff_without_entering_trace_events(revision_apps):
    tools, report_id = _tools(revision_apps)
    original = tools.services.artifact_store.load_for_view(report_id).representation.stored_content
    offset, chunks, digest = 0, [], hashlib.sha256(original.encode()).hexdigest()
    while True:
        result = await tools.report_read(str(report_id), offset=offset, limit=7)
        assert result["success"]
        assert result["information_authority"] == "MODEL_CONTEXT"
        page = result["data"]
        assert page["artifact_id"] == str(report_id)
        assert page["sha256"] == digest
        assert page["content_authority"] == "MODEL_CONTEXT"
        assert page["offset"] == offset
        assert page["content"] == original[offset:offset + 7]
        chunks.append(page["content"])
        if page["next_offset"] is None:
            assert page["truncated"] is False
            break
        assert page["truncated"] is True
        offset = page["next_offset"]
    assert "".join(chunks) == original
    assert "".join(item.safe_result["content"] for item in tools.evidence_items()) == original
    events = tools.services.trace_recorder.events(tools.binding.run_id)
    assert '"content"' not in json.dumps([event.model_dump(mode="json") for event in events])
    assert all(str(report_id) in item.reference_ids for item in tools.evidence_items())
    assert all(item.information_authority.value == "MODEL_CONTEXT" for item in tools.evidence_items())


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", (7, 8000))
async def test_report_page_reaches_finalizer_after_checkpoint_roundtrip(revision_apps, limit):
    from types import MethodType, SimpleNamespace
    from labbioagentos import (
        CapabilityEvidenceBundle, PantheonRuntimeFactory, PantheonTypedStageInvoker,
        ResponseSchemaRef, RuntimeInvocationMode, RuntimeStageInput, RuntimeWorkspaceIdentifiers,
    )
    from test_runtime_milestone_c2 import _catalog, _profile

    tools, report_id = _tools(revision_apps, stage=WorkflowStage.UNDERSTAND)
    read = await tools.report_read(str(report_id), limit=limit)
    assert read["success"]
    binding = tools.binding
    evidence = CapabilityEvidenceBundle.model_validate_json(CapabilityEvidenceBundle(
        run_id=binding.run_id, stage_id=binding.stage_id, invocation_id=binding.invocation_id,
        items=tools.evidence_items(),
    ).model_dump_json())
    stage_input = RuntimeStageInput(
        run_id=binding.run_id, stage_id=binding.stage_id, invocation_id=binding.invocation_id,
        instruction="Understand the supplied revision request.",
        workspace=RuntimeWorkspaceIdentifiers(**binding.workspace.model_dump()),
        allowed_capabilities=("report_read",),
    )
    team, prompts = await PantheonRuntimeFactory(_catalog()).create_team(
        ("coordinator",), invocation_mode=RuntimeInvocationMode.FINALIZE)
    seen = []

    async def run(_self, message, **_kwargs):
        visible = json.loads(message)
        item = visible["capability_evidence"]["items"][0]
        assert item["information_authority"] == "MODEL_CONTEXT"
        assert item["safe_result"] == read["data"]
        seen.append(item["safe_result"]["content"])
        return SimpleNamespace(content={
            "stage_id": "UNDERSTAND", "summary": "Fixture handoff checked.",
            "body": {"kind": "UNDERSTAND", "requirements": ["Revise the previous report."]},
            "next_action": {"action": "transition", "target_stage": "PLAN"},
        })

    team.run = MethodType(run, team)
    result = await PantheonTypedStageInvoker(team, profile=_profile(), prompt=prompts["coordinator"],
        response_schema=ResponseSchemaRef(), trace_recorder=tools.services.trace_recorder,
    ).invoke(stage_input, capability_evidence=evidence)
    assert result.next_action.target_stage is WorkflowStage.PLAN
    assert seen == [read["data"]["content"]]
    assert len(tools.evidence_items()) == 1  # No automatic reread or extra page.


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ("raw", "wrong_type", "wrong_owner", "no_capability", "invalid_limit", "wrong_hash"))
async def test_report_read_fails_closed(revision_apps, case):
    tools, report_id = _tools(revision_apps, allow=case != "no_capability")
    if case in {"raw", "wrong_type", "wrong_owner"}:
        existing = tools.services.artifact_store.get_ref(report_id)
        changes = {
            "raw": {"exposure_class": ArtifactExposureClass.RAW, "release_basis": ArtifactReleaseBasis.RAW_INGESTION},
            "wrong_type": {"artifact_type": "execution-stdout"},
            "wrong_owner": {"owner_user_id": "foreign-user"},
        }[case]
        ref = tools.services.artifact_store.register(
            **{**existing.model_dump(exclude={"storage_locator", "original_filename", "created_at", "artifact_id", "artifact_schema"}),
               "schema": existing.artifact_schema, **changes},
            representation=ArtifactRepresentation(stored_content="DO_NOT_RELEASE"),
        )
        report_id = ref.artifact_id
    result = await tools.report_read(str(report_id), limit=0 if case == "invalid_limit" else 100,
                                     expected_sha256="0" * 64 if case == "wrong_hash" else None)
    assert result["success"] is False
    assert "DO_NOT_RELEASE" not in json.dumps(result)


@pytest.mark.asyncio
async def test_pantheon_filter_cannot_silently_alter_report_page(revision_apps, monkeypatch):
    tools, report_id = _tools(revision_apps)
    import pantheon.utils.llm
    monkeypatch.setattr(pantheon.utils.llm, "filter_base64_in_tool_result", lambda value: {**value, "content": "changed"})
    result = await tools.report_read(str(report_id))
    assert not result["success"]
    assert result["error"]["error_code"] == "REPORT_PAGE_TRANSPORT_UNSUPPORTED"


@pytest.mark.asyncio
async def test_report_page_retains_unicode_exactly_and_enforces_policy(revision_apps, monkeypatch):
    tools, _ = _tools(revision_apps)
    report = tools.services.report_submission.submit(
        title="Unicode fixture", report_text="α β\n报告 🧬\n", evidence_artifact_ids=(),
        principal=tools.binding.principal, workspace=tools.binding.workspace,
        run_id=tools.binding.run_id, stage_id=WorkflowStage.REPORT,
        invocation_id=tools.binding.invocation_id,
    )
    result = await tools.report_read(str(report.report_artifact_id), offset=2, limit=4)
    assert result["success"]
    assert result["data"]["content"] == "β\n报告"
    stored = tools.services.artifact_store.load_for_view(report.report_artifact_id)
    assert result["data"]["sha256"] == hashlib.sha256(stored.representation.stored_content.encode()).hexdigest()
    from labbioagentos.artifacts import ArtifactExposureDenied
    def denied(*_args, **_kwargs):
        raise ArtifactExposureDenied("fixture policy denial")
    monkeypatch.setattr(tools.services.artifact_exposure, "artifact_query", denied)
    blocked = await tools.report_read(str(report.report_artifact_id))
    assert not blocked["success"]
    assert blocked["error"]["error_code"] == "ARTIFACT_EXPOSURE_DENIED"


@pytest.mark.asyncio
@pytest.mark.parametrize("offset,limit", ((-1, 10), (0, -1), (0, 8001), (0, True), (0, "10"), (1000, 10)))
async def test_invalid_report_pages_do_not_release_prose(revision_apps, offset, limit):
    tools, report_id = _tools(revision_apps)
    result = await tools.report_read(str(report_id), offset=offset, limit=limit)
    assert not result["success"]
    assert "bounded observation" not in json.dumps(result)


@pytest.mark.asyncio
async def test_provider_report_schema_retains_pagination_bounds(revision_apps):
    tools, _ = _tools(revision_apps)
    provider = LocalProvider(tools)
    await provider.initialize()
    schema = next(item.inputSchema for item in await provider.list_tools() if item.name == "report_read")
    parameters = schema["parameters"]
    assert parameters["required"] == ["artifact_id"]
    assert parameters["properties"]["offset"]["minimum"] == 0
    assert parameters["properties"]["limit"]["minimum"] == 1
    assert parameters["properties"]["limit"]["maximum"] == 8000
    assert parameters["properties"]["expected_sha256"]["anyOf"] == [
        {"type": "string", "pattern": "^[0-9a-f]{64}$"}, {"type": "null"},
    ]


@pytest.mark.asyncio
async def test_original_report_mutation_between_pages_is_detected(revision_apps):
    tools, report_id = _tools(revision_apps)
    first = await tools.report_read(str(report_id), limit=7)
    assert first["success"]
    envelope = tools.services.artifact_store.root / f"{report_id}.json"
    stored = json.loads(envelope.read_text())
    stored["representation"]["stored_content"] = "changed document"
    envelope.write_text(json.dumps(stored))
    changed = await tools.report_read(str(report_id), offset=7,
                                      expected_sha256=first["data"]["sha256"])
    assert not changed["success"]
    assert "changed document" not in json.dumps(changed)
