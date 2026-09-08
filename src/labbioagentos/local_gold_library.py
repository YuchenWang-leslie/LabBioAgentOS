"""Owned Gold catalog and derived Markdown views; SQLite remains authoritative."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

from .governance import AuthorizationDenied
from .local_gold import _personal_store
from .skills import SkillSearchContext


class GoldExportConflict(ValueError):
    """A derived file was edited or does not belong to this exporter."""


def _owned_skills(service, principal, project_id=None):
    store = _personal_store(service)
    if store.user_id != principal.user_id:
        raise AuthorizationDenied("Gold library requires its exact local owner")
    return service.search(SkillSearchContext(
        user_id=principal.user_id, project_id=project_id, lab_id=principal.lab_id,
        include_lab=False,
    ), principal=principal)


def list_gold_library(service, principal, *, project_id=None, offset=0, limit=20,
                      tags=(), artifact_types=(), all_versions=False) -> dict:
    """Catalog only, without relevance ranking or authorization to use a Skill."""
    if not 0 <= offset or not 1 <= limit <= 100:
        raise ValueError("Invalid personal Gold catalog page")
    skills = _owned_skills(service, principal, project_id)
    if not all_versions:
        latest = {}
        for skill in skills:
            if skill.skill_id not in latest or latest[skill.skill_id].version < skill.version:
                latest[skill.skill_id] = skill
        skills = tuple(latest.values())
    # Filter after latest-version selection: never revive superseded guidance
    # merely because an old version matches a tag that the current one removed.
    skills = sorted((skill for skill in skills
                     if set(tags).issubset(skill.procedure.tags)
                     and set(artifact_types).issubset(skill.procedure.artifact_types)),
                    key=lambda skill: (skill.name.casefold(), str(skill.skill_id), skill.version))
    selected = skills[offset:offset + limit]
    next_offset = offset + len(selected)
    truncated = next_offset < len(skills)
    return {
        "user_id": principal.user_id, "available_count": len(skills),
        "offset": offset, "returned_count": len(selected), "truncated": truncated,
        "next_offset": next_offset if truncated else None, "all_versions": all_versions,
        "items": [{
            "skill_id": str(skill.skill_id), "version": skill.version, "name": skill.name,
            "description": skill.description, "scope": skill.scope.value,
            "tags": sorted(skill.procedure.tags),
            "artifact_types": sorted(skill.procedure.artifact_types),
            "applicability": skill.procedure.applicability,
            "known_limitations": list(skill.procedure.known_limitations),
            "input_contract_ids": list(skill.procedure.input_contract_ids),
            "output_contract_ids": list(skill.procedure.output_contract_ids),
        } for skill in selected],
    }


def _content_hash(skill) -> str:
    value = skill.model_dump(mode="json")
    for field in ("tags", "artifact_types"):
        value["procedure"][field] = sorted(value["procedure"][field])
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _section(title, values) -> str:
    if not values:
        return ""
    return f"\n## {title}\n\n" + "\n\n".join(values) + "\n"


def _markdown(skill) -> bytes:
    procedure = skill.procedure
    text = (
        f"# {skill.name}\n\n"
        "Derived read-only view of approved Agent-authored guidance. SQLite is the sole "
        "authority; editing this file does not approve or change a Skill. A proposed edit "
        "requires a new candidate and explicit approval. This file is not executable.\n\n"
        f"Skill ID: `{skill.skill_id}`  \nVersion: {skill.version}  \n"
        f"Owner: {skill.owner_user_id}  \nScope: {skill.scope.value}  \n"
        f"Content SHA256: `{_content_hash(skill)}`  \n"
        f"Approved by: {skill.approved_by}  \nApproved at: {skill.approved_at.isoformat()}\n\n"
        f"{skill.description}\n"
    )
    for title, values in (
        ("Tags", sorted(procedure.tags)),
        ("Artifact types", sorted(procedure.artifact_types)),
        ("Applicability", (procedure.applicability,)),
        ("Reference workflow", procedure.workflow_outline),
        ("Collaboration guidance", procedure.agent_collaboration_guidance),
        ("Execution guidance", procedure.execution_guidance),
        ("Parameter guidance", procedure.parameter_guidance),
        ("Reusable principles", procedure.reusable_principles),
        ("Validation expectations", procedure.validation_expectations),
        ("Known failure modes", procedure.known_failure_modes),
        ("Debug lessons", procedure.debug_lessons),
        ("Known limitations", procedure.known_limitations),
        ("Input contracts", procedure.input_contract_ids),
        ("Output contracts", procedure.output_contract_ids),
    ):
        text += _section(title, values)
    for index, point in enumerate(procedure.adaptation_points, 1):
        text += f"\n## Adaptation point {index}\n\n{point.decision}\n\nModifiable: true\n"
        text += _section("Required current evidence", point.evidence_requirements)
        text += _section("Selection considerations", point.selection_considerations)
        text += _section("Revalidation", point.revalidation_requirements)
    text += _section("Source lineage", (
        f"Source run: `{skill.source_run_id}`",
        f"Source bundle: `{skill.source_bundle_id}`",
        f"Source proposal: `{skill.source_proposal_id}`",
        f"Parent Skill: `{skill.parent_skill_id}`; parent version: {skill.parent_version}",
        f"Source usage record: `{skill.source_usage_record_id}`",
        *[f"Instruction: `{value}`" for value in procedure.important_instruction_ids],
        *[f"Script Artifact: `{value}`" for value in procedure.script_artifact_ids],
        *[f"Trace event: `{value}`" for value in procedure.source_trace_event_ids],
    ))
    return text.encode("utf-8")


def _directory(path: Path) -> None:
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("Gold export directories must not traverse symlinks")
    if not path.is_dir() or path.stat().st_mode & 0o077:
        raise ValueError("Gold export requires private directories")


def _file_hash(path: Path) -> str | None:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise GoldExportConflict("Gold export target is not an unaliased file") from exc
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
            raise GoldExportConflict("Gold export requires private, unaliased regular files")
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _write_new(path: Path, body: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())


def export_gold_library(service, principal) -> dict:
    """Export approved versions only; preserve edits instead of silently repairing them."""
    store = _personal_store(service)
    root = store.path.parent
    _directory(root)
    with store._lock:
        skills = _owned_skills(service, principal)
        # One managed root represents one authenticated local user/lab. Do not
        # replace an existing catalog with a different lab's partial view.
        snapshot = store._load()._snapshot()
        if any(item.lab_id != principal.lab_id for item in (*snapshot.gold, *snapshot.proposals)):
            raise AuthorizationDenied("Gold export requires the library's exact lab")
        rendered = {}
        index = ("# Gold Skills\n\nGenerated catalog of approved Agent-authored versions. "
                 "SQLite is authoritative. Do not edit generated files; edits are not imported "
                 "and a conflicting file stops export.\n\n")
        if not skills:
            index += "No approved Gold Skills.\n"
        for skill in skills:
            relative = f"{skill.skill_id}/v{skill.version}.md"
            rendered[relative] = _markdown(skill)
            # Keep names as prose, never as Markdown link targets or filesystem paths.
            index += (f"## {skill.name}\n\n[{skill.skill_id} v{skill.version}]({relative})\n\n"
                      f"{skill.description}\n\nTags: {', '.join(sorted(skill.procedure.tags))}\n\n"
                      f"Applicability: {skill.procedure.applicability}\n\n")
        index_body = index.encode("utf-8")
        index_hash = hashlib.sha256(index_body).hexdigest()
        connection = store._connection
        connection.execute("CREATE TABLE IF NOT EXISTS local_gold_export "
                           "(path TEXT PRIMARY KEY, sha256 TEXT NOT NULL)")
        connection.execute("BEGIN IMMEDIATE")
        try:
            previous = connection.execute(
                "SELECT sha256 FROM local_gold_export WHERE path = 'INDEX.md'"
            ).fetchone()
            existing_index = _file_hash(root / "INDEX.md")
            if existing_index is not None and (previous is None or existing_index != previous[0]):
                raise GoldExportConflict("Gold catalog was modified or is not exporter-owned")
            for relative, body in rendered.items():
                path = root / relative
                if path.parent.exists() or path.parent.is_symlink():
                    _directory(path.parent)
                existing = _file_hash(path)
                if existing is not None and existing != hashlib.sha256(body).hexdigest():
                    raise GoldExportConflict("An approved Gold version export was modified")
            for relative, body in rendered.items():
                path = root / relative
                path.parent.mkdir(mode=0o700, exist_ok=True)
                _directory(path.parent)
                if _file_hash(path) is None:
                    _write_new(path, body)
            if existing_index != index_hash:
                temporary = None
                try:
                    with tempfile.NamedTemporaryFile(dir=root, prefix=".index-", suffix=".tmp",
                                                     delete=False) as stream:
                        temporary = Path(stream.name)
                        stream.write(index_body)
                        stream.flush()
                        os.fsync(stream.fileno())
                    _directory(root)
                    if _file_hash(root / "INDEX.md") != existing_index:
                        raise GoldExportConflict("Gold catalog changed during export")
                    os.replace(temporary, root / "INDEX.md")
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
            connection.execute("INSERT OR REPLACE INTO local_gold_export(path, sha256) VALUES (?, ?)",
                               ("INDEX.md", index_hash))
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
    return {"event": "gold_exported", "user_id": principal.user_id,
            "exported_versions": len(skills), "index": str(root / "INDEX.md"),
            "authority": "skills.sqlite", "export_status": "complete"}
