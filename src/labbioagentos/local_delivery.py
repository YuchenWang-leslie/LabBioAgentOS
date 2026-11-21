"""Explicit local delivery of registered results; never a model capability."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
from urllib.parse import quote
from uuid import UUID

from labbioagentos.application import ApplicationRunHandle, LabBioApplication
from labbioagentos.artifacts import ArtifactNotFoundError, ArtifactReleaseBasis
from labbioagentos.governance import AccessAction, AuthorizationDenied, Principal, WorkspaceContext
from labbioagentos.trace import TraceEventType
from labbioagentos.runtime.contracts import ExecuteStageBody, ReportStageBody, RuntimeReferenceKind
from labbioagentos.runtime.delivery import DeliveryManifest, DeliverySelectionError, build_delivery_manifest


class LocalDeliveryError(ValueError):
    """A local export cannot preserve scope, provenance, or existing files."""


def selected_execution_id(record):
    """Use the persisted Agent-selected execution, never timestamps or filenames."""
    for result in reversed(record.runtime_results):
        if isinstance(result.body, ExecuteStageBody):
            body = result.body
            return (body.execution_reference.reference_id
                if body.execution_status == "SUCCEEDED" and body.execution_reference else None)
    return None


def selected_report_id(record):
    """Use the persisted Agent-selected report, never registration order."""
    for result in reversed(record.runtime_results):
        if isinstance(result.body, ReportStageBody):
            reference = result.body.report_reference
            if reference is None or reference.kind is not RuntimeReferenceKind.ARTIFACT:
                return None
            try:
                return UUID(reference.reference_id)
            except ValueError as exc:
                raise LocalDeliveryError("Selected report identity is invalid") from exc
    return None


def selected_delivery_manifest(store, record):
    """The selected report owns the result set; absent means the legacy contract."""
    report_id = selected_report_id(record)
    if report_id is None:
        return None
    try:
        stored = store.load_for_view(report_id)
        ref = stored.ref
        if ((ref.owner_user_id, ref.project_id, ref.lab_id, ref.run_id) !=
                (record.owner_user_id, record.project_id, record.lab_id, record.run_id)
                or ref.release_basis is not ArtifactReleaseBasis.MODEL_AUTHORED_REPORT):
            raise LocalDeliveryError('Selected report is outside the bound run')
        if 'delivery_manifest' not in stored.representation.summary:
            return None
        return DeliveryManifest.model_validate_json(json.dumps(
            stored.representation.summary['delivery_manifest']))
    except (ArtifactNotFoundError, ValueError) as exc:
        raise LocalDeliveryError('Selected delivery manifest is unavailable or invalid') from exc


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
    selected_execution = selected_execution_id(record)
    selected_program = None
    for event in application.trace_events(handle):
        execution_id = event.payload.get("execution_id")
        if (event.event_type is TraceEventType.EXECUTION_PLANNED
                and execution_id == selected_execution
                and "script_artifact_id" in event.payload):
            try:
                binding = (UUID(event.payload["script_artifact_id"]),
                    event.payload["script_hash"], event.stage_id, event.invocation_id)
            except (KeyError, TypeError, ValueError) as exc:
                raise LocalDeliveryError("Selected program binding is invalid") from exc
            if event.run_id != handle.run_id or selected_program is not None and selected_program != binding:
                raise LocalDeliveryError("Selected program bindings disagree")
            selected_program = binding
        elif event.event_type is TraceEventType.OUTPUT_COLLECTED:
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
    programs = []
    historical_reports = []
    outputs = []
    historical_outputs = []
    selected_report = selected_report_id(record)
    input_ids = {*record.input_artifact_ids, *record.context_artifact_ids}
    delivery = selected_delivery_manifest(store, record)
    delivery_outputs = {item.artifact_id: item for item in delivery.outputs} if delivery else {}
    delivery_programs = {item.artifact_id: item for item in delivery.programs} if delivery else {}
    if delivery is not None:
        try:
            verified = build_delivery_manifest(store, application.access_service, tuple(delivery_outputs),
                principal=principal, workspace=workspace, run_id=record.run_id,
                imported_artifact_ids=tuple(input_ids), trace_recorder=application.trace_recorder)
        except DeliverySelectionError as exc:
            raise LocalDeliveryError('Selected delivery sources failed integrity verification') from exc
        if verified != delivery.model_dump(mode='json'):
            raise LocalDeliveryError('Saved delivery provenance differs from its registered sources')
        for artifact_id in (*delivery_outputs, *delivery_programs):
            refs[artifact_id] = store.get_ref(artifact_id)
    for artifact_id, ref in sorted(refs.items(), key=lambda item: str(item[0])):
        if artifact_id in input_ids and artifact_id not in delivery_outputs and artifact_id not in delivery_programs:
            continue
        is_report = (
            artifact_id in outcome.report_artifact_ids
            and ref.release_basis is ArtifactReleaseBasis.MODEL_AUTHORED_REPORT
        )
        is_output = "requested_exposure" in ref.metadata and "execution_id" in ref.metadata
        is_program = (artifact_id in delivery_programs if delivery is not None else
                      selected_program is not None and artifact_id == selected_program[0])
        if not is_report and not is_output and not is_program:
            continue
        application.access_service.require_artifact(principal, ref, AccessAction.READ_ARTIFACT)
        _stored_file(store.root / f"{artifact_id}.json", store.root)
        source = _stored_file(Path(ref.storage_locator), store.root)
        if is_program:
            size, digest = _digest(source)
            if delivery is not None:
                expected = delivery_programs[artifact_id]
                if (size, digest) != (expected.size_bytes, expected.sha256):
                    raise LocalDeliveryError('Selected original program changed during export')
                execution_id = str(expected.execution_id)
                filename = ('PROGRAM.py' if execution_id == selected_execution else
                            f'programs/{execution_id}/PROGRAM.py')
                writes[filename] = source
                programs.append({'artifact_id': str(artifact_id), 'execution_id': execution_id,
                    'producer_run_id': str(expected.producer_run_id),
                    'exposure_class': ref.exposure_class.value, 'file': filename,
                    'size_bytes': size, 'sha256': digest})
                continue
            if (ref.artifact_type != "execution-script"
                    or ref.metadata.get("execution_id") != selected_execution
                    or ref.stage_id != selected_program[2]
                    or ref.producer_invocation_id != selected_program[3]
                    or digest != selected_program[1]
                    or digest != ref.metadata.get("sha256")):
                raise LocalDeliveryError("Selected original program failed integrity verification")
            writes["PROGRAM.py"] = source
            programs.append({"artifact_id": str(artifact_id), "execution_id": selected_execution,
                "exposure_class": ref.exposure_class.value, "file": "PROGRAM.py",
                "size_bytes": size, "sha256": digest})
            continue
        if is_report:
            content = store.load_for_view(artifact_id).representation.stored_content
            if not isinstance(content, str):
                raise LocalDeliveryError("Model-authored report content is unavailable")
            data = content.encode("utf-8")
            is_final = artifact_id == selected_report
            filename = (
                "REPORT.md"
                if is_final
                else f"history/reports/{artifact_id}/REPORT.md"
            )
            writes[filename] = data
            (reports if is_final else historical_reports).append({
                "artifact_id": str(artifact_id), "file": filename,
                "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            })
            continue
        if artifact_id in collected:
            registered, provenance = collected[artifact_id]
        elif artifact_id in delivery_outputs and ref.run_id != record.run_id:
            imported = delivery_outputs[artifact_id]
            registered = None
            provenance = {'relative_path': imported.filename,
                'size_bytes': imported.size_bytes, 'sha256': imported.sha256}
        else:
            raise LocalDeliveryError("Output filename provenance is unavailable")
        if registered is not None and (
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
        is_final = (artifact_id in delivery_outputs if delivery is not None else
                    ref.metadata["execution_id"] == selected_execution)
        filename = f"{'outputs' if is_final else 'history/outputs'}/{artifact_id}/{PurePosixPath(original).name}"
        writes[filename] = source
        (outputs if is_final else historical_outputs).append({
            "artifact_id": str(artifact_id), "execution_id": ref.metadata["execution_id"],
            "exposure_class": ref.exposure_class.value, "file": filename,
            "size_bytes": size, "sha256": digest,
            **({'producer_run_id': str(ref.run_id)} if delivery is not None else {}),
        })

    if selected_report is not None and not reports:
        raise LocalDeliveryError("Selected report Artifact is unavailable in the run store")
    if delivery is None and selected_program is not None and not programs:
        raise LocalDeliveryError("Selected original program Artifact is unavailable in the run store")
    if delivery is not None and (
            {item['artifact_id'] for item in outputs} != {str(item) for item in delivery_outputs}
            or {item['artifact_id'] for item in programs} != {str(item) for item in delivery_programs}):
        raise LocalDeliveryError('The selected result set was not completely exported')

    manifest = {
        "run_id": str(outcome.run_id), "status": outcome.status.value,
        "final_stage": outcome.final_stage.value if outcome.final_stage else None,
        "issue_codes": list(outcome.issue_codes),
        "report_artifact_ids": [report["artifact_id"] for report in reports],
        "reports": reports, "programs": programs, "outputs": outputs,
        "selected_execution_id": selected_execution,
        "historical_reports": historical_reports,
        "historical_outputs": historical_outputs,
        **({'delivery_contract': delivery.contract,
            'selected_artifact_ids': [str(item.artifact_id) for item in delivery.outputs]}
            if delivery is not None else {}),
    }
    writes["RESULT.json"] = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    readme = [
        "# Local run results", "", f"Run: `{outcome.run_id}`",
        f"Workflow status: `{outcome.status.value}`", "",
        "Exact copies of registered artifacts; RAW outputs remain local-only.", "",
    ]
    for item in (*reports, *programs, *outputs):
        readme.append(f"- [{PurePosixPath(item['file']).name}]({quote(item['file'], safe='/')})")
    if not reports and not programs and not outputs:
        readme.append("No registered deliverables are available in this snapshot.")
    if historical_outputs:
        readme.extend(["", "## Historical / unselected execution outputs", "",
            "Retained for audit; these are not the selected final results.", ""])
        for item in historical_outputs:
            readme.append(f"- [{item['execution_id']} / {PurePosixPath(item['file']).name}]({quote(item['file'], safe='/')})")
    if historical_reports:
        readme.extend(["", "## Historical / unselected reports", "",
            "Retained for audit; these are not the selected final report.", ""])
        for item in historical_reports:
            readme.append(f"- [{item['artifact_id']} / REPORT.md]({quote(item['file'], safe='/')})")
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
