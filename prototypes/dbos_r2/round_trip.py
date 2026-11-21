"""R2 feasibility probe only: one fake provider turn and one fake tool.

No production entry point imports this module. All payloads are synthetic. DBOS
stores Native references only; the caller verifies them even on cached replay.
The existing process lease deliberately refuses takeover from a live process.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from uuid import UUID, uuid5

from dbos import DBOS, SetWorkflowID

from labbioagentos.artifacts import ArtifactExposureClass, ArtifactReleaseBasis, ArtifactRepresentation
from labbioagentos.artifacts.store import ArtifactNotFoundError, LocalArtifactStore
from labbioagentos.contracts import InformationAuthority, WorkflowStage
from labbioagentos.execution.job_ledger import JobLedger
from labbioagentos.governance.models import Principal, WorkspaceContext
from labbioagentos.kernel.contracts import FinishReason, ModelTurn, ToolCall
from labbioagentos.local_continuation import run_writer
from labbioagentos.runtime.contracts import CapabilityEvidenceItem, CapabilityEvidenceStatus
from labbioagentos.runtime.observations import ObservationStore, canonical_bytes
from labbioagentos.runtime.tooling import RuntimeCapabilityContext, ToolResult
from runtime_binding import probe_settings, runtime_identity


RUN_ID = UUID("608c5795-59ed-45d4-beb6-eaf417ab9340")


def identity(name: str) -> UUID:
    return uuid5(RUN_ID, name)


def binding() -> RuntimeCapabilityContext:
    return RuntimeCapabilityContext(
        principal=Principal(user_id="r2-user", lab_id="r2-lab"),
        workspace=WorkspaceContext(user_id="r2-user", project_id="r2", lab_id="r2-lab"),
        run_id=RUN_ID, stage_id=WorkflowStage.EXECUTE,
        invocation_id=identity("stage"), actor_profile_key="r2",
        actor_agent_name="R2Probe", capability_allowlist=("execution_submit",),
    )


def artifact_store(root: str) -> LocalArtifactStore:
    return LocalArtifactStore(Path(root) / "native-artifacts")


def native_record(root: str, name: str, payload: dict | None = None):
    """Use the existing append-only Artifact API; this is not a new journal."""
    store = artifact_store(root)
    try:
        record = store.load_for_view(identity(name))
    except ArtifactNotFoundError:
        if payload is None:
            return None
        store.register(
            artifact_id=identity(name), artifact_type="r2-internal-receipt",
            exposure_class=ArtifactExposureClass.STRUCTURAL,
            representation=ArtifactRepresentation(stored_content=json.dumps(payload, sort_keys=True)),
            owner_user_id="r2-user", project_id="r2", lab_id="r2-lab", run_id=RUN_ID,
            metadata={"r2_payload_sha256": hashlib.sha256(canonical_bytes(payload)).hexdigest()},
        )
        record = store.load_for_view(identity(name))
    value = json.loads(record.representation.stored_content)
    ref = record.ref
    path = store.root / f"{identity(name)}.json"
    if ((ref.artifact_id, ref.owner_user_id, ref.project_id, ref.lab_id, ref.run_id,
         ref.artifact_type, ref.release_basis, Path(ref.storage_locator)) !=
            (identity(name), "r2-user", "r2", "r2-lab", RUN_ID, "r2-internal-receipt",
             ArtifactReleaseBasis.INTERNAL_ONLY, path)
            or ref.metadata.get("r2_payload_sha256") != hashlib.sha256(canonical_bytes(value)).hexdigest()):
        raise ValueError("Native receipt scope or integrity differs")
    if payload is not None and value != payload:
        raise ValueError("Native identity already bound to different input or version")
    # A readable envelope is not yet an acknowledged durable provider receipt.
    # Re-sync existing records too: a previous sync may have failed after writing.
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    for directory in path.parents:
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return value


def receipt_ref(root: str, name: str) -> dict:
    payload = native_record(root, name)
    if payload is None:
        raise ValueError("Native receipt missing; scheduler completion is not proof")
    return {"receipt_id": str(identity(name)), "name": name,
            "sha256": hashlib.sha256(canonical_bytes(payload)).hexdigest()}


def verify(root: str, ref: dict) -> dict:
    if receipt_ref(root, ref["name"]) != ref:
        raise ValueError("Native receipt integrity differs")
    return native_record(root, ref["name"])


def crash(point: str) -> None:
    if os.environ.get("R2_CRASH_POINT") == point:
        os._exit(75)  # Test fixture: abrupt controller loss, not graceful cancellation.


def remote_acceptance(root: str, name: str) -> None:
    """Fake external service fixture; recovery never reads this service counter."""
    path = Path(root) / f"fake-{name}-acceptances"
    with path.open("a", encoding="utf-8") as stream:
        stream.write("accepted\n")
        stream.flush()
        os.fsync(stream.fileno())


@DBOS.step(retries_allowed=False)
def provider_step(root: str, request: dict) -> dict:
    if native_record(root, "provider-response") is not None:
        return receipt_ref(root, "provider-response")
    if native_record(root, "provider-intent") is not None:
        raise RuntimeError("PROVIDER_OUTCOME_UNKNOWN: usage unknown; automatic replay refused")
    native_record(root, "provider-intent", {"request": request, "usage": None})
    remote_acceptance(root, "provider")
    turn = ModelTurn(
        tool_calls=(ToolCall(call_id="fake-call-1", name="execution_submit", arguments_json='{"value":7}'),),
        finish_reason=FinishReason.TOOL_CALLS,
    )
    crash("provider-return-before-native")
    native_record(root, "provider-response", turn.model_dump(mode="json"))
    crash("provider-native-before-dbos")
    return receipt_ref(root, "provider-response")


@DBOS.step(retries_allowed=False)
def tool_step(root: str, provider_ref: dict) -> dict:
    turn = ModelTurn.model_validate_json(canonical_bytes(verify(root, provider_ref)))
    call = turn.tool_calls[0]
    native_record(root, "tool-request", call.model_dump(mode="json"))
    context = binding()
    execution_id, observation_id = identity("execution"), identity("observation")
    ledger = JobLedger(Path(root) / "native-jobs.sqlite")
    try:
        previous = ledger.execution_submission(RUN_ID, execution_id)
        if previous is not None:
            observation = ObservationStore(Path(root) / "observations").load(context, observation_id)
            if canonical_bytes(observation.public_response).decode() != previous:
                raise ValueError("Native observation and execution receipt differ")
            return receipt_ref(root, "tool-response")
        if ledger.get(execution_id) is not None:
            raise RuntimeError("TOOL_OUTCOME_UNKNOWN: reconcile Native execution; replay refused")
        ledger.record_intent(run_id=RUN_ID, execution_id=execution_id,
                             launcher="r2-fake", workspace_root=Path(root))
        crash("tool-intent-before-submit")
        remote_acceptance(root, "tool")
        crash("tool-accepted-before-receipt")
        ledger.record_submitted(execution_id, "fake-job-1")
        ledger.record_collected(execution_id)
        item = CapabilityEvidenceItem(
            capability_invocation_id=observation_id, actor_profile_key="r2",
            actor_agent_name="R2Probe", capability_name="execution_submit",
            information_authority=InformationAuthority.AUTHORITATIVE_EVIDENCE,
            status=CapabilityEvidenceStatus.COMPLETED, safe_result=json.loads(call.arguments_json),
        )
        response = ToolResult(success=True, capability_invocation_id=observation_id,
                              information_authority=item.information_authority,
                              data=item.safe_result).model_dump(mode="json")
        ObservationStore(Path(root) / "observations").save(context, item, response)
        native_record(root, "tool-response", response)
        ledger.record_execution_submission(run_id=RUN_ID, execution_id=execution_id,
                                           payload=canonical_bytes(response).decode())
        crash("tool-native-before-dbos")
        return receipt_ref(root, "tool-response")
    finally:
        ledger.close()


@DBOS.workflow(name="labbio-r2-single-round")
def one_round(root: str, request: dict) -> dict:
    provider = provider_step(root, request)
    return {"provider": provider, "tool": tool_step(root, provider)}


def run_round(root: Path, version: str, request: str) -> None:
    """The caller owns the run lease for the complete worker lifetime."""
    frozen = {"version": version, "request": request,
              "runtime": runtime_identity(probe_settings(root))}
    native_record(str(root), "run-binding", frozen)
    DBOS(config={"name": "labbio-r2-probe",
                 "application_version": hashlib.sha256(canonical_bytes(frozen)).hexdigest(),
                 "system_database_url": f"sqlite:///{root / 'dbos.sqlite'}",
                 "executor_id": "r2-local", "enable_otlp": False, "log_level": "ERROR"})
    DBOS.launch()
    try:
        with SetWorkflowID(str(RUN_ID)):
            refs = one_round(str(root), {"text": request})
        for ref in refs.values():
            verify(str(root), ref)
        # Cached scheduler completion must still prove current Native evidence.
        observation = ObservationStore(root / "observations").load(binding(), identity("observation"))
        if observation.public_response != verify(str(root), refs["tool"]):
            raise ValueError("Native tool receipt differs from scoped observation")
        print(json.dumps(refs, sort_keys=True), flush=True)
    finally:
        DBOS.destroy(workflow_completion_timeout_sec=5)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--version", default="r2-prototype-1")
    parser.add_argument("--request", default="synthetic")
    args = parser.parse_args()
    root = args.root.absolute()
    root.mkdir(parents=True, exist_ok=True)
    with run_writer(root):
        run_round(root, args.version, args.request)


if __name__ == "__main__":
    main()
