"""Lossless wire deduplication; the complete evidence remains persisted unchanged."""

import json


def finalization_evidence(evidence) -> dict:
    value = evidence.model_dump(mode='json')
    seen = {}
    for item in value['items']:
        result = item.get('safe_result')
        if result is None:
            continue
        identity = json.dumps(result, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        # Tiny values are cheaper to repeat than to reference. Retain every call,
        # outcome, authority, error and provenance; deduplicate exact bodies only.
        key = (item['information_authority'], identity)
        if len(identity) > 512 and key in seen:
            item['safe_result'] = {'identical_to_capability_invocation_id': seen[key]}
        else:
            seen[key] = item['capability_invocation_id']
    return value
