"""Thin local task entrypoint over the existing application boundary."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from uuid import UUID, uuid4

from .application import ApplicationRunRequest
from .local_config import build_application, load_settings, runtime_manifest
from .local_delivery import export_run


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="labbio", description="Run a local Agent task without a Python launcher."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Submit data and a natural-language task")
    run.add_argument("--data", type=Path, action="append", required=True)
    run.add_argument("--task", required=True)
    run.add_argument("--preference", action="append", default=[])
    run.add_argument("--format", choices=("raw", "h5ad"), default=None,
                     help="Explicit trusted inspector; default comes from configuration")
    run.add_argument("--output", type=Path, help="New directory below configured result_root")
    for name in ("status", "export"):
        command = commands.add_parser(name, help=f"Read persisted run {name} without a model call")
        command.add_argument("--run-dir", type=Path, required=True)
    for command in commands.choices.values():
        command.add_argument("--config", type=Path,
                             default=Path("~/.config/labbioagent/runtime.toml"))
    return parser


def _emit(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True), flush=True)


def _write_json(path: Path, value: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")


def _run_directory(root: Path, requested: Path, *, create: bool) -> Path:
    root = root.resolve()
    candidate = requested.expanduser().absolute()
    resolved = candidate.resolve()
    if resolved == root or not resolved.is_relative_to(root):
        raise ValueError("Run directory must be below configured result_root")
    # Do not follow an output alias, including a symlink to another allowed run.
    if any(path.is_symlink() for path in (candidate, *candidate.parents)):
        raise ValueError("Run directory must not traverse symlinks")
    if create:
        resolved.mkdir(parents=True, exist_ok=False, mode=0o700)
    elif not resolved.is_dir():
        raise ValueError("Run directory does not exist")
    return resolved


def _open_run_id(directory: Path) -> UUID:
    # Check local persistence targets before SQLite or artifact stores open them.
    for name in ("RUN.json", "state.sqlite", "state.sqlite-wal", "state.sqlite-shm",
                 "artifacts", "executions", "run-trace.jsonl", "model-boundaries.jsonl"):
        if (directory / name).is_symlink():
            raise ValueError("Run persistence targets must not be symlinks")
    for name in ("RUN.json", "state.sqlite"):
        if not (directory / name).is_file():
            raise ValueError("Run persistence is incomplete")
    manifest = directory / "RUN.json"
    if manifest.stat().st_size > 4096:
        raise ValueError("Run manifest exceeds its bound")
    record = json.loads(manifest.read_text(encoding="utf-8"))
    return UUID(record["run_id"])


def _task_text(task: str, preferences: list[str]) -> str:
    if not task.strip() or any(not item.strip() for item in preferences):
        raise ValueError("Task and preferences cannot be blank")
    # These values remain user assertions, never configuration or authority.
    text = task
    if preferences:
        text += "\n\nUser preferences (user assertions):\n" + json.dumps(
            preferences, ensure_ascii=False
        )
    if len(text) > 32_000:
        raise ValueError("Task and preferences exceed the application bound")
    return text


def _close(application) -> None:
    application.run_state_store.close()


async def _run(args: argparse.Namespace, settings) -> int:
    task_text = _task_text(args.task, args.preference)
    if len(args.data) > 128:
        raise ValueError("Too many input files")
    supplied = tuple(path.expanduser().absolute() for path in args.data)
    if any(source.is_symlink() for source in supplied):
        raise ValueError("Input cannot be a symlink")
    sources = tuple(path.resolve(strict=True) for path in supplied)
    if len(set(sources)) != len(sources):
        raise ValueError("Duplicate input files")
    for source in sources:
        if not source.is_file() or not any(
            source.is_relative_to(root.resolve(strict=True)) for root in settings.input_roots
        ):
            raise ValueError("Input must be a file within configured input_roots")
    directory = _run_directory(
        settings.result_root, args.output or settings.result_root / str(uuid4()), create=True
    )
    selected_format = args.format or settings.default_format
    _write_json(directory / "REQUEST.json", {
        "task": args.task, "preferences": args.preference,
        "data": [str(path) for path in sources], "format": selected_format,
    })
    manifest = runtime_manifest(settings)
    _write_json(directory / "RUNTIME.json", manifest)
    _emit({"event": "preparing", "run_directory": str(directory)})
    application = build_application(settings, directory)
    try:
        if application.configuration.runtime_revision != manifest["runtime_revision"]:
            raise ValueError("Runtime changed during composition; start with a stable configuration")
        inputs, context = [], []
        for source in sources:
            ref = application.register_input_file(
                source, principal=settings.principal, workspace=settings.workspace,
                artifact_type="local-file" if selected_format == "raw" else selected_format,
            )
            inputs.append(ref.artifact_id)
            if selected_format != "raw":
                inspected = application.inspect_bioformat(
                    ref.artifact_id, format_key=selected_format,
                    principal=settings.principal, workspace=settings.workspace,
                )
                context.extend(item.artifact_id for item in inspected.artifacts)
        handle = application.create_run(ApplicationRunRequest(
            task_text=task_text, principal=settings.principal, workspace=settings.workspace,
            input_artifact_ids=tuple(inputs), context_artifact_ids=tuple(context),
        ))
        _write_json(directory / "RUN.json", {"run_id": str(handle.run_id)})
        _emit({"event": "started", "run_id": str(handle.run_id),
               "run_directory": str(directory)})
        result = await application.run(handle)
        delivery = export_run(application, handle, directory / "delivery",
                              principal=settings.principal, workspace=settings.workspace)
        _emit({"event": "finished", **result.model_dump(mode="json"),
               "run_directory": str(directory), "delivery": delivery})
        return 0 if result.status.value == "COMPLETED" else 2
    finally:
        _close(application)


def _read(args: argparse.Namespace, settings) -> int:
    directory = _run_directory(settings.result_root, args.run_dir, create=False)
    run_id = _open_run_id(directory)
    application = build_application(settings, directory, load_provider=False)
    try:
        status = application.recovery_status(
            run_id, principal=settings.principal, workspace=settings.workspace
        )
        if args.command == "status":
            _emit(status.model_dump(mode="json"))
            return 0
        handle = application.recover_run(
            run_id, principal=settings.principal, workspace=settings.workspace
        )
        result = export_run(application, handle, directory / "delivery",
                            principal=settings.principal, workspace=settings.workspace)
        _emit(result)
        return 0
    finally:
        _close(application)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        settings = load_settings(args.config.expanduser())
        if args.command == "run":
            return asyncio.run(_run(args, settings))
        return _read(args, settings)
    except KeyboardInterrupt:
        _emit({"error": "LOCAL_COMMAND_INTERRUPTED",
               "detail": "Check persisted status; interruption is not confirmed cancellation."})
        return 130
    except Exception as exc:
        # Exception messages can contain provider bodies, credentials or raw data.
        _emit({"error": "LOCAL_COMMAND_FAILED", "error_type": type(exc).__name__})
        return 1


def entrypoint() -> int:
    # CLI owns its process. Library diagnostics can contain full provider bodies;
    # retain their occurrence, never their unstructured content or traceback.
    from pantheon.utils.log import logger

    def diagnostic(message) -> None:
        print(json.dumps({"event": "runtime_diagnostic",
                          "level": message.record["level"].name}), file=sys.stderr)

    logger.remove()
    logger.add(diagnostic, level="WARNING", backtrace=False, diagnose=False)
    return main()


if __name__ == "__main__":
    sys.exit(entrypoint())
