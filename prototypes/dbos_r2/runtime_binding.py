"""R2 trusted binding: reuse Native configuration identity, never read keys.

The default is explicitly a fake provider/tool environment. A real container
binding additionally hashes the host-selected SIF and executable; this proves
identity, not successful preflight or permission to resume a Workflow.
"""

import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys

from labbioagentos.execution.models import ExecutionPlanDraft
from labbioagentos.kernel.contracts import ModelRequest, ModelTurn
from labbioagentos.local_config import LocalSettings, _source_digest, runtime_manifest
from labbioagentos.runtime.contracts import CapabilityEvidenceItem
from labbioagentos.runtime.observations import canonical_bytes
from labbioagentos.runtime.tooling import ToolResult


def probe_settings(root):
    return LocalSettings.model_validate({
        "result_root": root, "input_roots": [root],
        "identity": {"user_id": "r2-user", "project_id": "r2", "lab_id": "r2-lab"},
        "provider": {"endpoint": "https://r2-fake.invalid/v1", "env_file": root / "unused.env",
                     "api_key_env": "R2_UNUSED", "model_identifier": "r2-synthetic",
                     "trust_env": False, "max_retries": 0},
        "execution": {"backend": "apptainer", "image_key": "r2-not-executed",
                      "image_reference": "sha256:" + "0" * 64,
                      "resources": {"cpus": 4, "memory_mb": 4096, "pids_limit": 128,
                                    "timeout_seconds": 90},
                      "apptainer": {"binary": str(root / "unused-apptainer"),
                                    "sif_store": root / "unused-sif", "scratch_root": root / "scratch",
                                    "resource_enforcement": "systemd-affinity"}},
        "launcher": {"kind": "local"},
    })


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1048576), b""):
            digest.update(block)
    return digest.hexdigest()


def installation_digest(executable):
    # The user installer has one explicit, receipted layout. Do not hash just
    # its shell entrypoint while leaving the actual executable/helpers unbound.
    prefix = Path(executable).absolute().parent.parent
    receipt = json.loads((prefix / "installation-receipt.json").read_text())
    if (receipt.get("prefix") != str(prefix) or receipt.get("status") != "INSTALLED"
            or receipt.get("setuid") is not False):
        raise ValueError("A matching non-setuid user installation receipt is required")
    entries = []
    for path in sorted(prefix.rglob("*")):
        name = path.relative_to(prefix).as_posix()
        if path.is_symlink():
            if not path.resolve(strict=True).is_relative_to(prefix):
                raise ValueError("Installation links must stay inside the bound prefix")
            entries.append((name, "symlink", os.readlink(path)))
        elif path.is_file():
            entries.append((name, "file", path.stat().st_mode & 0o7777, file_digest(path)))
        elif path.is_dir():
            entries.append((name, "directory", path.stat().st_mode & 0o7777))
        else:
            raise ValueError("Unsupported entry in user installation")
    return hashlib.sha256(canonical_bytes(entries)).hexdigest()


def runtime_identity(settings, *, sif_path=None, executable_path=None):
    if (sif_path is None) != (executable_path is None):
        raise ValueError("Both resolved environment files must be bound")
    environment = {"kind": "FAKE_PROVIDER_AND_TOOL_NO_CONTAINER"}
    if sif_path is not None:
        digest = file_digest(sif_path)
        if settings.execution.image_reference != "sha256:" + digest:
            raise ValueError("SIF identity differs from trusted configuration")
        environment = {"kind": "HASH_BOUND_NOT_PREFLIGHT",
                       "sif_sha256": digest, "executable_sha256": file_digest(executable_path),
                       "user_installation_sha256": installation_digest(executable_path)}
    schemas = {model.__name__: model.model_json_schema() for model in (
        ModelRequest, ModelTurn, ToolResult, CapabilityEvidenceItem, ExecutionPlanDraft,
    )}
    return {
        "format_version": 2,
        "native_manifest": runtime_manifest(settings),
        "schema_sha256": hashlib.sha256(canonical_bytes(schemas)).hexdigest(),
        "prototype_sha256": _source_digest(Path(__file__).parent),
        "python": {"version": sys.version, "executable_sha256": file_digest(sys.executable)},
        "dependencies": {name: importlib.metadata.version(name) for name in (
            "dbos", "pydantic", "sqlalchemy", "aiosqlite", "httpx",
        )},
        "execution_environment": environment,
    }
