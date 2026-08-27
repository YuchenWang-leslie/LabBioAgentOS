"""Minimal local artifact persistence addressed only by artifact UUID."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from threading import Lock
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from labbioagentos.contracts import WorkflowStage
from labbioagentos.trace import RunTraceRecorder, TraceEventType

from .models import (
    ArtifactExposureClass,
    ArtifactRef,
    ArtifactRepresentation,
    ArtifactSchema,
)


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
        representation: ArtifactRepresentation,
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
    def get_ref(self, artifact_id: UUID | str) -> ArtifactRef:
        """Return metadata only."""

    @abstractmethod
    def load_for_view(self, artifact_id: UUID | str) -> StoredArtifact:
        """Trusted exposure-service read; never register this as an agent tool."""


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
        representation: ArtifactRepresentation,
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
            run_id=run_id,
            stage_id=stage_id,
            producer_invocation_id=producer_invocation_id,
            storage_locator=str(path),
            artifact_schema=schema,
            exposure_class=exposure_class,
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
        if self.trace_recorder is not None and run_id is not None:
            self.trace_recorder.emit(
                run_id,
                TraceEventType.ARTIFACT_REGISTERED,
                stage_id=stage_id,
                invocation_id=producer_invocation_id,
                status="REGISTERED",
                payload={
                    "artifact_id": str(identifier),
                    "artifact_type": artifact_type,
                    "exposure_class": exposure_class.value,
                },
            )
        return ref

    def exists(self, artifact_id: UUID | str) -> bool:
        identifier = coerce_artifact_id(artifact_id)
        return self._path_for(identifier).is_file()

    def get_ref(self, artifact_id: UUID | str) -> ArtifactRef:
        return self.load_for_view(artifact_id).ref

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
