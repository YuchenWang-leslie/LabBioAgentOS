"""Lossless wire deduplication; the complete evidence remains persisted unchanged."""

import hashlib
import json


def finalization_evidence(evidence, *, include_safe_results: bool = True) -> dict:
    # Optional nulls carry no evidence. Keeping them for every item can make the
    # metadata envelope larger than the bounded result bodies it replaced.
    value = evidence.model_dump(mode='json', exclude_none=True)
    seen = {}
    for item in value['items']:
        result = item.get('safe_result')
        if result is None:
            continue
        identity = json.dumps(result, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        if not include_safe_results:
            item['safe_result'] = None
            item['safe_result_projection'] = {
                'status': 'OMITTED_FROM_FINALIZATION_CONTEXT',
                'serialized_bytes': len(identity.encode('utf-8')),
                'sha256': hashlib.sha256(identity.encode('utf-8')).hexdigest(),
            }
            continue
        # Tiny values are cheaper to repeat than to reference. Retain every call,
        # outcome, authority, error and provenance; deduplicate exact bodies only.
        key = (item['information_authority'], identity)
        if len(identity) > 512 and key in seen:
            item['safe_result'] = {'identical_to_capability_invocation_id': seen[key]}
        else:
            seen[key] = item['capability_invocation_id']
    return value
