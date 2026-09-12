"""Local account/project adapter; the workflow and scientific runtime stay unchanged."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from .governance import WorkspaceContext
from .local_workspace import WorkspaceRegistry


ADMIN_COMMANDS = {"workspace-init", "user-create", "project-create"}
GOLD_COMMANDS = {"gold-list", "gold-export", "gold-propose", "gold-review", "gold-decide"}


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
        if name not in {"gold-list", "gold-export"}:
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
            command.add_argument("--tag", action="append", default=[],
                                 help="Exact tag; repeated filters must all match")
            command.add_argument("--artifact-type", action="append", default=[],
                                 help="Exact artifact type; repeated filters must all match")
            command.add_argument("--all-versions", action="store_true",
                                 help="Include approved history, not just each Skill's latest version")
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
        "environment": settings.environment.model_copy(update={
            "root": workspace.user_root / "Environments",
        }) if settings.environment is not None else None,
    })


def gold_catalog(args, settings) -> dict:
    from .local_gold import build_personal_gold_service, close_personal_gold
    from .local_gold_library import list_gold_library

    if settings.gold_root is None:
        raise ValueError("Gold catalog requires an authenticated managed workspace")
    service = build_personal_gold_service(settings.gold_root, settings.principal.user_id)
    try:
        return list_gold_library(service, settings.principal,
            project_id=settings.workspace.project_id, offset=args.offset, limit=args.limit,
            tags=args.tag, artifact_types=args.artifact_type, all_versions=args.all_versions)
    finally:
        close_personal_gold(service)


def gold_export(settings) -> dict:
    from .local_gold import build_personal_gold_service, close_personal_gold
    from .local_gold_library import export_gold_library

    if settings.gold_root is None:
        raise ValueError("Gold export requires an authenticated managed workspace")
    service = build_personal_gold_service(settings.gold_root, settings.principal.user_id)
    try:
        return export_gold_library(service, settings.principal)
    finally:
        close_personal_gold(service)


def configured_curator(application):
    """Compose Agent-authored advisory guidance and review, without task answers."""
    from pantheon.agent import Agent
    from .runtime.pantheon import PantheonRuntimeFactory
    from .skills import (
        PantheonAuditedAdaptiveSkillCurator, SkillCuratorAudit,
    )
    from .skills.models import SkillAdaptiveCuratorDraft
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
