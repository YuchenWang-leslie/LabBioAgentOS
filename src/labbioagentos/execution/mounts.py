"""Trusted artifact mount resolution and execution workspace creation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import UUID

from labbioagentos.artifacts import ArtifactRef, ArtifactStore

from .errors import MountResolutionError
from .models import ExecutionPlan


@dataclass(frozen=True)
class ResolvedMount:
    """Host-controlled bind mount, never constructed from model host paths."""

    source: Path
    target: PurePosixPath
    read_only: bool
    artifact_id: UUID | None = None


@dataclass(frozen=True)
class ExecutionWorkspace:
    """One host-owned execution directory with fixed-purpose subpaths."""

    root: Path
    script_path: Path
    parameters_path: Path
    input_manifest_path: Path
    output_root: Path
    log_root: Path
    script_hash: str


class ExecutionWorkspaceManager:
    """Creates paths from execution IDs only, never from model-supplied paths."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        if self.root == Path(self.root.anchor):
            raise ValueError("Execution workspace root cannot be the filesystem root")
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise ValueError(f"Execution workspace root is not a directory: {self.root}")

    def prepare(
        self,
        plan: ExecutionPlan,
        input_mounts: tuple[ResolvedMount, ...] = (),
    ) -> ExecutionWorkspace:
        execution_root = (self.root / str(plan.execution_id)).resolve()
        if execution_root.parent != self.root:
            raise MountResolutionError("Execution workspace escaped its configured root")
        try:
            execution_root.mkdir(parents=False, exist_ok=False)
            output_root = execution_root / "outputs"
            log_root = execution_root / "logs"
            output_root.mkdir()
            log_root.mkdir()
            script_path = execution_root / "script.py"
            parameters_path = execution_root / "parameters.json"
            input_manifest_path = execution_root / "input-manifest.json"
            script_path.write_text(plan.script_content, encoding="utf-8")
            parameters_path.write_text(
                json.dumps(plan.parameters, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            input_manifest_path.write_text(
                json.dumps(
                    {
                        str(mount.artifact_id): str(mount.target)
                        for mount in input_mounts
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            raise MountResolutionError(
                f"Could not prepare execution workspace: {exc}"
            ) from exc
        script_hash = hashlib.sha256(plan.script_content.encode("utf-8")).hexdigest()
        return ExecutionWorkspace(
            root=execution_root,
            script_path=script_path,
            parameters_path=parameters_path,
            input_manifest_path=input_manifest_path,
            output_root=output_root,
            log_root=log_root,
            script_hash=script_hash,
        )


class MountResolver:
    """Resolve artifact IDs through the trusted store and root allowlist."""

    def __init__(
        self,
        store: ArtifactStore,
        *,
        approved_input_roots: tuple[str | Path, ...],
    ):
        self.store = store
        roots = tuple(Path(root).expanduser().resolve() for root in approved_input_roots)
        if not roots:
            raise ValueError("At least one approved input root is required")
        if any(root == Path(root.anchor) for root in roots):
            raise ValueError("Filesystem root cannot be an approved input root")
        self.approved_input_roots = roots

    def resolve_inputs(
        self, artifact_ids: tuple[UUID, ...]
    ) -> tuple[ResolvedMount, ...]:
        mounts: list[ResolvedMount] = []
        for artifact_id in artifact_ids:
            ref = self.store.get_ref(artifact_id)
            source = self._validate_locator(ref)
            mounts.append(
                ResolvedMount(
                    source=source,
                    target=PurePosixPath(
                        "/labbio/inputs",
                        str(ref.artifact_id),
                        source.name,
                    ),
                    read_only=True,
                    artifact_id=ref.artifact_id,
                )
            )
        return tuple(mounts)

    def _validate_locator(self, ref: ArtifactRef) -> Path:
        candidate = Path(ref.storage_locator)
        candidate_text = candidate.as_posix().lower()
        if candidate.name.lower() == "docker.sock" or candidate_text.endswith(
            "/docker.sock"
        ):
            raise MountResolutionError("Docker socket mounts are prohibited")
        if candidate.is_symlink():
            raise MountResolutionError("Input artifact locator cannot be a symlink")
        try:
            source = candidate.resolve(strict=True)
        except OSError as exc:
            raise MountResolutionError(
                f"Input artifact locator does not exist for {ref.artifact_id}"
            ) from exc
        if not source.is_file():
            raise MountResolutionError("Input artifact locator must be a regular file")
        if "," in str(source) or "\n" in str(source):
            raise MountResolutionError("Input artifact locator is not mount-safe")
        if not any(source.is_relative_to(root) for root in self.approved_input_roots):
            raise MountResolutionError(
                f"Input artifact {ref.artifact_id} is outside approved roots"
            )
        return source
