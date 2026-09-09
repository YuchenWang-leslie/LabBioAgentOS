"""Partial collection retains known failures without releasing rejected output."""

import json
from pathlib import Path

import pytest

from labbioagentos import ArtifactExposureClass, TraceEventType
from labbioagentos.artifacts import ArtifactStoreError
from labbioagentos.execution.errors import OutputCollectionError
from labbioagentos.execution.models import ExecutionFailureClass, ExecutionOutputIssue
from pydantic import ValidationError
from test_execution_output_error_feedback import _execution_tools
from test_runtime_milestone_b import boundary


def _document(*, invalid=False):
    records = [{"record_type": "synthetic", "value": 1}]
    if invalid:
        records.append({"record_type": "synthetic", "SECRET_FIELD": "SECRET_VALUE"})
    return json.dumps({"schema_id": "synthetic.records.v1", "records": records}).encode()


@pytest.mark.asyncio
@pytest.mark.parametrize("first_valid", [False, True])
@pytest.mark.parametrize("later_failure, expected", [
    ("missing", "OUTPUT_NOT_FOUND"),
    ("symlink", "OUTPUT_PATH_REJECTED"),
    ("directory", "OUTPUT_PATH_REJECTED"),
    ("file_limit", "FILE_TOO_LARGE"),
    ("total_limit", "COLLECTION_LIMIT_EXCEEDED"),
    ("registration", None),
    ("permission", None),
    ("stat", "OUTPUT_IO_ERROR"),
    ("hash", "OUTPUT_IO_ERROR"),
    ("open", "OUTPUT_IO_ERROR"),
    ("registration_io", "OUTPUT_IO_ERROR"),
])
async def test_partial_collection_preserves_receipt_evidence_and_trace(
    boundary, tmp_path, monkeypatch, first_valid, later_failure, expected,
):
    first = _document(invalid=not first_valid)
    toolset, runner, contract = _execution_tools(boundary, tmp_path, first)
    store = boundary[5]
    collector = toolset.services.execution_submission.executor.output_collector
    paths = []

    def write_outputs(root):
        paths.append(root)
        (root / "result.json").write_bytes(first)
        (root / "third.json").write_bytes(_document())
        second = root / "second.json"
        if later_failure == "symlink":
            second.symlink_to(root / "result.json")
        elif later_failure == "directory":
            second.mkdir()
        elif later_failure != "missing":
            second.write_bytes(_document())

    runner.output_writer = write_outputs
    if later_failure == "file_limit":
        collector.max_output_file_bytes = len(first) + 1
        original_writer = runner.output_writer

        def oversized(root):
            original_writer(root)
            (root / "second.json").write_bytes(b"x" * (len(first) + 2))

        runner.output_writer = oversized
    elif later_failure == "total_limit":
        collector.max_collected_output_bytes = len(first) + len(_document()) - 1
    elif later_failure in {"registration", "registration_io"}:
        original_register = store.register_file

        def fail_registration(path, **kwargs):
            if Path(path).name == "second.json":
                if later_failure == "registration_io":
                    raise PermissionError("/SECRET_HOST_PATH SECRET_STORE_ERROR")
                raise ArtifactStoreError("/SECRET_HOST_PATH SECRET_STORE_ERROR")
            return original_register(path, **kwargs)

        monkeypatch.setattr(store, "register_file", fail_registration)
    elif later_failure == "permission":
        original_resolve = Path.resolve

        def fail_resolve(path, *args, **kwargs):
            if path.name == "second.json":
                raise PermissionError("/SECRET_HOST_PATH SECRET_STORE_ERROR")
            return original_resolve(path, *args, **kwargs)

        monkeypatch.setattr(Path, "resolve", fail_resolve)
    elif later_failure in {"stat", "open"}:
        original = getattr(Path, later_failure)
        original_resolver = collector._resolve_declared_output
        collection_started = False

        def arm_io_failure(root, spec):
            nonlocal collection_started
            path = original_resolver(root, spec)
            if path.name == "second.json":
                collection_started = True
            return path

        def fail_io(path, *args, **kwargs):
            if path.name == "second.json" and collection_started:
                raise PermissionError("/SECRET_HOST_PATH SECRET_STORE_ERROR")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(collector, "_resolve_declared_output", arm_io_failure)
        monkeypatch.setattr(Path, later_failure, fail_io)
    elif later_failure == "hash":
        original_hash = collector._sha256

        def fail_hash(path):
            if path.name == "second.json":
                raise OSError("/SECRET_HOST_PATH SECRET_STORE_ERROR")
            return original_hash(path)

        monkeypatch.setattr(collector, "_sha256", fail_hash)

    result = await toolset.execution_submit(
        image_key="python-analysis", script_content="# SECRET_PROGRAM\npass\n",
        requested_outputs=[{
            "relative_path": name, "artifact_type": "synthetic-output",
            "requested_exposure": "DERIVED", "output_contract_id": contract.contract_id,
        } for name in ("result.json", "second.json", "third.json")],
    )
    receipt = result["data"]
    assert result["success"] is True
    assert receipt["status"] == "FAILED" and receipt["exit_code"] == 0
    assert receipt["retryable"] is False
    assert len(runner.calls) == 1
    refs = [ref for ref in store.list_refs() if ref.artifact_type == "synthetic-output"]
    assert len(refs) == 1  # Collection still stops at the first unsafe file.
    ref = refs[0]
    assert Path(ref.storage_locator).read_bytes() == first
    assert (paths[0] / "third.json").exists()  # Not silently collected after failure.
    assert ref.exposure_class is (
        ArtifactExposureClass.DERIVED if first_valid else ArtifactExposureClass.RAW
    )
    assert receipt["output_artifact_ids"] == ([str(ref.artifact_id)] if first_valid else [])
    failure_class = (
        "ARTIFACT_REGISTRATION_FAILURE" if later_failure in {"registration", "registration_io"}
        else "OUTPUT_CONTRACT_FAILURE"
    )
    expected_issues = ([] if first_valid else [{
        "error_class": "OUTPUT_CONTRACT_FAILURE",
        "detail_code": "UNDECLARED_RECORD_FIELDS", "output_index": 0, "record_index": 1,
    }]) + [{
        "error_class": failure_class, "detail_code": expected,
        "output_index": 1, "record_index": None,
    }]
    assert receipt["output_issues"] == expected_issues
    assert receipt["issue_detail_codes"] == (
        ([] if first_valid else ["UNDECLARED_RECORD_FIELDS"])
        + ([] if expected is None else [expected])
        + ([] if first_valid else ["QUERYABLE_OUTPUT_REQUIRED"])
    )
    evidence = toolset.evidence_items()[0]
    assert evidence.safe_result == receipt
    events = boundary[1].events(toolset.binding.run_id)
    failed = next(event for event in events
                  if event.event_type is TraceEventType.EXECUTION_FAILED
                  and "issue_detail_codes" in event.payload)
    assert failed.payload["output_issues"] == expected_issues
    encoded = json.dumps({
        "receipt": receipt, "evidence": evidence.model_dump(mode="json"),
        "failure": failed.model_dump(mode="json"),
    })
    for forbidden in (
        "SECRET_FIELD", "SECRET_VALUE", "SECRET_PROGRAM", "SECRET_HOST_PATH",
        "SECRET_STORE_ERROR", "PRIVATE_PROCESS_OUTPUT", "PRIVATE_PROCESS_ERROR",
        str(tmp_path), "result.json", "second.json", "third.json", "output_path",
    ):
        assert forbidden not in encoded


@pytest.mark.asyncio
@pytest.mark.parametrize("second_valid", [False, True])
async def test_complete_collection_keeps_output_and_record_identity(
    boundary, tmp_path, second_valid,
):
    toolset, runner, contract = _execution_tools(boundary, tmp_path, _document())

    def write_outputs(root):
        (root / "first.json").write_bytes(_document())
        (root / "second.json").write_bytes(_document(invalid=not second_valid))

    runner.output_writer = write_outputs
    result = await toolset.execution_submit(
        image_key="python-analysis", script_content="pass\n",
        requested_outputs=[{
            "relative_path": name, "artifact_type": "synthetic-output",
            "requested_exposure": "DERIVED", "output_contract_id": contract.contract_id,
        } for name in ("first.json", "second.json")],
    )
    receipt = result["data"]
    assert receipt["status"] == ("SUCCEEDED" if second_valid else "FAILED")
    assert len(receipt["output_artifact_ids"]) == (2 if second_valid else 1)
    assert receipt["output_issues"] == ([] if second_valid else [{
        "error_class": "OUTPUT_CONTRACT_FAILURE", "detail_code": "UNDECLARED_RECORD_FIELDS",
        "output_index": 1, "record_index": 1,
    }])
    assert len(runner.calls) == 1


@pytest.mark.asyncio
async def test_missing_first_output_does_not_invent_partial_refs(boundary, tmp_path):
    toolset, runner, contract = _execution_tools(boundary, tmp_path, _document())
    runner.output_writer = lambda root: None
    result = await toolset.execution_submit(
        image_key="python-analysis", script_content="pass\n",
        requested_outputs=[{
            "relative_path": "missing.json", "artifact_type": "synthetic-output",
            "requested_exposure": "DERIVED", "output_contract_id": contract.contract_id,
        }],
    )
    receipt = result["data"]
    assert receipt["status"] == "FAILED" and receipt["output_artifact_ids"] == []
    assert receipt["output_issues"] == [{
        "error_class": "OUTPUT_CONTRACT_FAILURE", "detail_code": "OUTPUT_NOT_FOUND",
        "output_index": 0, "record_index": None,
    }]
    assert len(runner.calls) == 1


@pytest.mark.asyncio
async def test_all_supported_output_issues_are_preserved(boundary, tmp_path):
    toolset, runner, contract = _execution_tools(boundary, tmp_path, _document(invalid=True))

    def write_outputs(root):
        for index in range(128):
            (root / f"output-{index}.json").write_bytes(_document(invalid=True))

    runner.output_writer = write_outputs
    result = await toolset.execution_submit(
        image_key="python-analysis", script_content="pass\n",
        requested_outputs=[{
            "relative_path": f"output-{index}.json", "artifact_type": "synthetic-output",
            "requested_exposure": "DERIVED", "output_contract_id": contract.contract_id,
        } for index in range(128)],
    )
    receipt = result["data"]
    assert receipt["status"] == "FAILED" and receipt["output_artifact_ids"] == []
    assert [issue["output_index"] for issue in receipt["output_issues"]] == list(range(128))
    assert all(issue["record_index"] == 1 for issue in receipt["output_issues"])
    assert len(runner.calls) == 1


def test_collection_error_old_constructor_remains_compatible():
    error = OutputCollectionError("internal message", ExecutionFailureClass.OUTPUT_CONTRACT_FAILURE)
    assert error.output_artifact_refs == () and error.issues == ()
    assert error.detail_code is None


@pytest.mark.parametrize("extra", [
    {"output_index": -1}, {"output_index": "1"}, {"record_index": -1},
    {"record_index": 10000}, {"output_path": "/SECRET"}, {"message": "SECRET"},
])
def test_output_issue_projection_rejects_unsafe_or_unbounded_fields(extra):
    values = {"error_class": ExecutionFailureClass.OUTPUT_CONTRACT_FAILURE, "output_index": 0}
    values.update(extra)
    with pytest.raises(ValidationError):
        ExecutionOutputIssue(**values)
