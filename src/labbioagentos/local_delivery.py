"""Explicit local delivery of registered results; never a model capability."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
from urllib.parse import quote
from uuid import UUID

from labbioagentos.application import ApplicationRunHandle, LabBioApplication
from labbioagentos.artifacts import ArtifactReleaseBasis
from labbioagentos.governance import AccessAction, AuthorizationDenied, Principal, WorkspaceContext
from labbioagentos.trace import TraceEventType


class LocalDeliveryError(ValueError):
    """A local export cannot preserve scope, provenance, or existing files."""


def _no_symlinks(path: Path) -> None:
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise LocalDeliveryError("Local delivery paths cannot contain symlinks")


def _stored_file(path: Path, root: Path) -> Path:
    _no_symlinks(path)
    resolved = path.resolve()
    if not path.is_absolute() or not resolved.is_relative_to(root) or not resolved.is_file():
        raise LocalDeliveryError("Artifact payload is outside the local store or unavailable")
    return resolved


def _digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def export_run(
    application: LabBioApplication,
    handle: ApplicationRunHandle,
    directory: Path,
    *,
    principal: Principal,
    workspace: WorkspaceContext,
) -> dict:
    """Copy exact registered reports/outputs without running or recovering a task.

    The caller supplies current trusted local identity. A recovered run must
    already be attached through ``application.recover_run``. A changed snapshot
    requires a new destination; existing conflicting files are never overwritten.
    """

    record = application.run_state_store.get(handle.run_id)
    if (
        principal.user_id != workspace.user_id
        or principal.lab_id != workspace.lab_id
        or (workspace.user_id, workspace.project_id, workspace.lab_id)
        != (record.owner_user_id, record.project_id, record.lab_id)
    ):
        raise AuthorizationDenied("Local delivery identity does not match the run scope")
    application.access_service.require_project(
        principal, workspace.project_id, AccessAction.READ_PROJECT, run_id=handle.run_id
    )
    store = application.artifact_store
    _no_symlinks(store.root)
    for envelope in store.root.glob("*.json"):
        _stored_file(envelope, store.root)
    outcome = application.result(handle)
    refs = {ref.artifact_id: ref for ref in store.list_refs() if ref.run_id == handle.run_id}
    for ref in refs.values():
        if (ref.owner_user_id, ref.project_id, ref.lab_id) != (
            record.owner_user_id, record.project_id, record.lab_id
        ):
            raise AuthorizationDenied("Local delivery Artifact is outside the run scope")

    # Original names are retained in collection audit, not in blob locators.
    # These events provide file provenance only; outcome comes from application.result.
    pending = {}
    collected = {}
    for event in application.trace_events(handle):
        execution_id = event.payload.get("execution_id")
        if event.event_type is TraceEventType.OUTPUT_COLLECTED:
            pending[execution_id] = event
        elif event.event_type is TraceEventType.OUTPUT_REGISTERED:
            preceding = pending.pop(execution_id, None)
            if preceding is None:
                raise LocalDeliveryError("Registered output has no collection provenance")
            try:
                artifact_id = UUID(event.payload["artifact_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise LocalDeliveryError("Registered output provenance is invalid") from exc
            if (
                event.run_id != handle.run_id
                or preceding.run_id != event.run_id
                or preceding.stage_id != event.stage_id
                or preceding.invocation_id != event.invocation_id
                or artifact_id in collected
            ):
                raise LocalDeliveryError("Registered output provenance does not match the run")
            collected[artifact_id] = (event, preceding.payload)
    if set(collected) - set(refs):
        raise LocalDeliveryError("Registered output Artifact is unavailable in the run store")

    directory = Path(directory).absolute()
    _no_symlinks(directory)
    if directory.exists() and not directory.is_dir():
        raise LocalDeliveryError("Local delivery destination is not a directory")
    writes: dict[str, bytes | Path] = {}
    reports = []
    outputs = []
    input_ids = {*record.input_artifact_ids, *record.context_artifact_ids}
    for artifact_id, ref in sorted(refs.items(), key=lambda item: str(item[0])):
        if artifact_id in input_ids:
            continue
        is_report = (
            artifact_id in outcome.report_artifact_ids
            and ref.release_basis is ArtifactReleaseBasis.MODEL_AUTHORED_REPORT
        )
        is_output = "requested_exposure" in ref.metadata and "execution_id" in ref.metadata
        if not is_report and not is_output:
            continue
        application.access_service.require_artifact(principal, ref, AccessAction.READ_ARTIFACT)
        _stored_file(store.root / f"{artifact_id}.json", store.root)
        source = _stored_file(Path(ref.storage_locator), store.root)
        if is_report:
            content = store.load_for_view(artifact_id).representation.stored_content
            if not isinstance(content, str):
                raise LocalDeliveryError("Model-authored report content is unavailable")
            data = content.encode("utf-8")
            filename = "REPORT.md" if not reports else f"REPORT-{artifact_id}.md"
            writes[filename] = data
            reports.append({
                "artifact_id": str(artifact_id), "file": filename,
                "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            })
            continue
        if artifact_id not in collected:
            raise LocalDeliveryError("Output filename provenance is unavailable")
        registered, provenance = collected[artifact_id]
        if (
            ref.stage_id != registered.stage_id
            or ref.producer_invocation_id != registered.invocation_id
            or ref.metadata["execution_id"] != registered.payload.get("execution_id")
            or ref.metadata.get("requested_exposure") != registered.payload.get("requested_exposure")
            or ref.exposure_class.value != registered.payload.get("actual_exposure")
            or any(ref.metadata.get(key) != provenance.get(key) for key in ("size_bytes", "sha256"))
        ):
            raise LocalDeliveryError("Output registry and collection provenance disagree")
        original = provenance.get("relative_path")
        if (
            not isinstance(original, str) or not original or len(original) > 240
            or "\\" in original or any(ord(char) < 32 for char in original)
            or PurePosixPath(original).is_absolute()
            or any(part in {"", ".", ".."} for part in original.split("/"))
        ):
            raise LocalDeliveryError("Output filename provenance is unsafe")
        size, digest = _digest(source)
        if (size, digest) != (provenance.get("size_bytes"), provenance.get("sha256")):
            raise LocalDeliveryError("Registered output payload failed integrity verification")
        filename = f"outputs/{artifact_id}/{PurePosixPath(original).name}"
        writes[filename] = source
        outputs.append({
            "artifact_id": str(artifact_id), "execution_id": ref.metadata["execution_id"],
            "exposure_class": ref.exposure_class.value, "file": filename,
            "size_bytes": size, "sha256": digest,
        })

    manifest = {
        "run_id": str(outcome.run_id), "status": outcome.status.value,
        "final_stage": outcome.final_stage.value if outcome.final_stage else None,
        "issue_codes": list(outcome.issue_codes),
        "report_artifact_ids": [report["artifact_id"] for report in reports],
        "reports": reports, "outputs": outputs,
    }
    writes["RESULT.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    readme = [
        "# Local run results", "", f"Run: `{outcome.run_id}`",
        f"Workflow status: `{outcome.status.value}`", "",
        "Exact copies of registered artifacts; RAW outputs remain local-only.", "",
    ]
    for item in (*reports, *outputs):
        readme.append(f"- [{item['artifact_id']}]({quote(item['file'], safe='/')})")
    if not reports and not outputs:
        readme.append("No registered deliverables are available in this snapshot.")
    writes["README.md"] = ("\n".join(readme) + "\n").encode()
    for filename, data in writes.items():
        target = directory / filename
        _no_symlinks(target)
        if any(parent.exists() and not parent.is_dir() for parent in target.parents):
            raise LocalDeliveryError("Local delivery parent conflicts with an existing file")
        expected = _digest(data) if isinstance(data, Path) else (len(data), hashlib.sha256(data).hexdigest())
        if target.exists() and (not target.is_file() or _digest(target) != expected):
            raise LocalDeliveryError("Local delivery conflicts with an existing file")
    for filename, data in writes.items():
        target = directory / filename
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as destination:
            if isinstance(data, Path):
                with data.open("rb") as source_stream:
                    shutil.copyfileobj(source_stream, destination)
            else:
                destination.write(data)
    return manifest
