"""Local replay of one formally selected execution; no model or network resolution."""
from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .artifacts import ArtifactExposureClass, ArtifactReleaseBasis, ArtifactRepresentation
from .contracts import WorkflowStage
from .execution.apptainer import SifStore
from .execution.models import ExecutionPlanDraft
from .governance import AccessAction, AuthorizationDenied
from .local_config import _source_digest, build_application, runtime_manifest
from .local_delivery import _no_symlinks, selected_delivery_manifest, selected_execution_id


Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ReplayInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    artifact_id: UUID
    filename: str = Field(min_length=1, max_length=255)
    sha256: Digest
    size_bytes: int = Field(ge=0)
    digest_basis: Literal["registered", "external_preregistered"] = "registered"

    @field_validator("filename")
    @classmethod
    def safe_component(cls, value):
        if (value in {".", ".."} or Path(value).name != value or "\\" in value
                or any(ord(c) < 32 or ord(c) == 127 for c in value)):
            raise ValueError("Unsafe replay input filename")
        return value


class ReplayBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    format: Literal["labbio-execution-replay-v1"] = "labbio-execution-replay-v1"
    run_id: UUID
    execution_id: UUID
    script_id: UUID
    source_sha256: Digest
    source_bytes: int = Field(gt=0, le=4_000_000)
    source_run_revision: str = Field(pattern=r"^local-[0-9a-f]{64}$")
    resource_enforcement: Literal["systemd-affinity", "systemd-cgroups"]
    sif_reference: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    replay_runtime_sha256: Digest
    output_contract_sha256: Digest
    receipt_sha256: Digest
    inputs: tuple[ReplayInput, ...] = Field(max_length=128)
    draft_contract: dict


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _safe_file(path):
    _no_symlinks(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Unsafe replay file path")
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise ValueError("Replay files must be unaliased regular files")
    return st


def _read_file(path, *, target=None, limit=None):
    before = _safe_file(path)
    if limit is not None and before.st_size > limit:
        raise ValueError("Replay file exceeds its bound")
    digest = sha256()
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as reader:
        st = os.fstat(reader.fileno())
        if (st.st_dev, st.st_ino, st.st_nlink) != (before.st_dev, before.st_ino, 1):
            raise ValueError("Replay file changed during verification")
        if limit is not None and st.st_size > limit:
            raise ValueError("Replay file exceeds its bound")
        read = 0
        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
            read += len(chunk)
            if read > st.st_size:
                raise ValueError("Replay file grew during verification")
            digest.update(chunk)
            if target is not None:
                target.write(chunk)
        after = os.fstat(reader.fileno())
        if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (st.st_size, st.st_mtime_ns, st.st_ctime_ns):
            raise ValueError("Replay file changed during verification")
    return digest.hexdigest(), st.st_size


def verify_file(path, expected, size):
    if _read_file(path) != (expected, size):
        raise ValueError("Replay file differs from its pinned identity")


def _checked_bytes(path, expected, limit):
    from io import BytesIO
    buffer = BytesIO()
    actual, _ = _read_file(path, target=buffer, limit=limit)
    if actual != expected:
        raise ValueError("Replay file differs from its pinned identity")
    return buffer.getvalue()


@contextmanager
def _fresh_directory(path):
    path = Path(path).expanduser().absolute()
    _no_symlinks(path)
    if ".." in path.parts:
        raise ValueError("Unsafe replay destination")
    path.mkdir(mode=0o700, exist_ok=False)
    marker = path/"INCOMPLETE"
    with marker.open("x") as out:
        out.write("The operation has not finished. Preserve this directory for inspection.\n")
    yield path
    marker.unlink()


def _package_hash():
    return _source_digest(Path(__file__).parent)


def _input_file(root, item):
    return root/"inputs"/str(item.artifact_id)/item.filename


def export_replay(app, run_id, run_directory, destination, pins, *, principal, workspace):
    record = app.run_state_store.get(run_id)
    if ((principal.user_id, principal.lab_id) != (workspace.user_id, workspace.lab_id)
            or (record.owner_user_id, record.project_id, record.lab_id) !=
               (workspace.user_id, workspace.project_id, workspace.lab_id)):
        raise AuthorizationDenied("Replay export requires the exact run owner and workspace")
    app.access_service.require_project(principal, workspace.project_id, AccessAction.READ_PROJECT, run_id=run_id)
    if record.workflow_run.status.value != "COMPLETED" or record.recovery_state.value != "STABLE":
        raise ValueError("Replay export requires a stable completed run")
    chosen = selected_execution_id(record)
    if chosen is None:
        raise ValueError("No formally selected successful execution")
    execution_id = UUID(str(chosen))
    ref, draft, receipt = app.execution_submission.replay_binding(execution_id, principal=principal,
        workspace=workspace, run_id=run_id)
    if (receipt.status.value != "SUCCEEDED" or draft.network_required or receipt.execution_provenance is None
            or receipt.execution_provenance.backend != "apptainer"):
        raise ValueError("Replay requires a successful offline Apptainer binding")
    program_blob = app.artifact_store.root / "blobs" / str(ref.artifact_id) / "content"
    if (Path(ref.storage_locator) != program_blob or ref.artifact_type != "execution-script"
            or ref.metadata.get("execution_id") != str(execution_id)):
        raise ValueError("Replay program is not the registered execution script")
    _safe_file(program_blob)
    delivery = selected_delivery_manifest(app.artifact_store, record)
    if delivery is not None and not any(
            (p.execution_id, p.producer_run_id, p.artifact_id, p.sha256) ==
            (execution_id, run_id, ref.artifact_id, receipt.script_hash) for p in delivery.programs):
        raise ValueError("The selected delivery does not include this exact original program")
    source = draft.script_content.encode("utf-8")
    if sha256(source).hexdigest() != receipt.script_hash or ref.metadata.get("sha256") != receipt.script_hash:
        raise ValueError("Replay program identity differs")
    runtime_file = run_directory/"RUNTIME.json"
    _safe_file(runtime_file)
    runtime = json.loads(runtime_file.read_bytes())
    recorded_revision = runtime.pop("runtime_revision")
    content_revision = "local-" + sha256(json.dumps(runtime, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if recorded_revision != record.runtime_revision or recorded_revision != content_revision:
        raise ValueError("Run manifest does not match the saved runtime")
    policy = runtime["configuration"]["execution"]
    placement = runtime["configuration"].get("launcher") or {"kind":"local"}
    if (policy["backend"] != "apptainer" or placement["kind"] != "local"
            or policy["apptainer"]["resource_enforcement"] not in {"systemd-affinity", "systemd-cgroups"}):
        raise ValueError("Replay v1 requires a local enforced Apptainer execution")
    input_set = set(draft.input_artifact_ids)
    imported = set(record.input_artifact_ids) | set(record.context_artifact_ids)
    if not set(pins) <= input_set:
        raise ValueError("An external pin is outside the selected execution inputs")
    checked = []
    with _fresh_directory(destination) as dst:
        for aid in draft.input_artifact_ids:
            f = app.artifact_store.get_ref(aid)
            if ((f.owner_user_id, f.project_id, f.lab_id) !=
                    (workspace.user_id, workspace.project_id, workspace.lab_id)
                    or (f.run_id != run_id and aid not in imported)
                    or f.release_basis is ArtifactReleaseBasis.USER_APPROVED_RELEASE):
                raise AuthorizationDenied("Replay input is outside its local export grant")
            app.access_service.require_artifact(principal, f, AccessAction.READ_ARTIFACT)
            blob = app.artifact_store.root/"blobs"/str(aid)/"content"
            if Path(f.storage_locator) != blob:
                raise ValueError("Replay requires a registered file-backed direct input")
            expected = f.metadata.get("sha256") or pins.get(aid)
            if not expected or (aid in pins and expected != pins[aid]):
                raise ValueError("Direct input requires its registered digest or an independent SHA-256 pin")
            size = _safe_file(blob).st_size
            item = ReplayInput(artifact_id=aid, filename=f.original_filename, sha256=expected,
                size_bytes=size, digest_basis="registered" if f.metadata.get("sha256") else "external_preregistered")
            target = _input_file(dst, item)
            target.parent.mkdir(parents=True)
            with target.open("xb") as out:
                if _read_file(blob, target=out) != (expected, size) or f.metadata.get("size_bytes",size) != size:
                    raise ValueError("Direct input failed integrity verification")
            checked.append(item)
        (dst/"program.py").write_bytes(source)
        receipt_bytes = _canonical(receipt.model_dump(mode="json"))
        (dst/"receipt.json").write_bytes(receipt_bytes)
        bundle = ReplayBundle(run_id=run_id, execution_id=execution_id, script_id=ref.artifact_id,
            source_bytes=len(source), source_sha256=receipt.script_hash, source_run_revision=recorded_revision,
            resource_enforcement=policy["apptainer"]["resource_enforcement"],
            sif_reference=receipt.execution_provenance.image_digest,
            receipt_sha256=sha256(receipt_bytes).hexdigest(), replay_runtime_sha256=_package_hash(),
            output_contract_sha256=sha256(_canonical(runtime["profile"]["output_contract"])).hexdigest(),
            inputs=tuple(checked), draft_contract=draft.model_dump(mode="json", exclude={"script_content"}))
        encoded = _canonical(bundle.model_dump(mode="json"))
        (dst/"manifest.json").write_bytes(encoded)
        (dst/"README.md").write_text(
            "# Offline LabBio execution replay\n\n"
            "Use the same LabBio runner build as the exporter, a compatible local Apptainer installation, "
            "and a trusted local runtime TOML supplied by the receiving host. Its verified SIF store must "
            f"already contain `{bundle.sif_reference}`. This command does not download an image.\n\n"
            "Set MANIFEST_SHA256 to the trusted manifest_sha256 returned by export-replay, obtained "
            "separately from the bundle. Set LOCAL_CONFIG to the receiving host's own trusted TOML. "
            "Run from the bundle directory and choose a new output directory.\n\n"
            "```sh\nlabbio replay --config \"$LOCAL_CONFIG\" --bundle . "
            "--manifest-sha256 \"$MANIFEST_SHA256\" --output ../new-replay\n```\n\n"
            "Neither the original workspace or registry nor a model API key is required. "
            "replay-result.json records the new execution outcome. A successful exit alone does not "
            "establish scientific validity or agreement with the original outputs.\n",
            encoding="utf-8",
        )
    return {"event":"replay_exported","execution_id":str(execution_id),"manifest_sha256":sha256(encoded).hexdigest()}


async def replay(settings, bundle_directory, manifest_sha256, destination):
    """Use local trusted settings for policy and runtime, never provider credentials."""
    from .cli import _close
    from .execution.models import ExecutionReceipt
    src = Path(bundle_directory).expanduser().absolute()
    if os.path.lexists(src/"INCOMPLETE"):
        raise ValueError("Cannot replay an incomplete export")
    b = ReplayBundle.model_validate_json(_checked_bytes(src/"manifest.json", manifest_sha256, 8_000_000))
    receipt = ExecutionReceipt.model_validate_json(_checked_bytes(src/"receipt.json", b.receipt_sha256, 4_000_000))
    source = _checked_bytes(src/"program.py", b.source_sha256, b.source_bytes)
    if len(source) != b.source_bytes or "script_content" in b.draft_contract:
        raise ValueError("Replay program does not match its contract")
    draft = ExecutionPlanDraft.model_validate_json(_canonical(dict(b.draft_contract,script_content=source.decode("utf-8"))))
    if (receipt.execution_id != b.execution_id or receipt.status.value != "SUCCEEDED"
            or receipt.script_hash != b.source_sha256 or receipt.resources != draft.resources
            or receipt.input_artifact_ids != draft.input_artifact_ids or receipt.image_key != draft.image_key
            or receipt.execution_provenance is None or receipt.execution_provenance.backend != "apptainer"
            or receipt.execution_provenance.image_digest != b.sif_reference or draft.network_required
            or tuple(i.artifact_id for i in b.inputs) != draft.input_artifact_ids):
        raise ValueError("Replay binding, receipt and input manifest disagree")
    ap = settings.execution.apptainer
    if (settings.execution.backend != "apptainer" or ap is None or not ap.verify_sif_on_execute
            or settings.launcher is not None and settings.launcher.kind != "local"
            or ap.resource_enforcement != b.resource_enforcement
            # Only an operator-configured image is trusted here; catalog images are not opened (C15).
            or {image.key: image.reference for image in settings.execution.images}.get(draft.image_key)
            != b.sif_reference):
        raise ValueError("Replay requires a trusted matching local Apptainer image with digest verification")
    if b.replay_runtime_sha256 != _package_hash():
        raise ValueError("Replay requires the same LabBio exporter and runner build")
    profile = runtime_manifest(settings)["profile"]
    if sha256(_canonical(profile["output_contract"])).hexdigest() != b.output_contract_sha256:
        raise ValueError("Output registration contract differs from source execution")
    SifStore(ap.sif_store).verify(b.sif_reference)
    for item in b.inputs:
        verify_file(_input_file(src, item), item.sha256, item.size_bytes)
    # The source workspace, identity registry and personal services are never opened.
    with _fresh_directory(destination) as dst:
        local = settings.model_copy(update={"result_root": dst, "input_roots": (dst,),
            "managed_root": None, "gold_root": None, "environment": None})
        app = build_application(local, dst, load_provider=False)
        try:
            run_id = uuid4()
            for item in b.inputs:
                f = app.artifact_store.register_file(_input_file(src,item), artifact_id=item.artifact_id,
                    artifact_type="replay-input", exposure_class=ArtifactExposureClass.RAW,
                    release_basis=ArtifactReleaseBasis.RAW_INGESTION, representation=ArtifactRepresentation(),
                    owner_user_id=local.principal.user_id, project_id=local.workspace.project_id,
                    lab_id=local.workspace.lab_id, run_id=run_id)
                verify_file(Path(f.storage_locator), item.sha256, item.size_bytes)
            after = await app.execution_submission.submit(
                draft, principal=local.principal, workspace=local.workspace, run_id=run_id,
                stage_id=WorkflowStage.EXECUTE, invocation_id=uuid4(),
                mountable_input_artifact_ids=tuple(item.artifact_id for item in b.inputs),
            )
            (dst/"replay-result.json").write_bytes(_canonical(after.model_dump(mode="json")))
            return {"event":"replayed","source_execution_id":str(b.execution_id),
                "execution_id":str(after.execution_id),"status":after.status.value,
                "outputs":f"executions/{after.execution_id}/outputs","manifest_sha256":manifest_sha256}
        finally:
            _close(app)
