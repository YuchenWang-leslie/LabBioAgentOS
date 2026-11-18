"""Host-derived provenance for an Agent-selected, local-only result set."""

from hashlib import sha256
import json
import os
from pathlib import Path
import stat
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from labbioagentos.artifacts import ArtifactNotFoundError, ArtifactOriginKind
from labbioagentos.governance import AccessAction
from labbioagentos.trace import TraceEventType


class DeliveryFile(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)
    artifact_id: UUID
    producer_run_id: UUID
    execution_id: UUID
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    size_bytes: int = Field(ge=0)


class DeliveryOutput(DeliveryFile):
    filename: str = Field(min_length=1, max_length=240, pattern=r'^[^/\\\x00-\x1f\x7f]+$')


class DeliveryManifest(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)
    contract: Literal['ARTIFACT_SET_V1'] = 'ARTIFACT_SET_V1'
    outputs: tuple[DeliveryOutput, ...] = Field(max_length=64)
    programs: tuple[DeliveryFile, ...] = Field(max_length=64)


class DeliverySelectionError(ValueError):
    def __init__(self, reason, index=None):
        super().__init__(reason)
        self.reason, self.index = reason, index


def _identity(store, ref):
    path = store.root / 'blobs' / str(ref.artifact_id) / 'content'
    envelope = store.root / f'{ref.artifact_id}.json'
    if (Path(ref.storage_locator) != path or any(p.is_symlink()
            for target in (path, envelope) for p in (target, *target.parents))):
        raise ValueError('Invalid owned blob')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('Not a regular file')
        digest = sha256()
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    if ((before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
            or digest.hexdigest() != ref.metadata.get('sha256')
            or ref.metadata.get('size_bytes', before.st_size) != before.st_size):
        raise ValueError('Changed source')
    return {'artifact_id': str(ref.artifact_id), 'producer_run_id': str(ref.run_id),
        'execution_id': ref.metadata['execution_id'], 'sha256': digest.hexdigest(),
        'size_bytes': before.st_size}


def build_delivery_manifest(store, access_service, artifact_ids, *, principal,
        workspace, run_id, imported_artifact_ids=(), trace_recorder=None):
    """No source contents, read credit, release promotion, or scientific selection.

    Current programs require an exact program-binding event, not submission intent. Imported originals
    must be explicit immutable inputs; they do not acquire a new execution receipt.
    """
    if (len(artifact_ids) > 64 or len(set(artifact_ids)) != len(artifact_ids)
            or any(not isinstance(item, UUID) for item in artifact_ids)):
        raise DeliverySelectionError('INVALID_IDENTITIES')
    scope = (principal.user_id, workspace.project_id, principal.lab_id)
    if (workspace.user_id, workspace.project_id, workspace.lab_id) != scope:
        raise DeliverySelectionError('OUTSIDE_SCOPE')
    access_service.require_project(principal, workspace.project_id, AccessAction.READ_PROJECT, run_id=run_id)
    refs = store.list_refs()
    events = trace_recorder.events(run_id) if trace_recorder else ()
    outputs, programs = [], {}
    for index, artifact_id in enumerate(artifact_ids):
        try:
            ref = store.get_ref(artifact_id)
            if ((ref.owner_user_id, ref.project_id, ref.lab_id) != scope
                    or (ref.run_id != run_id and artifact_id not in imported_artifact_ids)):
                raise DeliverySelectionError('OUTSIDE_SCOPE', index)
            access_service.require_artifact(principal, ref, AccessAction.READ_ARTIFACT)
            if ref.origin_kind is not ArtifactOriginKind.REGISTERED_EXECUTION_OUTPUT:
                raise DeliverySelectionError('NOT_REGISTERED_OUTPUT', index)
            UUID(ref.metadata['execution_id'])
            output = _identity(store, ref)
            if 'size_bytes' not in ref.metadata or not ref.original_filename:
                raise DeliverySelectionError('INCOMPLETE_PROVENANCE', index)
            output['filename'] = ref.original_filename
            candidates = [item for item in refs
                if item.artifact_type == 'execution-script'
                and 'requested_exposure' not in item.metadata
                and item.metadata.get('execution_id') == ref.metadata['execution_id']
                and (item.run_id, item.stage_id, item.producer_invocation_id) ==
                    (ref.run_id, ref.stage_id, ref.producer_invocation_id)
                and (item.owner_user_id, item.project_id, item.lab_id) == scope
                and (item.run_id == run_id or item.artifact_id in imported_artifact_ids)]
            if len(candidates) != 1:
                raise DeliverySelectionError('ORIGINAL_PROGRAM_UNAVAILABLE', index)
            program = candidates[0]
            access_service.require_artifact(principal, program, AccessAction.READ_ARTIFACT)
            identity = _identity(store, program)
            if ref.run_id == run_id:
                bindings = {(event.payload.get('script_artifact_id'), event.payload.get('script_hash'),
                    event.stage_id, event.invocation_id) for event in events
                    if event.event_type is TraceEventType.EXECUTION_PLANNED
                    and 'script_artifact_id' in event.payload
                    and event.payload.get('execution_id') == ref.metadata['execution_id']}
                if bindings != {(str(program.artifact_id), identity['sha256'],
                        program.stage_id, program.producer_invocation_id)}:
                    raise DeliverySelectionError('ORIGINAL_PROGRAM_BINDING_MISMATCH', index)
            programs[str(program.artifact_id)] = identity
            outputs.append(output)
        except ArtifactNotFoundError:
            raise DeliverySelectionError('ARTIFACT_NOT_FOUND', index) from None
        except (OSError, KeyError, ValueError) as exc:
            if isinstance(exc, DeliverySelectionError):
                raise
            raise DeliverySelectionError('SOURCE_INTEGRITY_FAILED', index) from None
    return DeliveryManifest.model_validate_json(json.dumps({
        'contract': 'ARTIFACT_SET_V1', 'outputs': outputs,
        'programs': list(programs.values())})).model_dump(mode='json')
