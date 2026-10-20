"""Workload accounting stays descriptive; recovery cannot replay a checkpoint."""

import importlib.util
import json
from contextlib import nullcontext
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

SPEC = importlib.util.spec_from_file_location(
    "runtime_stress_report", Path(__file__).parents[1] / "scripts/runtime_stress_report.py")
metrics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(metrics)


def test_repeated_reads_use_recorded_requests_not_equal_answers():
    def call(identity, capability, **fields):
        return {"capability_invocation_id": identity, "capability_name": capability,
                "actor_profile_key": "reviewer", "status": "COMPLETED", **fields}
    query = {"artifact_id": "source", "view_type": "TABLE", "limit": 10, "offset": 0}
    aggregate = {"artifact_id": "source", "operation": "COUNT", "field": None,
                 "match_field": None, "match_value": None, "value": 10}
    items = [
        call("q1", "artifact_query", artifact_query_request=query),
        call("q2", "artifact_query", artifact_query_request=query),
        call("q3", "artifact_query", artifact_query_request={**query, "offset": 10}),
        call("a1", "artifact_aggregate", safe_result=aggregate),
        call("a2", "artifact_aggregate", safe_result=aggregate),
        call("a3", "artifact_aggregate", safe_result={**aggregate, "match_value": "different"}),
        call("u1", "file_read", safe_result={"source": "same text"}),
    ]
    boundary = {"kind": "capability_evidence",
                "payload": {"invocation_id": "phase-1", "items": items}}
    result = metrics.repeated_read_requests([boundary, boundary])
    assert result["measured_calls"] == 6
    assert result["repeated_calls"] == 2
    assert result["by_capability"] == {"artifact_query": 1, "artifact_aggregate": 1}
    assert result["unmeasured_read_calls"] == 1
    assert "same text" not in json.dumps(result)


def test_repeated_reads_do_not_cross_phase_or_actor_boundaries():
    boundaries = []
    for phase, actor, identity in (("one", "a", "1"), ("two", "a", "2"), ("two", "b", "3")):
        boundaries.append({"kind": "capability_evidence", "payload": {
            "invocation_id": phase, "items": [{
                "capability_invocation_id": identity, "capability_name": "artifact_query",
                "actor_profile_key": actor, "status": "COMPLETED",
                "artifact_query_request": {"artifact_id": "source", "view_type": "METADATA",
                                           "limit": None, "offset": 0}}]}})
    result = metrics.repeated_read_requests(boundaries)
    assert result["measured_calls"] == 3
    assert result["repeated_calls"] == 0


def test_repeated_read_unknown_arguments_are_not_fabricated():
    result = metrics.repeated_read_requests([{"kind": "capability_evidence", "payload": {
        "invocation_id": "phase", "items": [{
            "capability_invocation_id": "one", "capability_name": "artifact_aggregate",
            "actor_profile_key": "a", "status": "ERROR", "safe_result": None}]} }])
    assert result["measured_calls"] == 0
    assert result["repeated_calls"] is None
    assert result["unmeasured_read_calls"] == 1


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def attempt(root, name, status="FAILED"):
    receipt, directory = root / "receipts" / name, root / "runs" / name
    run_id = str(uuid4())
    state = {"run_id": run_id, "record_version": 8,
        "runtime_revision": "frozen", "runtime_results": [],
        "workflow_run": {"status": status, "current_stage": "REPORT", "failure_reason": "FIXTURE"},
        "recovery_state": "STABLE"}
    write(receipt / "MANIFEST.json", {
        "arm": name.split("-")[0], "case": "autonomous", "replicate": 1,
        "source_commit": "frozen-commit", "runner_sha256": "frozen-runner",
        "inputs": [{"path": "fixture-input", "sha256": "input-sha"}],
    })
    write(directory / "RUNTIME.json", {"runtime_revision": "frozen",
                                      "source": {"labbio_sha256": "frozen-code"}})
    write(directory / "RUN.json", {"run_id": run_id})
    con = sqlite3.connect(directory / "state.sqlite")
    con.execute("CREATE TABLE application_run_state(run_id TEXT,record_version INTEGER,payload TEXT)")
    con.execute("INSERT INTO application_run_state VALUES(?,?,?)", (run_id, 8, json.dumps(state)))
    con.commit()
    con.close()
    return receipt, directory, state


def event(state, name, payload):
    return {"event_id": str(uuid4()), "run_id": state["run_id"],
            "event_type": name, "payload": payload, "status": "FIXTURE"}


def provider_event(state, kind, invocation_id, turn_index, **facts):
    row = event(state, kind, {
        "kernel_invocation_id": invocation_id, "kernel_event_id": str(uuid4()),
        "numeric_facts": {"turn_index": turn_index, **facts},
    })
    if kind == "PROVIDER_REQUEST_PREPARED":
        row["status"] = "READY"
    return row


def provider_sample(tmp_path, events_for, status="FAILED"):
    receipt, directory, state = attempt(tmp_path, "candidate-autonomous-1", status)
    rows = events_for(state, str(uuid4()))
    (directory / "run-trace.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows))
    return metrics.summarize_attempt(tmp_path, receipt)


def test_prepared_request_without_observation_does_not_make_subtotal_a_total(tmp_path):
    def rows(state, invocation):
        prepared = [provider_event(state, "PROVIDER_REQUEST_PREPARED", invocation, n,
                                   request_bytes=100) for n in range(1, 27)]
        observed = [provider_event(state, "PROVIDER_TURN_OBSERVED", invocation, n,
                                  total_tokens=610945 if n == 25 else 1)
                    for n in range(1, 26)]
        return prepared + observed

    sample = provider_sample(tmp_path, rows)
    usage = sample["provider_usage"]["total_tokens"]
    assert usage["observed_subtotal"] == 610969
    assert usage["total"] is None
    assert usage["unknown_turns"] == 0
    coverage = sample["provider_usage_coverage"]
    assert coverage["prepared_without_outcome"] == 1
    assert coverage["unreported_usage_requests"] == 0
    assert coverage["scope"] == "RECORDED_PROVIDER_REQUESTS"
    assert coverage["request_outcomes_complete"] is False


def test_complete_request_accounting_retains_known_totals(tmp_path):
    def rows(state, invocation):
        return [row for n, tokens in ((1, 4), (2, 7)) for row in (
            provider_event(state, "PROVIDER_REQUEST_PREPARED", invocation, n),
            provider_event(state, "PROVIDER_TURN_OBSERVED", invocation, n,
                           total_tokens=tokens, elapsed_ms=20),
        )]

    sample = provider_sample(tmp_path, rows, "COMPLETED")
    assert sample["provider_usage"]["total_tokens"]["total"] == 11
    assert sample["provider_usage_coverage"]["request_outcomes_complete"] is True
    assert sample["provider_usage_coverage"]["snapshot_complete"] is True


def test_unreported_request_keeps_tokens_unknown_and_known_elapsed(tmp_path):
    def rows(state, invocation):
        return [
            provider_event(state, "PROVIDER_REQUEST_PREPARED", invocation, 1),
            provider_event(state, "PROVIDER_TURN_OBSERVED", invocation, 1,
                           total_tokens=4, elapsed_ms=20),
            provider_event(state, "PROVIDER_REQUEST_PREPARED", invocation, 2),
            provider_event(state, "PROVIDER_USAGE_UNREPORTED", invocation, 2,
                           total_tokens=None, elapsed_ms=35),
        ]

    sample = provider_sample(tmp_path, rows)
    assert sample["provider_turns_observed"] == 1
    usage = sample["provider_usage"]
    assert usage["total_tokens"]["total"] is None
    assert usage["total_tokens"]["observed_subtotal"] == 4
    assert usage["total_tokens"]["unknown_unreported_requests"] == 1
    assert usage["elapsed_ms"]["total"] == 55
    assert usage["elapsed_ms"]["observed_subtotal"] == 20
    assert usage["elapsed_ms"]["unreported_request_subtotal"] == 35
    assert sample["provider_usage_coverage"]["prepared_without_outcome"] == 0
    assert sample["provider_usage_coverage"]["unreported_usage_requests"] == 1


@pytest.mark.parametrize("input_tokens,output_tokens,tokens", [(9, 12, 21), (0, 0, 0), (9, None, None)])
def test_failure_response_usage_is_counted_without_inventing_a_model_turn(
    tmp_path, input_tokens, output_tokens, tokens,
):
    def rows(state, invocation):
        return [
            provider_event(state, "PROVIDER_REQUEST_PREPARED", invocation, 1),
            provider_event(state, "PROVIDER_TURN_OBSERVED", invocation, 1,
                           input_tokens=1, output_tokens=3, total_tokens=4, elapsed_ms=20),
            provider_event(state, "PROVIDER_REQUEST_PREPARED", invocation, 2),
            provider_event(state, "PROVIDER_USAGE_ON_FAILURE", invocation, 2,
                           input_tokens=input_tokens, output_tokens=output_tokens,
                           total_tokens=tokens, elapsed_ms=35),
        ]

    sample = provider_sample(tmp_path, rows)
    assert sample["provider_turns_observed"] == 1
    coverage = sample["provider_usage_coverage"]
    assert coverage["failure_usage_requests"] == 1
    assert coverage["failure_usage_scope"] == "RECORDED_VALIDATED_FAILURE_RESPONSE_USAGE"
    assert coverage["unreported_usage_requests"] == 0
    assert coverage["prepared_without_outcome"] == 0
    assert coverage["request_outcomes_complete"] is True
    usage = sample["provider_usage"]
    assert usage["total_tokens"]["total"] == (4 + tokens if tokens is not None else None)
    assert usage["total_tokens"]["observed_subtotal"] == 4
    assert usage["total_tokens"]["failure_response_subtotal"] == tokens
    assert usage["total_tokens"]["known_failure_responses"] == (tokens is not None)
    assert usage["total_tokens"]["unknown_failure_responses"] == (tokens is None)
    assert usage["total_tokens"]["unreported_request_subtotal"] is None
    assert usage["input_tokens"]["total"] == 1 + input_tokens
    assert usage["input_tokens"]["failure_response_subtotal"] == input_tokens
    assert usage["elapsed_ms"]["total"] == 55
    assert usage["elapsed_ms"]["observed_subtotal"] == 20
    assert usage["elapsed_ms"]["failure_response_subtotal"] == 35


def test_failure_response_outcome_is_deduplicated_by_invocation_and_turn(tmp_path):
    def rows(state, invocation):
        prepared = provider_event(state, "PROVIDER_REQUEST_PREPARED", invocation, 1)
        failure = provider_event(state, "PROVIDER_USAGE_ON_FAILURE", invocation, 1,
                                 total_tokens=21)
        other = str(uuid4())
        return [prepared, failure, failure, {**failure, "event_id": str(uuid4())},
                provider_event(state, "PROVIDER_REQUEST_PREPARED", other, 1),
                provider_event(state, "PROVIDER_USAGE_ON_FAILURE", other, 1, total_tokens=7)]

    sample = provider_sample(tmp_path, rows)
    assert sample["provider_turns_observed"] == 0
    assert sample["provider_usage_coverage"]["failure_usage_requests"] == 2
    usage = sample["provider_usage"]["total_tokens"]
    assert usage["total"] == usage["failure_response_subtotal"] == 28
    assert usage["observed_subtotal"] is None and usage["known_turns"] == 0


@pytest.mark.parametrize("conflict", [
    "PROVIDER_TURN_OBSERVED", "PROVIDER_USAGE_UNREPORTED", "PROVIDER_USAGE_ON_FAILURE",
])
def test_failure_usage_conflicting_outcomes_do_not_invent_totals(tmp_path, conflict):
    sample = provider_sample(tmp_path, lambda state, invocation: [
        provider_event(state, "PROVIDER_REQUEST_PREPARED", invocation, 1),
        provider_event(state, "PROVIDER_USAGE_ON_FAILURE", invocation, 1, total_tokens=21),
        provider_event(state, conflict, invocation, 1, total_tokens=None),
    ])
    assert sample["metrics_error"] == "ValueError"
    assert "provider_usage" not in sample


@pytest.mark.parametrize("status,prepared", [("RUNNING", True), ("FAILED", False)])
def test_failure_response_subtotal_does_not_imply_complete_accounting(tmp_path, status, prepared):
    def rows(state, invocation):
        return ([provider_event(state, "PROVIDER_REQUEST_PREPARED", invocation, 1)]
                if prepared else []) + [
            provider_event(state, "PROVIDER_USAGE_ON_FAILURE", invocation, 1, total_tokens=21)]

    sample = provider_sample(tmp_path, rows, status)
    assert sample["provider_usage"]["total_tokens"]["total"] is None
    assert sample["provider_usage"]["total_tokens"]["failure_response_subtotal"] == 21
    assert sample["provider_turns_observed"] == 0
    assert sample["provider_usage_coverage"]["failure_usage_requests"] == 1


def test_new_failure_usage_does_not_fill_older_unreported_request(tmp_path):
    sample = provider_sample(tmp_path, lambda state, invocation: [
        provider_event(state, "PROVIDER_REQUEST_PREPARED", invocation, 1),
        provider_event(state, "PROVIDER_USAGE_UNREPORTED", invocation, 1,
                       total_tokens=None, elapsed_ms=10),
        provider_event(state, "PROVIDER_REQUEST_PREPARED", invocation, 2),
        provider_event(state, "PROVIDER_USAGE_ON_FAILURE", invocation, 2,
                       total_tokens=21, elapsed_ms=20),
    ])
    usage = sample["provider_usage"]
    assert usage["total_tokens"]["total"] is None
    assert usage["total_tokens"]["failure_response_subtotal"] == 21
    assert usage["total_tokens"]["unknown_unreported_requests"] == 1
    assert usage["elapsed_ms"]["total"] == 30
    assert sample["provider_usage_coverage"]["unreported_usage_requests"] == 1
    assert sample["provider_usage_coverage"]["failure_usage_requests"] == 1


def nonfinal_usage_event(state, invocation, *, total=21):
    row = provider_event(
        state, "PROVIDER_USAGE_UNREPORTED", invocation, 2,
        input_tokens=None, output_tokens=None, total_tokens=None, elapsed_ms=35,
        last_validated_input_tokens=0 if total == 0 else 9,
        last_validated_output_tokens=0 if total == 0 else None if total is None else 12,
        last_validated_total_tokens=total)
    row["status"] = "PARTIAL_RESPONSE_USAGE"
    row["payload"]["reason_code"] = "NONFINAL_USAGE_SNAPSHOT"
    return row


@pytest.mark.parametrize("total", [21, 0, None])
def test_nonfinal_usage_snapshot_is_auditable_but_never_an_exact_subtotal(tmp_path, total):
    invocation = str(uuid4())
    def rows(state, _):
        snapshot = nonfinal_usage_event(state, invocation, total=total)
        return [
            provider_event(state, "PROVIDER_REQUEST_PREPARED", invocation, 1),
            provider_event(state, "PROVIDER_TURN_OBSERVED", invocation, 1,
                           total_tokens=4, elapsed_ms=20),
            provider_event(state, "PROVIDER_REQUEST_PREPARED", invocation, 2),
            snapshot, snapshot, {**snapshot, "event_id": str(uuid4())},
        ]

    sample = provider_sample(tmp_path, rows)
    coverage = sample["provider_usage_coverage"]
    assert coverage["request_outcomes_complete"] is True
    assert coverage["prepared_without_outcome"] == 0
    assert coverage["nonfinal_usage_snapshot_requests"] == coverage["unreported_usage_requests"] == 1
    assert coverage["failure_usage_requests"] == 0
    assert sample["provider_turns_observed"] == 1
    usage = sample["provider_usage"]
    assert usage["total_tokens"]["total"] is None
    assert usage["total_tokens"]["observed_subtotal"] == 4
    assert usage["total_tokens"]["failure_response_subtotal"] is None
    assert usage["total_tokens"]["unreported_request_subtotal"] is None
    assert usage["total_tokens"]["unknown_unreported_requests"] == 1
    assert usage["elapsed_ms"]["total"] == 55
    assert sample["provider_usage_snapshots"] == {
        "scope": "NONFINAL_REPORTED_SNAPSHOTS_NOT_TOTALS_OR_LOWER_BOUNDS",
        "records": [{
            "kernel_invocation_id": invocation, "turn_index": 2,
            "last_validated_input_tokens": 0 if total == 0 else 9,
            "last_validated_output_tokens": 0 if total == 0 else None if total is None else 12,
            "last_validated_total_tokens": total,
        }],
    }


@pytest.mark.parametrize("kind,total", [
    ("PROVIDER_USAGE_UNREPORTED", None), ("PROVIDER_USAGE_ON_FAILURE", 24),
])
def test_no_partial_snapshot_is_inferred_from_old_unknown_or_complete_usage(tmp_path, kind, total):
    sample = provider_sample(tmp_path, lambda state, invocation: [
        provider_event(state, "PROVIDER_REQUEST_PREPARED", invocation, 1),
        provider_event(state, kind, invocation, 1, total_tokens=total),
    ])
    assert sample["provider_usage_snapshots"]["records"] == []
    assert sample["provider_usage_coverage"]["nonfinal_usage_snapshot_requests"] == 0
    assert sample["provider_usage"]["total_tokens"]["total"] == total


@pytest.mark.parametrize("field,value", [
    ("last_validated_total_tokens", "PRIVATE_CANARY"),
    ("kernel_invocation_id", "PRIVATE_CANARY"),
    ("turn_index", "PRIVATE_CANARY"),
    ("total_tokens", 21),
])
def test_malformed_nonfinal_snapshot_cannot_leak_text_or_become_exact_usage(tmp_path, field, value):
    def rows(state, invocation):
        row = nonfinal_usage_event(state, invocation)
        if field == "kernel_invocation_id":
            row["payload"][field] = value
        else:
            row["payload"]["numeric_facts"][field] = value
        return [provider_event(state, "PROVIDER_REQUEST_PREPARED", invocation, 2), row]

    sample = provider_sample(tmp_path, rows)
    assert sample["metrics_error"] == "ValueError"
    assert "provider_usage" not in sample
    assert "PRIVATE_CANARY" not in json.dumps(sample)


def test_active_snapshot_does_not_claim_final_total_or_failed_prepared_request(tmp_path):
    def rows(state, invocation):
        return [
            provider_event(state, "PROVIDER_REQUEST_PREPARED", invocation, 1),
            provider_event(state, "PROVIDER_TURN_OBSERVED", invocation, 1, total_tokens=4),
            provider_event(state, "PROVIDER_REQUEST_PREPARED", invocation, 2),
        ]

    sample = provider_sample(tmp_path, rows, "RUNNING")
    assert sample["provider_usage"]["total_tokens"]["total"] is None
    coverage = sample["provider_usage_coverage"]
    assert coverage["snapshot_complete"] is False
    assert coverage["prepared_without_outcome"] == 1
    assert coverage["unreported_usage_requests"] == 0
    assert sample["status"] == "RUNNING" and sample["process_result"] is None


def test_provider_events_are_counted_once_by_request_identity(tmp_path):
    def rows(state, invocation):
        prepared = provider_event(state, "PROVIDER_REQUEST_PREPARED", invocation, 1,
                                  request_bytes=100)
        observed = provider_event(state, "PROVIDER_TURN_OBSERVED", invocation, 1, total_tokens=4)
        return [prepared, observed, prepared, observed, {**observed, "event_id": str(uuid4())},
                {**prepared, "event_id": str(uuid4())}]

    sample = provider_sample(tmp_path, rows, "COMPLETED")
    assert sample["provider_turns_observed"] == 1
    assert sample["provider_usage"]["total_tokens"]["total"] == 4
    assert sample["provider_usage_coverage"]["prepared_requests"] == 1
    assert sample["request_size"] == {"measured_requests": 1, "max_bytes": 100}


def test_rejected_request_preparation_is_not_an_unreported_provider_call(tmp_path):
    def rows(state, invocation):
        rejected = provider_event(state, "PROVIDER_REQUEST_PREPARED", invocation, 2)
        rejected["status"] = "REJECTED"
        return [
            provider_event(state, "PROVIDER_REQUEST_PREPARED", invocation, 1),
            provider_event(state, "PROVIDER_TURN_OBSERVED", invocation, 1, total_tokens=4),
            rejected,
        ]

    sample = provider_sample(tmp_path, rows)
    assert sample["provider_usage"]["total_tokens"]["total"] == 4
    assert sample["provider_usage_coverage"]["rejected_preparations"] == 1
    assert sample["provider_usage_coverage"]["prepared_without_outcome"] == 0


def test_legacy_observed_only_trace_does_not_claim_full_request_coverage(tmp_path):
    sample = provider_sample(tmp_path, lambda state, invocation: [
        event(state, "PROVIDER_TURN_OBSERVED", {"numeric_facts": {"total_tokens": 4}}),
        event(state, "PROVIDER_TURN_OBSERVED", {"numeric_facts": {}}),
    ])
    usage = sample["provider_usage"]["total_tokens"]
    assert usage["total"] is None and usage["observed_subtotal"] == 4
    assert (usage["known_turns"], usage["unknown_turns"]) == (1, 1)
    coverage = sample["provider_usage_coverage"]
    assert coverage["scope"] == "OBSERVED_TURNS_ONLY"
    assert coverage["request_outcomes_complete"] is None
    assert coverage["prepared_without_outcome"] is None


def test_turn_index_restarts_in_another_invocation_are_not_duplicates(tmp_path):
    def rows(state, invocation):
        return [row for identity, tokens in ((invocation, 4), (str(uuid4()), 7)) for row in (
            provider_event(state, "PROVIDER_REQUEST_PREPARED", identity, 1),
            provider_event(state, "PROVIDER_TURN_OBSERVED", identity, 1, total_tokens=tokens),
        )]

    sample = provider_sample(tmp_path, rows, "COMPLETED")
    assert sample["provider_turns_observed"] == 2
    assert sample["provider_usage"]["total_tokens"]["total"] == 11


def test_conflicting_duplicate_outcomes_do_not_fabricate_usage(tmp_path):
    def rows(state, invocation):
        return [
            provider_event(state, "PROVIDER_REQUEST_PREPARED", invocation, 1),
            provider_event(state, "PROVIDER_TURN_OBSERVED", invocation, 1, total_tokens=4),
            provider_event(state, "PROVIDER_TURN_OBSERVED", invocation, 1, total_tokens=7),
        ]

    sample = provider_sample(tmp_path, rows)
    assert sample["metrics_error"] == "ValueError"
    assert "provider_usage" not in sample


def test_partial_trace_retains_known_subtotal_but_not_total(tmp_path):
    receipt, directory, state = attempt(tmp_path, "candidate-autonomous-1", "COMPLETED")
    invocation = str(uuid4())
    rows = [
        provider_event(state, "PROVIDER_REQUEST_PREPARED", invocation, 1),
        provider_event(state, "PROVIDER_TURN_OBSERVED", invocation, 1, total_tokens=4),
    ]
    (directory / "run-trace.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows) + '{"partial":')
    sample = metrics.summarize_attempt(tmp_path, receipt)
    assert sample["provider_usage"]["total_tokens"]["observed_subtotal"] == 4
    assert sample["provider_usage"]["total_tokens"]["total"] is None
    assert sample["provider_usage_coverage"]["snapshot_complete"] is False


def selected_delivery(directory, state):
    execution_id, report_id, output_id = (str(uuid4()) for _ in range(3))
    state["runtime_results"] = [
        {"stage_id": "EXECUTE", "body": {"execution_status": "SUCCEEDED",
            "execution_reference": {"reference_id": execution_id}}},
        {"stage_id": "REPORT", "body": {"report_reference": {"reference_id": report_id}}},
    ]
    destination = directory / "deliveries" / str(state["record_version"])
    rows = []
    for identity, filename, body in (
        (report_id, "REPORT.md", "Agent-authored report"),
        (output_id, "output.csv", "fixture,data\n1,2\n"),
    ):
        destination.mkdir(parents=True, exist_ok=True)
        path = destination / filename
        path.write_text(body)
        row = {"artifact_id": identity, "file": filename,
               "sha256": metrics.digest(path), "size_bytes": path.stat().st_size}
        rows.append(row)
        source = directory / "artifacts" / f"{identity}.txt"
        source.parent.mkdir(exist_ok=True)
        source.write_text(body)
        ref = {"artifact_id": identity, "run_id": state["run_id"],
               "storage_locator": str(source), "metadata": {}}
        if identity == report_id:
            ref["release_basis"] = "MODEL_AUTHORED_REPORT"
        else:
            row["execution_id"] = execution_id
            ref["metadata"] = {"execution_id": execution_id, "requested_exposure": "RAW",
                               "sha256": row["sha256"], "size_bytes": row["size_bytes"]}
        write(source.with_suffix(".json"), {
            "ref": ref, "representation": {"stored_content": body}})
    manifest = {"run_id": state["run_id"], "status": state["workflow_run"]["status"],
        "selected_execution_id": execution_id, "report_artifact_ids": [report_id],
        "reports": [rows[0]], "outputs": [rows[1]]}
    write(destination / "RESULT.json", manifest)
    return destination, manifest


def persist_state(directory, state):
    with sqlite3.connect(directory / "state.sqlite") as connection:
        connection.execute("UPDATE application_run_state SET payload=?", (json.dumps(state),))


@pytest.mark.parametrize("clarifications,classification,count", [
    ([], "NO_RECORDED_ANSWERS", 0),
    ([{"status": "WAITING"}], "NO_RECORDED_ANSWERS", 0),
    ([{"status": "ANSWERED", "answer_text": "PRIVATE_ANSWER",
       "answered_by": "PRIVATE_AUTHOR"}], "ASSISTED", 1),
    ([{"status": "RESOLVED", "answer_text": "PRIVATE_ANSWER",
       "answered_by": "PRIVATE_AUTHOR"}], "ASSISTED", 1),
    ([{"status": "ANSWERED", "answer_text": "PRIVATE_ANSWER",
       "answered_by": "PRIVATE_AUTHOR"}, {"status": "RESOLVED",
       "answer_text": "PRIVATE_ANSWER", "answered_by": "PRIVATE_AUTHOR"}], "ASSISTED", 2),
    ([{"status": "ANSWERED", "answer_text": "a" * 4000,
       "answered_by": "b" * 256}], "ASSISTED", 1),
    ([{"status": "ANSWERED", "answer_text": "a" * 4001,
       "answered_by": "PRIVATE_AUTHOR"}], "UNKNOWN", None),
    ([{"status": "RESOLVED", "answer_text": "PRIVATE_ANSWER",
       "answered_by": "b" * 257}], "UNKNOWN", None),
    ([{"status": "ANSWERED", "answer_text": " ", "answered_by": "PRIVATE_AUTHOR"}],
     "UNKNOWN", None),
    ([{"status": "ANSWERED", "answer_text": "PRIVATE_ANSWER"}], "UNKNOWN", None),
    ([{"status": "WAITING", "answer_text": "PRIVATE_ANSWER"}], "UNKNOWN", None),
    ([{"status": "UNRECOGNIZED"}], "UNKNOWN", None),
    (["PRIVATE_MALFORMED"], "UNKNOWN", None),
    (None, "UNKNOWN", None),
    ({}, "UNKNOWN", None),
])
def test_assistance_uses_persisted_clarification_status_without_exposing_answers(
    tmp_path, clarifications, classification, count,
):
    receipt, directory, state = attempt(tmp_path, "candidate-autonomous-1")
    state["workflow_run"]["clarifications"] = clarifications
    persist_state(directory, state)
    sample = metrics.summarize_attempt(tmp_path, receipt)
    assert sample["human_assistance"] == {
        "scope": "PERSISTED_CLARIFICATION_ANSWERS",
        "status": classification, "answered_count": count}
    assert "PRIVATE_" not in json.dumps(sample)
    assert sample["unassisted_delivered_report_and_outputs"] is (
        None if classification == "UNKNOWN" else False)


def test_missing_legacy_clarifications_are_unknown_not_zero(tmp_path):
    receipt, _, _ = attempt(tmp_path, "baseline-autonomous-1")
    sample = metrics.summarize_attempt(tmp_path, receipt)
    assert sample["human_assistance"]["status"] == "UNKNOWN"
    assert sample["human_assistance"]["answered_count"] is None
    assert sample["unassisted_delivered_report_and_outputs"] is None


def test_answered_continuation_delivery_is_not_counted_as_unassisted(tmp_path):
    initial_receipts = {}
    for index, clarification_status in enumerate((None, "RESOLVED", "LEGACY"), start=1):
        receipt, directory, state = attempt(
            tmp_path, f"candidate-autonomous-{index}", "COMPLETED")
        if clarification_status != "LEGACY":
            state["workflow_run"]["clarifications"] = (
                [{"status": "RESOLVED", "answer_text": "PRIVATE_ANSWER",
                  "answered_by": "PRIVATE_AUTHOR"}] if clarification_status else [])
        selected_delivery(directory, state)
        persist_state(directory, state)
        write(receipt / "PROCESS_RESULT.json", {"exit_code": 2, "elapsed_seconds": 1.0})
        initial_receipts[receipt] = (receipt / "PROCESS_RESULT.json").read_bytes()
    result = metrics.report(tmp_path)
    group = result["by_arm"]["candidate"]
    assert group["delivered_report_and_outputs"] == 3
    assert group["delivery_scope"] == "CURRENT_PERSISTED_STATE"
    assert group["unassisted_delivered_report_and_outputs"] == 1
    assert group["assisted_delivered_report_and_outputs"] == 1
    assert group["unknown_assistance_delivered_report_and_outputs"] == 1
    assert group["unknown_assistance_attempts"] == 1
    assert "does not establish absence of other human intervention" in result["claim_limit"]
    assisted = result["attempts"][1]
    assert assisted["engine_completed"] and assisted["delivered_report_and_outputs"]
    assert assisted["unassisted_delivered_report_and_outputs"] is False
    assert assisted["process_result"] == {"exit_code": 2, "elapsed_seconds": 1.0}
    assert assisted["process_result_scope"] == "INITIAL_RUNNER_ONLY"
    assert assisted["delivery_scope"] == "CURRENT_PERSISTED_STATE"
    assert "PRIVATE_" not in json.dumps(result)
    for receipt, original in initial_receipts.items():
        assert (receipt / "PROCESS_RESULT.json").read_bytes() == original


@pytest.mark.parametrize("corruption", [
    "duplicate_report", "unselected_output", "missing_registered_output", "rewritten_manifest_hash",
])
def test_delivery_requires_exact_registered_selected_inventory(tmp_path, corruption):
    receipt, directory, state = attempt(tmp_path, "candidate-autonomous-1", "COMPLETED")
    destination, manifest = selected_delivery(directory, state)
    con = sqlite3.connect(directory / "state.sqlite")
    con.execute("UPDATE application_run_state SET payload=?", (json.dumps(state),))
    con.commit()
    con.close()
    assert metrics.summarize_attempt(tmp_path, receipt)["delivered_report_and_outputs"]
    if corruption == "duplicate_report":
        manifest["reports"] *= 2
        manifest["outputs"] = []
    elif corruption == "unselected_output":
        manifest["outputs"][0]["execution_id"] = str(uuid4())
    elif corruption == "missing_registered_output":
        extra = json.loads((directory / "artifacts" / (
            manifest["outputs"][0]["artifact_id"] + ".json")).read_text())
        extra["ref"]["artifact_id"] = str(uuid4())
        write(directory / "artifacts" / (extra["ref"]["artifact_id"] + ".json"), extra)
    else:
        output = destination / manifest["outputs"][0]["file"]
        output.write_text("tampered")
        manifest["outputs"][0].update(sha256=metrics.digest(output), size_bytes=8)
    write(destination / "RESULT.json", manifest)
    assert not metrics.summarize_attempt(tmp_path, receipt)["delivered_report_and_outputs"]


def test_every_attempt_is_counted_and_unreported_usage_remains_unknown(tmp_path):
    _, first, state = attempt(tmp_path, "baseline-autonomous-1")
    attempt(tmp_path, "candidate-autonomous-1", "COMPLETED")
    (tmp_path / "receipts" / "candidate-broken-2").mkdir()
    events = [
        event(state, "PROVIDER_TURN_OBSERVED", {"numeric_facts": {"elapsed_ms": 20, "total_tokens": 4}}),
        event(state, "PROVIDER_TURN_OBSERVED", {"numeric_facts": {"elapsed_ms": 35}}),
    ]
    (first / "run-trace.jsonl").write_text("\n".join(json.dumps(item) for item in events) + "\n")
    result = metrics.report(tmp_path)
    assert result["attempt_count"] == 3
    assert result["by_arm"]["baseline"]["engine_completed"] == 0
    assert result["by_arm"]["candidate"]["engine_completed"] == 1
    assert result["by_arm"]["candidate"]["delivered_report_and_outputs"] == 0
    sample = result["attempts"][0]
    assert sample["provider_usage"]["total_tokens"] == {
        "total": None, "observed_subtotal": 4, "known_turns": 1, "unknown_turns": 1,
        "unreported_request_subtotal": None, "known_unreported_requests": 0,
        "unknown_unreported_requests": 0, "failure_response_subtotal": None,
        "known_failure_responses": 0, "unknown_failure_responses": 0}
    assert sample["provider_usage"]["elapsed_ms"]["total"] is None
    assert sample["provider_usage"]["elapsed_ms"]["observed_subtotal"] == 55
    assert sample["provider_usage_coverage"]["scope"] == "OBSERVED_TURNS_ONLY"
    assert sample["request_size"]["max_bytes"] is None
    assert all(item["scientific_review"] == "NOT_PERFORMED" for item in result["attempts"])


@pytest.mark.parametrize("workflow_status", ["FAILED", "RUNNING"])
def test_residue_checks_exact_execution_identity_not_unrelated_container(monkeypatch, workflow_status):
    identity = uuid4()
    sample = {"execution_ids": [str(identity)], "process_result": {"exit_code": 1},
              "status": workflow_status}
    runner = Mock(return_value=SimpleNamespace(stdout="unrelated\n"))
    monkeypatch.setattr(metrics.subprocess, "run", runner)
    metrics.check_process_residue([sample])
    assert sample["process_residue_check"] == "NO_MATCHING_CONTAINERS"
    runner.return_value.stdout = f"labbio-{identity.hex}\nunrelated\n"
    metrics.check_process_residue([sample])
    assert sample["process_residue_check"] == (
        "UNKNOWN_CURRENT_CONTROLLER_STATUS" if workflow_status == "RUNNING" else "RESIDUE_FOUND")
    assert sample["remaining_container_names"] == [f"labbio-{identity.hex}"]
    sample["process_result"] = None
    metrics.check_process_residue([sample])
    assert sample["process_residue_check"] == "ACTIVE_ATTEMPT"


def test_process_exit_is_distinct_from_persisted_running_workflow(tmp_path):
    receipt, _, _ = attempt(tmp_path, "candidate-autonomous-1", "RUNNING")
    write(receipt / "PROCESS_RESULT.json", {"exit_code": 1, "elapsed_seconds": 2.0})
    sample = metrics.summarize_attempt(tmp_path, receipt)
    assert sample["status"] == "RUNNING"
    assert sample["workflow_inflight_after_process_exit"] is True
    assert sample["process_result_scope"] == "INITIAL_RUNNER_ONLY"
    assert not sample["engine_completed"]


def test_residue_unavailable_is_unknown(monkeypatch):
    monkeypatch.setattr(metrics.subprocess, "run", Mock(side_effect=FileNotFoundError))
    sample = {}
    metrics.check_process_residue([sample])
    assert sample["process_residue_check"] == "UNKNOWN_DOCKER_UNAVAILABLE"


def test_residue_covers_scoped_environment_builds_and_does_not_call_resume_terminal(monkeypatch):
    build_id = uuid4().hex
    sample = {"execution_ids": [], "environment_build_ids": [build_id],
              "process_result": {"exit_code": 75}, "status": "FAILED"}
    runner = Mock(return_value=SimpleNamespace(stdout=f"builder-container\t{build_id}\nunrelated\tother\n"))
    monkeypatch.setattr(metrics.subprocess, "run", runner)
    metrics.check_process_residue([sample])
    assert sample["process_residue_check"] == "RESIDUE_FOUND"
    assert sample["remaining_container_names"] == ["builder-container"]
    sample["resume_before"] = {"run_id": "fixture"}
    metrics.check_process_residue([sample])
    assert sample["process_residue_check"] == "UNKNOWN_RESUME_STATUS"


def test_residue_does_not_claim_absence_when_trace_is_incomplete(monkeypatch):
    monkeypatch.setattr(metrics.subprocess, "run", Mock(return_value=SimpleNamespace(stdout="")))
    sample = {"execution_ids": [], "process_result": {"exit_code": 1}, "status": "FAILED",
              "trace_integrity_valid": False}
    metrics.check_process_residue([sample])
    assert sample["process_residue_check"] == "UNKNOWN_EXECUTION_IDENTITIES"


def test_truncated_or_invalid_trace_lines_are_not_silently_accepted(tmp_path):
    path = tmp_path / "trace"
    path.write_text('{"ok":1}\nmalformed\n{"partial":')
    records, health = metrics.json_lines(path)
    assert records == [{"ok": 1}]
    assert health == {"present": True, "invalid_lines": 1, "partial_final_line": True}


def test_wrong_run_trace_is_flagged_without_echoing_sensitive_content(tmp_path):
    receipt, directory, state = attempt(tmp_path, "baseline-autonomous-1")
    trace = event(state, "PROVIDER_TURN_OBSERVED", {"secret": "DO_NOT_PRINT"})
    trace["run_id"] = str(uuid4())
    (directory / "run-trace.jsonl").write_text(json.dumps(trace) + "\n")
    result = metrics.summarize_attempt(tmp_path, receipt)
    assert result["metrics_error"] == "ValueError"
    assert "DO_NOT_PRINT" not in json.dumps(result)


def test_delivery_is_selected_by_persisted_ids_not_mtime_or_largest_version(tmp_path):
    directory = tmp_path / "run"
    state = {"run_id": "run-fixture", "record_version": 8,
        "workflow_run": {"status": "COMPLETED"},
        "runtime_results": [
            {"stage_id": "EXECUTE", "body": {"execution_status": "SUCCEEDED",
                "execution_reference": {"reference_id": "exec-selected"}}},
            {"stage_id": "REPORT", "body": {"report_reference": {"reference_id": "report-selected"}}},
        ]}
    manifest = {"run_id": state["run_id"], "status": "COMPLETED",
        "selected_execution_id": "exec-selected", "report_artifact_ids": ["report-selected"],
        "reports": [], "outputs": []}
    write(directory / "deliveries" / "999" / "RESULT.json", manifest)
    assert not metrics.delivery_metrics(directory, state)["snapshot_current"]
    destination = directory / "deliveries" / "8"
    destination.mkdir()
    (destination / "REPORT.md").write_text("fixture")
    manifest["reports"] = [{"artifact_id": "report-selected", "file": "REPORT.md",
        "sha256": metrics.digest(destination / "REPORT.md"), "size_bytes": 7}]
    write(directory / "artifacts" / "report-selected.json", {
        "ref": {"artifact_id": "report-selected", "run_id": state["run_id"],
                "release_basis": "MODEL_AUTHORED_REPORT", "metadata": {}},
        "representation": {"stored_content": "fixture"}})
    write(destination / "RESULT.json", manifest)
    assert metrics.delivery_metrics(directory, state)["integrity_valid"]
    (destination / "REPORT.md").write_text("changed")
    assert not metrics.delivery_metrics(directory, state)["integrity_valid"]


def checkpoint():
    run_id, invocation_id, evidence_id = (str(uuid4()) for _ in range(3))
    state = {"run_id": run_id, "record_version": 12,
        "recovery_state": "STAGE_IN_FLIGHT", "inflight_stage": "EXECUTE",
        "inflight_invocation_id": invocation_id,
        "inflight_evidence": {"run_id": run_id, "stage_id": "EXECUTE", "invocation_id": invocation_id,
                             "evidence_id": evidence_id, "items": []}}
    stop = {"run_id": run_id, "stage": "EXECUTE", "evidence_id": evidence_id,
            "boundary": "AFTER_DURABLE_CAPABILITY_EVIDENCE"}
    return state, stop


@pytest.mark.parametrize("change", [
    {"evidence_id": str(uuid4())}, {"run_id": str(uuid4())},
    {"stage": "PLAN"}, {"boundary": "PROVIDER_RETURNED"},
])
def test_injected_stop_must_match_exact_persisted_checkpoint(change):
    state, stop = checkpoint()
    with pytest.raises(ValueError, match="exact durable"):
        metrics.validate_checkpoint(state, {**stop, **change})


@pytest.mark.asyncio
@pytest.mark.parametrize("action,uncertain,version", [
    ("STABLE", False, 12), ("BLOCKED", True, 12),
    ("FINALIZE_ONLY", True, 12), ("FINALIZE_ONLY", False, 13),
])
async def test_resume_rejects_uncertain_changed_or_noncheckpoint_state_before_provider(action, uncertain, version):
    state, stop = checkpoint()
    assessment = SimpleNamespace(continuation_action=action, uncertain_side_effects=uncertain,
        recovery=SimpleNamespace(recoverable=True, record_version=version))
    application = SimpleNamespace(reconcile_run=Mock(return_value=assessment), continue_run=AsyncMock())
    settings = SimpleNamespace(principal="principal", workspace="workspace", provider="provider")
    load, export = Mock(), Mock()
    with pytest.raises(ValueError, match="finalization-only"):
        await metrics.continue_checked(application, settings, state, stop, load_provider=load, export=export)
    load.assert_not_called()
    application.continue_run.assert_not_awaited()
    export.assert_not_called()


@pytest.mark.asyncio
async def test_valid_resume_uses_native_continue_once_not_execution_submission():
    state, stop = checkpoint()
    assessment = SimpleNamespace(continuation_action="FINALIZE_ONLY", uncertain_side_effects=False,
        recovery=SimpleNamespace(recoverable=True, record_version=12))
    outcome = SimpleNamespace(status=SimpleNamespace(value="COMPLETED"))
    application = SimpleNamespace(reconcile_run=Mock(return_value=assessment),
                                  reconcile_jobs=AsyncMock(return_value=()),
                                  continue_run=AsyncMock(return_value=outcome))
    settings = SimpleNamespace(principal="principal", workspace="workspace", provider="provider")
    load, export = Mock(), Mock(return_value={"delivery": "fixture"})
    result, delivery = await metrics.continue_checked(
        application, settings, state, stop, load_provider=load, export=export)
    application.continue_run.assert_awaited_once()
    assert str(application.continue_run.await_args.args[0]) == state["run_id"]
    load.assert_called_once_with("provider")
    export.assert_called_once()
    assert result is outcome and delivery == {"delivery": "fixture"}


@pytest.mark.asyncio
@pytest.mark.parametrize("corruption", [None, "truncated_trace", "foreign_trace", "no_delivery"])
async def test_full_resume_wrapper_preserves_execution_ids_and_refuses_second_attempt(
    tmp_path, monkeypatch, corruption,
):
    from labbioagentos import cli, local_config, local_continuation
    root, source = tmp_path / "results", tmp_path / "source"
    source.mkdir()
    name = "candidate-recovery-1"
    receipt, directory = root / "receipts" / name, root / "runs" / name
    state, stop = checkpoint()
    state["runtime_revision"] = "frozen"
    final_state = {**state, "record_version": state["record_version"] + 1,
                   "workflow_run": {"status": "COMPLETED"}}
    selected_delivery(directory, final_state)
    runtime = {"runtime_revision": "frozen"}
    write(receipt / "MANIFEST.json", {"case": "recovery", "source": str(source),
        "runner_sha256": metrics.digest(Path(metrics.__file__).with_name("runtime_stress.py")),
        "inputs": []})
    write(receipt / "PROCESS_RESULT.json", {"exit_code": 75})
    write(receipt / "CONFIG.json", {})
    write(receipt / "INJECTED_STOP.json", stop)
    write(directory / "RUNTIME.json", runtime)
    started = event(state, "EXECUTION_STARTED", {"execution_id": "execution-fixture"})
    (directory / "run-trace.jsonl").write_text(json.dumps(started) + "\n")
    settings = SimpleNamespace(result_root=root / "runs", principal="principal",
                               workspace="workspace", provider="provider")
    assessment = SimpleNamespace(continuation_action="FINALIZE_ONLY", uncertain_side_effects=False,
        recovery=SimpleNamespace(recoverable=True, record_version=state["record_version"]))
    application = SimpleNamespace(reconcile_run=Mock(return_value=assessment),
        reconcile_jobs=AsyncMock(return_value=()),
        continue_run=AsyncMock(return_value=SimpleNamespace(status=SimpleNamespace(value="COMPLETED"))))
    build, close, load = Mock(return_value=application), Mock(), Mock()
    monkeypatch.setattr(metrics, "state_snapshot",
        lambda _: final_state if application.continue_run.await_count else state)
    monkeypatch.setattr(local_config, "LocalSettings",
                        SimpleNamespace(model_validate_json=lambda _: settings))
    monkeypatch.setattr(local_config, "runtime_manifest", lambda _: runtime)
    monkeypatch.setattr(local_config, "_load_provider", load)
    monkeypatch.setattr(local_continuation, "run_writer", lambda _: nullcontext())
    def export(*_):
        if corruption == "truncated_trace":
            with (directory / "run-trace.jsonl").open("a") as stream:
                stream.write('{"event_type":"EXECUTION_STARTED",')
        if corruption == "foreign_trace":
            foreign = event({**state, "run_id": str(uuid4())}, "EXECUTION_COMPLETED", {})
            with (directory / "run-trace.jsonl").open("a") as stream:
                stream.write(json.dumps(foreign) + "\n")
        if corruption == "no_delivery":
            return None
        return {"status": "delivered"}
    monkeypatch.setattr(local_continuation, "export_snapshot", export)
    monkeypatch.setattr(cli, "build_application", build)
    monkeypatch.setattr(cli, "_close", close)
    assert await metrics.resume(root, name, source) == (0 if corruption is None else 2)
    result = metrics.read_json(receipt / "RESUME_RESULT.json")
    assert result["no_reexecution"] is (corruption not in {"truncated_trace", "foreign_trace"})
    assert result["execution_ids_before"] == result["execution_ids_after"] == ["execution-fixture"]
    assert metrics.read_json(receipt / "RESUME_BEFORE.json")["evidence_id"] == stop["evidence_id"]
    application.continue_run.assert_awaited_once()
    with pytest.raises((FileExistsError, ValueError)):
        await metrics.resume(root, name, source)
    application.continue_run.assert_awaited_once()
    load.assert_called_once()
    assert close.call_count == (1 if corruption in {"truncated_trace", "foreign_trace"} else 2)
