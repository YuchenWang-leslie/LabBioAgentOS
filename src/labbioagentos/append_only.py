"""Directories whose files are only ever added, never replaced or edited.

Used for records other members read on a shared filesystem while one account
writes (DECIDED-C15-H): the image catalog and the lab member registry. Each file
holds one JSON payload and its sha256, checked on every read.
"""

from __future__ import annotations

import hashlib
import fcntl
import json
import os
import re
import stat
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ValidationError

_MAX_FILE_BYTES = 1_048_576
EVENT_NAME = re.compile(r"[0-9]{8}T[0-9]{12}Z-[0-9a-f]{32}\.json")


class AppendOnlyError(ValueError):
    """A file is unreadable, fails its integrity check, or would replace another."""


@contextmanager
def writer_lock(root: Path):
    """Serialize a read-validate-append transaction; keep the lock outside event directories."""
    descriptor = os.open(root / ".writer.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK,
                         0o600)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                or info.st_mode & 0o077):
            raise AppendOnlyError("The writer lock must be a private unaliased regular file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def digest(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def event_name(at: datetime) -> str:
    """UTC time first, so name order is event order."""
    return f"{at.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid4().hex}.json"


def next_event_time(directory: Path) -> datetime:
    """Now, but strictly after the newest event, so name order never ties."""
    now = datetime.now(timezone.utc)
    names = [path.name for path in list_files(directory, EVENT_NAME)]
    if not names:
        return now
    latest = datetime.strptime(max(names)[:22], "%Y%m%dT%H%M%S%fZ").replace(tzinfo=timezone.utc)
    return max(now, latest + timedelta(microseconds=1))


def add_file(path: Path, kind: str, payload: dict, mode: int = 0o640) -> None:
    temporary = path.with_name(f".{uuid4().hex}.tmp")
    body = json.dumps({kind: payload, "digest": digest(payload)}, sort_keys=True).encode("utf-8")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)  # By default readable by the directory's group, which may be the lab.
        os.link(temporary, path)  # Fails if the name exists: nothing is ever replaced.
    except FileExistsError:
        raise AppendOnlyError("A file with this name already exists") from None
    finally:
        temporary.unlink(missing_ok=True)


def list_files(directory: Path, pattern: re.Pattern) -> list[Path]:
    if not directory.is_dir():
        return []
    found = []
    for path in directory.iterdir():
        if path.name.startswith(".") and path.name.endswith(".tmp"):
            continue  # An interrupted write never became a record.
        if path.is_symlink() or not path.is_file() or pattern.fullmatch(path.name) is None:
            raise AppendOnlyError("The directory holds an unexpected entry")
        found.append(path)
    return found


def read_file(path: Path, kind: str, model: type[BaseModel]):
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            raise ValueError()
        content = json.loads(path.read_text(encoding="utf-8"))
        if set(content) != {kind, "digest"} or content["digest"] != digest(content[kind]):
            raise ValueError()
        return model.model_validate_json(json.dumps(content[kind]))
    except (OSError, UnicodeError, ValueError, ValidationError):
        raise AppendOnlyError("A file is unreadable or fails its integrity check") from None


def read_events(directory: Path, model: type[BaseModel]) -> list:
    return [read_file(path, "event", model) for path in sorted(list_files(directory, EVENT_NAME))]
