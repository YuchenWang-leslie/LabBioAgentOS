"""Controlled output inspection and conservative ArtifactRef registration."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from labbioagentos.artifacts import (
    ArtifactExposureClass,
    ArtifactReleaseBasis,
    ArtifactRef,
    ArtifactRepresentation,
    ArtifactSchema,
    ArtifactStore,
    ArtifactStoreError,
)
from labbioagentos.trace import RunTraceRecorder, TraceEventType

from .errors import OutputCollectionError
from .models import (
    ExecutionFailureClass,
    ExecutionIssue,
    ExecutionPlan,
    OutputArtifactSpec,
    OutputDeclassificationMode,
    StructuredOutputContract,
)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Non-finite JSON number {value!r} is not allowed")


@dataclass(frozen=True)
class ArtifactRegistrationDecision:
    """Trusted actual classification plus a safe store representation."""

    requested_exposure: ArtifactExposureClass
    actual_exposure: ArtifactExposureClass
    contract_valid: bool
    release_authorized: bool
    reason: str
    representation: ArtifactRepresentation
    artifact_schema: ArtifactSchema | None = None
    schema_id: str | None = None
    release_basis: ArtifactReleaseBasis = ArtifactReleaseBasis.INTERNAL_ONLY


@dataclass(frozen=True)
class CollectedOutput:
    """Registered reference and optional deterministic contract issue."""

    ref: ArtifactRef
    decision: ArtifactRegistrationDecision
    issue: ExecutionIssue | None = None


class ArtifactRegistrationPolicy:
    """Promote only approved, bounded flat JSON records to DERIVED."""

    def __init__(
        self,
        contracts: tuple[StructuredOutputContract, ...] = (),
    ):
        entries: dict[str, StructuredOutputContract] = {}
        for contract in contracts:
            if contract.contract_id in entries:
                raise ValueError(
                    f"Duplicate output contract ID: {contract.contract_id}"
                )
            entries[contract.contract_id] = contract
        self._contracts = entries

    def resolve_contract(self, contract_id: str) -> StructuredOutputContract:
        """Resolve trusted output shape without reading a future output file."""

        try:
            return self._contracts[contract_id]
        except KeyError as exc:
            raise ValueError(
                f"Output contract {contract_id!r} is not approved"
            ) from exc

    def assess(
        self,
        spec: OutputArtifactSpec,
        path: Path,
    ) -> ArtifactRegistrationDecision:
        raw = self._raw_decision(
            spec,
            "Unstructured execution outputs default to RAW.",
        )
        if spec.requested_exposure is not ArtifactExposureClass.DERIVED:
            return raw
        if spec.output_contract_id is None:
            return self._raw_decision(
                spec,
                "Requested DERIVED exposure has no approved output contract.",
            )
        try:
            contract = self.resolve_contract(spec.output_contract_id)
        except ValueError:
            return self._raw_decision(
                spec,
                "Requested output contract is not approved.",
            )
        try:
            document = self._load_and_validate(path, contract)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            return self._raw_decision(
                spec,
                f"Structured output contract validation failed: {type(exc).__name__}.",
                contract_valid=False,
                schema_id=contract.schema_id,
            )

        records = tuple(document["records"])
        if contract.declassification_mode is OutputDeclassificationMode.NONE:
            return self._raw_decision(
                spec,
                "Output shape is valid but the trusted contract does not authorize remote release.",
                contract_valid=True,
                schema_id=contract.schema_id,
            )
        if not self._strings_match_preexecution_declaration(
            records, spec, contract
        ):
            return self._raw_decision(
                spec,
                "Output shape is valid but runtime strings were not declared before execution.",
                contract_valid=True,
                schema_id=contract.schema_id,
            )
        return ArtifactRegistrationDecision(
            requested_exposure=spec.requested_exposure,
            actual_exposure=ArtifactExposureClass.DERIVED,
            contract_valid=True,
            release_authorized=True,
            reason=(
                "Output matched the approved shape and pre-execution scalar "
                "declassification contract."
            ),
            representation=ArtifactRepresentation(
                records=records,
                record_count=len(records),
            ),
            artifact_schema=ArtifactSchema(
                columns=tuple(sorted(contract.allowed_fields)),
            ),
            schema_id=contract.schema_id,
            release_basis=ArtifactReleaseBasis.TRUSTED_EXECUTION_DECLASSIFICATION,
        )

    @staticmethod
    def _strings_match_preexecution_declaration(
        records: tuple[dict[str, Any], ...],
        spec: OutputArtifactSpec,
        contract: StructuredOutputContract,
    ) -> bool:
        declared = spec.predeclared_string_values
        if not set(declared).issubset(contract.allowed_fields):
            return False
        for record in records:
            for field, value in record.items():
                if isinstance(value, str) and value not in declared.get(field, ()):
                    return False
        return True

    @staticmethod
    def _load_and_validate(
        path: Path,
        contract: StructuredOutputContract,
    ) -> dict[str, Any]:
        if path.stat().st_size > contract.max_file_bytes:
            raise ValueError("Structured output exceeds contract byte limit")
        document = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=_reject_json_constant,
        )
        if not isinstance(document, dict) or set(document) != {"schema_id", "records"}:
            raise ValueError(
                "Structured output must contain exactly schema_id and records"
            )
        if document["schema_id"] != contract.schema_id:
            raise ValueError("Structured output schema_id does not match contract")
        records = document["records"]
        if not isinstance(records, list) or len(records) > contract.max_records:
            raise ValueError("Structured output record count exceeds contract")
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("Structured output records must be objects")
            fields = set(record)
            if not contract.required_fields.issubset(fields):
                raise ValueError("Structured output record is missing required fields")
            if not fields.issubset(contract.allowed_fields):
                raise ValueError("Structured output record has undeclared fields")
            for value in record.values():
                if isinstance(value, (dict, list)):
                    raise ValueError("Nested structured output values are not allowed")
                if not isinstance(value, (str, int, float, bool, type(None))):
                    raise ValueError("Structured output values must be JSON scalars")
                if isinstance(value, str) and len(value) > contract.max_scalar_string_length:
                    raise ValueError("Structured output string exceeds contract limit")
        return document

    @staticmethod
    def _raw_decision(
        spec: OutputArtifactSpec,
        reason: str,
        *,
        contract_valid: bool = False,
        schema_id: str | None = None,
    ) -> ArtifactRegistrationDecision:
        return ArtifactRegistrationDecision(
            requested_exposure=spec.requested_exposure,
            actual_exposure=ArtifactExposureClass.RAW,
            contract_valid=contract_valid,
            release_authorized=False,
            reason=reason,
            representation=ArtifactRepresentation(),
            schema_id=schema_id,
        )


class OutputCollector:
    """Inspect declared output paths only and register them through policy."""

    def __init__(
        self,
        store: ArtifactStore,
        registration_policy: ArtifactRegistrationPolicy,
        *,
        trace_recorder: RunTraceRecorder | None = None,
        max_output_file_bytes: int = 16_777_216,
        max_collected_output_bytes: int = 67_108_864,
    ):
        self.store = store
        self.registration_policy = registration_policy
        self.trace_recorder = trace_recorder
        if max_output_file_bytes < 1 or max_collected_output_bytes < 1:
            raise ValueError("Output collection byte limits must be positive")
        if max_output_file_bytes > max_collected_output_bytes:
            raise ValueError("Per-file output limit cannot exceed the collection limit")
        self.max_output_file_bytes = max_output_file_bytes
        self.max_collected_output_bytes = max_collected_output_bytes

    def collect(
        self,
        plan: ExecutionPlan,
        output_root: Path,
    ) -> tuple[CollectedOutput, ...]:
        controlled_root = output_root.resolve(strict=True)
        collected: list[CollectedOutput] = []
        collected_bytes = 0
        for spec in plan.requested_outputs:
            path = self._resolve_declared_output(controlled_root, spec)
            size = path.stat().st_size
            collected_bytes += size
            if size > self.max_output_file_bytes:
                raise OutputCollectionError(
                    "Declared output exceeds the trusted per-file collection limit",
                    ExecutionFailureClass.OUTPUT_CONTRACT_FAILURE,
                )
            if collected_bytes > self.max_collected_output_bytes:
                raise OutputCollectionError(
                    "Declared outputs exceed the trusted total collection limit",
                    ExecutionFailureClass.OUTPUT_CONTRACT_FAILURE,
                )
            sha256 = self._sha256(path)
            self._emit(
                plan,
                TraceEventType.OUTPUT_COLLECTED,
                {
                    "relative_path": spec.relative_path,
                    "size_bytes": size,
                    "sha256": sha256,
                },
            )
            decision = self.registration_policy.assess(spec, path)
            metadata = {
                "execution_id": str(plan.execution_id),
                "requested_exposure": spec.requested_exposure.value,
                "actual_exposure": decision.actual_exposure.value,
                "contract_valid": decision.contract_valid,
                "release_authorized": decision.release_authorized,
                "size_bytes": size,
                "sha256": sha256,
            }
            if spec.output_contract_id is not None:
                metadata["output_contract_id"] = spec.output_contract_id
            if decision.schema_id is not None:
                metadata["schema_id"] = decision.schema_id
            try:
                ref = self.store.register_file(
                    path,
                    artifact_type=spec.artifact_type,
                    exposure_class=decision.actual_exposure,
                    release_basis=decision.release_basis,
                    representation=decision.representation,
                    owner_user_id=plan.owner_user_id,
                    project_id=plan.project_id,
                    lab_id=plan.lab_id,
                    run_id=plan.run_id,
                    stage_id=plan.stage_id,
                    producer_invocation_id=plan.invocation_id,
                    schema=decision.artifact_schema,
                    metadata=metadata,
                )
            except ArtifactStoreError as exc:
                raise OutputCollectionError(
                    f"Artifact registration failed for {spec.relative_path}: {exc}",
                    ExecutionFailureClass.ARTIFACT_REGISTRATION_FAILURE,
                ) from exc
            issue = None
            if spec.output_contract_id is not None and not decision.contract_valid:
                issue = ExecutionIssue(
                    error_class=ExecutionFailureClass.OUTPUT_CONTRACT_FAILURE,
                    output_path=spec.relative_path,
                    message=decision.reason,
                )
            collected.append(
                CollectedOutput(ref=ref, decision=decision, issue=issue)
            )
            self._emit(
                plan,
                TraceEventType.OUTPUT_REGISTERED,
                {
                    "artifact_id": str(ref.artifact_id),
                    "artifact_type": ref.artifact_type,
                    "requested_exposure": spec.requested_exposure.value,
                    "actual_exposure": ref.exposure_class.value,
                    "contract_valid": decision.contract_valid,
                    "release_authorized": decision.release_authorized,
                },
            )
        return tuple(collected)

    @staticmethod
    def _resolve_declared_output(
        output_root: Path,
        spec: OutputArtifactSpec,
    ) -> Path:
        candidate = output_root.joinpath(*Path(spec.relative_path).parts)
        cursor = output_root
        for part in Path(spec.relative_path).parts:
            cursor = cursor / part
            if cursor.is_symlink():
                raise OutputCollectionError(
                    f"Output path contains a symlink: {spec.relative_path}",
                    ExecutionFailureClass.OUTPUT_CONTRACT_FAILURE,
                )
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise OutputCollectionError(
                f"Declared output does not exist: {spec.relative_path}",
                ExecutionFailureClass.OUTPUT_CONTRACT_FAILURE,
            ) from exc
        if not resolved.is_relative_to(output_root) or not resolved.is_file():
            raise OutputCollectionError(
                f"Output escaped the controlled root: {spec.relative_path}",
                ExecutionFailureClass.OUTPUT_CONTRACT_FAILURE,
            )
        return resolved

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _emit(
        self,
        plan: ExecutionPlan,
        event_type: TraceEventType,
        payload: dict[str, Any],
    ) -> None:
        if self.trace_recorder is None:
            return
        self.trace_recorder.emit(
            plan.run_id,
            event_type,
            stage_id=plan.stage_id,
            invocation_id=plan.invocation_id,
            status="RECORDED",
            payload={"execution_id": str(plan.execution_id), **payload},
        )
