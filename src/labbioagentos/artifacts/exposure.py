"""Deterministic artifact exposure policy and controlled view generation."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import ValidationError

from labbioagentos.governance import (
    AccessAction,
    AccessService,
    AuthorizationDenied,
    Principal,
)
from labbioagentos.model_safety import validate_model_visible_json
from labbioagentos.trace import RunTraceRecorder, TraceEventType

from .approvals import ArtifactApprovalStore, InMemoryArtifactApprovalStore
from .models import (
    ArtifactConsumer,
    ArtifactExposureClass,
    ArtifactProvenance,
    ArtifactQuery,
    ArtifactReleaseBasis,
    ArtifactSchema,
    ArtifactView,
    ArtifactViewType,
    ExposureDecision,
    validate_artifact_query,
)
from .store import ArtifactStore, coerce_artifact_id


class ArtifactQueryError(ValueError):
    """A query was malformed or used an unsupported view shape."""


class ArtifactExposureDenied(PermissionError):
    """ExposurePolicy denied the requested consumer/view combination."""


class ExposurePolicy:
    """Structural privacy policy with no scientific ranking or interpretation."""

    _REMOTE_VIEWS = {
        ArtifactExposureClass.STRUCTURAL: frozenset(
            {ArtifactViewType.METADATA, ArtifactViewType.SCHEMA}
        ),
        ArtifactExposureClass.AGGREGATE: frozenset(
            {
                ArtifactViewType.METADATA,
                ArtifactViewType.SCHEMA,
                ArtifactViewType.SUMMARY,
            }
        ),
        ArtifactExposureClass.DERIVED: frozenset(ArtifactViewType),
        ArtifactExposureClass.USER_APPROVED: frozenset(ArtifactViewType),
    }
    _REMOTE_RELEASE_BASES = {
        ArtifactExposureClass.STRUCTURAL: frozenset(
            {ArtifactReleaseBasis.TRUSTED_STRUCTURAL_INSPECTOR}
        ),
        ArtifactExposureClass.AGGREGATE: frozenset(
            {ArtifactReleaseBasis.TRUSTED_AGGREGATE_INSPECTOR}
        ),
        ArtifactExposureClass.DERIVED: frozenset(
            {
                ArtifactReleaseBasis.TRUSTED_EXECUTION_DECLASSIFICATION,
                ArtifactReleaseBasis.MODEL_AUTHORED_REPORT,
            }
        ),
        ArtifactExposureClass.USER_APPROVED: frozenset(
            {ArtifactReleaseBasis.USER_APPROVED_RELEASE}
        ),
    }

    def __init__(
        self,
        *,
        approval_store: ArtifactApprovalStore | None = None,
        user_approved_enabled: bool = False,
        max_top_n: int = 100,
        default_top_n: int = 10,
    ):
        if max_top_n < 1:
            raise ValueError("max_top_n must be positive")
        if default_top_n < 1 or default_top_n > max_top_n:
            raise ValueError("default_top_n must be between 1 and max_top_n")
        self.approval_store = approval_store or InMemoryArtifactApprovalStore()
        self.user_approved_enabled = user_approved_enabled
        self.max_top_n = max_top_n
        self.default_top_n = default_top_n

    def decide(
        self,
        ref,
        query: ArtifactQuery,
        consumer: ArtifactConsumer,
    ) -> ExposureDecision:
        if (
            ref.exposure_class is ArtifactExposureClass.RAW
            and consumer is ArtifactConsumer.REMOTE_LLM
        ):
            return self._deny(
                ref,
                query,
                consumer,
                "RAW artifacts cannot be exposed to a remote LLM.",
            )

        if ref.exposure_class is ArtifactExposureClass.USER_APPROVED:
            if not self.user_approved_enabled:
                return self._deny(
                    ref,
                    query,
                    consumer,
                    "USER_APPROVED model exposure is disabled in this configuration.",
                )
            approval = self.approval_store.get(ref.artifact_id, consumer)
            if approval is None:
                return self._deny(
                    ref,
                    query,
                    consumer,
                    "USER_APPROVED classification requires an explicit approval for this consumer.",
                )

        if consumer is ArtifactConsumer.REMOTE_LLM:
            release_bases = self._REMOTE_RELEASE_BASES.get(
                ref.exposure_class, frozenset()
            )
            if ref.release_basis not in release_bases:
                return self._deny(
                    ref,
                    query,
                    consumer,
                    "Artifact classification has no compatible trusted remote-release basis.",
                )
            allowed_views = self._REMOTE_VIEWS.get(ref.exposure_class, frozenset())
        elif ref.exposure_class is ArtifactExposureClass.RAW:
            allowed_views = frozenset(
                {ArtifactViewType.METADATA, ArtifactViewType.SCHEMA}
            )
        else:
            allowed_views = frozenset(ArtifactViewType)

        if query.view_type not in allowed_views:
            return self._deny(
                ref,
                query,
                consumer,
                f"{query.view_type.value} is not allowed for "
                f"{ref.exposure_class.value} artifacts and "
                f"{consumer.value} consumers.",
            )

        effective_limit = None
        if query.view_type is ArtifactViewType.TOP_N:
            effective_limit = min(query.limit or self.default_top_n, self.max_top_n)
        return ExposureDecision(
            artifact_id=ref.artifact_id,
            consumer=consumer,
            view_type=query.view_type,
            allowed=True,
            reason="Exposure class and view type are allowed.",
            effective_limit=effective_limit,
        )

    @staticmethod
    def _deny(ref, query, consumer, reason: str) -> ExposureDecision:
        return ExposureDecision(
            artifact_id=ref.artifact_id,
            consumer=consumer,
            view_type=query.view_type,
            allowed=False,
            reason=reason,
        )


class ArtifactModelViewProjector:
    """Build explicit bounded model views from store-private Artifact state."""

    _MODEL_METADATA_KEYS = frozenset(
        {
            "actual_exposure",
            "contract_valid",
            "execution_id",
            "format",
            "inspection_schema_version",
            "output_contract_id",
            "release_authorized",
            "requested_exposure",
            "schema_id",
            "sha256",
            "size_bytes",
        }
    )

    def build(self, ref, representation, query, decision) -> ArtifactView:
        metadata = {}
        if query.view_type is ArtifactViewType.METADATA:
            metadata = {
                key: value
                for key, value in ref.metadata.items()
                if key in self._MODEL_METADATA_KEYS
            }
            validate_model_visible_json(metadata, reject_absolute_paths=True)

        schema = None
        if query.view_type is ArtifactViewType.SCHEMA and ref.artifact_schema is not None:
            properties = {}
            if ref.release_basis in {
                ArtifactReleaseBasis.TRUSTED_STRUCTURAL_INSPECTOR,
                ArtifactReleaseBasis.TRUSTED_AGGREGATE_INSPECTOR,
            }:
                properties = ref.artifact_schema.properties
                validate_model_visible_json(properties, reject_absolute_paths=True)
            keys = sorted(ref.artifact_schema.dtypes)[:128]
            schema = ArtifactSchema(
                shape=(
                    tuple(ref.artifact_schema.shape[:16])
                    if ref.artifact_schema.shape is not None
                    else None
                ),
                columns=tuple(ref.artifact_schema.columns[:128]),
                dtypes={key: ref.artifact_schema.dtypes[key][:128] for key in keys},
                properties=properties,
            )

        summary = {}
        if query.view_type is ArtifactViewType.SUMMARY:
            summary = representation.summary
            validate_model_visible_json(summary, reject_absolute_paths=True)

        records = ()
        truncated = False
        if query.view_type is ArtifactViewType.TOP_N:
            limit = decision.effective_limit
            if limit is None:
                raise ArtifactQueryError("TOP_N policy decision omitted an effective limit")
            records = representation.records[:limit]
            validate_model_visible_json(records, reject_absolute_paths=True)
            truncated = representation.record_count > len(records)

        return ArtifactView(
            artifact_id=ref.artifact_id,
            artifact_type=ref.artifact_type,
            view_type=query.view_type,
            exposure_class=ref.exposure_class,
            release_basis=ref.release_basis,
            metadata=metadata,
            artifact_schema=schema,
            columns=schema.columns if schema is not None else (),
            summary=summary,
            records=records,
            returned_count=len(records),
            available_count=(
                representation.record_count
                if query.view_type is ArtifactViewType.TOP_N
                else 0
            ),
            effective_limit=decision.effective_limit,
            truncated=truncated,
            provenance=ArtifactProvenance(
                owner_user_id=ref.owner_user_id,
                project_id=ref.project_id,
                lab_id=ref.lab_id,
                run_id=ref.run_id,
                stage_id=ref.stage_id,
                producer_invocation_id=ref.producer_invocation_id,
            ),
        )


class ArtifactExposureService:
    """The sole agent-facing route from ArtifactRef to ArtifactView."""

    def __init__(
        self,
        store: ArtifactStore,
        policy: ExposurePolicy,
        *,
        access_service: AccessService | None = None,
        trace_recorder: RunTraceRecorder | None = None,
        projector: ArtifactModelViewProjector | None = None,
    ):
        if not isinstance(store, ArtifactStore):
            raise TypeError("store must implement ArtifactStore")
        self.store = store
        self.policy = policy
        self.access_service = access_service
        self.trace_recorder = trace_recorder
        self.projector = projector or ArtifactModelViewProjector()

    def artifact_query(
        self,
        artifact_id: UUID | str,
        query: ArtifactQuery | dict[str, Any],
        consumer: ArtifactConsumer | str,
        *,
        principal: Principal | None = None,
    ) -> ArtifactView:
        try:
            identifier = coerce_artifact_id(artifact_id)
            validated_query = validate_artifact_query(query)
            validated_consumer = ArtifactConsumer(consumer)
        except (ValueError, ValidationError) as exc:
            raise ArtifactQueryError(f"Invalid artifact query: {exc}") from exc

        ref = self.store.get_ref(identifier)
        self._authorize(principal, ref)
        self._emit(
            ref,
            TraceEventType.ARTIFACT_VIEW_REQUESTED,
            consumer=validated_consumer,
            query=validated_query,
            status="REQUESTED",
        )
        decision = self.policy.decide(ref, validated_query, validated_consumer)
        if not decision.allowed:
            self._emit(
                ref,
                TraceEventType.ARTIFACT_EXPOSURE_DENIED,
                consumer=validated_consumer,
                query=validated_query,
                status="DENIED",
                extra={"reason": decision.reason},
            )
            raise ArtifactExposureDenied(decision.reason)

        stored = self.store.load_for_view(identifier)
        view = self.projector.build(
            stored.ref, stored.representation, validated_query, decision
        )
        self._emit(
            ref,
            TraceEventType.ARTIFACT_EXPOSED,
            consumer=validated_consumer,
            query=validated_query,
            status="EXPOSED",
            extra={
                "returned_count": view.returned_count,
                "available_count": view.available_count,
                "effective_limit": view.effective_limit,
                "truncated": view.truncated,
            },
        )
        return view

    def artifact_ref(
        self,
        artifact_id: UUID | str,
        *,
        principal: Principal | None = None,
    ):
        """Return metadata only after the same identity/scope authorization."""

        identifier = coerce_artifact_id(artifact_id)
        ref = self.store.get_ref(identifier)
        self._authorize(principal, ref)
        return ref

    def _authorize(self, principal: Principal | None, ref) -> None:
        if self.access_service is None:
            return
        if principal is None:
            raise AuthorizationDenied(
                "A Principal is required for governed Artifact access"
            )
        self.access_service.require_artifact(
            principal,
            ref,
            AccessAction.READ_ARTIFACT,
        )

    def _emit(
        self,
        ref,
        event_type: TraceEventType,
        *,
        consumer: ArtifactConsumer,
        query: ArtifactQuery,
        status: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if self.trace_recorder is None or ref.run_id is None:
            return
        payload = {
            "artifact_id": str(ref.artifact_id),
            "artifact_type": ref.artifact_type,
            "exposure_class": ref.exposure_class.value,
            "consumer": consumer.value,
            "view_type": query.view_type.value,
        }
        if query.limit is not None:
            payload["requested_limit"] = query.limit
        if extra:
            payload.update(extra)
        self.trace_recorder.emit(
            ref.run_id,
            event_type,
            stage_id=ref.stage_id,
            invocation_id=ref.producer_invocation_id,
            status=status,
            payload=payload,
        )


class PantheonArtifactQueryAdapter:
    """Narrow future ToolProvider boundary that returns only ArtifactView."""

    def __init__(
        self,
        service: ArtifactExposureService,
        *,
        consumer: ArtifactConsumer = ArtifactConsumer.REMOTE_LLM,
        principal: Principal | None = None,
    ):
        self.service = service
        self.consumer = consumer
        self.principal = principal

    async def artifact_query(
        self,
        artifact_id: str,
        query: dict[str, Any],
        consumer: str | None = None,
    ) -> ArtifactView:
        requested_consumer = self.consumer
        if consumer is not None:
            try:
                requested_consumer = ArtifactConsumer(consumer)
            except ValueError as exc:
                raise ArtifactQueryError(f"Unsupported artifact consumer: {consumer}") from exc
            if requested_consumer is not self.consumer:
                raise ArtifactExposureDenied(
                    "The Pantheon artifact tool consumer is fixed and cannot be "
                    "escalated by tool input."
                )
        return self.service.artifact_query(
            artifact_id,
            query,
            requested_consumer,
            principal=self.principal,
        )
