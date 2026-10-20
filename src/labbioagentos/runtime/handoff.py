"""Lossless wire deduplication; the complete evidence remains persisted unchanged."""

import hashlib
import json


def finalization_evidence(evidence, *, include_safe_results: bool = True,
                          compact: bool = False,
                          include_explicit_completion: bool = True) -> dict:
    # Optional nulls carry no evidence. Keeping them for every item can make the
    # metadata envelope larger than the bounded result bodies it replaced.
    value = evidence.model_dump(mode='json', exclude_none=True)
    # Prepared observations are delivered in their own exact, bounded message.
    value.pop('prepared_handoff', None)
    if not include_explicit_completion:
        value.pop('explicit_completion', None)
    if compact:
        value['projection'] = {
            'status': 'COMPACT_PROVENANCE',
            'full_evidence_id': str(evidence.evidence_id),
            'omitted_item_fields': ['trace_event_ids', 'reference_ids', 'artifact_read_receipt'],
        }
    seen = {}
    for item in value['items']:
        if compact:
            item.pop('trace_event_ids', None)
            item.pop('reference_ids', None)
            item.pop('artifact_read_receipt', None)
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
