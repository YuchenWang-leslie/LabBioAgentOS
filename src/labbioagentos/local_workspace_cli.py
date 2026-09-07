"""Local account/project adapter; the workflow and scientific runtime stay unchanged."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from .governance import WorkspaceContext
from .local_workspace import WorkspaceRegistry


ADMIN_COMMANDS = {"workspace-init", "user-create", "project-create"}
GOLD_COMMANDS = {"gold-list", "gold-propose", "gold-review", "gold-decide"}


def add_commands(commands) -> None:
    for name in sorted(ADMIN_COMMANDS):
        command = commands.add_parser(name, help="Trusted local workspace administration")
        command.add_argument("--workspace-root", type=Path, required=True)
        if name != "workspace-init":
            command.add_argument("--user", required=True)
        if name == "user-create":
            command.add_argument("--credential-file", type=Path, required=True)
        if name == "project-create":
            command.add_argument("--project", required=True)
    for name in ("gate", "decide", *sorted(GOLD_COMMANDS)):
        command = commands.add_parser(name, help="Inspect or explicitly authorize governed state")
        if name != "gold-list":
            command.add_argument("--run-dir", type=Path, required=True)
        if name in {"gold-review", "gold-decide"}:
            command.add_argument("--proposal-id", type=UUID, required=True)
        if name in {"decide", "gold-decide"}:
            command.add_argument("--gate-id", required=True)
            command.add_argument("--decision", choices=("approve", "reject"), required=True)
        if name == "decide":
            command.add_argument("--domain-reference-id")
        if name == "gold-list":
            command.add_argument("--offset", type=int, default=0)
            command.add_argument("--limit", type=int, default=20)
    for name, command in commands.choices.items():
        if name not in ADMIN_COMMANDS:
            command.add_argument("--workspace-root", type=Path)
            command.add_argument("--user")
            command.add_argument("--project")
            command.add_argument("--credential-file", type=Path)


def administer(args) -> dict:
    """Only the trusted local operator may expose these management commands."""
    if args.command == "workspace-init":
        with WorkspaceRegistry.initialize(args.workspace_root):
            return {"event": "workspace_initialized", "root": str(args.workspace_root.absolute())}
    with WorkspaceRegistry(args.workspace_root) as registry:
        if args.command == "user-create":
            registry.create_user(args.user, args.credential_file)
            return {"event": "user_created", "user_id": args.user,
                    "credential_file": str(args.credential_file.expanduser().absolute())}
        registry.create_project(args.user, args.project)
        return {"event": "project_created", "user_id": args.user, "project_id": args.project}


def scoped_settings(args, settings):
    root = args.workspace_root or settings.managed_root
    if root is None:
        if any((args.user, args.project, args.credential_file)) or args.command in GOLD_COMMANDS:
            raise ValueError("Managed identity requires a configured workspace registry")
        return settings
    if not args.user or not args.project:
        raise ValueError("Managed commands require user and project")
    credential = args.credential_file or (
        Path.home() / ".config" / "labbioagent" / "users" / f"{args.user}.token"
    )
    with WorkspaceRegistry(root) as registry:
        workspace = registry.resolve(args.user, args.project, credential)
    if args.command == "run":
        args.data = [workspace.check_input(path) for path in args.data]
        if args.output is not None:
            workspace.validate_run(args.output)
    elif hasattr(args, "run_dir"):
        workspace.validate_run(args.run_dir)
    # Runtime identity and roots come from the authenticated registry, not task
    # prose, a caller's project owner claim, or the legacy TOML identity.
    return settings.model_copy(update={
        "identity": WorkspaceContext(user_id=workspace.user_id,
            project_id=workspace.project_id, lab_id=workspace.lab_id),
        "input_roots": (workspace.data_root,), "result_root": workspace.result_root,
        "managed_root": Path(root).expanduser().absolute(), "gold_root": workspace.gold_root,
    })


def gold_catalog(args, settings) -> dict:
    from .local_gold import build_personal_gold_service, close_personal_gold
    from .skills import SkillSearchContext

    if settings.gold_root is None or not 0 <= args.offset or not 1 <= args.limit <= 100:
        raise ValueError("Invalid personal Gold catalog request")
    service = build_personal_gold_service(settings.gold_root, settings.principal.user_id)
    try:
        skills = service.search(SkillSearchContext(
            user_id=settings.principal.user_id, project_id=settings.workspace.project_id,
            lab_id=settings.principal.lab_id, include_lab=False,
        ), principal=settings.principal)
        latest = {}
        for skill in skills:
            if skill.skill_id not in latest or latest[skill.skill_id].version < skill.version:
                latest[skill.skill_id] = skill
        active = sorted(latest.values(), key=lambda item: str(item.skill_id))
        selected = active[args.offset:args.offset + args.limit]
        return {"user_id": settings.principal.user_id, "available_count": len(active),
                "offset": args.offset, "returned_count": len(selected),
                "truncated": args.offset + len(selected) < len(active),
                "items": [{"skill_id": str(item.skill_id), "version": item.version,
                           "name": item.name, "description": item.description,
                           "scope": item.scope.value} for item in selected]}
    finally:
        close_personal_gold(service)


def configured_curator(application):
    """Reuse the accepted Agent draft/audit/revision protocol without task answers."""
    from pantheon.agent import Agent
    from .runtime.pantheon import PantheonRuntimeFactory
    from .skills import (
        PantheonAuditedAdaptiveSkillCurator, SkillAdaptiveCuratorDraft, SkillCuratorAudit,
    )
    from .skills.curator import (
        SKILL_CURATOR_ADAPTIVE_INSTRUCTIONS, SKILL_CURATOR_AUDIT_INSTRUCTIONS,
        SKILL_CURATOR_REVISION_INSTRUCTIONS,
    )

    model = application.configuration.profile_catalog.models["runtime-default"]
    identifier = PantheonRuntimeFactory._configure_transport(model)
    agents = [Agent(name=name, model=identifier, instructions=instructions,
                    response_format=schema, use_memory=False,
                    model_params={"thinking": PantheonRuntimeFactory._thinking_model_param(model),
                                  "max_tokens": model.max_output_tokens})
              for name, instructions, schema in (
                  ("GoldDraft", SKILL_CURATOR_ADAPTIVE_INSTRUCTIONS, SkillAdaptiveCuratorDraft),
                  ("GoldAudit", SKILL_CURATOR_AUDIT_INSTRUCTIONS, SkillCuratorAudit),
                  ("GoldRevision", SKILL_CURATOR_REVISION_INSTRUCTIONS, SkillAdaptiveCuratorDraft),
              )]
    return PantheonAuditedAdaptiveSkillCurator(
        *agents, boundary_observer=application.configuration.boundary_observer,
    )
