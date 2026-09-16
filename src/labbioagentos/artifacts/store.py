"""Minimal local artifact persistence addressed only by artifact UUID."""

from __future__ import annotations

from abc import ABC, abstractmethod
import os
import shutil
import stat
from pathlib import Path
from threading import Lock
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from labbioagentos.contracts import WorkflowStage
from labbioagentos.trace import RunTraceRecorder, TraceEventType

from .models import (
    ArtifactExposureClass,
    ArtifactReleaseBasis,
    ArtifactRef,
    ArtifactRepresentation,
    ArtifactSchema,
)
from .preview import RawHeadPreview, inspect_file


class ArtifactStoreError(RuntimeError):
    """Local artifact persistence failed."""


class ArtifactNotFoundError(ArtifactStoreError):
    """The requested artifact identifier is not registered."""


class ArtifactIdentifierError(ValueError):
    """An artifact lookup used something other than a UUID."""


def coerce_artifact_id(value: UUID | str) -> UUID:
    """Accept UUID text only; paths and traversal expressions are invalid."""

    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise ArtifactIdentifierError("artifact_id must be a UUID")
    try:
        return UUID(value)
    except ValueError as exc:
        raise ArtifactIdentifierError("artifact_id must be a UUID, not a path") from exc


class StoredArtifact(BaseModel):
    """Store-private envelope used by the exposure service."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: ArtifactRef
    representation: ArtifactRepresentation


class ArtifactStore(ABC):
    """Trusted data-plane store; this interface is not exposed as an agent tool."""

    @abstractmethod
    def register(
        self,
        *,
        artifact_type: str,
        exposure_class: ArtifactExposureClass,
        release_basis: ArtifactReleaseBasis = ArtifactReleaseBasis.INTERNAL_ONLY,
        representation: ArtifactRepresentation,
        owner_user_id: str = "local-user",
        project_id: str = "local-project",
        lab_id: str = "local-lab",
        run_id: UUID | None = None,
        stage_id: WorkflowStage | None = None,
        producer_invocation_id: UUID | None = None,
        schema: ArtifactSchema | None = None,
        metadata: dict[str, JsonValue] | None = None,
        artifact_id: UUID | None = None,
    ) -> ArtifactRef:
        """Persist one trusted representation and return metadata only."""

    @abstractmethod
    def exists(self, artifact_id: UUID | str) -> bool:
        """Return whether a UUID-addressed artifact exists."""

    @abstractmethod
    def register_file(
        self,
        source: str | Path,
        *,
        artifact_type: str,
        exposure_class: ArtifactExposureClass,
        release_basis: ArtifactReleaseBasis = ArtifactReleaseBasis.INTERNAL_ONLY,
        representation: ArtifactRepresentation,
        owner_user_id: str = "local-user",
        project_id: str = "local-project",
        lab_id: str = "local-lab",
        run_id: UUID | None = None,
        stage_id: WorkflowStage | None = None,
        producer_invocation_id: UUID | None = None,
        schema: ArtifactSchema | None = None,
        metadata: dict[str, JsonValue] | None = None,
        artifact_id: UUID | None = None,
    ) -> ArtifactRef:
        """Copy a trusted local file into store ownership and return its reference."""

    @abstractmethod
    def get_ref(self, artifact_id: UUID | str) -> ArtifactRef:
        """Return metadata only."""

    @abstractmethod
    def load_for_view(self, artifact_id: UUID | str) -> StoredArtifact:
        """Trusted exposure-service read; never register this as an agent tool."""

    def list_refs(self) -> tuple[ArtifactRef, ...]:
        """Optional trusted enumeration used by bounded governed projections."""

        raise ArtifactStoreError("This ArtifactStore does not support enumeration")


class LocalArtifactStore(ArtifactStore):
    """Small JSON store for local development and synthetic tests."""

    def __init__(
        self,
        root: str | Path,
        *,
        trace_recorder: RunTraceRecorder | None = None,
    ):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise ArtifactStoreError(f"Artifact root is not a directory: {self.root}")
        self.trace_recorder = trace_recorder
        self._lock = Lock()

    def register(
        self,
        *,
        artifact_type: str,
        exposure_class: ArtifactExposureClass,
        release_basis: ArtifactReleaseBasis = ArtifactReleaseBasis.INTERNAL_ONLY,
        representation: ArtifactRepresentation,
        owner_user_id: str = "local-user",
        project_id: str = "local-project",
        lab_id: str = "local-lab",
        run_id: UUID | None = None,
        stage_id: WorkflowStage | None = None,
        producer_invocation_id: UUID | None = None,
        schema: ArtifactSchema | None = None,
        metadata: dict[str, JsonValue] | None = None,
        artifact_id: UUID | None = None,
    ) -> ArtifactRef:
        identifier = artifact_id or uuid4()
        path = self._path_for(identifier)
        ref = ArtifactRef(
            artifact_id=identifier,
            artifact_type=artifact_type,
            owner_user_id=owner_user_id,
            project_id=project_id,
            lab_id=lab_id,
            run_id=run_id,
            stage_id=stage_id,
            producer_invocation_id=producer_invocation_id,
            storage_locator=str(path),
            artifact_schema=schema,
            exposure_class=exposure_class,
            release_basis=release_basis,
            metadata=metadata or {},
        )
        stored = StoredArtifact(ref=ref, representation=representation)
        with self._lock:
            try:
                with path.open("x", encoding="utf-8") as handle:
                    handle.write(stored.model_dump_json())
                    handle.write("\n")
            except FileExistsError as exc:
                raise ArtifactStoreError(
                    f"Artifact already exists: {identifier}"
                ) from exc
            except OSError as exc:
                raise ArtifactStoreError(
                    f"Could not register artifact {identifier}: {exc}"
                ) from exc
        self._emit_registered(ref)
        return ref

    def exists(self, artifact_id: UUID | str) -> bool:
        identifier = coerce_artifact_id(artifact_id)
        return self._path_for(identifier).is_file()

    def register_file(
        self,
        source: str | Path,
        *,
        artifact_type: str,
        exposure_class: ArtifactExposureClass,
        release_basis: ArtifactReleaseBasis = ArtifactReleaseBasis.INTERNAL_ONLY,
        representation: ArtifactRepresentation,
        owner_user_id: str = "local-user",
        project_id: str = "local-project",
        lab_id: str = "local-lab",
        run_id: UUID | None = None,
        stage_id: WorkflowStage | None = None,
        producer_invocation_id: UUID | None = None,
        schema: ArtifactSchema | None = None,
        metadata: dict[str, JsonValue] | None = None,
        artifact_id: UUID | None = None,
    ) -> ArtifactRef:
        source_path = Path(source)
        if source_path.is_symlink():
            raise ArtifactStoreError("Artifact source cannot be a symlink")
        try:
            source_path = source_path.resolve(strict=True)
        except OSError as exc:
            raise ArtifactStoreError(f"Artifact source does not exist: {source}") from exc
        if not source_path.is_file():
            raise ArtifactStoreError(f"Artifact source is not a file: {source_path}")

        identifier = artifact_id or uuid4()
        envelope_path = self._path_for(identifier)
        blob_directory = self.root / "blobs" / str(identifier)
        blob_path = blob_directory / "content"
        ref = ArtifactRef(
            artifact_id=identifier,
            artifact_type=artifact_type,
            owner_user_id=owner_user_id,
            project_id=project_id,
            lab_id=lab_id,
            run_id=run_id,
            stage_id=stage_id,
            producer_invocation_id=producer_invocation_id,
            storage_locator=str(blob_path),
            original_filename=source_path.name,
            artifact_schema=schema,
            exposure_class=exposure_class,
            release_basis=release_basis,
            metadata=metadata or {},
        )
        stored = StoredArtifact(ref=ref, representation=representation)
        with self._lock:
            if envelope_path.exists() or blob_directory.exists():
                raise ArtifactStoreError(f"Artifact already exists: {identifier}")
            try:
                blob_directory.mkdir(parents=True, exist_ok=False)
                shutil.copyfile(source_path, blob_path)
                with envelope_path.open("x", encoding="utf-8") as handle:
                    handle.write(stored.model_dump_json())
                    handle.write("\n")
            except OSError as exc:
                raise ArtifactStoreError(
                    f"Could not register artifact file {identifier}: {exc}"
                ) from exc
        self._emit_registered(ref)
        return ref

    def get_ref(self, artifact_id: UUID | str) -> ArtifactRef:
        return self.load_for_view(artifact_id).ref

    def inspect_raw_head(self, ref: ArtifactRef) -> RawHeadPreview:
        """Trusted exposure-service operation, never a path-accepting model tool."""
        if (ref.exposure_class is not ArtifactExposureClass.RAW
                or ref.release_basis is not ArtifactReleaseBasis.RAW_INGESTION
                or self.get_ref(ref.artifact_id) != ref):
            raise ValueError("Preview requires the current ingested RAW identity")
        if ref.original_filename is None:
            return RawHeadPreview(status="UNSUPPORTED_FORMAT")
        expected = self.root / "blobs" / str(ref.artifact_id) / "content"
        if (Path(ref.storage_locator) != expected
                or any(path.is_symlink() for path in (expected, *expected.parents))):
            raise ValueError("Preview source is not an owned regular blob")
        descriptor = os.open(expected, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("Preview source is not a regular file")
            return inspect_file(stream, filename=ref.original_filename,
                                size_bytes=os.fstat(stream.fileno()).st_size)

    def list_refs(self) -> tuple[ArtifactRef, ...]:
        refs: list[ArtifactRef] = []
        for path in sorted(self.root.glob("*.json"), key=lambda item: item.name):
            try:
                refs.append(
                    StoredArtifact.model_validate_json(
                        path.read_text(encoding="utf-8")
                    ).ref
                )
            except (OSError, ValidationError) as exc:
                raise ArtifactStoreError(
                    f"Could not enumerate artifact envelope {path.name}: {exc}"
                ) from exc
        return tuple(refs)

    def load_for_view(self, artifact_id: UUID | str) -> StoredArtifact:
        identifier = coerce_artifact_id(artifact_id)
        path = self._path_for(identifier)
        if not path.is_file():
            raise ArtifactNotFoundError(f"Artifact not found: {identifier}")
        try:
            return StoredArtifact.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as exc:
            raise ArtifactStoreError(
                f"Could not read artifact {identifier}: {exc}"
            ) from exc

    def _path_for(self, artifact_id: UUID) -> Path:
        path = (self.root / f"{artifact_id}.json").resolve()
        if path.parent != self.root:
            raise ArtifactIdentifierError("Artifact path escaped the configured root")
        return path

    def _emit_registered(self, ref: ArtifactRef) -> None:
        if self.trace_recorder is None or ref.run_id is None:
            return
        self.trace_recorder.emit(
            ref.run_id,
            TraceEventType.ARTIFACT_REGISTERED,
            stage_id=ref.stage_id,
            invocation_id=ref.producer_invocation_id,
            status="REGISTERED",
            payload={
                "artifact_id": str(ref.artifact_id),
                "artifact_type": ref.artifact_type,
                "exposure_class": ref.exposure_class.value,
                "release_basis": ref.release_basis.value,
                "owner_user_id": ref.owner_user_id,
                "project_id": ref.project_id,
                "lab_id": ref.lab_id,
            },
        )
