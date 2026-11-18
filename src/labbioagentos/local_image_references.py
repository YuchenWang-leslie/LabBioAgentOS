"""Live references to images, kept in each member's own area (DECIDED-C15-H, refined 2026-11-18).

Each use of an image is recorded when it happens: an execution submission
records the run and its directory, and a derived-environment build records
the environment and its record file. A reference is live while the run can
still continue (its record is not terminal) or while the environment record
exists; only live references block deleting an image. The index is
append-only, one file per (kind, image, holder), so recording is idempotent.

When the index is first created, the run directories that can be found
under the member's result roots, and the member's environment records, are
added to it. A member's live references to lab images are mirrored into the
lab area's `usage/` markers, which is all the holding account can see.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_validator

from .append_only import AppendOnlyError, add_file, list_files, read_file
from .execution.identity import SourceIdentity

REFERENCES = "references"
_BACKFILLED = ".references-filled"  # Beside the index, whose directory holds reference files only.
_NAME = re.compile(r"[0-9a-f]{40}\.json")
_TERMINAL = frozenset({"COMPLETED", "FAILED", "CANCELLED"})
_MAX_RUN_DEPTH = 6
_MAX_TRACE_LINE_BYTES = 1_048_576


class ReferenceKind(StrEnum):
    RUN = "RUN"
    ENVIRONMENT = "ENVIRONMENT"


class ImageReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: ReferenceKind
    # The image's key, and its backend-neutral source when known; either identifies it.
    image_key: StrictStr | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    image_source: StrictStr | None = Field(default=None, min_length=1, max_length=512)
    holder: StrictStr = Field(pattern=r"^([0-9a-f]{32}|env-[0-9a-f]{64})$")  # Run id or environment key.
    location: StrictStr = Field(min_length=1, max_length=4096)  # Run directory or environment record.
    at: datetime

    @model_validator(mode="after")
    def identify(self) -> "ImageReference":
        if self.image_key is None and self.image_source is None:
            raise ValueError("A reference names its image by key or by source")
        if not Path(self.location).is_absolute():
            raise ValueError("A reference location is an absolute path")
        return self

    def matches(self, key: str, source: str | None) -> bool:
        return self.image_key == key or (source is not None and self.image_source == source)


class ReferenceIndex:
    """The member's append-only reference files."""

    def __init__(self, member_root: Path):
        self.root = Path(member_root) / REFERENCES

    @property
    def exists(self) -> bool:
        return self.root.is_dir() and (self.root.parent / _BACKFILLED).is_file()

    def record(self, reference: ImageReference) -> None:
        self.root.mkdir(mode=0o700, exist_ok=True)
        identity = "|".join((reference.kind.value, reference.image_key or "", reference.image_source or "",
                             reference.holder))
        try:
            add_file(self.root / (hashlib.sha256(identity.encode()).hexdigest()[:40] + ".json"), "reference",
                     reference.model_dump(mode="json"))
        except AppendOnlyError:
            pass  # Already recorded.

    def references(self) -> list[ImageReference]:
        return [read_file(path, "reference", ImageReference) for path in list_files(self.root, _NAME)]

    def ensure(self, *, result_roots: tuple[Path, ...], environment_root: Path | None) -> None:
        """Create the index once, adding the runs and environments that already exist."""
        if self.exists:
            return
        self.root.mkdir(mode=0o700, exist_ok=True)
        for directory in _run_directories(result_roots):
            for key, source in _traced_images(directory):
                self.record(run_reference(directory, key=key, source=source))
        if environment_root is not None:
            for path, base_source in _environment_records(environment_root):
                self.record(ImageReference(kind=ReferenceKind.ENVIRONMENT, image_source=base_source,
                                           holder="env-" + path.stem, location=str(path), at=_now()))
        (self.root.parent / _BACKFILLED).touch(mode=0o600)

    def live(self, key: str, source: str | None = None, *, deleted: Callable[[str], bool] = lambda _key: False
             ) -> list[tuple[ImageReference, str]]:
        """The live references to one image, each with why it is live."""
        return [(reference, why) for reference in self.references()
                if reference.matches(key, source) and (why := liveness(reference, deleted=deleted)) is not None]


def run_reference(directory: Path, *, key: str | None, source: str | None, run_id: str | None = None
                  ) -> ImageReference:
    return ImageReference(kind=ReferenceKind.RUN, image_key=key, image_source=source,
                          holder=run_id or _run_id(directory), location=str(directory), at=_now())


def liveness(reference: ImageReference, *, deleted: Callable[[str], bool] = lambda _key: False) -> str | None:
    """Why a reference is live, or None when it no longer holds the image.

    An environment holds its base while its record exists and its file was not deleted
    (`deleted` says, by the environment's key): records are kept forever (DECIDED-C15-H).
    """
    from .local_conversations import read_run_record

    location = Path(reference.location)
    if reference.kind is ReferenceKind.ENVIRONMENT:
        return "ENVIRONMENT_RECORDED" if location.is_file() and not deleted(reference.holder) else None
    if not location.exists():
        return None  # The run directory was removed.
    try:
        record = read_run_record(location)
    except (OSError, ValueError):
        return "RUN_UNREADABLE"  # Unknown is treated as live, never as free.
    if record.workflow_run.status.value in _TERMINAL and record.recovery_state.value == "STABLE":
        return None
    return "RUN_NOT_TERMINAL"


def _run_directories(roots: tuple[Path, ...]) -> Iterator[Path]:
    """Directories with a RUN.json under each root, without following links or entering a run."""
    for root in roots:
        if not root.is_dir() or root.is_symlink():
            continue
        base = len(root.parts)
        for current, directories, files in os.walk(root, followlinks=False):
            if "RUN.json" in files:
                directories[:] = []
                yield Path(current)
            elif len(Path(current).parts) - base >= _MAX_RUN_DEPTH:
                directories[:] = []


def _run_id(directory: Path) -> str:
    try:
        manifest = json.loads((directory / "RUN.json").read_text(encoding="utf-8"))
        return manifest["run_id"].replace("-", "")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ValueError("The run manifest is unreadable") from None


def _traced_images(directory: Path) -> set[tuple[str, str | None]]:
    """(key, source) of every execution a run's trace records; an unreadable trace adds nothing."""
    images = set()
    trace = directory / "run-trace.jsonl"
    if not trace.is_file() or trace.is_symlink():
        return images
    try:
        _run_id(directory)
    except ValueError:
        return images
    with trace.open("rb") as stream:
        for line in stream:
            if len(line) > _MAX_TRACE_LINE_BYTES or b"EXECUTION_PLANNED" not in line:
                continue
            try:
                event = json.loads(line)
                payload = event["payload"]
                key = payload["image_key"]
            except (ValueError, KeyError, TypeError):
                continue
            if event.get("event_type") != "EXECUTION_PLANNED" or not isinstance(key, str):
                continue
            source = None
            if isinstance(payload.get("image_source"), dict):
                try:
                    source = SourceIdentity.model_validate(payload["image_source"], strict=False).canonical
                except ValueError:
                    source = None
            images.add((key, source))
    return images


def _environment_records(root: Path) -> Iterator[tuple[Path, str]]:
    records = Path(root) / "records"
    if not records.is_dir() or records.is_symlink():
        return
    for path in sorted(records.glob("*.json")):
        if path.is_symlink() or re.fullmatch(r"[0-9a-f]{64}", path.stem) is None:
            continue
        try:
            base = json.loads(path.read_text(encoding="utf-8")).get("base_source_identity")
            source = SourceIdentity.model_validate(base, strict=False).canonical
        except (OSError, ValueError, AttributeError):
            continue  # Records from before C15 name only a realization.
        yield path, source


def _now() -> datetime:
    return datetime.now(timezone.utc)


def result_roots(settings) -> tuple[Path, ...]:
    """Where this member's runs live: every project of a managed user, or the one result root."""
    if settings.managed_root is not None:
        projects = Path(settings.managed_root) / settings.identity.user_id / "projects"
        if projects.is_dir() and not projects.is_symlink():
            roots = tuple(sorted(project / "runs" for project in projects.iterdir()
                                 if project.is_dir() and not project.is_symlink()))
            if roots:
                return roots
    return (Path(settings.result_root),)


def member_index(settings) -> ReferenceIndex | None:
    """The member's reference index, created on first use; None without a lab area."""
    if settings.lab is None:
        return None
    from .local_lab import private_member_root

    index = ReferenceIndex(private_member_root(settings.lab.member_root))
    index.ensure(result_roots=result_roots(settings),
                 environment_root=settings.environment.root if settings.environment is not None else None)
    return index


class UseRecorder:
    """Records the images one member's runs and environments use, and marks the lab's."""

    def __init__(self, settings, catalog, index: ReferenceIndex):
        from .local_lab import LabArea

        self.catalog, self.index, self.user_id = catalog, index, settings.principal.user_id
        self.area = LabArea(settings.lab.root)

    def run(self, key: str, run_id, run_directory: Path) -> None:
        record = self.catalog.record(key)
        self.index.record(run_reference(Path(run_directory), key=key,
                                        source=record.source.canonical if record is not None else None,
                                        run_id=run_id.hex))
        self._mark(key, run_id.hex)

    def environment(self, base, environment_key: str, record_path: Path) -> None:
        self.index.record(ImageReference(kind=ReferenceKind.ENVIRONMENT, image_key=base.key,
                                         image_source=base.source_identity.canonical, holder=environment_key,
                                         location=str(record_path), at=_now()))
        self._mark(base.key, environment_key)

    def _mark(self, key: str, holder: str) -> None:
        from .execution.catalog import ImageScope

        if self.catalog.scope(key) is ImageScope.LAB:
            self.area.mark_usage(key, self.user_id, holder)
