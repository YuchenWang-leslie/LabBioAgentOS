"""Lossless wire deduplication; the complete evidence remains persisted unchanged."""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StrictStr, create_model
from pydantic_core import PydanticCustomError

from labbioagentos.kernel import (
    AnthropicMessagesProvider, GeminiGenerateContentProvider,
    GeminiInteractionsProvider, OpenAICompatibleChatProvider, OpenAIResponsesProvider, PublicMessage,
)
from labbioagentos.kernel.contracts import DEFAULT_MAX_REQUEST_BYTES
from labbioagentos.kernel.request_metrics import measure_request_size


_JSON_TEXT_PROVIDERS = (
    OpenAICompatibleChatProvider, OpenAIResponsesProvider, AnthropicMessagesProvider,
    GeminiGenerateContentProvider, GeminiInteractionsProvider,
)

# The aggregate selection is not a single tool response or public message.
# Actual finalizer admission still includes prompt, schema and wire framing.
MAX_HANDOFF_BYTES = DEFAULT_MAX_REQUEST_BYTES
_MAX_ACTION_SCHEMA_CHARACTERS = 8_000


class HandoffActionControl(BaseModel):
    """Current host-generated admission contract, never a rejected or chosen action."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    authority: Literal["CONTROL_STATE"] = "CONTROL_STATE"
    field: Literal["next_action"] = "next_action"
    schema_status: Literal["AVAILABLE", "WITHHELD_TOO_LARGE"]
    action_schema_encoding: Literal["JSON"] = "JSON"
    action_schema: StrictStr | None = Field(default=None,
        min_length=2, max_length=_MAX_ACTION_SCHEMA_CHARACTERS)
    action_schema_characters: int = Field(ge=0)
    action_schema_sha256: StrictStr = Field(pattern=r"^[a-f0-9]{64}$")


class HandoffActionNotCurrentError(ValueError):
    def __init__(self, action_format: type[BaseModel]):
        # Preserve the exact schema without adding its nesting to the existing
        # bounded tool/trace envelope. This is host-generated control, not input.
        schema_text = json.dumps(
            action_format.model_json_schema(), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False,
        )
        available = len(schema_text) <= _MAX_ACTION_SCHEMA_CHARACTERS
        self.control = HandoffActionControl(
            schema_status="AVAILABLE" if available else "WITHHELD_TOO_LARGE",
            action_schema=schema_text if available else None,
            action_schema_characters=len(schema_text),
            action_schema_sha256=hashlib.sha256(schema_text.encode("utf-8")).hexdigest(),
        )
        super().__init__("next_action is outside the current handoff action contract")


def selected_handoff_messages(handoff) -> tuple[PublicMessage, ...]:
    raw = handoff.model_dump_json(exclude_none=True)
    if len(raw.encode('utf-8')) > MAX_HANDOFF_BYTES:
        raise ValueError('Prepared handoff exceeds its aggregate byte bound')
    if len(raw.encode('utf-8')) <= 60_000:
        return (PublicMessage(role='USER', content=raw),)
    count = (len(raw) + 11_999) // 12_000
    digest = hashlib.sha256(raw.encode('utf-8')).hexdigest()
    return tuple(PublicMessage(role='USER', content=json.dumps({
        'authority': 'CONTROL_STATE', 'handoff_delivery': 'EXACT_SELECTED_HANDOFF',
        'part_index': part, 'part_count': count, 'sha256': digest,
        'instruction': 'Ordered exact JSON fragments; authority and source-read limits are unchanged.',
    }, separators=(',', ':')) + '\n' + raw[part*12_000:(part+1)*12_000])
        for part in range(count))


def decision_context_sha256(stage_input) -> str:
    """Bind a proposal to identity and authoritative user/control input, not tool prose."""
    value = stage_input.model_dump(mode='json', include={
        'run_id', 'stage_id', 'invocation_id', 'workspace', 'instruction',
        'goal_reference', 'workflow_control', 'clarifications', 'gate_decisions',
    })
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
        ensure_ascii=False).encode('utf-8')).hexdigest()


def stage_decision_control(stage_input, evidence) -> dict | None:
    if evidence is None or evidence.decision_context_sha256 is None:
        return None  # Historical bundles retain their original contract.
    prepared = evidence.prepared_handoff
    decision = prepared.stage_decision if prepared is not None else None
    if decision is None:
        return {'status': 'MISSING', 'scope': 'The investigating Agent must choose its own next_action in prepare_handoff.'}
    if decision.context_sha256 != evidence.decision_context_sha256:
        raise ValueError('Stage decision context differs from its capability bundle')
    if decision.context_sha256 != decision_context_sha256(stage_input):
        return {'status': 'SUPERSEDED_BY_CURRENT_INPUT',
            'scope': 'Authoritative user or control input changed. The archived decision is historical; choose a current legal action without replaying tools.'}
    return {'status': 'BOUND', 'next_action': decision.next_action.model_dump(mode='json'),
        'context_sha256': decision.context_sha256,
        'scope': 'The investigating Agent owns this exact action, including its reason and question. Format the handoff without changing the decision; it remains a proposal subject to WorkflowEngine authority.'}


def decision_bound_response_format(base_format, control):
    if control is None or control['status'] != 'BOUND':
        return base_format
    expected = control['next_action']

    def preserve_action(value):
        candidate = value.model_dump(mode='json') if hasattr(value, 'model_dump') else value
        if candidate != expected:
            raise PydanticCustomError('stage_decision_mismatch',
                'Preserve the exact investigating Agent next_action from stage_decision_control.')
        return value

    return create_model('DecisionBoundStageResult', __base__=base_format,
        next_action=(Annotated[base_format.model_fields['next_action'].annotation,
            BeforeValidator(preserve_action)], Field(json_schema_extra={'enum': [expected]})))


def _serialized_bytes(value) -> int:
    return len(json.dumps(value, separators=(',', ':'), ensure_ascii=False).encode('utf-8'))


def _capacity(byte_count: int, max_bytes: int = 60_000) -> dict:
    return {
        'serialized_bytes': byte_count, 'max_bytes': max_bytes,
        'remaining_bytes': max(0, max_bytes - byte_count),
        'over_by_bytes': max(0, byte_count - max_bytes),
    }


def handoff_capacity(handoff) -> dict:
    """Measure a selection without copying its content into the status reply."""
    if handoff is None:
        return {'status': 'NOT_PREPARED', **_capacity(0, MAX_HANDOFF_BYTES), 'observation_ids': [], 'item_bytes': {}}
    size = len(handoff.model_dump_json(exclude_none=True).encode('utf-8'))
    return {
        'status': 'READY' if size <= MAX_HANDOFF_BYTES else 'TOO_LARGE',
        **_capacity(size, MAX_HANDOFF_BYTES),
        'observation_ids': [str(item.observation_id) for item in handoff.observations],
        'item_bytes': {
            str(item.observation_id): len(item.model_dump_json(exclude_none=True).encode('utf-8'))
            for item in handoff.observations
        },
    }


def finalization_capacity(payload, content: str, evidence, *, additional_provenance_bytes: int = 0) -> dict:
    """Explain the exact control-message bound, separate from request admission."""
    size = len(content.encode('utf-8')) + additional_provenance_bytes
    components = {key: _serialized_bytes(value) for key, value in payload.items()}
    if additional_provenance_bytes:
        components['capability_evidence'] += additional_provenance_bytes
    selected = handoff_capacity(evidence.prepared_handoff if evidence is not None else None)
    completion = evidence.explicit_completion if evidence is not None else None
    return {
        'finalization_control': {
            **_capacity(size), 'component_bytes': components,
            'framing_bytes': size - sum(components.values()),
        },
        'prepared_handoff': selected,
        'completion_content_bytes': len(completion.encode('utf-8')) if completion is not None else 0,
        'within_message_limits': size <= 60_000 and selected['over_by_bytes'] == 0,
        'complete_request': {'status': 'UNAVAILABLE'},
        'within_request_limits': None,
        'request_admission': 'NOT_MEASURED',
        'scope': 'Per-message entries cover control and selected observations only. '
        'complete_request, when available, separately measures the current first finalizer '
        'request including system prompt and output schema; it is not a reservation '
        'for later tool results, completion text or provider turns.',
    }


def complete_request_capacity(request, provider, max_request_bytes, *, additional_provenance_bytes=0):
    """First-request snapshot and a separate bound for future scalar width.

    UUID fields always occupy 36 characters. A positive finite binary64 JSON
    number needs at most 17 significant digits, decimal point and exponent;
    24 bytes conservatively bound the future monotonic deadline's encoding.
    """
    sizes = measure_request_size(request, provider)
    deadline_width = len(request.model_dump_json(include={'deadline_monotonic'}).encode()) - len(
        b'{"deadline_monotonic":}')
    reference_count = (additional_provenance_bytes + 1) // 39 if additional_provenance_bytes else 0
    # Each quoted UUID adds two escaping backslashes when the control JSON is
    # embedded as a message string. No placeholder evidence IDs are constructed.
    provenance_public = additional_provenance_bytes + 2 * reference_count
    public_upper = sizes.public_request_bytes + provenance_public + 24 - deadline_width
    # These exact implementations serialize control as a JSON text message and
    # do not send deadline/UUID envelope fields. Overrides are not covered.
    known_wire_projection = type(provider) in _JSON_TEXT_PROVIDERS
    wire_upper = (sizes.wire_request_bytes + provenance_public
                  if known_wire_projection and sizes.wire_request_bytes is not None else None)
    checked_bytes = max(public_upper, sizes.wire_request_bytes or 0, wire_upper or 0)
    return {
        'status': 'WITHIN_KNOWN_LIMITS' if checked_bytes <= max_request_bytes else 'TOO_LARGE',
        'snapshot': sizes.model_dump(mode='json'),
        'max_request_bytes': max_request_bytes,
        'deadline_width_bytes': deadline_width, 'deadline_width_max_bytes': 24,
        'metadata_bound_status': 'FUTURE_DEADLINE_WIDTH_RESERVED',
        'additional_provenance_public_bytes': provenance_public,
        'public_request_bytes_upper_bound': public_upper,
        'wire_request_bytes_upper_bound': wire_upper,
        'wire_provenance_delta_bytes': provenance_public if known_wire_projection else None,
        'wire_bound_status': ('JSON_TEXT_FIRST_REQUEST' if known_wire_projection else
            'SNAPSHOT_ONLY' if sizes.wire_request_bytes is not None else 'UNAVAILABLE'),
        'checked_request_bytes': checked_bytes,
        'checked_request_bytes_kind': 'MAX_PUBLIC_UPPER_AND_KNOWN_WIRE',
        'remaining_bytes': max(0, max_request_bytes - checked_bytes),
        'over_by_bytes': max(0, checked_bytes - max_request_bytes),
        'within_request_limits': checked_bytes <= max_request_bytes,
        'scope': 'Current first tool-free request only, with a reserved future deadline width. '
        'Snapshot precedes the status response itself; future completion and tool results are not reserved. '
        'UUID identities have fixed width; public provenance additions include JSON escaping. '
        'Unknown wire sizes or provenance deltas remain unknown, not zero.',
    }


def finalization_evidence(evidence, *, include_safe_results: bool = True,
                          compact: bool = False,
                          include_explicit_completion: bool = True) -> dict:
    # Optional nulls carry no evidence. Keeping them for every item can make the
    # metadata envelope larger than the bounded result bodies it replaced.
    value = evidence.model_dump(mode='json', exclude_none=True)
    # Full host read attestations remain durable for reauthorization/recovery.
    # The formatter receives only the resolved control, not duplicated receipts.
    reads = value.pop('validated_artifact_reads', ())
    if reads:
        value['validation_read_attestation'] = {
            'authority': 'CONTROL_STATE', 'count': len(reads),
            'scope': 'HOST_ONLY_SNAPSHOT; use validation_result_control for current eligibility',
        }
    # Public peer replies are preserved in the bundle and later stage context;
    # do not silently feed all old advice to the tool-free formatter.
    value.pop('delegations', None)
    if evidence.delegations:
        from .context import delegation_directory
        value['delegation_directory'] = delegation_directory(evidence.delegations)
    # Prepared observations are delivered in their own exact, bounded message.
    value.pop('prepared_handoff', None)
    if not include_explicit_completion:
        value.pop('explicit_completion', None)
    omit_archived_audits = compact and not include_safe_results and evidence.prepared_handoff is not None
    omitted_fields = ['trace_event_ids', 'reference_ids', 'artifact_read_receipt']
    if omit_archived_audits:
        omitted_fields += ['error_details', 'execution_review', 'artifact_query_request',
            'artifact_aggregate_request', 'skill_search_request', 'execution_submit_request',
            'report_reference_selections']
    if compact:
        value['projection'] = {
            'status': 'COMPACT_PROVENANCE',
            'full_evidence_id': str(evidence.evidence_id),
            'omitted_item_fields': omitted_fields,
        }
    seen = {}
    for item in value['items']:
        if compact:
            # Full request audits and diagnostics remain in the immutable bundle;
            # Agent-selected original responses are delivered separately, unchanged.
            for field in omitted_fields:
                item.pop(field, None)
        result = item.get('safe_result')
        if result is None:
            continue
        identity = json.dumps(result, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        if not include_safe_results:
            item['safe_result'] = None
            projection = {'status': 'OMITTED_FROM_FINALIZATION_CONTEXT'}
            if not compact:
                projection['serialized_bytes'] = len(identity.encode('utf-8'))
                projection['sha256'] = hashlib.sha256(identity.encode('utf-8')).hexdigest()
            item['safe_result_projection'] = projection
            continue
        # Tiny values are cheaper to repeat than to reference. Retain every call,
        # outcome, authority, error and provenance; deduplicate exact bodies only.
        key = (item['information_authority'], identity)
        if len(identity) > 512 and key in seen:
            item['safe_result'] = {'identical_to_capability_invocation_id': seen[key]}
        else:
            seen[key] = item['capability_invocation_id']
    return value
