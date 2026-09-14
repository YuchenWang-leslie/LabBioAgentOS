"""Local conversation lookup and explicit, non-replaying continuation commands."""

from contextlib import contextmanager
import fcntl
import hashlib
import os
from pathlib import Path
import stat
from uuid import uuid4

from .application import ApplicationRunHandle, ApplicationRunRequest
from .artifacts import ArtifactReleaseBasis
from .artifacts.store import StoredArtifact
from .local_conversations import ConversationStore, read_run_record
from .local_delivery import _digest, _no_symlinks, export_run
from .local_revision import import_revision_artifacts


class RunWriterActiveError(RuntimeError):
    """Another local command still owns this run; interruption is not proven."""


@contextmanager
def run_writer(directory: Path, *, create: bool = True):
    """Process-scoped, nonblocking lease. Never delete or replace the lock inode."""
    path = directory / ".writer.lock"
    _no_symlinks(path)
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
                             | (os.O_CREAT if create else 0), 0o600)
    except FileNotFoundError:
        if create:
            raise
        yield
        return
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
            raise PermissionError("Run lock must be a private unaliased regular file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RunWriterActiveError("Run has an active writer") from None
        yield
    finally:
        os.close(descriptor)


def writer_active(directory: Path) -> bool:
    try:
        with run_writer(directory, create=False):
            return False
    except RunWriterActiveError:
        return True


def _record_scope(record, settings) -> None:
    if (record.owner_user_id, record.project_id, record.lab_id) != (
        settings.principal.user_id, settings.workspace.project_id, settings.principal.lab_id,
    ):
        raise PermissionError("Run belongs to another workspace")


def resolve_run(settings, conversation_id, run_id):
    with ConversationStore(settings.result_root, settings.principal, settings.workspace) as catalog:
        link = catalog.get_run(conversation_id, run_id)
        directory = catalog.root / link.relative_directory
    record = read_run_record(directory)
    _record_scope(record, settings)
    if record.run_id != link.run_id:
        raise ValueError("Conversation directory identity changed")
    return link, directory, record


def result_refs(directory, record):
    """Registered producer identity, never a claim of workflow completion."""
    root = directory / "artifacts"
    _no_symlinks(root)
    if not root.is_dir():
        raise ValueError("Artifact store is missing")
    refs = []
    for path in sorted(root.glob("*.json")):
        _no_symlinks(path)
        if path.stat().st_nlink != 1:
            raise PermissionError("Artifact envelope must not be an alias")
        ref = StoredArtifact.model_validate_json(path.read_bytes()).ref
        if path.stem != str(ref.artifact_id):
            raise ValueError("Artifact identity differs from envelope")
        if (ref.owner_user_id, ref.project_id, ref.lab_id) != (
            record.owner_user_id, record.project_id, record.lab_id,
        ):
            raise PermissionError("Artifact belongs to another workspace")
        is_report = (ref.artifact_type == "report"
                     and ref.release_basis is ArtifactReleaseBasis.MODEL_AUTHORED_REPORT)
        is_output = "requested_exposure" in ref.metadata and "execution_id" in ref.metadata
        if ref.run_id == record.run_id and (is_report or is_output):
            refs.append(ref)
    return tuple(refs)


def history(args, settings):
    with ConversationStore(settings.result_root, settings.principal, settings.workspace) as catalog:
        page = catalog.list_runs(args.conversation, offset=args.offset, limit=args.limit)
    for item in page["items"]:
        directory = settings.result_root / item["relative_directory"]
        record = read_run_record(directory)
        _record_scope(record, settings)
        if str(record.run_id) != item["run_id"]:
            raise ValueError("Conversation directory identity changed")
        refs = result_refs(directory, record)
        item.update({
            "status": record.workflow_run.status.value,
            "recovery_state": record.recovery_state.value,
            "current_stage": record.workflow_run.current_stage,
            "task_excerpt": record.task_text[:1000],
            "task_truncated": len(record.task_text) > 1000,
            "record_version": record.record_version,
            "pending_clarification": (record.workflow_run.pending_clarification.model_dump(mode="json")
                                      if record.workflow_run.status.value == "WAITING_FOR_USER"
                                      and record.workflow_run.pending_clarification else None),
            "registered_result_count": len(refs),
            "registered_results": [{"artifact_id": str(ref.artifact_id),
                "artifact_type": ref.artifact_type, "release_basis": ref.release_basis.value}
                for ref in refs[:128]],
            "results_truncated": len(refs) > 128,
        })
    return page


def link_legacy(args, settings):
    from .cli import _run_directory
    directory = _run_directory(settings.result_root, args.run_dir, create=False)
    record = read_run_record(directory)
    with ConversationStore(settings.result_root, settings.principal, settings.workspace) as catalog:
        return catalog.add_run(args.conversation, record, directory).model_dump(mode="json")


def export_snapshot(application, run_id, directory, settings):
    record = application.run_state_store.get(run_id)
    if record.workflow_run.status.value == "WAITING_FOR_USER":
        return None
    # A previous delivery may describe an earlier stable checkpoint. Preserve it.
    destination = directory / "deliveries" / str(record.record_version)
    return export_run(application, ApplicationRunHandle(run_id=run_id), destination,
                      principal=settings.principal, workspace=settings.workspace)


async def reconcile_or_continue(args, settings):
    from . import cli
    from .local_config import _load_provider
    _, directory, _ = resolve_run(settings, args.conversation, args.run_id)
    if args.command == "reconcile" and writer_active(directory):
        cli._emit({"run_id": str(args.run_id), "continuation_action": "BLOCKED",
                   "issue_code": "RUN_WRITER_ACTIVE", "active_writer": True})
        return 2
    # Also hold the lease for a read-only reconciliation so the inspected phase
    # cannot change beneath the report. No provider or stage action is invoked.
    with run_writer(directory):
        application = cli.build_application(settings, directory, load_provider=False)
        try:
            assessment = application.reconcile_run(
                args.run_id, principal=settings.principal, workspace=settings.workspace,
            )
            cli._emit({"event": "reconciled", "conversation_id": args.conversation,
                       "active_writer": False, **assessment.model_dump(mode="json")})
            if args.command == "reconcile":
                return 0
            if assessment.continuation_action == "BLOCKED":
                return 2
            status = assessment.recovery.run_status.value
            if status == "WAITING_FOR_USER":
                cli._emit({"event": "waiting_for_user", "run_id": str(args.run_id),
                           "pending_clarification": (application.run_state_store.get(args.run_id)
                               .workflow_run.pending_clarification.model_dump(mode="json")
                               if assessment.continuation_action == "WAITING_FOR_ANSWER" else None),
                           "detail": "Answer the pending question or resolve the approval gate; no model was called."})
                return 2
            if status not in {"COMPLETED", "FAILED", "CANCELLED"}:
                _load_provider(settings.provider)
            result = await application.continue_run(
                args.run_id, principal=settings.principal, workspace=settings.workspace,
            )
            delivery = export_snapshot(application, args.run_id, directory, settings)
            cli._emit({"event": "continued", "conversation_id": args.conversation,
                       **result.model_dump(mode="json"), "delivery": delivery})
            return 0 if result.status.value == "COMPLETED" else 2
        finally:
            cli._close(application)


async def question_or_answer(args, settings):
    """Reuse conversation identity, writer lock and immutable continuation export."""
    from . import cli
    from .local_config import _load_provider

    _, directory, _ = resolve_run(settings, args.conversation, args.run_id)
    with run_writer(directory):
        application = cli.build_application(settings, directory, load_provider=False)
        try:
            if args.command == "question":
                record = application.run_state_store.get(args.run_id)
                pending = record.workflow_run.pending_clarification
                cli._emit({"run_id": str(args.run_id), "status": record.workflow_run.status.value,
                    "pending_clarification": (pending.model_dump(mode="json") if pending
                                               and record.workflow_run.status.value == "WAITING_FOR_USER" else None),
                    "clarifications": [item.model_dump(mode="json") for item in record.workflow_run.clarifications]})
                return 0
            recorded = application.submit_answer(args.run_id, question_id=args.question_id,
                answer_text=args.text, principal=settings.principal, workspace=settings.workspace)
            cli._emit({"event": "answer_saved" if recorded else "answer_already_saved",
                       "run_id": str(args.run_id), "question_id": args.question_id})
            if not recorded or args.save_only:
                return 0
            _load_provider(settings.provider)
            result = await application.continue_run(args.run_id, principal=settings.principal,
                                                   workspace=settings.workspace)
            delivery = export_snapshot(application, args.run_id, directory, settings)
            cli._emit({"event": "continued", "conversation_id": args.conversation,
                       **result.model_dump(mode="json"), "delivery": delivery})
            return 0 if result.status.value == "COMPLETED" else 2
        finally:
            cli._close(application)


async def revise(args, settings):
    from . import cli
    from .local_config import runtime_manifest
    _, source_directory, record = resolve_run(settings, args.conversation, args.from_run)
    if record.recovery_state.value != "STABLE" or record.workflow_run.status.value not in {
        "COMPLETED", "FAILED", "CANCELLED",
    }:
        raise ValueError("Reconcile a nonterminal source before requesting a revision")
    refs = result_refs(source_directory, record)
    selected = tuple(args.artifact_id) if args.artifact_id else tuple(ref.artifact_id for ref in refs)
    if not selected or len(set(selected)) != len(selected):
        raise ValueError("Revision requires distinct selected source results")
    if not set(selected).issubset({ref.artifact_id for ref in refs}):
        raise ValueError("Revision selection is not a registered result of the source run")
    artifact_ids = tuple(dict.fromkeys((*record.input_artifact_ids, *record.context_artifact_ids, *selected)))
    if len(artifact_ids) > 128:
        raise ValueError("Revision context exceeds its bound; narrow the selected artifacts")
    task = cli._task_text(args.task, args.preference)
    directory = cli._run_directory(settings.result_root,
        args.output or settings.result_root / str(uuid4()), create=True)
    with run_writer(source_directory), run_writer(directory):
        source = cli.build_application(settings, source_directory, load_provider=False)
        application = None
        try:
            manifest = runtime_manifest(settings)
            cli._write_json(directory / "RUNTIME.json", manifest)
            application = cli.build_application(settings, directory, load_provider=False)
            if application.configuration.runtime_revision != manifest["runtime_revision"]:
                raise ValueError("Runtime changed during revision composition")
            imported = import_revision_artifacts(source, application, args.from_run, artifact_ids,
                principal=settings.principal, workspace=settings.workspace)
            inputs, context, receipt = [], [], []
            for ref in imported:
                embedded = Path(ref.storage_locator) == application.artifact_store.root / f"{ref.artifact_id}.json"
                (context if embedded else inputs).append(ref.artifact_id)
                if ref.release_basis is ArtifactReleaseBasis.MODEL_AUTHORED_REPORT:
                    text = application.artifact_store.load_for_view(ref.artifact_id).representation.stored_content
                    content = text.encode("utf-8")
                    size, digest = len(content), hashlib.sha256(content).hexdigest()
                else:
                    size, digest = _digest(Path(ref.storage_locator))
                receipt.append({"artifact_id": str(ref.artifact_id),
                                "producer_run_id": str(ref.run_id) if ref.run_id else None,
                                "snapshot_size_bytes": size, "snapshot_sha256": digest,
                                "registered_sha256": ref.metadata.get("sha256")})
            handle = application.create_run(ApplicationRunRequest(task_text=task,
                principal=settings.principal, workspace=settings.workspace,
                input_artifact_ids=tuple(inputs), context_artifact_ids=tuple(context)))
            cli._write_json(directory / "RUN.json", {"run_id": str(handle.run_id)})
            cli._write_json(directory / "REQUEST.json", {"task": args.task,
                "preferences": args.preference, "conversation_id": args.conversation,
                "parent_run_id": str(args.from_run)})
            cli._write_json(directory / "REVISION.json", {"run_id": str(handle.run_id),
                "parent_run_id": str(args.from_run), "conversation_id": args.conversation,
                "selected_artifact_ids": [str(item) for item in selected], "imports": receipt})
            with ConversationStore(settings.result_root, settings.principal, settings.workspace) as catalog:
                link = catalog.add_run(args.conversation, application.run_state_store.get(handle.run_id),
                                       directory, parent_run_id=args.from_run)
            cli._emit({"event": "started", **link.model_dump(mode="json"), "run_directory": str(directory)})
            from .local_config import _load_provider
            _load_provider(settings.provider)
            result = await application.run(handle)
            delivery = export_snapshot(application, handle.run_id, directory, settings)
            cli._emit({"event": "finished", "conversation_id": args.conversation,
                       "parent_run_id": str(args.from_run), **result.model_dump(mode="json"),
                       "run_directory": str(directory), "delivery": delivery})
            return 0 if result.status.value == "COMPLETED" else 2
        finally:
            if application is not None:
                cli._close(application)
            cli._close(source)
