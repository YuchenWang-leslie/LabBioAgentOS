"""The lab area: member registry, roles and lab images, held by one OS account.

On a cluster each member runs LabBio under their own OS account, so the
operating system enforces the boundary, not LabBio code (DECIDED-C15-B). The lab
area is writable only by its holding account (a lab service account or an
administrator's own account) and readable by members through a lab group.
Every approval, revocation and role change is an event file naming the acting
LAB_ADMIN; nothing is edited in place.
"""

from __future__ import annotations

import grp
import os
import re
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from .append_only import AppendOnlyError, add_file, event_name, next_event_time, read_events, read_file
from .execution.catalog import CatalogStore
from .governance.models import PrincipalRole

_IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_MANIFEST = "lab.json"

_LAB_FIX_HINTS = {
    "LAB_AREA_UNAVAILABLE": (
        "The configured [lab] root is missing or is not a lab area; an administrator creates it "
        "with lab-init under the holding account."
    ),
    "LAB_AREA_PERMISSIONS": (
        "The lab area must be a directory owned by its holding account, writable only by that "
        "account, readable by the lab group and closed to everyone else. The holding account "
        "must itself belong to the lab group to read members' requests."
    ),
    "LAB_MEMBER_NOT_APPROVED": "Ask a lab administrator to approve this user with member-approve.",
    "LAB_MEMBER_UID_MISMATCH": (
        "This user is approved for another OS account. Run LabBio under the account that was "
        "approved, or ask an administrator to approve this account."
    ),
    "LAB_ADMIN_REQUIRED": (
        "Lab administration runs under the account that holds the lab area, as a registered "
        "LAB_ADMIN."
    ),
    "LAB_USAGE_MISSING": (
        "This lab area predates image usage markers. The holding account adds the usage "
        "directory the next time it runs any labbio command; until then members cannot start."
    ),
    "LAB_MEMBER_ACTION_INVALID": (
        "The requested member change does not apply to the member's current state, or would "
        "leave the lab without an administrator."
    ),
}


class LabError(ValueError):
    """A lab area or membership problem, reported by a fixed code and setting names only."""

    def __init__(self, code: str, fields: tuple[str, ...] = ()):
        if code not in _LAB_FIX_HINTS:
            raise ValueError("Unknown lab code")
        super().__init__(code)
        self.code, self.fields = code, fields

    @property
    def hint(self) -> str:
        return _LAB_FIX_HINTS[self.code]


class MemberAction(StrEnum):
    APPROVE = "APPROVE"
    REVOKE = "REVOKE"
    GRANT_ADMIN = "GRANT_ADMIN"
    REVOKE_ADMIN = "REVOKE_ADMIN"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class LabManifest(_Model):
    lab_id: StrictStr = Field(pattern=_IDENTIFIER)
    created_by: StrictStr = Field(pattern=_IDENTIFIER)
    created_at: datetime


class MemberEvent(_Model):
    user_id: StrictStr = Field(pattern=_IDENTIFIER)
    uid: StrictInt = Field(ge=0)
    action: MemberAction
    actor: StrictStr = Field(pattern=_IDENTIFIER)
    reason: StrictStr = Field(min_length=1, max_length=500)
    at: datetime


_GIB = 1_073_741_824
_HOST = r"^[a-z0-9]+(?:[.-][a-z0-9]+)*(?::[0-9]{1,5})?$"


class LabPolicy(_Model):
    """Administrator limits for image admission (DECIDED-C15-H); the newest event applies."""

    max_image_bytes: StrictInt = Field(default=16 * _GIB, ge=1)
    member_quota_bytes: StrictInt = Field(default=64 * _GIB, ge=1)
    lab_quota_bytes: StrictInt = Field(default=512 * _GIB, ge=1)
    # Empty means any registry; otherwise only these hosts may be admitted from.
    allowed_registries: tuple[StrictStr, ...] = Field(default=(), max_length=64)
    actor: StrictStr | None = Field(default=None, pattern=_IDENTIFIER)
    reason: StrictStr | None = Field(default=None, max_length=500)
    at: datetime | None = None

    def registry_allowed(self, host: str) -> bool:
        return not self.allowed_registries or host in self.allowed_registries


@dataclass(frozen=True)
class LabMember:
    user_id: str
    uid: int
    roles: frozenset[PrincipalRole]


def _replay(events: list[MemberEvent]) -> dict[str, LabMember]:
    """Current members; an event that does not fit the state before it fails closed."""
    members: dict[str, LabMember] = {}
    for event in events:
        current = members.get(event.user_id)
        if event.action is MemberAction.APPROVE:
            if current is not None:
                raise LabError("LAB_MEMBER_ACTION_INVALID")
            members[event.user_id] = LabMember(event.user_id, event.uid, frozenset({PrincipalRole.MEMBER}))
            continue
        if current is None or current.uid != event.uid:
            raise LabError("LAB_MEMBER_ACTION_INVALID")
        admin = PrincipalRole.LAB_ADMIN in current.roles
        if event.action is MemberAction.REVOKE:
            del members[event.user_id]
        elif event.action is MemberAction.GRANT_ADMIN and not admin:
            members[event.user_id] = LabMember(current.user_id, current.uid,
                                               current.roles | {PrincipalRole.LAB_ADMIN})
        elif event.action is MemberAction.REVOKE_ADMIN and admin:
            members[event.user_id] = LabMember(current.user_id, current.uid,
                                               current.roles - {PrincipalRole.LAB_ADMIN})
        else:
            raise LabError("LAB_MEMBER_ACTION_INVALID")
        if not any(PrincipalRole.LAB_ADMIN in item.roles for item in members.values()):
            raise LabError("LAB_MEMBER_ACTION_INVALID")
    return members


_USAGE_NAME = re.compile(r"([a-z0-9][a-z0-9._-]{0,127})\+([A-Za-z0-9][A-Za-z0-9._-]{0,127})\+"
                         r"([0-9a-f]{32}|env-[0-9a-f]{64})")


def _usage_name(key: str, user_id: str, holder: str) -> str:
    name = f"{key}+{user_id}+{holder}"
    if _USAGE_NAME.fullmatch(name) is None:
        raise ValueError("A usage marker names an image key, a user and a run or environment")
    return name


@dataclass(frozen=True)
class UsageMarker:
    """One member's run (32 hex) or environment (env-...) uses one lab image."""

    key: str
    user_id: str
    holder: str
    path: Path
    verified: bool  # Written by the account registered for user_id; any marker still blocks deletion.
    mine: bool


class LabArea:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser().absolute()
        self.members_root = self.root / "members"
        self.policy_root = self.root / "policy"
        self.promotions_root = self.root / "promotions"
        self.sif_root = self.root / "sif"
        # The directories members may write (drop directories): promotion requests
        # (DECIDED-C15-B) and markers of the lab images their runs use (DECIDED-C15-H).
        self.requests_root = self.root / "requests"
        self.usage_root = self.root / "usage"
        self.catalog = CatalogStore(self.root / "catalog")

    @classmethod
    def initialize(cls, root: Path, *, lab_id: str, admin_user_id: str, group: str | None) -> "LabArea":
        """Create a lab area held by this account, with its first LAB_ADMIN."""
        area = cls(root)
        if area.root.exists():
            raise LabError("LAB_AREA_UNAVAILABLE", ("lab_root",))
        area.root.mkdir(parents=True, mode=0o700)
        if group is not None:
            try:
                os.chown(area.root, -1, grp.getgrnam(group).gr_gid)
            except (KeyError, PermissionError):
                area.root.rmdir()
                raise LabError("LAB_AREA_PERMISSIONS", ("group",)) from None
        # Group members may read and traverse, never write. setgid makes every new file
        # take the lab group, whichever primary group the administrator logs in with.
        mode = 0o2750 if group is not None else 0o700
        os.chmod(area.root, mode)
        for directory in area.shared_directories()[1:]:
            directory.mkdir(mode=0o700)
            if group is not None:
                os.chown(directory, -1, area.root.stat().st_gid)
            os.chmod(directory, mode)
        for drop in area.drop_directories():
            area._create_drop(drop, shared=group is not None)
        now = datetime.now(timezone.utc)
        add_file(area.root / _MANIFEST, "manifest",
                 LabManifest(lab_id=lab_id, created_by=admin_user_id, created_at=now).model_dump(mode="json"))
        uid = os.getuid()
        for action in (MemberAction.APPROVE, MemberAction.GRANT_ADMIN):
            area._append(MemberEvent(user_id=admin_user_id, uid=uid, action=action, actor=admin_user_id,
                                     reason="Lab area created by its holding account.",
                                     at=next_event_time(area.members_root)))
        return area

    def manifest(self) -> LabManifest:
        try:
            return read_file(self.root / _MANIFEST, "manifest", LabManifest)
        except AppendOnlyError:
            raise LabError("LAB_AREA_UNAVAILABLE", ("lab_root",)) from None

    def check_permissions(self) -> None:
        """The OS boundary itself: refuse to run on a lab area others could rewrite."""
        try:
            info = os.lstat(self.root)
        except OSError:
            raise LabError("LAB_AREA_UNAVAILABLE", ("lab_root",)) from None
        if not (self.usage_root.exists() or self.usage_root.is_symlink()):
            # An area created before usage markers: its holder adds the directory.
            if not stat.S_ISDIR(info.st_mode) or not self.held_by_me():
                raise LabError("LAB_USAGE_MISSING", ("usage",))
            self._create_drop(self.usage_root, shared=bool(info.st_mode & 0o050))
        for added in self._later_directories():
            if not (added.exists() or added.is_symlink()) and stat.S_ISDIR(info.st_mode) and self.held_by_me():
                # Older than realization events or kept locks; members read none until the holder adds it.
                added.mkdir(mode=0o700)
                if info.st_mode & 0o050:
                    os.chown(added, -1, info.st_gid)
                os.chmod(added, stat.S_IMODE(info.st_mode))
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o027:
            raise LabError("LAB_AREA_PERMISSIONS", ("lab_root",))
        if not self.held_by_me() and os.access(self.root, os.W_OK):
            raise LabError("LAB_AREA_PERMISSIONS", ("lab_root",))
        if info.st_mode & 0o050:
            # A shared area without setgid gives new files the writer's own group,
            # which members cannot read.
            for directory in self.shared_directories():
                if directory in self._later_directories() and not directory.exists():
                    continue  # Older area, read by a member before its holder added it.
                mode = os.lstat(directory).st_mode
                if not mode & stat.S_ISGID or mode & 0o027 or os.lstat(directory).st_gid != info.st_gid:
                    raise LabError("LAB_AREA_PERMISSIONS", ("lab_root",))
            for drop in self.drop_directories():
                dropped = os.lstat(drop)
                if (stat.S_IMODE(dropped.st_mode) != 0o3770 or dropped.st_uid != info.st_uid
                        or dropped.st_gid != info.st_gid):
                    raise LabError("LAB_AREA_PERMISSIONS", ("lab_root",))
            if self.held_by_me() and info.st_gid not in {os.getegid(), *os.getgroups()}:
                # Members' request files are readable only through the lab group.
                raise LabError("LAB_AREA_PERMISSIONS", ("lab_group",))
        elif any(stat.S_IMODE(os.lstat(drop).st_mode) & 0o077 for drop in self.drop_directories()):
            raise LabError("LAB_AREA_PERMISSIONS", ("lab_root",))

    def sif_store(self):
        """The lab's SIF files: written by the holding account, read by members."""
        from .execution.apptainer import SifStore

        return SifStore(self.sif_root, shared_owner=os.lstat(self.root).st_uid)

    def drop_directories(self) -> tuple[Path, ...]:
        return (self.requests_root, self.usage_root)

    def _create_drop(self, directory: Path, *, shared: bool) -> None:
        directory.mkdir(mode=0o700)
        if shared:
            os.chown(directory, -1, self.root.stat().st_gid)
            # Group-writable, but sticky: a member removes only their own files.
            os.chmod(directory, 0o3770)

    def mark_usage(self, key: str, user_id: str, holder: str) -> None:
        """Member side: this member's run or environment uses this lab image. Idempotent."""
        name = _usage_name(key, user_id, holder)
        try:
            descriptor = os.open(self.usage_root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o640)
        except FileExistsError:
            return
        os.close(descriptor)

    def usage_markers(self) -> list[UsageMarker]:
        """Every marker, with whether its file was written by the member it names."""
        uids = {member.user_id: member.uid for member in self.members().values()}
        markers = []
        for path in sorted(self.usage_root.iterdir()) if self.usage_root.is_dir() else ():
            match = _USAGE_NAME.fullmatch(path.name)
            if match is None:
                continue
            key, user_id, holder = match.groups()
            owner = path.lstat().st_uid
            markers.append(UsageMarker(key=key, user_id=user_id, holder=holder, path=path,
                                       verified=not path.is_symlink() and uids.get(user_id) == owner,
                                       mine=owner == os.getuid()))
        return markers

    def shared_directories(self) -> tuple[Path, ...]:
        return (self.root, self.members_root, self.policy_root, self.promotions_root, self.sif_root,
                self.catalog.root, self.catalog.records_root, self.catalog.events_root,
                *self._later_directories())

    def _later_directories(self) -> tuple[Path, ...]:
        """Catalog directories added after lab areas were first created; the holder adds them."""
        return (self.catalog.realizations_root, self.catalog.locks_root)

    def held_by_me(self) -> bool:
        return os.lstat(self.root).st_uid == os.getuid()

    def members(self) -> dict[str, LabMember]:
        try:
            return _replay(read_events(self.members_root, MemberEvent))
        except AppendOnlyError:
            raise LabError("LAB_AREA_UNAVAILABLE", ("lab_root",)) from None

    def require_member(self, user_id: str, uid: int) -> LabMember:
        member = self.members().get(user_id)
        if member is None:
            raise LabError("LAB_MEMBER_NOT_APPROVED", ("identity.user_id",))
        if member.uid != uid:
            raise LabError("LAB_MEMBER_UID_MISMATCH", ("identity.user_id",))
        return member

    def change(self, action: MemberAction, *, user_id: str, uid: int | None, reason: str) -> MemberEvent:
        """Apply one member change as the registered LAB_ADMIN of this holding account."""
        actor = self._acting_admin()
        members = self.members()
        if uid is None:
            if user_id not in members:
                raise LabError("LAB_MEMBER_ACTION_INVALID")
            uid = members[user_id].uid
        event = MemberEvent(user_id=user_id, uid=uid, action=action, actor=actor.user_id, reason=reason,
                            at=next_event_time(self.members_root))
        _replay([*read_events(self.members_root, MemberEvent), event])  # Refuse before writing.
        self._append(event)
        return event

    def policy(self) -> LabPolicy:
        try:
            events = read_events(self.policy_root, LabPolicy)
        except AppendOnlyError:
            raise LabError("LAB_AREA_UNAVAILABLE", ("lab_root",)) from None
        return events[-1] if events else LabPolicy()

    def set_policy(self, *, reason: str, **limits) -> LabPolicy:
        actor = self._acting_admin()
        for host in limits.get("allowed_registries", ()):
            if not re.fullmatch(_HOST, host):
                raise LabError("LAB_MEMBER_ACTION_INVALID", ("allowed_registries",))
        policy = LabPolicy(**{**self.policy().model_dump(exclude={"actor", "reason", "at"}), **limits},
                           actor=actor.user_id, reason=reason, at=next_event_time(self.policy_root))
        add_file(self.policy_root / event_name(policy.at), "event", policy.model_dump(mode="json"))
        return policy

    def require_admin(self) -> LabMember:
        """The registered LAB_ADMIN acting under this holding account."""
        return self._acting_admin()

    def _acting_admin(self) -> LabMember:
        if not self.held_by_me():
            raise LabError("LAB_ADMIN_REQUIRED")
        admins = [member for member in self.members().values()
                  if member.uid == os.getuid() and PrincipalRole.LAB_ADMIN in member.roles]
        if len(admins) != 1:
            raise LabError("LAB_ADMIN_REQUIRED")
        return admins[0]

    def _append(self, event: MemberEvent) -> None:
        add_file(self.members_root / event_name(event.at), "event", event.model_dump(mode="json"))

    def summary(self) -> dict:
        """Operator view: user ids, roles and whether each uid is this account; no paths."""
        return {"lab_id": self.manifest().lab_id, "members": [
            {"user_id": member.user_id, "uid": member.uid,
             "roles": sorted(role.value for role in member.roles)}
            for member in sorted(self.members().values(), key=lambda item: item.user_id)]}


def private_member_root(path: Path) -> Path:
    """A member's own area: created 0700, and refused if anyone else could read it."""
    root = Path(path).expanduser().absolute()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = os.lstat(root)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise LabError("LAB_AREA_PERMISSIONS", ("lab.member_root",))
    return root


def lab_error_payload(error: LabError) -> dict:
    return {"error": error.code, "detail": error.hint, "fields": list(error.fields)}


LAB_COMMANDS = {"lab-init", "lab-members", "lab-policy", "member-approve", "member-revoke", "admin-grant",
                "admin-revoke"}
_ACTIONS = {"member-approve": MemberAction.APPROVE, "member-revoke": MemberAction.REVOKE,
            "admin-grant": MemberAction.GRANT_ADMIN, "admin-revoke": MemberAction.REVOKE_ADMIN}


def add_lab_commands(commands) -> None:
    for name in sorted(LAB_COMMANDS):
        command = commands.add_parser(name, help="Lab area administration under its holding account")
        command.add_argument("--lab-root", type=Path, required=True)
        if name == "lab-init":
            command.add_argument("--lab-id", required=True)
            command.add_argument("--admin-user", required=True)
            command.add_argument("--group", help="OS group whose members may read the lab area")
        elif name == "lab-policy":
            command.add_argument("--max-image-gib", type=int)
            command.add_argument("--member-quota-gib", type=int)
            command.add_argument("--lab-quota-gib", type=int)
            command.add_argument("--allowed-registry", action="append",
                                 help="Repeat per host; give none to keep the current list")
            command.add_argument("--any-registry", action="store_true", help="Clear the allowlist")
            command.add_argument("--reason", required=True)
        elif name != "lab-members":
            command.add_argument("--user", required=True)
            command.add_argument("--reason", required=True)
        if name == "member-approve":
            command.add_argument("--uid", type=int, required=True,
                                 help="The member's own OS account uid on this system")


def administer_lab(args) -> dict:
    if args.command == "lab-init":
        area = LabArea.initialize(args.lab_root, lab_id=args.lab_id, admin_user_id=args.admin_user,
                                  group=args.group)
        return {"event": "lab_initialized", **area.summary()}
    area = LabArea(args.lab_root)
    area.check_permissions()
    if args.command == "lab-members":
        return {**area.summary(), "policy": area.policy().model_dump(mode="json")}
    if args.command == "lab-policy":
        limits = {name: value * _GIB for name, value in (
            ("max_image_bytes", args.max_image_gib), ("member_quota_bytes", args.member_quota_gib),
            ("lab_quota_bytes", args.lab_quota_gib)) if value is not None}
        if args.any_registry:
            limits["allowed_registries"] = ()
        elif args.allowed_registry:
            limits["allowed_registries"] = tuple(sorted(set(args.allowed_registry)))
        return {"event": "policy", **area.set_policy(reason=args.reason, **limits).model_dump(mode="json")}
    event = area.change(_ACTIONS[args.command], user_id=args.user,
                        uid=getattr(args, "uid", None), reason=args.reason)
    return {"event": event.action.value.lower(), "user_id": event.user_id, "uid": event.uid,
            "actor": event.actor}


__all__ = [
    "LAB_COMMANDS", "LabArea", "LabError", "LabManifest", "LabMember", "LabPolicy", "MemberAction",
    "MemberEvent",
    "add_lab_commands", "administer_lab", "lab_error_payload", "private_member_root",
]
