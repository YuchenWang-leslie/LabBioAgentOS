"""Trusted local workspace registration and authenticated path confinement.

This is an application boundary, not isolation from another process using the
same Unix account. Registry and credential administration belongs to the local
operator; task callers receive only their own credential.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
from urllib.parse import quote


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value):
        raise ValueError("Invalid workspace identifier")
    return value


def _safe_path(value: Path) -> Path:
    path = Path(value).expanduser().absolute()
    if ".." in path.parts or any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("Workspace paths cannot traverse aliases")
    return path.resolve()


def _within(value: Path, root: Path) -> Path:
    path = _safe_path(value)
    if path == root or not path.is_relative_to(root):
        raise PermissionError("Path is outside the selected workspace area")
    return path


def _private_file(path: Path, *, maximum_bytes: int | None = None) -> None:
    info = path.stat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or info.st_mode & 0o077 or (maximum_bytes is not None and info.st_size > maximum_bytes)):
        raise PermissionError("Private local persistence file is invalid")


@dataclass(frozen=True)
class ManagedWorkspace:
    user_id: str
    project_id: str
    lab_id: str
    user_root: Path
    project_root: Path
    data_root: Path
    result_root: Path
    gold_root: Path

    def check_input(self, path: Path) -> Path:
        source = _within(path, _safe_path(self.data_root))
        info = source.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise PermissionError("Input must be an unaliased regular project data file")
        return source

    def validate_run(self, path: Path) -> Path:
        """Validate a new or existing run path without creating or opening it."""
        candidate = _within(path, _safe_path(self.result_root))
        if candidate.exists() and not candidate.is_dir():
            raise ValueError("Run path is not a directory")
        return candidate


class WorkspaceRegistry:
    """SQLite identities; paths are derived from registered IDs, never stored input."""

    def __init__(self, root: Path):
        self.root = _safe_path(root)
        if not self.root.is_dir() or self.root.stat().st_mode & 0o077:
            raise PermissionError("Workspace registry root must be a private directory")
        database = self.root / "registry.sqlite"
        for suffix in ("", "-journal", "-wal", "-shm"):
            _safe_path(self.root / ("registry.sqlite" + suffix))
        _private_file(database)
        self._connection = sqlite3.connect(
            "file:" + quote(str(database), safe="/") + "?mode=rw", uri=True
        )
        self._connection.execute("PRAGMA foreign_keys = ON")
        if self._connection.execute("PRAGMA user_version").fetchone()[0] != 1:
            self.close()
            raise ValueError("Unsupported workspace registry format")

    @classmethod
    def initialize(cls, root: Path) -> "WorkspaceRegistry":
        root = _safe_path(root)
        if root == Path(root.anchor) or (root.exists() and (not root.is_dir() or any(root.iterdir()))):
            raise ValueError("Workspace initialization requires a new or empty directory")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        database = root / "registry.sqlite"
        descriptor = os.open(database, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        connection = sqlite3.connect(database)
        try:
            with connection:
                connection.execute("BEGIN")
                connection.execute("CREATE TABLE users (user_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL)")
                connection.execute(
                    "CREATE TABLE projects (user_id TEXT NOT NULL REFERENCES users(user_id), "
                    "project_id TEXT NOT NULL, PRIMARY KEY(user_id, project_id))"
                )
                connection.execute("PRAGMA user_version = 1")
        finally:
            connection.close()
        return cls(root)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "WorkspaceRegistry":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    def create_user(self, user_id: str, credential_path: Path) -> None:
        user_id = _identifier(user_id)
        credential = _safe_path(credential_path)
        if credential.is_relative_to(self.root) or not credential.parent.is_dir():
            raise ValueError("Credential must have an existing parent outside the managed workspace")
        user_root = _safe_path(self.root / user_id)
        if credential.exists() or user_root.exists():
            raise FileExistsError("User directory or credential already exists")
        token = secrets.token_urlsafe(32)
        created = []
        credential_created = False
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT INTO users VALUES (?, ?)",
                    (user_id, hashlib.sha256(token.encode("ascii")).hexdigest()),
                )
                for directory in (user_root, user_root / "GoldSkills", user_root / "projects"):
                    directory.mkdir(mode=0o700)
                    created.append(directory)
                descriptor = os.open(credential, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                credential_created = True
                with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                    stream.write(token + "\n")
        except Exception:
            if credential_created:
                credential.unlink()
            for directory in reversed(created):
                directory.rmdir()
            raise

    def authenticate(self, user_id: str, credential_path: Path) -> None:
        try:
            user_id = _identifier(user_id)
            credential = _safe_path(credential_path)
            _private_file(credential, maximum_bytes=256)
            descriptor = os.open(credential, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "r", encoding="ascii") as stream:
                token = stream.read(257).removesuffix("\n")
            if not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
                raise ValueError("Invalid credential format")
            row = self._connection.execute(
                "SELECT token_hash FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            matches = hmac.compare_digest(
                hashlib.sha256(token.encode("ascii")).hexdigest(), row[0] if row else "0" * 64
            )
            if row is None or not matches:
                raise ValueError("Invalid credential")
        except (OSError, UnicodeError, ValueError):
            raise PermissionError("Workspace authentication failed") from None

    def create_project(self, user_id: str, project_id: str) -> None:
        user_id, project_id = _identifier(user_id), _identifier(project_id)
        if self._connection.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)).fetchone() is None:
            raise PermissionError("Workspace user is not registered")
        projects_root = _safe_path(self.root / user_id / "projects")
        if not projects_root.is_dir():
            raise ValueError("Registered user directory is unavailable")
        project_root = _safe_path(projects_root / project_id)
        if project_root.exists():
            raise FileExistsError("Project directory already exists")
        created = []
        try:
            with self._connection:
                self._connection.execute("INSERT INTO projects VALUES (?, ?)", (user_id, project_id))
                for directory in (project_root, project_root / "data", project_root / "runs"):
                    directory.mkdir(mode=0o700)
                    created.append(directory)
        except Exception:
            for directory in reversed(created):
                directory.rmdir()
            raise

    def resolve(self, user_id: str, project_id: str, credential_path: Path) -> ManagedWorkspace:
        self.authenticate(user_id, credential_path)
        project_id = _identifier(project_id)
        row = self._connection.execute(
            "SELECT 1 FROM projects WHERE user_id = ? AND project_id = ?", (user_id, project_id)
        ).fetchone()
        if row is None:
            raise PermissionError("Project is not registered for the authenticated user")
        user_root = self.root / user_id
        project_root = user_root / "projects" / project_id
        paths = (user_root, project_root, project_root / "data", project_root / "runs", user_root / "GoldSkills")
        if any(not _safe_path(path).is_dir() for path in paths):
            raise ValueError("Registered workspace directory is unavailable")
        return ManagedWorkspace(user_id, project_id, "local-lab", *paths)
