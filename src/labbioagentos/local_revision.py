"""Trusted, immutable transfer of explicitly selected prior-run Artifacts."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
from uuid import UUID

from .artifacts import ArtifactExposureClass, ArtifactReleaseBasis
from .artifacts.store import StoredArtifact
from .contracts import RunStatus
from .governance import AccessAction, AuthorizationDenied
from .local_delivery import LocalDeliveryError, _digest, _no_symlinks, _stored_file
from .run_state import RunRecoveryState


class RevisionImportError(ValueError):
    """A prior Artifact cannot be copied without losing scope or integrity."""


def import_revision_artifacts(
    source_application, target_application, source_run_id: UUID,
    artifact_ids: tuple[UUID, ...], *, principal, workspace,
):
    """Copy selected immutable records; never reclassify, run, or revise them.

    The caller owns conversation binding. This boundary independently checks
    exact run/user/project scope. Existing Artifact IDs and producer lineage
    remain unchanged; only store-local locators differ. USER_APPROVED is not
    transferred because its approval store is a separate authority. Legacy RAW
    inputs and reports without original hashes receive byte-stable snapshot
    copying, not a retrospective claim of ingestion-time integrity.
    """
    source, target = source_application.artifact_store, target_application.artifact_store
    scope = (principal.user_id, workspace.project_id, principal.lab_id)
    record = source_application.run_state_store.get(source_run_id)
    if ((workspace.user_id, workspace.project_id, workspace.lab_id) != scope
            or (record.owner_user_id, record.project_id, record.lab_id) != scope):
        raise AuthorizationDenied("Revision source does not match the current workspace")
    for application in (source_application, target_application):
        application.access_service.require_project(principal, workspace.project_id, AccessAction.WRITE_PROJECT)
    if (record.recovery_state is not RunRecoveryState.STABLE
            or record.workflow_run.status not in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}):
        raise RevisionImportError("Revision source must be a reconciled terminal run")
    if (not artifact_ids or len(artifact_ids) > 128
            or len(set(artifact_ids)) != len(artifact_ids)
            or any(not isinstance(item, UUID) for item in artifact_ids)):
        raise RevisionImportError("Revision selection must contain distinct Artifact UUIDs")
    if source.root == target.root:
        raise RevisionImportError("A revision requires a separate Artifact store")
    selected = []
    try:
        _no_symlinks(target.root)
        for artifact_id in artifact_ids:
            envelope = _stored_file(source.root / f"{artifact_id}.json", source.root)
            envelope_bytes = envelope.read_bytes()
            stored = StoredArtifact.model_validate_json(envelope_bytes)
            ref = stored.ref
            if ref.artifact_id != artifact_id:
                raise RevisionImportError("Artifact envelope identity does not match selection")
            if (ref.owner_user_id, ref.project_id, ref.lab_id) != scope:
                raise AuthorizationDenied("Revision Artifact is outside the current workspace")
            if ref.run_id != source_run_id and artifact_id not in (*record.input_artifact_ids, *record.context_artifact_ids):
                raise RevisionImportError("Selected Artifact is not evidence of the source run")
            source_application.access_service.require_artifact(principal, ref, AccessAction.READ_ARTIFACT)
            if ref.exposure_class is ArtifactExposureClass.USER_APPROVED:
                raise RevisionImportError("User-approved release requires its separate approval authority")
            payload = _stored_file(Path(ref.storage_locator), source.root)
            if payload not in {envelope, source.root / "blobs" / str(artifact_id) / "content"}:
                raise RevisionImportError("Artifact payload locator does not match its identity")
            if envelope.stat().st_nlink != 1 or payload.stat().st_nlink != 1:
                raise RevisionImportError("Revision files cannot be hardlinks")
            is_embedded = payload == envelope
            size, digest = _digest(payload)
            if ref.release_basis is ArtifactReleaseBasis.MODEL_AUTHORED_REPORT:
                text = stored.representation.stored_content
                if not is_embedded or ref.artifact_type != "report" or not isinstance(text, str):
                    raise RevisionImportError("Report payload is invalid")
                content = text.encode("utf-8")
                if (("sha256" in ref.metadata and ref.metadata["sha256"] != hashlib.sha256(content).hexdigest())
                        or ("size_bytes" in ref.metadata and ref.metadata["size_bytes"] != len(content))):
                    raise RevisionImportError("Report payload integrity check failed")
            elif not is_embedded:
                legacy_input = (
                    artifact_id in record.input_artifact_ids
                    and ref.exposure_class is ArtifactExposureClass.RAW
                    and ref.release_basis is ArtifactReleaseBasis.RAW_INGESTION
                )
                expected = (
                    ref.metadata.get("sha256", digest if legacy_input else None),
                    ref.metadata.get("size_bytes", size if legacy_input else None),
                )
                if expected != (digest, size):
                    raise RevisionImportError("File payload lacks matching registered integrity evidence")
            target_envelope = target.root / f"{artifact_id}.json"
            target_blob = target.root / "blobs" / str(artifact_id) / "content"
            _no_symlinks(target_envelope)
            _no_symlinks(target_blob)
            if target_envelope.exists() or target_blob.parent.exists():
                raise RevisionImportError("Selected Artifact already exists in the successor store")
            selected.append((stored, envelope, envelope_bytes, payload, size, digest, is_embedded))
    except LocalDeliveryError as exc:
        raise RevisionImportError("Revision source path failed integrity checks") from exc

    imported = []
    for stored, envelope, envelope_bytes, payload, size, digest, is_embedded in selected:
        ref = stored.ref
        destination = target.root / f"{ref.artifact_id}.json"
        locator = destination if is_embedded else target.root / "blobs" / str(ref.artifact_id) / "content"
        if not is_embedded:
            locator.parent.mkdir(parents=True, exist_ok=False, mode=0o700)
            with locator.open("xb") as output, payload.open("rb") as input_stream:
                shutil.copyfileobj(input_stream, output)
            if _digest(locator) != (size, digest):
                raise RevisionImportError("Revision payload changed during copying")
        if envelope.read_bytes() != envelope_bytes or _digest(payload) != (size, digest):
            raise RevisionImportError("Revision source changed during copying")
        copied = stored.model_copy(update={"ref": ref.model_copy(update={"storage_locator": str(locator)})})
        with os.fdopen(os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as output:
            output.write(copied.model_dump_json() + "\n")
            output.flush()
            os.fsync(output.fileno())
        imported.append(copied.ref)
    return tuple(imported)
