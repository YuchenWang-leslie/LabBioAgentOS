"""Engineering-only attempt metrics and exact, non-replaying recovery continuation.

``report`` reads every receipt directory, including incomplete/failed attempts.
``resume`` is an explicit write operation only for the runner's exact durable
EXECUTE interruption. It does not inject programs, answers or scientific choices.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from urllib.parse import quote
from uuid import UUID


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path: Path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def write_new(path: Path, value):
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")


def json_lines(path: Path):
    """A concurrent final write is incomplete, not a fabricated missing event."""
    if not path.exists():
        return [], {"present": False, "invalid_lines": 0, "partial_final_line": False}
    records, invalid, partial = [], 0, False
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                if line.endswith("\n"):
                    invalid += 1
                else:
                    partial = True
    return records, {"present": True, "invalid_lines": invalid, "partial_final_line": partial}


def state_snapshot(directory: Path):
    run_id = str(UUID(read_json(directory / "RUN.json")["run_id"]))
    database = directory / "state.sqlite"
    connection = sqlite3.connect("file:" + quote(str(database), safe="/") + "?mode=ro", uri=True)
    try:
        row = connection.execute(
            "SELECT record_version,payload FROM application_run_state WHERE run_id=?", (run_id,)
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise ValueError("No authoritative run record")
    state = json.loads(row[1])
    if (state["run_id"], state["record_version"]) != (run_id, row[0]):
        raise ValueError("Run identity or version mismatch")
    return state


def execution_ids(events):
    """Execution-start IDs are identities; terminal event counts can be duplicated."""
    return sorted({item["payload"]["execution_id"] for item in events
                   if item.get("event_type") == "EXECUTION_STARTED"
                   and item.get("payload", {}).get("execution_id")})


def trace_valid(events, health, run_id):
    seen = {}
    for item in events:
        if (not isinstance(item, dict) or item.get("run_id") != run_id
                or not item.get("event_id")
                or (item["event_id"] in seen and seen[item["event_id"]] != item)):
            return False
        seen[item["event_id"]] = item
    return health["present"] and not (health["invalid_lines"] or health["partial_final_line"])


def selected_ids(state):
    execution, report = None, None
    for result in state.get("runtime_results", []):
        body = result.get("body", {})
        if result.get("stage_id") == "EXECUTE":
            ref = body.get("execution_reference") or {}
            execution = ref.get("reference_id") if body.get("execution_status") == "SUCCEEDED" else None
        if result.get("stage_id") == "REPORT":
            report = (body.get("report_reference") or {}).get("reference_id")
    return execution, report


def usage_totals(turns, field, *, unreported=(), failure_responses=(), complete=False):
    values = [(item.get("payload", {}).get("numeric_facts") or {}).get(field) for item in turns]
    known = [value for value in values if type(value) in (int, float)]
    failed = [(item.get("payload", {}).get("numeric_facts") or {}).get(field)
              for item in unreported]
    failed_known = [value for value in failed if type(value) in (int, float)]
    reported_failures = [(item.get("payload", {}).get("numeric_facts") or {}).get(field)
                         for item in failure_responses]
    known_failures = [value for value in reported_failures if type(value) in (int, float)]
    return {"total": (sum(known) + sum(failed_known) + sum(known_failures)
                     if complete and (values or failed or reported_failures)
                     and len(known) == len(values) and len(failed_known) == len(failed)
                     and len(known_failures) == len(reported_failures) else None),
            "observed_subtotal": sum(known) if known else None,
            "known_turns": len(known), "unknown_turns": len(values) - len(known),
            "unreported_request_subtotal": sum(failed_known) if failed_known else None,
            "known_unreported_requests": len(failed_known),
            "unknown_unreported_requests": len(failed) - len(failed_known),
            "failure_response_subtotal": sum(known_failures) if known_failures else None,
            "known_failure_responses": len(known_failures),
            "unknown_failure_responses": len(reported_failures) - len(known_failures)}


def provider_usage_records(events, *, snapshot_complete):
    """Prepared is not sent or billed; only an outcome supplies measured usage."""
    groups = {name: {} for name in (
        "PROVIDER_REQUEST_PREPARED", "PROVIDER_TURN_OBSERVED",
        "PROVIDER_USAGE_UNREPORTED", "PROVIDER_USAGE_ON_FAILURE")}
    uncorrelated = set()
    for event in events:
        kind = event["event_type"]
        if kind not in groups:
            continue
        payload = event.get("payload", {})
        invocation = payload.get("kernel_invocation_id")
        index = (payload.get("numeric_facts") or {}).get("turn_index")
        if isinstance(invocation, str) and invocation and type(index) is int and index > 0:
            identity = (invocation, index)
        else:
            identity = ("event", event["event_id"])
            uncorrelated.add((kind, identity))
        previous = groups[kind].get(identity)
        if previous is not None and (
            previous.get("payload") != event.get("payload")
            or previous.get("status") != event.get("status")
        ):
            raise ValueError("Conflicting provider request accounting")
        groups[kind][identity] = event
    prepared = groups["PROVIDER_REQUEST_PREPARED"]
    observed = groups["PROVIDER_TURN_OBSERVED"]
    unreported = groups["PROVIDER_USAGE_UNREPORTED"]
    failure_responses = groups["PROVIDER_USAGE_ON_FAILURE"]
    if (observed.keys() & unreported.keys()
            or failure_responses.keys() & (observed.keys() | unreported.keys())):
        raise ValueError("Conflicting provider request outcomes")
    ready = {identity for identity, event in prepared.items() if event["status"] == "READY"}
    rejected = {identity for identity, event in prepared.items() if event["status"] == "REJECTED"}
    outcomes = observed.keys() | unreported.keys() | failure_responses.keys()
    has_request_trace = bool(prepared or unreported or failure_responses)
    complete = (not uncorrelated and len(ready) + len(rejected) == len(prepared)
                and ready == outcomes) if has_request_trace else None
    coverage = {
        "scope": "RECORDED_PROVIDER_REQUESTS" if has_request_trace else "OBSERVED_TURNS_ONLY",
        "snapshot_complete": snapshot_complete,
        "prepared_requests": len(ready),
        "rejected_preparations": len(rejected),
        "unreported_usage_requests": len(unreported),
        "failure_usage_requests": len(failure_responses),
        "failure_usage_scope": "RECORDED_VALIDATED_FAILURE_RESPONSE_USAGE",
        "prepared_without_outcome": len(ready - outcomes) if prepared else None,
        "uncorrelated_events": len(uncorrelated),
        "request_outcomes_complete": complete,
    }
    return (list(observed.values()), list(unreported.values()), list(failure_responses.values()),
            list(prepared.values()), coverage)


def repeated_read_requests(boundaries):
    """Count only reconstructable requests, not equal answers or wasted work.

    Read credit must be re-established in each phase. Repeated requests can be
    intentional; this metric is descriptive and never changes runtime behavior.
    """
    reads = {"artifact_query", "artifact_aggregate", "artifact_list", "file_read",
             "report_read", "execution_inspect", "evidence_read", "skill_view",
             "environment_list"}
    aggregate_fields = ("artifact_id", "operation", "field", "match_field", "match_value")
    seen_ids, seen_requests, repeats = set(), set(), Counter()
    measured = unknown = 0
    for boundary in boundaries:
        if boundary.get("kind") != "capability_evidence":
            continue
        bundle = boundary["payload"]
        for item in bundle.get("items", []):
            identity = item["capability_invocation_id"]
            capability = item["capability_name"]
            if identity in seen_ids or capability not in reads:
                continue
            seen_ids.add(identity)
            request = None
            if capability == "artifact_query":
                recorded = item.get("artifact_query_request")
                if isinstance(recorded, dict) and all(
                    field in recorded for field in ("artifact_id", "view_type", "limit", "offset")
                ):
                    request = recorded
            elif capability == "artifact_aggregate" and item["status"] == "COMPLETED":
                result = item.get("safe_result") or {}
                if isinstance(result, dict) and all(field in result for field in aggregate_fields):
                    request = {field: result[field] for field in aggregate_fields}
            if (not isinstance(request, dict) or not bundle.get("invocation_id")
                    or not item.get("actor_profile_key")):
                unknown += 1
                continue
            measured += 1
            key = (bundle["invocation_id"], item.get("actor_profile_key"), capability,
                   json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
            if key in seen_requests:
                repeats[capability] += 1
            seen_requests.add(key)
    return {"scope": "RECORDED_REQUEST_WITHIN_PHASE_AND_ACTOR",
            "measured_calls": measured, "unmeasured_read_calls": unknown,
            "repeated_calls": sum(repeats.values()) if measured else None,
            "by_capability": dict(repeats)}


def delivery_metrics(directory, state):
    execution_id, report_id = selected_ids(state)
    exact = directory / "deliveries" / str(state["record_version"]) / "RESULT.json"
    path = exact if exact.exists() else directory / "delivery" / "RESULT.json"
    result = {"selected_execution_id": execution_id, "selected_report_id": report_id,
              "snapshot_current": False, "files": [], "integrity_valid": None,
              "complete_selected_inventory": False}
    if not path.exists():
        return result
    manifest = read_json(path)
    if (manifest.get("run_id") != state["run_id"]
            or manifest.get("status") != state["workflow_run"]["status"]
            or manifest.get("selected_execution_id") != execution_id
            or manifest.get("report_artifact_ids", []) != ([report_id] if report_id else [])):
        return result
    result["snapshot_current"] = True
    # The delivery manifest is a copy inventory, not the source of truth for
    # selected identities or content. Bind it to the run's registered Artifacts.
    registry = {}
    store = directory / "artifacts"
    excluded = {*state.get("input_artifact_ids", []), *state.get("context_artifact_ids", [])}
    for record in store.glob("*.json"):
        if record.is_symlink():
            raise ValueError("Artifact registry contains a symlink")
        envelope = read_json(record)
        ref = envelope["ref"]
        if ref["artifact_id"] != record.stem:
            raise ValueError("Artifact registry identity mismatch")
        if ref.get("run_id") == state["run_id"] and ref["artifact_id"] not in excluded:
            registry[ref["artifact_id"]] = envelope
    expected_outputs = {
        identity for identity, envelope in registry.items()
        if "requested_exposure" in envelope["ref"].get("metadata", {})
        and envelope["ref"]["metadata"].get("execution_id") == execution_id
    } if execution_id else set()
    reports, outputs = manifest.get("reports", []), manifest.get("outputs", [])
    report_ids = [item["artifact_id"] for item in reports]
    output_ids = [item["artifact_id"] for item in outputs]
    result["complete_selected_inventory"] = bool(
        report_id and report_ids == [report_id] and expected_outputs
        and len(output_ids) == len(set(output_ids)) and set(output_ids) == expected_outputs
        and all(item.get("execution_id") == execution_id for item in outputs))
    filenames = [item["file"] for item in (*reports, *outputs)]
    unique_files = len(filenames) == len(set(filenames))
    for item in (*manifest.get("reports", []), *manifest.get("outputs", [])):
        target = path.parent / item["file"]
        if (target.is_symlink() or not target.resolve().is_relative_to(path.parent.resolve())
                or not target.is_file()):
            valid = False
        else:
            valid = digest(target) == item["sha256"] and target.stat().st_size == item["size_bytes"]
        envelope = registry.get(item["artifact_id"])
        registered = False
        if envelope:
            ref = envelope["ref"]
            if item in reports and ref.get("release_basis") == "MODEL_AUTHORED_REPORT":
                content = envelope["representation"].get("stored_content")
                if isinstance(content, str):
                    raw = content.encode("utf-8")
                    registered = (len(raw), hashlib.sha256(raw).hexdigest()) == (
                        item["size_bytes"], item["sha256"])
            elif item in outputs and item["artifact_id"] in expected_outputs:
                metadata = ref["metadata"]
                source = Path(ref["storage_locator"])
                registered = (
                    not source.is_symlink() and source.is_file()
                    and source.resolve().is_relative_to(store.resolve())
                    and (metadata.get("size_bytes"), metadata.get("sha256")) == (
                        item["size_bytes"], item["sha256"])
                    and (source.stat().st_size, digest(source)) == (
                        item["size_bytes"], item["sha256"]))
        result["files"].append({"artifact_id": item["artifact_id"], "sha256": item["sha256"],
                                "size_bytes": item["size_bytes"],
                                "integrity_valid": valid and registered})
    result["integrity_valid"] = bool(result["files"]) and unique_files and all(
        item["integrity_valid"] for item in result["files"])
    return result


def delivery_verified(delivery):
    return bool(delivery["snapshot_current"] and delivery["integrity_valid"]
                and delivery["complete_selected_inventory"])


def clarification_assistance(workflow_run):
    """Measure only persisted answers, not every possible human intervention."""
    result = {"scope": "PERSISTED_CLARIFICATION_ANSWERS",
              "status": "UNKNOWN", "answered_count": None}
    records = workflow_run.get("clarifications")
    if not isinstance(records, list):
        return result
    answered = 0
    for record in records:
        if not isinstance(record, dict):
            return result
        status = record.get("status")
        answer, author = record.get("answer_text"), record.get("answered_by")
        if status == "WAITING":
            if answer is not None or author is not None:
                return result
        elif status in ("ANSWERED", "RESOLVED"):
            if (not isinstance(answer, str) or not answer.strip()
                    or len(answer) > 4000 or not isinstance(author, str)
                    or not 1 <= len(author) <= 256):
                return result
            answered += 1
        else:
            return result
    result.update(status="ASSISTED" if answered else "NO_RECORDED_ANSWERS",
                  answered_count=answered)
    return result


def summarize_attempt(root: Path, receipt: Path):
    """Never emit task text, model prose, credentials or arbitrary exception text."""
    result = {"attempt": receipt.name, "scientific_review": "NOT_PERFORMED",
              "process_residue_check": "NOT_PERFORMED",
              "process_result_scope": "INITIAL_RUNNER_ONLY",
              "delivery_scope": "CURRENT_PERSISTED_STATE",
              "human_assistance": clarification_assistance({}),
              "unassisted_delivered_report_and_outputs": None}
    try:
        manifest = read_json(receipt / "MANIFEST.json")
        result.update({key: manifest.get(key) for key in ("arm", "case", "replicate", "source_commit")})
        result["input_sha256"] = [item["sha256"] for item in manifest["inputs"]]
        result["runner_sha256"] = manifest["runner_sha256"]
        result["process_result"] = (read_json(receipt / "PROCESS_RESULT.json")
                                    if (receipt / "PROCESS_RESULT.json").exists() else None)
        result["environment_build_ids"] = []
        config_path = receipt / "CONFIG.json"
        if config_path.exists():
            configured = (read_json(config_path).get("environment") or {}).get("root")
            environment_root = root / "environments" / receipt.name
            if configured and Path(configured).resolve() == environment_root.resolve():
                result["environment_build_ids"] = sorted(
                    path.name for path in (environment_root / "attempts").glob("*")
                    if path.is_dir() and not path.is_symlink() and len(path.name) == 32
                    and all(char in "0123456789abcdef" for char in path.name))
        directory = root / "runs" / receipt.name
        runtime = read_json(directory / "RUNTIME.json")
        result["runtime_revision"] = runtime["runtime_revision"]
        result["runtime_source"] = runtime.get("source")
        state = state_snapshot(directory)
        result.update(run_id=state["run_id"], record_version=state["record_version"],
                      status=state["workflow_run"]["status"],
                      current_stage=state["workflow_run"]["current_stage"],
                      failure_reason=state["workflow_run"].get("failure_reason"),
                      recovery_state=state["recovery_state"])
        result["human_assistance"] = clarification_assistance(state["workflow_run"])
        # This compares the initial runner receipt with the current Workflow;
        # it does not establish whether a continuation controller is alive.
        result["workflow_inflight_after_process_exit"] = (
            result["process_result"] is not None and result["status"] == "RUNNING")
        events, result["trace_read"] = json_lines(directory / "run-trace.jsonl")
        boundaries, result["boundary_read"] = json_lines(directory / "model-boundaries.jsonl")
        result["trace_integrity_valid"] = trace_valid(events, result["trace_read"], state["run_id"])
        if events and not trace_valid(events, {**result["trace_read"], "present": True,
                                               "invalid_lines": 0, "partial_final_line": False},
                                      state["run_id"]):
            raise ValueError("Run trace identity conflict")
        events = list({item["event_id"]: item for item in events}.values())
        turns, unreported, failure_responses, preparations, coverage = provider_usage_records(
            events, snapshot_complete=(
                result["trace_integrity_valid"]
                and result["status"] in {"COMPLETED", "FAILED", "CANCELLED"}),
        )
        snapshots = []
        for event in unreported:
            if event.get("status") != "PARTIAL_RESPONSE_USAGE":
                continue
            payload = event["payload"]
            facts = payload["numeric_facts"]
            invocation, index = payload.get("kernel_invocation_id"), facts.get("turn_index")
            if not isinstance(invocation, str) or type(index) is not int or index < 1:
                raise ValueError("Invalid nonfinal usage identity")
            snapshot = {"kernel_invocation_id": str(UUID(invocation)), "turn_index": index}
            for field in ("input_tokens", "output_tokens", "total_tokens"):
                key = "last_validated_" + field
                value = facts.get(key)
                if (facts.get(field) is not None
                        or (value is not None and (type(value) is not int or value < 0))):
                    raise ValueError("Invalid nonfinal usage snapshot")
                snapshot[key] = value
            snapshots.append(snapshot)
        coverage["nonfinal_usage_snapshot_requests"] = len(snapshots)
        result["provider_usage_snapshots"] = {
            "scope": "NONFINAL_REPORTED_SNAPSHOTS_NOT_TOTALS_OR_LOWER_BOUNDS",
            "records": snapshots,
        }
        result["provider_turns_observed"] = len(turns)
        result["provider_usage_coverage"] = coverage
        result["provider_usage"] = {
            field: usage_totals(turns, field, unreported=unreported,
                               failure_responses=failure_responses,
                               complete=(coverage["snapshot_complete"]
                                         and coverage["request_outcomes_complete"] is True))
            for field in ("input_tokens", "output_tokens", "total_tokens", "elapsed_ms")}
        sizes = [(item.get("payload", {}).get("numeric_facts") or {}).get("request_bytes")
                 for item in preparations]
        sizes = [size for size in sizes if type(size) is int]
        result["request_size"] = {"measured_requests": len(sizes), "max_bytes": max(sizes) if sizes else None}
        evidence = {}
        for boundary in boundaries:
            if boundary.get("kind") == "capability_evidence":
                bundle = boundary["payload"]
                if bundle["run_id"] != state["run_id"]:
                    raise ValueError("Foreign capability evidence")
                for item in bundle.get("items", []):
                    evidence[item["capability_invocation_id"]] = item
        calls = list(evidence.values())
        result["capability_outcomes_recorded"] = dict(Counter(item["status"] for item in calls))
        result["capability_failures"] = dict(Counter(
            item["error_code"] for item in calls if item.get("error_code")))
        result["tool_batch_rejections"] = sum(item["event_type"] == "TOOL_BATCH_REJECTED" for item in events)
        result["repeated_read_requests"] = repeated_read_requests(boundaries)
        result["capability_terminations"] = dict(Counter(
            item["payload"].get("termination_reason", "UNKNOWN") for item in events
            if item["event_type"] == "CAPABILITY_PHASE_COMPLETED"))
        result["environment_build_calls"] = sum(item["capability_name"] == "environment_build" for item in calls)
        result["execution_revise_calls"] = sum(item["capability_name"] == "execution_revise" for item in calls)
        result["execution_ids"] = execution_ids(events)
        result["execution_start_events"] = sum(item["event_type"] == "EXECUTION_STARTED" for item in events)
        # Executor and submission service can both emit the same terminal receipt.
        terminal_executions = {item["payload"]["execution_id"]: item["status"] for item in events
            if item["event_type"] in {"EXECUTION_COMPLETED", "EXECUTION_FAILED"}
            and item.get("payload", {}).get("execution_id")}
        result["execution_outcomes"] = dict(Counter(terminal_executions.values()))
        result["delivery"] = delivery_metrics(directory, state)
        result["engine_completed"] = result["status"] == "COMPLETED"
        result["delivered_report_and_outputs"] = delivery_verified(result["delivery"])
        assistance = result["human_assistance"]["status"]
        if assistance != "UNKNOWN":
            result["unassisted_delivered_report_and_outputs"] = (
                assistance == "NO_RECORDED_ANSWERS" and result["delivered_report_and_outputs"])
        for name in ("INJECTED_STOP", "RESUME_BEFORE", "RESUME_RESULT"):
            if (receipt / f"{name}.json").exists():
                result[name.lower()] = read_json(receipt / f"{name}.json")
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
        result["metrics_error"] = type(exc).__name__
        result.setdefault("status", "UNKNOWN")
    return result


def check_process_residue(attempts):
    """Read exact Docker container identities; never remove unrelated processes."""
    try:
        completed = subprocess.run(
            ["docker", "ps", "--all", "--format",
             '{{.Names}}\t{{.Label "labbio.environment.build"}}'],
            capture_output=True, text=True, timeout=15, check=True,
        )
        containers = {line.partition("\t")[0]: line.partition("\t")[2]
                      for line in completed.stdout.splitlines()}
    except (OSError, subprocess.SubprocessError):
        for item in attempts:
            item["process_residue_check"] = "UNKNOWN_DOCKER_UNAVAILABLE"
        return
    for item in attempts:
        if "execution_ids" not in item or item.get("trace_integrity_valid") is False:
            item["process_residue_check"] = "UNKNOWN_EXECUTION_IDENTITIES"
            continue
        try:
            expected = {f"labbio-{UUID(value).hex}" for value in item["execution_ids"]}
        except (ValueError, TypeError, AttributeError):
            item["process_residue_check"] = "UNKNOWN_EXECUTION_IDENTITIES"
            continue
        builds = set(item.get("environment_build_ids", []))
        expected.update(f"labbio-env-check-{value}" for value in builds)
        remaining = sorted(name for name, build_id in containers.items()
                           if name in expected or build_id in builds)
        item["remaining_container_names"] = remaining
        item["process_residue_scope"] = "EXACT_EXECUTION_AND_KNOWN_ENVIRONMENT_DOCKER_CONTAINERS"
        if "resume_before" in item and "resume_result" not in item:
            item["process_residue_check"] = "UNKNOWN_RESUME_STATUS"
            continue
        # The original runner can exit before another controller continues.
        # A RUNNING snapshot and the old receipt cannot prove current residue.
        terminal = item.get("process_result") is not None
        item["process_residue_check"] = (
            "UNKNOWN_CURRENT_CONTROLLER_STATUS" if (
                remaining and terminal and item.get("status") == "RUNNING") else
            "RESIDUE_FOUND" if remaining and terminal else
            "ACTIVE_ATTEMPT" if not terminal else "NO_MATCHING_CONTAINERS"
        )


def report(root: Path, *, check_processes=False):
    attempts = [summarize_attempt(root, path) for path in sorted((root / "receipts").iterdir())
                if path.is_dir()]
    if check_processes:
        check_process_residue(attempts)
    return {"generated_at": datetime.now(timezone.utc).isoformat(),
            "claim_limit": (
                "Descriptive engineering evidence only; no scientific validation. "
                "Assistance classification covers persisted clarification answers only and "
                "does not establish absence of other human intervention."),
            "attempt_count": len(attempts),
            "by_arm": {arm: {"attempts": len(group),
                "delivery_scope": "CURRENT_PERSISTED_STATE",
                "assistance_scope": "PERSISTED_CLARIFICATION_ANSWERS_ONLY",
                "engine_completed": sum(item.get("engine_completed", False) for item in group),
                "delivered_report_and_outputs": sum(item.get("delivered_report_and_outputs", False) for item in group),
                "unassisted_delivered_report_and_outputs": sum(
                    item["unassisted_delivered_report_and_outputs"] is True for item in group),
                "assisted_delivered_report_and_outputs": sum(
                    item.get("delivered_report_and_outputs", False)
                    and item["human_assistance"]["status"] == "ASSISTED" for item in group),
                "unknown_assistance_delivered_report_and_outputs": sum(
                    item.get("delivered_report_and_outputs", False)
                    and item["human_assistance"]["status"] == "UNKNOWN" for item in group),
                "unknown_assistance_attempts": sum(
                    item["human_assistance"]["status"] == "UNKNOWN" for item in group),
                "unknown_status": sum(item.get("status") == "UNKNOWN" for item in group)}
                for arm in ("baseline", "candidate")
                for group in [[item for item in attempts if item.get("arm") == arm]]},
            "attempts": attempts}


def validate_checkpoint(state, stop):
    evidence = state.get("inflight_evidence")
    if (stop.get("boundary") != "AFTER_DURABLE_CAPABILITY_EVIDENCE"
            or stop.get("stage") != "EXECUTE"
            or stop.get("run_id") != state["run_id"]
            or state.get("recovery_state") != "STAGE_IN_FLIGHT"
            or state.get("inflight_stage") != "EXECUTE"
            or not evidence or evidence.get("evidence_id") != stop.get("evidence_id")
            or evidence.get("run_id") != state["run_id"]
            or evidence.get("stage_id") != "EXECUTE"
            or evidence.get("invocation_id") != state.get("inflight_invocation_id")):
        raise ValueError("Injected stop does not match the exact durable EXECUTE checkpoint")


async def continue_checked(application, settings, state, stop, *, load_provider, export):
    """Native continuation under caller's writer lease; injectable only for tests."""
    validate_checkpoint(state, stop)
    run_id = UUID(state["run_id"])
    assessment = application.reconcile_run(
        run_id, principal=settings.principal, workspace=settings.workspace)
    if (assessment.continuation_action != "FINALIZE_ONLY" or assessment.uncertain_side_effects
            or not assessment.recovery.recoverable
            or assessment.recovery.record_version != state["record_version"]):
        raise ValueError("Reconciliation did not confirm exact finalization-only recovery")
    await application.reconcile_jobs(
        run_id, principal=settings.principal, workspace=settings.workspace)
    # Normal continuation loads provider only after host-authoritative reconciliation.
    load_provider(settings.provider)
    result = await application.continue_run(
        run_id, principal=settings.principal, workspace=settings.workspace)
    delivery = export(application, run_id, settings)
    return result, delivery


async def resume(root: Path, attempt: str, source: Path):
    if Path(attempt).name != attempt or attempt in {"", ".", ".."}:
        raise ValueError("An exact attempt directory name is required")
    receipt, directory = root / "receipts" / attempt, root / "runs" / attempt
    manifest = read_json(receipt / "MANIFEST.json")
    if (manifest["case"] != "recovery"
            or manifest["source"] != str(source.resolve(strict=True))
            or read_json(receipt / "PROCESS_RESULT.json")["exit_code"] != 75
            or digest(Path(__file__).with_name("runtime_stress.py")) != manifest["runner_sha256"]):
        raise ValueError("Attempt is not the exact interrupted recovery workload")
    for item in manifest["inputs"]:
        if digest(Path(item["path"])) != item["sha256"]:
            raise ValueError("Frozen input hash changed")
    sys.path.insert(0, str(source / "src"))
    from labbioagentos import cli
    from labbioagentos.local_config import LocalSettings, _load_provider, runtime_manifest
    from labbioagentos.local_continuation import run_writer, export_snapshot
    settings = LocalSettings.model_validate_json((receipt / "CONFIG.json").read_bytes())
    if settings.result_root.resolve() != (root / "runs").resolve():
        raise ValueError("Recovery configuration targets another result root")
    if runtime_manifest(settings) != read_json(directory / "RUNTIME.json"):
        raise ValueError("Runtime or configuration changed since the interrupted attempt")
    stop = read_json(receipt / "INJECTED_STOP.json")
    with run_writer(directory):
        state = state_snapshot(directory)
        validate_checkpoint(state, stop)
        before_events, health = json_lines(directory / "run-trace.jsonl")
        if not trace_valid(before_events, health, state["run_id"]):
            raise ValueError("Interrupted attempt trace is incomplete")
        before_ids = execution_ids(before_events)
        application = cli.build_application(settings, directory, load_provider=False)
        try:
            # This is the standard application recovery path, not a second executor.
            write_new(receipt / "RESUME_BEFORE.json", {
                "run_id": state["run_id"], "record_version": state["record_version"],
                "evidence_id": stop["evidence_id"], "runtime_revision": state["runtime_revision"],
                "execution_ids": before_ids, "confirmed_capability_invocation_ids": [
                    item["capability_invocation_id"] for item in state["inflight_evidence"]["items"]],
            })
            result, delivery = await continue_checked(
                application, settings, state, stop, load_provider=_load_provider,
                export=lambda app, run_id, config: export_snapshot(app, run_id, directory, config))
            after_events, after_health = json_lines(directory / "run-trace.jsonl")
            after_ids = execution_ids(after_events)
            before_starts = sum(e["event_type"] == "EXECUTION_STARTED" for e in before_events)
            after_starts = sum(e["event_type"] == "EXECUTION_STARTED" for e in after_events)
            trace_integrity = (trace_valid(after_events, after_health, state["run_id"])
                               and after_events[:len(before_events)] == before_events)
            no_reexecution = (trace_integrity and bool(before_ids) and before_ids == after_ids
                              and before_starts == after_starts)
            final_state = state_snapshot(directory)
            verified = bool(delivery is not None and final_state["run_id"] == state["run_id"]
                and final_state["workflow_run"]["status"] == result.status.value
                and delivery_verified(delivery_metrics(directory, final_state)))
            outcome = {"run_id": state["run_id"], "status": result.status.value,
                       "execution_ids_before": before_ids, "execution_ids_after": after_ids,
                       "no_reexecution": no_reexecution,
                       "trace_integrity_valid": trace_integrity,
                       "delivery_registered": delivery is not None, "delivery_verified": verified}
            write_new(receipt / "RESUME_RESULT.json", outcome)
            return 0 if result.status.value == "COMPLETED" and no_reexecution and verified else 2
        finally:
            cli._close(application)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    read = subcommands.add_parser("report")
    read.add_argument("--root", type=Path, required=True)
    read.add_argument("--check-processes", action="store_true")
    continuation = subcommands.add_parser("resume")
    continuation.add_argument("--root", type=Path, required=True)
    continuation.add_argument("--attempt", required=True)
    continuation.add_argument("--source", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "report":
        print(json.dumps(report(args.root.resolve(strict=True), check_processes=args.check_processes),
                         ensure_ascii=False, sort_keys=True, indent=2))
        return 0
    return asyncio.run(resume(args.root.resolve(strict=True), args.attempt, args.source))


if __name__ == "__main__":
    raise SystemExit(main())
