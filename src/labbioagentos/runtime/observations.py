"""Immutable, scoped public observations; never provider-private reasoning."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
from uuid import UUID

from pydantic import BaseModel, ConfigDict, JsonValue, model_validator

from labbioagentos.contracts import WorkflowStage
from .contracts import CapabilityEvidenceItem, HandoffObservation


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


class StoredObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    owner_user_id: str
    project_id: str
    lab_id: str
    run_id: UUID
    stage_id: WorkflowStage
    invocation_id: UUID
    item: CapabilityEvidenceItem
    public_response: dict[str, JsonValue]

    @model_validator(mode="after")
    def public_identity(self):
        HandoffObservation(
            observation_id=self.item.capability_invocation_id,
            capability_name=self.item.capability_name,
            information_authority=self.item.information_authority,
            public_response=self.public_response,
            artifact_read_receipt=self.item.artifact_read_receipt,
        )
        if self.public_response.get("success") != (self.item.status.value == "COMPLETED"):
            raise ValueError("Public observation outcome differs from audit")
        receipt = self.item.artifact_read_receipt
        if receipt is not None and (
            self.public_response.get("artifact_read_receipt") != receipt.model_dump(mode="json")
            or hashlib.sha256(canonical_bytes(self.public_response.get("data"))).hexdigest()
                != receipt.observation_sha256
        ):
            raise ValueError("Public response does not match its artifact read receipt")
        return self


class ObservationStore:
    """One run's append-only public evidence, separate from its model window."""

    def __init__(self, root: Path):
        self.root = Path(root).absolute()
        if any(path.is_symlink() for path in (self.root, *self.root.parents)):
            raise ValueError("Observation store cannot contain symlinks")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.root.is_dir() or self.root.stat().st_mode & 0o077:
            raise ValueError("Observation store must be a private directory")

    def save(self, binding, item: CapabilityEvidenceItem, public_response: dict) -> None:
        observation = StoredObservation(
            owner_user_id=binding.principal.user_id,
            project_id=binding.workspace.project_id, lab_id=binding.workspace.lab_id,
            run_id=binding.run_id, stage_id=binding.stage_id,
            invocation_id=binding.invocation_id, item=item, public_response=public_response,
        )
        payload = observation.model_dump(mode="json")
        encoded = canonical_bytes({
            "sha256": hashlib.sha256(canonical_bytes(payload)).hexdigest(),
            "observation": payload,
        })
        if len(encoded) > 1_048_576:
            raise ValueError("Observation exceeds its durable bound")
        path = self.root / f"{item.capability_invocation_id}.json"
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            if self.load(binding, item.capability_invocation_id) != observation:
                raise ValueError("Observation identity is immutable") from None
            return
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o400)
        directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def load(self, binding, observation_id: UUID) -> StoredObservation:
        identity = UUID(str(observation_id))
        path = self.root / f"{identity}.json"
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_size > 1_048_576 or info.st_mode & 0o077):
                raise ValueError("Invalid observation envelope")
            envelope = json.loads(stream.read())
        payload = envelope["observation"]
        if hashlib.sha256(canonical_bytes(payload)).hexdigest() != envelope["sha256"]:
            raise ValueError("Observation integrity differs")
        observation = StoredObservation.model_validate_json(canonical_bytes(payload))
        if observation.item.capability_invocation_id != identity:
            raise ValueError("Observation identity differs")
        if (observation.owner_user_id, observation.project_id, observation.lab_id,
            observation.run_id) != (
                binding.principal.user_id, binding.workspace.project_id,
                binding.workspace.lab_id, binding.run_id,
        ):
            raise PermissionError("Observation is outside the current run scope")
        return observation
