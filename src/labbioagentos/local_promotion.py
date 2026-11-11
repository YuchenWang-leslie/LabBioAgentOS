"""Promoting a personal image to a lab image through the drop directory (DECIDED-C15-B).

A member files a request, with the image file when its source is not an OCI
reference, in the lab area's `requests/` directory: the one place members may
write. An administrator under the holding account lists, approves or rejects
requests. Approval re-acquires the exact source, runs every admission check,
requires the same inventory, and records the decision in `promotions/`.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr

from .append_only import AppendOnlyError, add_file, event_name, next_event_time, read_file
from .execution.admission import AdmissionError
from .execution.catalog import ImageRecord, ImageScope, ImageState
from .local_lab import LabArea

MAX_PENDING_REQUESTS = 4
_REQUEST_NAME = re.compile(r"[0-9a-f]{32}\.json")
_IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"


class PromotionError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code

    @property
    def hint(self) -> str:
        return _HINTS.get(self.code, "The promotion request was not changed.")


_HINTS = {
    "IMAGE_NOT_PERSONAL": "Name one of your own ACTIVE personal images.",
    "PAYLOAD_REQUIRED": "Give --file: an image whose source is not an OCI reference must be delivered.",
    "PAYLOAD_NOT_ALLOWED": "An OCI image is pulled again by its digest; do not give --file.",
    "TOO_MANY_PENDING": "Withdraw or wait for a decision on one of your pending requests first.",
    "REQUEST_NOT_FOUND": "No pending request has this id.",
    "REQUEST_NOT_YOURS": "Only the member who filed a request can withdraw it.",
    "REQUEST_FORGED": "The request file does not belong to the member it names; it was not acted on.",
    "REQUEST_UNREADABLE": "This account cannot read the request; it must belong to the lab group.",
    "LAB_KEY_INVALID": "Lab image keys start with lab- and must not be in use.",
}


class PromotionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    request_id: StrictStr = Field(pattern=r"^[0-9a-f]{32}$")
    requester: StrictStr = Field(pattern=_IDENTIFIER)
    uid: StrictInt = Field(ge=0)
    record: ImageRecord
    payload: bool
    at: datetime


class PromotionOutcome(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class PromotionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    request_id: StrictStr
    requester: StrictStr
    personal_key: StrictStr
    lab_key: StrictStr | None = None
    outcome: PromotionOutcome
    actor: StrictStr
    reason: StrictStr = Field(min_length=1, max_length=500)
    at: datetime


def _request_paths(area: LabArea, request_id: str) -> tuple[Path, Path]:
    if re.fullmatch(r"[0-9a-f]{32}", request_id) is None:
        raise PromotionError("REQUEST_NOT_FOUND")
    return area.requests_root / f"{request_id}.json", area.requests_root / f"{request_id}.payload"


def file_request(area: LabArea, *, requester: str, record: ImageRecord, state: ImageState,
                 payload: Path | None, max_image_bytes: int) -> PromotionRequest:
    """Member side: write the payload, then the request that names it."""
    if record.scope is not ImageScope.PERSONAL or record.owner_user_id != requester or state is not ImageState.ACTIVE:
        raise PromotionError("IMAGE_NOT_PERSONAL")
    if record.repository is None and payload is None:
        raise PromotionError("PAYLOAD_REQUIRED")
    if record.repository is not None and payload is not None:
        raise PromotionError("PAYLOAD_NOT_ALLOWED")
    mine = [path for path in area.requests_root.glob("*.json") if path.stat().st_uid == os.getuid()]
    if len(mine) >= MAX_PENDING_REQUESTS:
        raise PromotionError("TOO_MANY_PENDING")
    request_id = uuid4().hex
    request_path, payload_path = _request_paths(area, request_id)
    if payload is not None:
        info = Path(payload).lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_image_bytes:
            raise AdmissionError("IMAGE_TOO_LARGE" if stat.S_ISREG(info.st_mode) else "SIF_INVALID")
        descriptor = os.open(payload_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        with open(payload, "rb") as source, os.fdopen(descriptor, "wb") as target:
            shutil.copyfileobj(source, target, 1_048_576)
            target.flush()
            os.fsync(target.fileno())
    request = PromotionRequest(request_id=request_id, requester=requester, uid=os.getuid(), record=record,
                               payload=payload is not None, at=datetime.now(timezone.utc))
    try:
        add_file(request_path, "request", request.model_dump(mode="json"))
    except BaseException:
        payload_path.unlink(missing_ok=True)
        raise
    return request


def withdraw(area: LabArea, request_id: str) -> None:
    request_path, payload_path = _request_paths(area, request_id)
    if not request_path.exists():
        raise PromotionError("REQUEST_NOT_FOUND")
    if request_path.lstat().st_uid != os.getuid():
        raise PromotionError("REQUEST_NOT_YOURS")
    payload_path.unlink(missing_ok=True)
    request_path.unlink()


def pending(area: LabArea) -> list[tuple[PromotionRequest | None, str, str | None]]:
    """(request, request id, problem) for each request file; a problem is never acted on."""
    members = area.members()
    found = []
    for path in sorted(area.requests_root.iterdir()):
        if path.name.endswith(".payload") or path.name.startswith("."):
            continue
        request_id = path.name.removesuffix(".json")
        if _REQUEST_NAME.fullmatch(path.name) is None or path.is_symlink():
            found.append((None, request_id, "REQUEST_FORGED"))
            continue
        if not os.access(path, os.R_OK):
            found.append((None, request_id, "REQUEST_UNREADABLE"))
            continue
        try:
            request = read_file(path, "request", PromotionRequest)
        except AppendOnlyError:
            found.append((None, request_id, "REQUEST_FORGED"))
            continue
        member = members.get(request.requester)
        payload = area.requests_root / f"{request_id}.payload"
        payload_owner = payload.lstat().st_uid if payload.exists() or payload.is_symlink() else None
        # A file's owner is who wrote it: request and payload must both come from the
        # requester's approved account, and a payload must exist exactly when one is named.
        forged = (member is None or member.uid != request.uid or path.lstat().st_uid != request.uid
                  or request.request_id != request_id or request.record.owner_user_id != request.requester
                  or request.payload != (payload_owner is not None)
                  or (payload_owner is not None and (payload_owner != request.uid or payload.is_symlink())))
        found.append((request, request_id, "REQUEST_FORGED" if forged else None))
    return found


def decide(area: LabArea, request_id: str, *, outcome: PromotionOutcome, reason: str,
           admit=None, lab_key: str | None = None) -> PromotionDecision:
    """Holder side. `admit(request, payload_path, lab_key)` runs the full admission for approval."""
    actor = area.require_admin()
    request_path, payload_path = _request_paths(area, request_id)
    match = next((item for item in pending(area) if item[1] == request_id), None)
    if match is None:
        raise PromotionError("REQUEST_NOT_FOUND")
    request, _, problem = match
    if outcome is PromotionOutcome.APPROVED:
        if problem is not None:
            raise PromotionError(problem)
        if lab_key is None or not lab_key.startswith("lab-") or lab_key in area.catalog.records():
            raise PromotionError("LAB_KEY_INVALID")
        admit(request, payload_path if request.payload else None, lab_key)
    decision = PromotionDecision(
        request_id=request_id, requester=request.requester if request else "unknown",
        personal_key=request.record.key if request else "unknown",
        lab_key=lab_key if outcome is PromotionOutcome.APPROVED else None, outcome=outcome,
        actor=actor.user_id, reason=reason, at=next_event_time(area.promotions_root))
    add_file(area.promotions_root / event_name(decision.at), "event", decision.model_dump(mode="json"))
    payload_path.unlink(missing_ok=True)  # The holder owns the directory, so it may remove any file.
    request_path.unlink(missing_ok=True)
    return decision


def request_summary(request: PromotionRequest | None, request_id: str, problem: str | None) -> dict:
    if request is None:
        return {"request_id": request_id, "problem": problem}
    return {"request_id": request_id, "requester": request.requester, "personal_key": request.record.key,
            "source": request.record.source.canonical, "size_bytes": request.record.size_bytes,
            "payload": request.payload, "purpose": request.record.purpose, "problem": problem,
            "at": request.at.isoformat()}

