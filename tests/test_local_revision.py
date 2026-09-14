"""Successor inputs preserve governed identity, content, and the source run."""

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from labbioagentos import (
    ApplicationRunRequest, ArtifactConsumer, ArtifactExposureClass,
    ArtifactQuery, ArtifactReleaseBasis, ArtifactRepresentation, ArtifactViewType,
    AuthorizationDenied, LabBioApplication, Principal, WorkflowStage, WorkspaceContext,
)
from labbioagentos.local_revision import RevisionImportError, import_revision_artifacts
from test_application_runtime_c5 import _configuration


@pytest.fixture
def revision_apps(tmp_path):
    applications = []
    for name in ("source", "successor"):
        root = tmp_path / name
        root.mkdir()
        applications.append(LabBioApplication(_configuration(root)))
    source, target = applications
    principal = Principal(user_id="user-c5", lab_id="lab-c5")
    workspace = WorkspaceContext(user_id="user-c5", project_id="project-c5", lab_id="lab-c5")
    handle = source.create_run(ApplicationRunRequest(
        task_text="Original fixture task", principal=principal, workspace=workspace,
    ))
    report = source.report_submission.submit(
        title="Original fixture report", report_text="# Original\n\nA bounded observation.",
        evidence_artifact_ids=(), principal=principal, workspace=workspace,
        run_id=handle.run_id, stage_id=WorkflowStage.REPORT, invocation_id=uuid4(),
    )
    source.cancel_run(handle)
    return source, target, handle, principal, workspace, report.report_artifact_id


def _import(fixture, ids=None):
    source, target, handle, principal, workspace, report_id = fixture
    return import_revision_artifacts(
        source, target, handle.run_id, ids or (report_id,),
        principal=principal, workspace=workspace,
    )


def test_import_keeps_identity_exposure_and_old_bytes_without_claiming_new_output(revision_apps):
    source, target, handle, principal, workspace, report_id = revision_apps
    before = {path: path.read_bytes() for path in source.artifact_store.root.rglob("*") if path.is_file()}
    original = source.artifact_store.load_for_view(report_id)
    refs = _import(revision_apps)
    copied = target.artifact_store.load_for_view(report_id)
    assert refs == (copied.ref,)
    assert copied.ref.run_id == handle.run_id
    assert copied.ref.model_dump(exclude={"storage_locator"}) == original.ref.model_dump(exclude={"storage_locator"})
    assert copied.representation == original.representation
    assert copied.ref.exposure_class is ArtifactExposureClass.DERIVED
    target.artifact_exposure.artifact_query(
        report_id, ArtifactQuery(view_type=ArtifactViewType.SUMMARY),
        ArtifactConsumer.REMOTE_LLM, principal=principal,
    )
    successor = target.create_run(ApplicationRunRequest(
        task_text="Revise the earlier result", principal=principal, workspace=workspace,
        context_artifact_ids=(report_id,),
    ))
    assert target.result(successor).report_artifact_ids == ()
    assert target.result(successor).derived_artifact_ids == ()
    assert all(path.read_bytes() == data for path, data in before.items())
    with pytest.raises(RevisionImportError):
        _import(revision_apps)


@pytest.mark.parametrize("tamper", ("content", "scope", "symlink", "locator", "unapproved"))
def test_import_rejects_tampering_before_target_writes(revision_apps, tmp_path, tamper):
    source, target, _, _, _, report_id = revision_apps
    envelope = source.artifact_store.root / f"{report_id}.json"
    document = json.loads(envelope.read_text())
    if tamper == "content":
        document["representation"]["stored_content"] = "changed content"
    elif tamper == "scope":
        document["ref"]["owner_user_id"] = "another-user"
    elif tamper == "locator":
        document["ref"]["storage_locator"] = str(tmp_path / "outside")
    elif tamper == "unapproved":
        document["ref"]["exposure_class"] = "USER_APPROVED"
        document["ref"]["release_basis"] = "USER_APPROVED_RELEASE"
    else:
        outside = tmp_path / "outside"
        envelope.rename(outside)
        envelope.symlink_to(outside)
    if tamper != "symlink":
        envelope.write_text(json.dumps(document))
    with pytest.raises((RevisionImportError, AuthorizationDenied)):
        _import(revision_apps)
    assert not tuple(target.artifact_store.root.iterdir())


def test_blob_copy_keeps_original_name_and_verifies_registered_hash(revision_apps, tmp_path):
    source, target, handle, principal, workspace, _ = revision_apps
    payload = tmp_path / "original measurements.tsv"
    data = b"measurement\tvalue\nfixture\t7\n"
    payload.write_bytes(data)
    ref = source.artifact_store.register_file(
        payload, artifact_type="table", exposure_class=ArtifactExposureClass.DERIVED,
        release_basis=ArtifactReleaseBasis.TRUSTED_EXECUTION_DECLASSIFICATION,
        representation=ArtifactRepresentation(records=({"measurement": "fixture", "value": 7},), record_count=1),
        owner_user_id=principal.user_id, project_id=workspace.project_id, lab_id=workspace.lab_id,
        run_id=handle.run_id, stage_id=WorkflowStage.EXECUTE, producer_invocation_id=uuid4(),
        metadata={"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)},
    )
    _import(revision_apps, (ref.artifact_id,))
    copied = target.artifact_store.get_ref(ref.artifact_id)
    assert copied.original_filename == payload.name
    assert Path(copied.storage_locator).read_bytes() == data
    assert Path(copied.storage_locator).is_relative_to(target.artifact_store.root)
    Path(ref.storage_locator).write_bytes(b"tampered")
    other = tmp_path / "another"
    other.mkdir()
    replacement = LabBioApplication(_configuration(other))
    with pytest.raises(RevisionImportError):
        import_revision_artifacts(source, replacement, handle.run_id, (ref.artifact_id,),
                                  principal=principal, workspace=workspace)
    assert not tuple(replacement.artifact_store.root.iterdir())


def test_original_raw_input_without_legacy_hash_is_copied_without_remote_release(revision_apps):
    source, target, _, principal, workspace, report_id = revision_apps
    path = source.configuration.allowed_input_roots[0] / "legacy.csv"
    path.write_bytes(b"PRIVATE_RAW_CONTENT")
    ref = source.register_input_file(path, principal=principal, workspace=workspace, artifact_type="csv")
    handle = source.create_run(ApplicationRunRequest(
        task_text="Original task with raw input", principal=principal, workspace=workspace,
        input_artifact_ids=(ref.artifact_id,),
    ))
    source.cancel_run(handle)
    imported = import_revision_artifacts(
        source, target, handle.run_id, (ref.artifact_id,), principal=principal, workspace=workspace,
    )[0]
    assert imported.run_id is None
    assert Path(imported.storage_locator).read_bytes() == b"PRIVATE_RAW_CONTENT"
    from labbioagentos.artifacts import ArtifactExposureDenied
    with pytest.raises(ArtifactExposureDenied):
        target.artifact_exposure.artifact_query(
            imported.artifact_id, ArtifactQuery(view_type=ArtifactViewType.SUMMARY),
            ArtifactConsumer.REMOTE_LLM, principal=principal,
        )


@pytest.mark.parametrize("case", ("foreign_principal", "foreign_project", "nonterminal", "duplicate", "unbound"))
def test_revision_source_binding_is_authoritative(revision_apps, case):
    source, target, handle, principal, workspace, report_id = revision_apps
    identifiers = (report_id,)
    if case == "foreign_principal":
        principal = Principal(user_id="other-user", lab_id=workspace.lab_id)
    elif case == "foreign_project":
        workspace = workspace.model_copy(update={"project_id": "another-project"})
    elif case == "nonterminal":
        handle = source.create_run(ApplicationRunRequest(
            task_text="Active task", principal=principal, workspace=workspace,
        ))
    elif case == "duplicate":
        identifiers = (report_id, report_id)
    else:
        ref = source.artifact_store.register(
            artifact_type="unbound", exposure_class=ArtifactExposureClass.STRUCTURAL,
            release_basis=ArtifactReleaseBasis.TRUSTED_STRUCTURAL_INSPECTOR,
            representation=ArtifactRepresentation(), owner_user_id=principal.user_id,
            project_id=workspace.project_id, lab_id=workspace.lab_id,
        )
        identifiers = (ref.artifact_id,)
    with pytest.raises((RevisionImportError, AuthorizationDenied)):
        import_revision_artifacts(source, target, handle.run_id, identifiers,
                                  principal=principal, workspace=workspace)
    assert not tuple(target.artifact_store.root.iterdir())
