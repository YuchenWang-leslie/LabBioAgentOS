"""Image catalog: append-only image records, state events and the merged view.

Records and events are one file each. A file is written to a temporary name and
then linked into place, so an existing file is never replaced; a state change is
a new event file (DECIDED-C15-H). The lab area may sit on a shared filesystem
that other members read while an administrator writes, so no database is used.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError, field_validator, model_validator

from .errors import ImageNotApprovedError
from .identity import SourceIdentity
from .images import ApprovedImage
from .inventory import InventoryOrigin, validate_purpose
from .models import ExecutionImageKey, ExecutionRuntime

_RECORD_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\.json")
_EVENT_NAME = re.compile(r"[0-9]{8}T[0-9]{12}Z-[0-9a-f]{32}\.json")
_MAX_FILE_BYTES = 1_048_576


class ImageScope(StrEnum):
    PERSONAL = "PERSONAL"  # Only its owner's runs can see and select it.
    LAB = "LAB"  # Configured by the deployment or approved by a LAB_ADMIN.


class ImageState(StrEnum):
    ACTIVE = "ACTIVE"
    DEPRECATED = "DEPRECATED"  # Hidden from models; runs that already used it may continue.
    REVOKED = "REVOKED"  # Never executes again; its record stays for provenance.


class GpuSupport(StrEnum):
    NONE = "NONE"
    SUPPORTED = "SUPPORTED"
    REQUIRED = "REQUIRED"


class ExecutionBackendName(StrEnum):
    DOCKER = "DOCKER"
    APPTAINER = "APPTAINER"


# A REVOKED image is never reactivated; a new admission is a new record.
_TRANSITIONS = {
    ImageState.ACTIVE: {ImageState.DEPRECATED, ImageState.REVOKED},
    ImageState.DEPRECATED: {ImageState.ACTIVE, ImageState.REVOKED},
    ImageState.REVOKED: set(),
}


class CatalogError(ValueError):
    """The catalog on disk is unreadable, inconsistent or would be changed unsafely."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Realization(_Model):
    """One backend's file for a source, produced by a recorded platform procedure."""

    backend: ExecutionBackendName
    resolved_image: StrictStr = Field(min_length=1, max_length=512)
    produced_by: StrictStr = Field(min_length=1, max_length=128)


class InventorySnapshot(_Model):
    origin: InventoryOrigin
    packages: dict[StrictStr, StrictStr] = Field(default_factory=dict, max_length=4096)
    invalid_count: int = Field(default=0, ge=0)
    truncated: bool = False


class ImageRecord(_Model):
    key: ExecutionImageKey
    scope: ImageScope
    owner_user_id: StrictStr | None = Field(default=None, min_length=1, max_length=128)
    source: SourceIdentity
    realizations: tuple[Realization, ...] = Field(min_length=1, max_length=16)
    runtime: ExecutionRuntime = ExecutionRuntime.PYTHON
    executable: tuple[StrictStr, ...] = ("python",)
    available_python_modules: tuple[StrictStr, ...] = Field(default=(), max_length=256)
    inventory: InventorySnapshot
    purpose: StrictStr | None = None
    gpu: GpuSupport = GpuSupport.NONE
    size_bytes: int | None = Field(default=None, ge=0)
    admitted_by: StrictStr = Field(min_length=1, max_length=128)
    admitted_at: datetime

    @field_validator("purpose")
    @classmethod
    def check_purpose(cls, value: str | None) -> str | None:
        return None if value is None else validate_purpose(value)

    @model_validator(mode="after")
    def check_owner(self) -> "ImageRecord":
        if (self.scope is ImageScope.PERSONAL) != (self.owner_user_id is not None):
            raise ValueError("A personal image has an owner and a lab image has none")
        backends = [item.backend for item in self.realizations]
        if len(set(backends)) != len(backends):
            raise ValueError("One realization per backend")
        return self

    def approved_image(self, backend: ExecutionBackendName) -> ApprovedImage | None:
        """The execution view on one backend; None when this backend has no realization."""
        realization = next((item for item in self.realizations if item.backend is backend), None)
        if realization is None:
            return None
        return ApprovedImage(
            key=self.key, reference=realization.resolved_image, runtime=self.runtime,
            executable=self.executable, available_python_modules=self.available_python_modules,
            installed_packages=dict(self.inventory.packages), source=self.source,
        )


class ImageEvent(_Model):
    key: ExecutionImageKey
    state: ImageState
    actor: StrictStr = Field(min_length=1, max_length=128)
    reason: StrictStr = Field(min_length=1, max_length=500)
    at: datetime


def _digest(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class CatalogStore:
    """One directory of image records and state events; files are only ever added."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.records_root = self.root / "records"
        self.events_root = self.root / "events"

    def initialize(self) -> "CatalogStore":
        for directory in (self.root, self.records_root, self.events_root):
            directory.mkdir(mode=0o700, exist_ok=True)
        return self

    def add_record(self, record: ImageRecord) -> None:
        if record.key in self.records():
            raise CatalogError("An image record with this key already exists")
        self._add(self.records_root / f"{record.key}.json", "record", record.model_dump(mode="json"))

    def add_event(self, event: ImageEvent, *, current: ImageState) -> None:
        if event.state not in _TRANSITIONS[current]:
            raise CatalogError(f"An image cannot change from {current.value} to {event.state.value}")
        stamp = event.at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        self._add(self.events_root / f"{stamp}-{uuid4().hex}.json", "event", event.model_dump(mode="json"))

    def records(self) -> dict[str, ImageRecord]:
        records = {}
        for path in self._files(self.records_root, _RECORD_NAME):
            record = self._read(path, "record", ImageRecord)
            if path.name != f"{record.key}.json":
                raise CatalogError("An image record file does not match its key")
            records[record.key] = record
        return records

    def events(self) -> list[ImageEvent]:
        # File names start with the UTC time, so name order is event order.
        return [self._read(path, "event", ImageEvent)
                for path in sorted(self._files(self.events_root, _EVENT_NAME))]

    def _add(self, path: Path, kind: str, payload: dict) -> None:
        temporary = path.with_name(f".{uuid4().hex}.tmp")
        body = json.dumps({kind: payload, "digest": _digest(payload)}, sort_keys=True).encode("utf-8")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, path)  # Fails if the name exists: nothing is ever replaced.
        except FileExistsError:
            raise CatalogError("A catalog file with this name already exists") from None
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _files(directory: Path, pattern: re.Pattern) -> Iterable[Path]:
        if not directory.is_dir():
            return []
        found = []
        for path in directory.iterdir():
            if path.name.startswith(".") and path.name.endswith(".tmp"):
                continue  # An interrupted write never became a catalog file.
            if path.is_symlink() or not path.is_file() or pattern.fullmatch(path.name) is None:
                raise CatalogError("The catalog directory holds an unexpected entry")
            found.append(path)
        return found

    @staticmethod
    def _read(path: Path, kind: str, model):
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                raise ValueError()
            content = json.loads(path.read_text(encoding="utf-8"))
            if set(content) != {kind, "digest"} or content["digest"] != _digest(content[kind]):
                raise ValueError()
            return model.model_validate_json(json.dumps(content[kind]))
        except (OSError, UnicodeError, ValueError, ValidationError):
            raise CatalogError("A catalog file is unreadable or fails its integrity check") from None


class ImageCatalog:
    """Merged view of the lab and personal catalogs for one user on one backend.

    Configured base images are lab images. Images the user's own environment
    builds produce are personal. A key held by both scopes fails closed rather
    than letting a personal image stand in for a lab one.
    """

    def __init__(self, *, owner_user_id: str, backend: ExecutionBackendName,
                 configured_keys: Iterable[str] = (), lab: CatalogStore | None = None,
                 personal: CatalogStore | None = None):
        self.owner_user_id, self.backend = owner_user_id, backend
        self.lab, self.personal = lab, personal
        self.configured_keys = frozenset(configured_keys)
        self._lab_records = lab.records() if lab is not None else {}
        self._personal_records = personal.records() if personal is not None else {}
        if any(record.scope is not ImageScope.LAB for record in self._lab_records.values()):
            raise CatalogError("The lab catalog holds a personal image")
        if any(record.owner_user_id != owner_user_id for record in self._personal_records.values()):
            raise CatalogError("The personal catalog holds another member's image")
        lab_keys = self.configured_keys | set(self._lab_records)
        if lab_keys & set(self._personal_records) or self.configured_keys & set(self._lab_records):
            raise CatalogError("An image key is held twice")
        self._states = {**self._replay(lab), **self._replay(personal)}

    @staticmethod
    def _replay(store: CatalogStore | None) -> dict[str, ImageState]:
        states: dict[str, ImageState] = {}
        for event in store.events() if store is not None else ():
            current = states.get(event.key, ImageState.ACTIVE)
            if event.state not in _TRANSITIONS[current]:
                raise CatalogError("The catalog events contain an illegal state change")
            states[event.key] = event.state
        return states

    def state(self, key: str) -> ImageState:
        return self._states.get(key, ImageState.ACTIVE)

    def scope(self, key: str) -> ImageScope:
        lab = key in self.configured_keys or key in self._lab_records
        return ImageScope.LAB if lab else ImageScope.PERSONAL

    def record(self, key: str) -> ImageRecord | None:
        return self._lab_records.get(key) or self._personal_records.get(key)

    def admitted_images(self) -> tuple[ApprovedImage, ...]:
        """Executable admitted images on this backend; revoked ones are never registered."""
        images = []
        for key, record in sorted({**self._lab_records, **self._personal_records}.items()):
            image = record.approved_image(self.backend)
            if image is not None and self.state(key) is not ImageState.REVOKED:
                images.append(image)
        return tuple(images)

    def visible(self, key: str) -> bool:
        """Shown to the model: only ACTIVE images."""
        return self.state(key) is ImageState.ACTIVE

    def require_selectable(self, key: str, *, used_by_run: bool) -> None:
        state = self.state(key)
        if state is ImageState.REVOKED:
            raise ImageNotApprovedError(f"Image key {key!r} has been revoked")
        if state is ImageState.DEPRECATED and not used_by_run:
            raise ImageNotApprovedError(
                f"Image key {key!r} is deprecated; only runs that already used it may continue")

    def describe(self, key: str) -> dict:
        """Model-visible catalog facts: no reference, registry, path or owner identity."""
        record = self.record(key)
        scope = self.scope(key)
        return {
            "scope": "LAB" if scope is ImageScope.LAB else "YOURS",
            "purpose": record.purpose if record else None,
            "gpu": (record.gpu if record else GpuSupport.NONE).value,
            "inventory_origin": record.inventory.origin.value if record else None,
            "size_mb": (round(record.size_bytes / 1_048_576) if record and record.size_bytes is not None
                        else None),
        }

    def set_state(self, key: str, state: ImageState, *, actor: str, reason: str) -> None:
        """Append a state event to the store that owns the key."""
        store = self.lab if self.scope(key) is ImageScope.LAB else self.personal
        if store is None:
            raise CatalogError("No catalog store owns this image")
        event = ImageEvent(key=key, state=state, actor=actor, reason=reason,
                           at=datetime.now(timezone.utc))
        store.add_event(event, current=self.state(key))
        self._states[key] = state
