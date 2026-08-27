"""Phase 5 acceptance tests for ArtifactRef and controlled ArtifactView exposure."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from labbioagentos import (
    ArtifactApproval,
    ArtifactConsumer,
    ArtifactExposureClass,
    ArtifactExposureDenied,
    ArtifactExposureService,
    ArtifactQuery,
    ArtifactQueryError,
    ArtifactRef,
    ArtifactRepresentation,
    ArtifactSchema,
    ArtifactView,
    ArtifactViewType,
    ExposurePolicy,
    InMemoryArtifactApprovalStore,
    InMemoryTraceSink,
    LocalArtifactStore,
    PantheonArtifactQueryAdapter,
    RunTraceRecorder,
    TraceEventType,
    WorkflowStage,
)


def _components(tmp_path, *, max_top_n=2, traced=False):
    sink = InMemoryTraceSink()
    recorder = RunTraceRecorder(sink) if traced else None
    store = LocalArtifactStore(tmp_path / "artifacts", trace_recorder=recorder)
    approvals = InMemoryArtifactApprovalStore()
    policy = ExposurePolicy(
        approval_store=approvals,
        max_top_n=max_top_n,
        default_top_n=min(2, max_top_n),
    )
    service = ArtifactExposureService(store, policy, trace_recorder=recorder)
    return store, approvals, service, sink


def _register(
    store,
    exposure_class,
    *,
    run_id=None,
    stage_id=WorkflowStage.EXECUTE,
    producer_invocation_id=None,
    schema=None,
    metadata=None,
    summary=None,
    records=(),
    stored_content=None,
):
    return store.register(
        artifact_type="synthetic-result",
        exposure_class=exposure_class,
        run_id=run_id,
        stage_id=stage_id,
        producer_invocation_id=producer_invocation_id,
        schema=schema,
        metadata=metadata,
        representation=ArtifactRepresentation(
            summary=summary or {},
            records=tuple(records),
            record_count=len(records),
            stored_content=stored_content,
        ),
    )


def test_raw_artifact_is_denied_to_remote_llm(tmp_path):
    store, _, service, _ = _components(tmp_path)
    ref = _register(
        store,
        ArtifactExposureClass.RAW,
        stored_content=[[1, 2], [3, 4]],
    )

    with pytest.raises(ArtifactExposureDenied, match="RAW artifacts"):
        service.artifact_query(
            ref.artifact_id,
            ArtifactQuery(view_type=ArtifactViewType.SCHEMA),
            ArtifactConsumer.REMOTE_LLM,
        )


def test_structural_artifact_exposes_metadata_and_schema(tmp_path):
    store, _, service, _ = _components(tmp_path)
    schema = ArtifactSchema(
        shape=(1000, 20000),
        columns=("sample", "cell_type"),
        dtypes={"sample": "string", "cell_type": "category"},
    )
    ref = _register(
        store,
        ArtifactExposureClass.STRUCTURAL,
        schema=schema,
        metadata={"format": "synthetic-table"},
    )

    metadata = service.artifact_query(
        ref.artifact_id,
        ArtifactQuery(view_type=ArtifactViewType.METADATA),
        ArtifactConsumer.REMOTE_LLM,
    )
    view = service.artifact_query(
        ref.artifact_id,
        ArtifactQuery(view_type=ArtifactViewType.SCHEMA),
        ArtifactConsumer.REMOTE_LLM,
    )

    assert metadata.metadata == {"format": "synthetic-table"}
    assert view.artifact_schema == schema
    assert view.columns == ("sample", "cell_type")
    assert view.records == ()


def test_aggregate_artifact_exposes_summary(tmp_path):
    store, _, service, _ = _components(tmp_path)
    ref = _register(
        store,
        ArtifactExposureClass.AGGREGATE,
        summary={"sample_count": 4, "group_sizes": {"A": 2, "B": 2}},
    )

    view = service.artifact_query(
        ref.artifact_id,
        ArtifactQuery(view_type=ArtifactViewType.SUMMARY),
        ArtifactConsumer.REMOTE_LLM,
    )

    assert view.summary["sample_count"] == 4
    assert view.records == ()


def test_derived_top_n_is_bounded_and_marks_truncation(tmp_path):
    store, _, service, _ = _components(tmp_path, max_top_n=2)
    records = (
        {"gene": "GZMK", "score": 2.1},
        {"gene": "TOX", "score": 1.7},
        {"gene": "CCR7", "score": 1.1},
    )
    ref = _register(store, ArtifactExposureClass.DERIVED, records=records)

    view = service.artifact_query(
        ref.artifact_id,
        ArtifactQuery(view_type=ArtifactViewType.TOP_N, limit=500),
        ArtifactConsumer.REMOTE_LLM,
    )

    assert view.records == records[:2]
    assert view.record_count == 3
    assert view.truncated is True


def test_user_approved_class_requires_consumer_specific_approval(tmp_path):
    store, approvals, service, _ = _components(tmp_path)
    ref = _register(
        store,
        ArtifactExposureClass.USER_APPROVED,
        records=({"label": "synthetic approved result"},),
    )
    query = ArtifactQuery(view_type=ArtifactViewType.TOP_N, limit=1)

    with pytest.raises(ArtifactExposureDenied, match="explicit approval"):
        service.artifact_query(ref.artifact_id, query, ArtifactConsumer.REMOTE_LLM)

    approvals.record(
        ArtifactApproval(
            artifact_id=ref.artifact_id,
            consumer=ArtifactConsumer.REMOTE_LLM,
            approved_by="synthetic-test-user",
        )
    )
    view = service.artifact_query(
        ref.artifact_id,
        query,
        ArtifactConsumer.REMOTE_LLM,
    )
    assert view.records == ({"label": "synthetic approved result"},)


def test_artifact_ref_is_metadata_only_and_locator_is_not_a_read_capability(tmp_path):
    store, _, service, _ = _components(tmp_path)
    ref = _register(
        store,
        ArtifactExposureClass.RAW,
        stored_content={"synthetic-secret": [1, 2, 3]},
    )

    serialized_ref = ref.model_dump(mode="json")
    assert "stored_content" not in serialized_ref
    assert "synthetic-secret" not in json.dumps(serialized_ref)
    assert ref.storage_locator.endswith(f"{ref.artifact_id}.json")
    with pytest.raises(ArtifactQueryError, match="not a path"):
        service.artifact_query(
            ref.storage_locator,
            {"view_type": "SCHEMA"},
            ArtifactConsumer.REMOTE_LLM,
        )


@pytest.mark.parametrize(
    "artifact_id",
    ("../../etc/passwd", "/tmp/arbitrary.json", "artifact://../secret"),
)
def test_arbitrary_path_traversal_is_rejected(tmp_path, artifact_id):
    _, _, service, _ = _components(tmp_path)
    with pytest.raises(ArtifactQueryError, match="not a path"):
        service.artifact_query(
            artifact_id,
            {"view_type": "METADATA"},
            ArtifactConsumer.REMOTE_LLM,
        )


@pytest.mark.parametrize(
    "query",
    (
        {"view_type": "DOWNLOAD"},
        {"view_type": "TOP_N", "filter": "open('/etc/passwd')"},
        {"view_type": "SUMMARY", "limit": 2},
    ),
)
def test_unsupported_or_programmable_queries_are_rejected(tmp_path, query):
    store, _, service, _ = _components(tmp_path)
    ref = _register(store, ArtifactExposureClass.DERIVED)
    with pytest.raises(ArtifactQueryError, match="Invalid artifact query"):
        service.artifact_query(
            ref.artifact_id,
            query,
            ArtifactConsumer.REMOTE_LLM,
        )


def test_artifact_view_retains_safe_provenance(tmp_path):
    store, _, service, _ = _components(tmp_path)
    run_id = uuid4()
    invocation_id = uuid4()
    ref = _register(
        store,
        ArtifactExposureClass.DERIVED,
        run_id=run_id,
        stage_id=WorkflowStage.VALIDATE,
        producer_invocation_id=invocation_id,
        summary={"status": "synthetic"},
    )
    view = service.artifact_query(
        ref.artifact_id,
        ArtifactQuery(view_type=ArtifactViewType.SUMMARY),
        ArtifactConsumer.REMOTE_LLM,
    )

    assert view.provenance.run_id == run_id
    assert view.provenance.stage_id is WorkflowStage.VALIDATE
    assert view.provenance.producer_invocation_id == invocation_id
    assert "storage_locator" not in view.model_dump(mode="json")


def test_trace_records_ids_and_counts_but_not_artifact_payload(tmp_path):
    store, _, service, sink = _components(tmp_path, traced=True)
    run_id = uuid4()
    ref = _register(
        store,
        ArtifactExposureClass.DERIVED,
        run_id=run_id,
        records=(
            {"private_fixture_token": "NEVER_TRACE_THIS_VALUE"},
            {"private_fixture_token": "SECOND_PRIVATE_VALUE"},
        ),
        stored_content={"large_local_value": "ALSO_NEVER_TRACE"},
    )
    service.artifact_query(
        ref.artifact_id,
        ArtifactQuery(view_type=ArtifactViewType.TOP_N, limit=1),
        ArtifactConsumer.REMOTE_LLM,
    )

    events = sink.read(run_id)
    assert [event.event_type for event in events] == [
        TraceEventType.ARTIFACT_REGISTERED,
        TraceEventType.ARTIFACT_VIEW_REQUESTED,
        TraceEventType.ARTIFACT_EXPOSED,
    ]
    trace_json = json.dumps(
        [event.model_dump(mode="json") for event in events]
    )
    assert str(ref.artifact_id) in trace_json
    assert "NEVER_TRACE_THIS_VALUE" not in trace_json
    assert "SECOND_PRIVATE_VALUE" not in trace_json
    assert "ALSO_NEVER_TRACE" not in trace_json
    assert "records" not in trace_json


def test_denied_exposure_is_traced_structurally(tmp_path):
    store, _, service, sink = _components(tmp_path, traced=True)
    run_id = uuid4()
    ref = _register(
        store,
        ArtifactExposureClass.RAW,
        run_id=run_id,
        stored_content=[[10, 20]],
    )
    with pytest.raises(ArtifactExposureDenied):
        service.artifact_query(
            ref.artifact_id,
            ArtifactQuery(view_type=ArtifactViewType.SCHEMA),
            ArtifactConsumer.REMOTE_LLM,
        )
    assert sink.read(run_id)[-1].event_type is TraceEventType.ARTIFACT_EXPOSURE_DENIED


def test_artifact_exposure_works_without_tracing(tmp_path):
    store, _, service, sink = _components(tmp_path, traced=False)
    ref = _register(
        store,
        ArtifactExposureClass.AGGREGATE,
        summary={"synthetic_count": 12},
    )
    view = service.artifact_query(
        ref.artifact_id,
        ArtifactQuery(view_type=ArtifactViewType.SUMMARY),
        ArtifactConsumer.REMOTE_LLM,
    )
    assert view.summary == {"synthetic_count": 12}
    assert sink.read() == ()


@pytest.mark.asyncio
async def test_pantheon_facing_adapter_returns_only_artifact_view(tmp_path):
    store, _, service, _ = _components(tmp_path)
    ref = _register(
        store,
        ArtifactExposureClass.DERIVED,
        records=({"item": "safe synthetic result"},),
    )
    adapter = PantheonArtifactQueryAdapter(service)

    result = await adapter.artifact_query(
        str(ref.artifact_id),
        {"view_type": "TOP_N", "limit": 1},
        consumer="REMOTE_LLM",
    )

    assert isinstance(result, ArtifactView)
    assert not isinstance(result, ArtifactRef)
    assert result.records == ({"item": "safe synthetic result"},)
    assert "storage_locator" not in result.model_dump(mode="json")

    with pytest.raises(ArtifactExposureDenied, match="cannot be escalated"):
        await adapter.artifact_query(
            str(ref.artifact_id),
            {"view_type": "TOP_N", "limit": 1},
            consumer="SYSTEM",
        )


def test_artifact_ref_rejects_embedded_content_metadata():
    with pytest.raises(ValidationError, match="cannot contain artifact content"):
        ArtifactRef(
            artifact_type="invalid-ref",
            storage_locator="/internal/opaque/path",
            exposure_class=ArtifactExposureClass.DERIVED,
            metadata={"records": [{"value": 1}]},
        )
