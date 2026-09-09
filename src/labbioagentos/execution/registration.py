"""Controlled output inspection and conservative ArtifactRef registration."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
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
from labbioagentos.model_safety import (
    UnsafeModelContentError,
    validate_model_visible_json,
)
from labbioagentos.trace import RunTraceRecorder, TraceEventType

from .errors import ExecutionOutputDeclarationError, OutputCollectionError
from .models import (
    ExecutionFailureClass,
    ExecutionIssue,
    ExecutionPlan,
    OutputArtifactSpec,
    OutputContractFailureCode,
    OutputDeclassificationMode,
    StructuredOutputContract,
)


class _OutputContractValidationError(ValueError):
    def __init__(self, code: OutputContractFailureCode, *, record_index: int | None = None):
        super().__init__(code.value)
        self.code = code
        self.record_index = record_index


def _reject_json_constant(value: str) -> None:
    del value
    raise _OutputContractValidationError(OutputContractFailureCode.INVALID_DOCUMENT)


@dataclass(frozen=True)
class ArtifactRegistrationDecision:
    """Trusted actual classification plus a safe store representation."""

    requested_exposure: ArtifactExposureClass
    actual_exposure: ArtifactExposureClass
    contract_valid: bool
    release_authorized: bool
    reason: str
    representation: ArtifactRepresentation
    failure_code: OutputContractFailureCode | None = None
    record_index: int | None = None
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

    def validate_output_declarations(
        self, specs: tuple[OutputArtifactSpec, ...], minimum_queryable_output_count: int
    ) -> None:
        """Reject impossible output intent; actual files still require full assessment."""

        if minimum_queryable_output_count == 0:
            return
        count = 0
        for spec in specs:
            if (
                spec.requested_exposure is not ArtifactExposureClass.DERIVED
                or spec.output_contract_id is None
            ):
                continue
            contract = self._contracts.get(spec.output_contract_id)
            if (
                contract is not None
                and contract.declassification_mode is OutputDeclassificationMode.BOUNDED_SCALARS
            ):
                count += 1
        if count < minimum_queryable_output_count:
            raise ExecutionOutputDeclarationError(minimum_queryable_output_count, count)

    def assess(
        self,
        spec: OutputArtifactSpec,
        path: Path,
    ) -> ArtifactRegistrationDecision:
        if spec.requested_exposure is not ArtifactExposureClass.DERIVED:
            return self._raw_decision(
                spec,
                "Only DERIVED exposure can be evaluated by an approved output contract.",
                failure_code=(
                    OutputContractFailureCode.REQUESTED_EXPOSURE_UNSUPPORTED
                    if spec.output_contract_id is not None
                    else None
                ),
            )
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
                failure_code=OutputContractFailureCode.CONTRACT_NOT_APPROVED,
            )
        try:
            document = self._load_and_validate(path, contract)
        except _OutputContractValidationError as exc:
            return self._raw_decision(
                spec,
                f"Structured output contract validation failed: {exc.code.value}.",
                contract_valid=False,
                failure_code=exc.code,
                record_index=exc.record_index,
                schema_id=contract.schema_id,
            )
        except OSError:
            return self._raw_decision(
                spec,
                "Structured output contract validation failed: INVALID_DOCUMENT.",
                contract_valid=False,
                failure_code=OutputContractFailureCode.INVALID_DOCUMENT,
                schema_id=contract.schema_id,
            )
        except UnicodeError:
            return self._raw_decision(
                spec,
                "Structured output contract validation failed: INVALID_DOCUMENT.",
                contract_valid=False,
                failure_code=OutputContractFailureCode.INVALID_DOCUMENT,
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
        try:
            self._validate_model_safe_records(records, contract)
        except UnsafeModelContentError:
            return self._raw_decision(
                spec,
                "Output shape is valid but model-visible safety validation failed.",
                contract_valid=True,
                failure_code=OutputContractFailureCode.MODEL_CONTENT_REJECTED,
                schema_id=contract.schema_id,
            )
        return ArtifactRegistrationDecision(
            requested_exposure=spec.requested_exposure,
            actual_exposure=ArtifactExposureClass.DERIVED,
            contract_valid=True,
            release_authorized=True,
            reason=(
                "Output matched the approved bounded-scalar shape and "
                "model-visible safety contract."
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
    def _validate_model_safe_records(
        records: tuple[dict[str, Any], ...],
        contract: StructuredOutputContract,
    ) -> None:
        max_fields = max(1, len(contract.allowed_fields))
        validate_model_visible_json(
            records,
            max_depth=2,
            max_mapping_items=max_fields,
            max_list_items=contract.max_records,
            max_string_length=contract.max_scalar_string_length,
            max_nodes=1 + contract.max_records * (1 + max_fields),
            max_serialized_bytes=contract.max_file_bytes,
            reject_absolute_paths=True,
        )

    @staticmethod
    def _load_and_validate(
        path: Path,
        contract: StructuredOutputContract,
    ) -> dict[str, Any]:
        if path.stat().st_size > contract.max_file_bytes:
            raise _OutputContractValidationError(
                OutputContractFailureCode.FILE_TOO_LARGE
            )
        try:
            document = json.loads(
                path.read_text(encoding="utf-8"),
                parse_constant=_reject_json_constant,
            )
        except json.JSONDecodeError as exc:
            raise _OutputContractValidationError(
                OutputContractFailureCode.INVALID_DOCUMENT
            ) from exc
        if not isinstance(document, dict) or set(document) != {"schema_id", "records"}:
            raise _OutputContractValidationError(
                OutputContractFailureCode.INVALID_DOCUMENT
            )
        if document["schema_id"] != contract.schema_id:
            raise _OutputContractValidationError(
                OutputContractFailureCode.INVALID_DOCUMENT
            )
        records = document["records"]
        if not isinstance(records, list):
            raise _OutputContractValidationError(
                OutputContractFailureCode.INVALID_DOCUMENT
            )
        if len(records) > contract.max_records:
            raise _OutputContractValidationError(
                OutputContractFailureCode.RECORD_LIMIT_EXCEEDED
            )
        for record_index, record in enumerate(records):
            if not isinstance(record, dict):
                raise _OutputContractValidationError(
                    OutputContractFailureCode.INVALID_DOCUMENT, record_index=record_index,
                )
            fields = set(record)
            if not contract.required_fields.issubset(fields):
                raise _OutputContractValidationError(
                    OutputContractFailureCode.INVALID_DOCUMENT, record_index=record_index,
                )
            if not fields.issubset(contract.allowed_fields):
                raise _OutputContractValidationError(
                    OutputContractFailureCode.UNDECLARED_RECORD_FIELDS, record_index=record_index,
                )
            for value in record.values():
                if isinstance(value, (dict, list)):
                    raise _OutputContractValidationError(
                        OutputContractFailureCode.INVALID_DOCUMENT, record_index=record_index,
                    )
                if not isinstance(value, (str, int, float, bool, type(None))):
                    raise _OutputContractValidationError(
                        OutputContractFailureCode.INVALID_DOCUMENT, record_index=record_index,
                    )
                if isinstance(value, str) and len(value) > contract.max_scalar_string_length:
                    raise _OutputContractValidationError(
                        OutputContractFailureCode.INVALID_DOCUMENT, record_index=record_index,
                    )
        return document

    @staticmethod
    def _raw_decision(
        spec: OutputArtifactSpec,
        reason: str,
        *,
        contract_valid: bool = False,
        failure_code: OutputContractFailureCode | None = None,
        record_index: int | None = None,
        schema_id: str | None = None,
    ) -> ArtifactRegistrationDecision:
        return ArtifactRegistrationDecision(
            requested_exposure=spec.requested_exposure,
            actual_exposure=ArtifactExposureClass.RAW,
            contract_valid=contract_valid,
            release_authorized=False,
            reason=reason,
            representation=ArtifactRepresentation(),
            failure_code=failure_code,
            record_index=record_index,
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
        collected: list[CollectedOutput] = []
        try:
            for item in self._collect_declared_outputs(plan, output_root):
                collected.append(item)
        except OutputCollectionError as exc:
            raise OutputCollectionError(
                str(exc), exc.error_class, detail_code=exc.detail_code,
                output_artifact_refs=tuple(item.ref for item in collected),
                issues=(
                    *(item.issue for item in collected if item.issue is not None),
                    ExecutionIssue(
                        error_class=exc.error_class, detail_code=exc.detail_code,
                        output_index=len(collected),
                        message="Declared output could not be safely collected or registered.",
                    ),
                ),
            ) from exc
        return tuple(collected)

    def _collect_declared_outputs(
        self, plan: ExecutionPlan, output_root: Path,
    ) -> Iterator[CollectedOutput]:
        controlled_root = output_root.resolve(strict=True)
        collected_bytes = 0
        for output_index, spec in enumerate(plan.requested_outputs):
            try:
                path = self._resolve_declared_output(controlled_root, spec)
                size = path.stat().st_size
                collected_bytes += size
                if size > self.max_output_file_bytes:
                    raise OutputCollectionError(
                        "Declared output exceeds the trusted per-file collection limit",
                        ExecutionFailureClass.OUTPUT_CONTRACT_FAILURE,
                        detail_code=OutputContractFailureCode.FILE_TOO_LARGE,
                    )
                if collected_bytes > self.max_collected_output_bytes:
                    raise OutputCollectionError(
                        "Declared outputs exceed the trusted total collection limit",
                        ExecutionFailureClass.OUTPUT_CONTRACT_FAILURE,
                        detail_code=OutputContractFailureCode.COLLECTION_LIMIT_EXCEEDED,
                    )
                sha256 = self._sha256(path)
            except OSError as exc:
                raise OutputCollectionError(
                    "Declared output inspection failed during filesystem IO.",
                    ExecutionFailureClass.OUTPUT_CONTRACT_FAILURE,
                    detail_code=OutputContractFailureCode.OUTPUT_IO_ERROR,
                ) from exc
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
            if decision.failure_code is not None:
                metadata["output_contract_failure_code"] = decision.failure_code.value
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
            except OSError as exc:
                raise OutputCollectionError(
                    "Declared output registration failed during filesystem IO.",
                    ExecutionFailureClass.ARTIFACT_REGISTRATION_FAILURE,
                    detail_code=OutputContractFailureCode.OUTPUT_IO_ERROR,
                ) from exc
            issue = None
            if spec.output_contract_id is not None and (
                not decision.contract_valid or decision.failure_code is not None
            ):
                issue = ExecutionIssue(
                    error_class=ExecutionFailureClass.OUTPUT_CONTRACT_FAILURE,
                    detail_code=decision.failure_code,
                    output_path=spec.relative_path,
                    output_index=output_index,
                    record_index=decision.record_index,
                    message=decision.reason,
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
                    "output_contract_failure_code": (
                        decision.failure_code.value
                        if decision.failure_code is not None
                        else None
                    ),
                },
            )
            yield CollectedOutput(ref=ref, decision=decision, issue=issue)

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
                    detail_code=OutputContractFailureCode.OUTPUT_PATH_REJECTED,
                )
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as exc:
            raise OutputCollectionError(
                f"Declared output does not exist: {spec.relative_path}",
                ExecutionFailureClass.OUTPUT_CONTRACT_FAILURE,
                detail_code=OutputContractFailureCode.OUTPUT_NOT_FOUND,
            ) from exc
        except OSError as exc:
            raise OutputCollectionError(
                "Declared output could not be resolved safely",
                ExecutionFailureClass.OUTPUT_CONTRACT_FAILURE,
            ) from exc
        if not resolved.is_relative_to(output_root) or not resolved.is_file():
            raise OutputCollectionError(
                f"Output escaped the controlled root: {spec.relative_path}",
                ExecutionFailureClass.OUTPUT_CONTRACT_FAILURE,
                detail_code=OutputContractFailureCode.OUTPUT_PATH_REJECTED,
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
