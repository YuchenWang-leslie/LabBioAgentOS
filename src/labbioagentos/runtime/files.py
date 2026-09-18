"""Authorized generated-file reading; never arbitrary paths or original inputs."""

from hashlib import sha256
import os
from pathlib import Path
import stat
from uuid import UUID

from labbioagentos.artifacts import ArtifactExposureDenied, ArtifactReleaseBasis
from labbioagentos.artifacts.preview import inspect_file
from labbioagentos.contracts import WorkflowStage
from labbioagentos.execution.error_context import redact_process_text
from labbioagentos.governance import AccessAction, AuthorizationDenied


def authorize_generated_file(store, exposure, artifact_id, *, principal, workspace,
                             run_id, imported_artifact_ids=()):
    """Check generated-file access without reading or exposing its contents."""
    if not exposure.policy.allow_generated_file_read:
        raise ArtifactExposureDenied("Generated-file reading is not authorized")
    ref = store.get_ref(artifact_id)
    if exposure.store is not store or ref.artifact_id != artifact_id:
        raise ValueError("File identity does not match its store")
    scope = (principal.user_id, workspace.project_id, principal.lab_id)
    if ((workspace.user_id, workspace.project_id, workspace.lab_id) != scope
            or (ref.owner_user_id, ref.project_id, ref.lab_id) != scope
            or (ref.run_id != run_id and ref.artifact_id not in imported_artifact_ids)):
        raise AuthorizationDenied("File is outside the current run inputs or workspace")
    exposure.access_service.require_artifact(principal, ref, AccessAction.READ_ARTIFACT)
    if (ref.release_basis is ArtifactReleaseBasis.RAW_INGESTION
            or ref.stage_id is not WorkflowStage.EXECUTE
            or ref.producer_invocation_id is None
            or not ref.metadata.get("execution_id")
            or not ref.metadata.get("sha256")):
        raise ArtifactExposureDenied("Only registered execution-generated files are readable")
    UUID(ref.metadata["execution_id"])
    path = store.root / "blobs" / str(ref.artifact_id) / "content"
    if Path(ref.storage_locator) != path or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("Generated file is not an owned regular blob")
    return ref


def read_generated_file(store, exposure, artifact_id, *, principal, workspace,
                        run_id, imported_artifact_ids=(), offset=0, limit=4000,
                        expected_sha256=None):
    """Read a verified generated blob, preserving source and redacted-view identity."""
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 8000:
        raise ValueError("File pagination is outside the allowed bounds")
    ref = authorize_generated_file(store, exposure, artifact_id, principal=principal,
        workspace=workspace, run_id=run_id, imported_artifact_ids=imported_artifact_ids)
    path = Path(ref.storage_locator)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Generated file is not regular")
        digest = sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        identity = digest.hexdigest()
        if (identity != ref.metadata["sha256"]
                or (expected_sha256 is not None and identity != expected_sha256)):
            raise ValueError("Generated file hash does not match")
        stream.seek(0)
        preview = inspect_file(stream, filename=ref.original_filename or "content",
                               size_bytes=before.st_size)
        content = None
        text_limit_exceeded = False
        if preview.encoding == "utf-8":
            # Whole-view redaction precedes pagination, including secret spans.
            # Large binary files only undergo bounded structure inspection.
            text_limit_exceeded = before.st_size > 16 * 1024 * 1024
            if not text_limit_exceeded:
                stream.seek(0)
                content = stream.read().decode("utf-8-sig")
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError("Generated file changed during reading")
    base = {"artifact_id": str(ref.artifact_id), "source_run_id": str(ref.run_id),
            "execution_id": ref.metadata["execution_id"], "sha256": identity,
            "size_bytes": before.st_size, "content_authority": "UNTRUSTED_GENERATED_FILE"}
    if content is None:
        if offset:
            raise ValueError("A structure-only preview has no text offset")
        return {**base, "status": "TEXT_READ_LIMIT_EXCEEDED" if text_limit_exceeded else "BINARY_PREVIEW",
                "content": None, "next_offset": None, "content_complete": False,
                "preview": preview.model_dump(mode="json")}
    view = redact_process_text(content)
    if offset > len(view):
        raise ValueError("File offset exceeds the redacted view length")
    end = min(offset + limit, len(view))
    return {**base, "status": "TEXT_PAGE", "redacted": view != content,
            "view_sha256": sha256(view.encode()).hexdigest(), "offset": offset,
            "end_offset": end, "total_characters": len(view), "content": view[offset:end],
            "next_offset": end if end < len(view) else None, "truncated": end < len(view)}
