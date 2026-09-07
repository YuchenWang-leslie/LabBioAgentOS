"""User-owned local storage over the existing approved Gold lifecycle."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

from .contracts import RunStatus
from .governance import AccessAction, AccessService, AuthorizationDenied, InMemoryProjectStore
from .skills import (
    GoldSkillService, SQLiteSkillStore, SkillProposalContext, SkillScope,
    SkillSourceProjector, SkillStoreError, SkillUserDecision,
)
from .skills.curator import SkillCuratorPort


class _PersonalSkillStore(SQLiteSkillStore):
    """Constrain this local composition, not the shared Gold contract."""

    def __init__(self, path: Path, user_id: str):
        self.user_id = user_id
        super().__init__(path)
        try:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS local_gold_owner "
                "(singleton INTEGER PRIMARY KEY CHECK(singleton = 1), user_id TEXT NOT NULL)"
            )
            self._connection.execute("BEGIN IMMEDIATE")
            row = self._connection.execute(
                "SELECT user_id FROM local_gold_owner WHERE singleton = 1"
            ).fetchone()
            if row is None:
                # Existing populated stores require an explicit migration, never
                # reassignment to the user named by a new local configuration.
                if any(super()._load()._snapshot().model_dump().values()):
                    raise AuthorizationDenied("Existing Gold store has no local owner binding")
                self._connection.execute(
                    "INSERT INTO local_gold_owner(singleton, user_id) VALUES (1, ?)",
                    (user_id,),
                )
            elif row[0] != user_id:
                raise AuthorizationDenied("Gold store belongs to another local user")
            self._load()
            self._connection.execute("COMMIT")
        except Exception:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            self.close()
            raise

    def _require_personal(self, value) -> None:
        if value.scope is not SkillScope.PERSONAL or value.owner_user_id != self.user_id:
            raise AuthorizationDenied("Local Gold must be PERSONAL and owned by the current user")

    def _load(self):
        store = super()._load()
        snapshot = store._snapshot()
        for item in (*snapshot.proposals, *snapshot.gold):
            self._require_personal(item)
        return store

    def save_proposal(self, proposal) -> None:
        self._require_personal(proposal)
        super().save_proposal(proposal)

    def search(self, context):
        if context.user_id != self.user_id:
            raise AuthorizationDenied("Local Gold search requires its exact owner")
        return super().search(context)


def build_personal_gold_service(gold_root: Path, user_id: str) -> GoldSkillService:
    """Open an owned database; files placed beside it are not imported as Gold."""
    if not isinstance(user_id, str) or not user_id.strip():
        raise ValueError("A local Gold owner is required")
    root = Path(gold_root).expanduser().absolute()
    if any(part.is_symlink() for part in (root, *root.parents)):
        raise ValueError("Gold root must not traverse symlinks")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    database = root / "skills.sqlite"
    for path in (database, root / "skills.sqlite-wal", root / "skills.sqlite-shm",
                 root / "skills.sqlite-journal"):
        if path.is_symlink() or (
            path.exists() and (not path.is_file() or path.stat().st_nlink != 1)
        ):
            raise ValueError("Gold persistence targets must be unaliased regular files")
    if not database.exists():
        descriptor = os.open(database, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
    return GoldSkillService(
        _PersonalSkillStore(database, user_id), SkillSourceProjector(),
        access_service=AccessService(InMemoryProjectStore()),
    )


def close_personal_gold(service: GoldSkillService) -> None:
    _personal_store(service).close()


def _personal_store(service: GoldSkillService) -> _PersonalSkillStore:
    if not isinstance(service.store, _PersonalSkillStore):
        raise SkillStoreError("A user-owned local Gold service is required")
    return service.store


def review_personal_gold(service: GoldSkillService, proposal_id: UUID, principal):
    """Read the exact candidate before an explicit approval or rejection."""
    store = _personal_store(service)
    if principal.user_id != store.user_id:
        raise AuthorizationDenied("Gold proposal belongs to another local user")
    proposal = store.get_proposal(proposal_id)
    if proposal.lab_id != principal.lab_id:
        raise AuthorizationDenied("Gold proposal belongs to another lab")
    return proposal


def decide_personal_gold(service, proposal_id, gate_id, approved, principal):
    """Apply the user's exact decision without manufacturing content or approval."""
    review_personal_gold(service, proposal_id, principal)
    return service.decide_proposal(
        proposal_id,
        SkillUserDecision(subject_id=proposal_id, gate_id=gate_id,
                          approved=approved, decided_by=principal.user_id),
        principal=principal,
    )


async def propose_from_run(application, handle, principal, workspace,
                           curator: SkillCuratorPort):
    """Ask an injected Agent curator to summarize safe successful-run evidence."""
    service = application.configuration.skill_service
    if service is None:
        raise SkillStoreError("This application has no local Gold service")
    store = _personal_store(service)
    result = application.result(handle)
    record = application.run_state_store.get(result.run_id)
    if (
        principal.user_id != store.user_id
        or (principal.user_id, principal.lab_id) != (workspace.user_id, workspace.lab_id)
        or (workspace.user_id, workspace.project_id, workspace.lab_id)
        != (record.owner_user_id, record.project_id, record.lab_id)
    ):
        raise AuthorizationDenied("Gold source must belong to the current user and project")
    application.access_service.require_project(
        principal, workspace.project_id, AccessAction.READ_PROJECT, run_id=result.run_id,
    )
    if result.status is not RunStatus.COMPLETED:
        raise SkillStoreError("Only a completed run can be proposed as local Gold")
    service.source_projector = SkillSourceProjector(application.artifact_store)
    bundle = service.create_source_bundle(
        application.trace_events(handle), run_id=result.run_id,
    )
    return await service.curate_proposal(
        bundle.bundle_id, curator,
        SkillProposalContext(scope=SkillScope.PERSONAL, owner_user_id=principal.user_id,
                             lab_id=principal.lab_id),
    )
