"""Owned Gold catalog and derived Markdown views; SQLite remains authoritative."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
import stat
import tempfile

from .governance import AuthorizationDenied
from .local_gold import _personal_store
from .skills import SkillSearchContext


class GoldExportConflict(ValueError):
    """A derived file was edited or does not belong to this exporter."""


def _slugify(text: str, max_length: int = 60) -> str:
    """Convert a skill name into a filesystem-safe human-readable slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.casefold()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if len(slug) > max_length:
        slug = slug[:max_length].rstrip("-")
    return slug or "unnamed"


def _export_dirname(skill) -> str:
    """Build a readable directory name: {slug}_v{version}_{short_uuid}."""
    slug = _slugify(skill.name)
    short_id = str(skill.skill_id)[:8]
    return f"{slug}_v{skill.version}_{short_id}"


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
    short_id = str(skill.skill_id)[:8]

    text = (
        f"# {skill.name}\n\n"
        f"> **WF+Skills Gold** | v{skill.version} | `{short_id}`\n"
        f"> SQLite is the sole authority; this file is a read-only derived view.\n\n"
        f"{skill.description}\n\n"
    )

    text += (
        "| Field | Value |\n|---|---|\n"
        f"| Skill ID | `{skill.skill_id}` |\n"
        f"| Version | {skill.version} |\n"
        f"| Owner | {skill.owner_user_id} |\n"
        f"| Scope | {skill.scope.value} |\n"
        f"| Approved by | {skill.approved_by} |\n"
        f"| Approved at | {skill.approved_at.isoformat()} |\n"
        f"| Content SHA256 | `{_content_hash(skill)[:16]}...` |\n\n"
    )

    if procedure.tags:
        text += "**Tags:** " + " · ".join(f"`{t}`" for t in sorted(procedure.tags)) + "\n\n"
    if procedure.artifact_types:
        text += "**Artifact types:** " + " · ".join(f"`{t}`" for t in sorted(procedure.artifact_types)) + "\n\n"

    text += f"## When to Use\n\n{procedure.applicability}\n\n"

    text += "## Workflow Guide\n\n"
    if procedure.workflow_outline:
        for i, step in enumerate(procedure.workflow_outline, 1):
            text += f"### Step {i}\n\n{step}\n\n"
    else:
        text += "_No workflow outline recorded._\n\n"

    hint_sections = [
        ("Execution Guidance", procedure.execution_guidance,
         "How to approach execution at each stage"),
        ("Parameter Guidance", procedure.parameter_guidance,
         "What to consider when choosing parameters"),
        ("Collaboration Guidance", procedure.agent_collaboration_guidance,
         "How agents should collaborate"),
    ]
    hints = [(title, items, desc) for title, items, desc in hint_sections if items]
    if hints:
        text += "## Step-by-Step Direction Hints\n\n"
        for title, items, desc in hints:
            text += f"### {title}\n\n_{desc}_\n\n"
            for item in items:
                text += f"- {item}\n"
            text += "\n"

    if procedure.reusable_principles:
        text += "## Reusable Principles\n\n"
        for principle in procedure.reusable_principles:
            text += f"- {principle}\n"
        text += "\n"

    if procedure.validation_expectations:
        text += "## Validation Expectations\n\n"
        for item in procedure.validation_expectations:
            text += f"- {item}\n"
        text += "\n"

    if procedure.known_failure_modes:
        text += "## Known Failure Modes\n\n"
        for item in procedure.known_failure_modes:
            text += f"- {item}\n"
        text += "\n"

    if procedure.debug_lessons:
        text += "## Debug Lessons\n\n"
        for item in procedure.debug_lessons:
            text += f"- {item}\n"
        text += "\n"

    if procedure.known_limitations:
        text += "## Known Limitations\n\n"
        for item in procedure.known_limitations:
            text += f"- {item}\n"
        text += "\n"

    if procedure.adaptation_points:
        text += "## Adaptation Points\n\n"
        text += "These decisions belong to the future task; the Gold does not prescribe them.\n\n"
        for index, point in enumerate(procedure.adaptation_points, 1):
            text += f"### Decision {index}\n\n**{point.decision}**\n\n"
            if point.evidence_requirements:
                text += "**Required evidence:**\n"
                for req in point.evidence_requirements:
                    text += f"- {req}\n"
                text += "\n"
            if point.selection_considerations:
                text += "**Selection considerations:**\n"
                for cons in point.selection_considerations:
                    text += f"- {cons}\n"
                text += "\n"
            if point.revalidation_requirements:
                text += "**Revalidation after choice:**\n"
                for req in point.revalidation_requirements:
                    text += f"- {req}\n"
                text += "\n"

    if procedure.input_contract_ids or procedure.output_contract_ids:
        text += "## Contracts\n\n"
        if procedure.input_contract_ids:
            text += "**Input:** " + ", ".join(f"`{c}`" for c in procedure.input_contract_ids) + "\n\n"
        if procedure.output_contract_ids:
            text += "**Output:** " + ", ".join(f"`{c}`" for c in procedure.output_contract_ids) + "\n\n"

    text += "---\n\n"
    text += f"*Source run: `{skill.source_run_id}` | Full provenance in skills.sqlite*\n"

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


def _replace_generated(path: Path, body: bytes, expected_hash: str | None) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".export-", suffix=".tmp",
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        _directory(path.parent)
        if _file_hash(path) != expected_hash:
            raise GoldExportConflict("Gold export changed during export")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def export_gold_library(service, principal) -> dict:
    """Export approved versions only; preserve edits instead of silently repairing them."""
    store = _personal_store(service)
    root = store.path.parent
    _directory(root)
    with store._lock:
        skills = _owned_skills(service, principal)
        snapshot = store._load()._snapshot()
        if any(item.lab_id != principal.lab_id for item in (*snapshot.gold, *snapshot.proposals)):
            raise AuthorizationDenied("Gold export requires the library's exact lab")
        rendered = {}
        legacy_hashes = {}
        index = (
            "# Gold Skills\n\n"
            "Generated catalog of approved Agent-authored versions. "
            "SQLite is authoritative. Do not edit generated files; edits are not imported "
            "and a conflicting file stops export.\n\n"
            "Each Gold is a **WF+Skills** record: workflow structure + per-step direction hints "
            "for the runtime model to adapt.\n\n"
        )
        if not skills:
            index += "No approved Gold Skills.\n"
        for skill in skills:
            dirname = _export_dirname(skill)
            relative = f"{dirname}/skill.md"
            rendered[relative] = _markdown(skill)
            legacy_hashes[relative] = hashlib.sha256(_markdown(skill)).hexdigest()
            short_id = str(skill.skill_id)[:8]
            tags_str = ", ".join(sorted(skill.procedure.tags)) if skill.procedure.tags else "(none)"
            index += (
                f"## {skill.name}\n\n"
                f"**v{skill.version}** · `{short_id}` · [{dirname}/skill.md]({relative})\n\n"
                f"{skill.description}\n\n"
                f"**Tags:** {tags_str}\n\n"
                f"**When to use:** {skill.procedure.applicability}\n\n"
            )
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
            migrations = {}
            for relative, body in rendered.items():
                path = root / relative
                if path.parent.exists() or path.parent.is_symlink():
                    _directory(path.parent)
                existing = _file_hash(path)
                if existing is not None and existing != hashlib.sha256(body).hexdigest():
                    if existing != legacy_hashes[relative]:
                        raise GoldExportConflict("An approved Gold version export was modified")
                    migrations[relative] = existing
            for relative, body in rendered.items():
                path = root / relative
                path.parent.mkdir(mode=0o700, exist_ok=True)
                _directory(path.parent)
                if relative in migrations:
                    _replace_generated(path, body, migrations[relative])
                elif _file_hash(path) is None:
                    _write_new(path, body)
            if existing_index != index_hash:
                _replace_generated(root / "INDEX.md", index_body, existing_index)
            connection.execute("INSERT OR REPLACE INTO local_gold_export(path, sha256) VALUES (?, ?)",
                               ("INDEX.md", index_hash))
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
    return {"event": "gold_exported", "user_id": principal.user_id,
            "exported_versions": len(skills), "index": str(root / "INDEX.md"),
            "authority": "skills.sqlite", "export_status": "complete"}
