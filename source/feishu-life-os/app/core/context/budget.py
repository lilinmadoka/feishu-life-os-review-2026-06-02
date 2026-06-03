from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from app.core.context_builder import MAX_CONTEXT_BYTES


def json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))


def truncate_text(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 3)]}..."


def estimate_tokens(value: Any) -> int:
    # Cheap deterministic estimate; only used for budgeting and trace metadata.
    return max(1, json_size(value) // 4)


def fit_provider_request(request: dict[str, Any], *, max_bytes: int = MAX_CONTEXT_BYTES) -> dict[str, Any]:
    data = deepcopy(request)
    if json_size(data) <= max_bytes:
        return data

    context_v2 = data.get("context_v2")
    capsules = context_v2.get("capsules") if isinstance(context_v2, dict) else None
    if not isinstance(capsules, list):
        return data

    for capsule in capsules:
        if not isinstance(capsule, dict):
            continue
        capsule["summary"] = truncate_text(capsule.get("summary"), 240)
        capsule["facts"] = _compact_facts(capsule.get("facts"), limit=3)
        capsule["assumptions"] = _compact_strings(capsule.get("assumptions"), limit=3, text_limit=120)
        capsule["missing_info"] = _compact_strings(capsule.get("missing_info"), limit=5, text_limit=80)
        capsule["decision_hints"] = _compact_strings(capsule.get("decision_hints"), limit=4, text_limit=120)
        capsule["forbidden_actions"] = _compact_strings(capsule.get("forbidden_actions"), limit=4, text_limit=120)
        capsule["evidence_refs"] = _compact_evidence(capsule.get("evidence_refs"), limit=5)
    if json_size(data) <= max_bytes:
        return data

    for capsule in capsules:
        if isinstance(capsule, dict):
            capsule["facts"] = []
    if json_size(data) <= max_bytes:
        return data

    while len(capsules) > 3 and json_size(data) > max_bytes:
        capsules.pop()
    while len(capsules) > 0 and json_size(data) > max_bytes:
        capsules.pop()
    if json_size(data) <= max_bytes:
        return data

    data.pop("context_v2", None)
    return data


def _compact_facts(value: Any, *, limit: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    facts = []
    for item in value[:limit]:
        if not isinstance(item, dict):
            continue
        fact = {}
        for key, item_value in item.items():
            if item_value in (None, "", []):
                continue
            if isinstance(item_value, str):
                fact[key] = truncate_text(item_value, 120)
            elif isinstance(item_value, list):
                fact[key] = item_value[:8]
            else:
                fact[key] = item_value
        facts.append(fact)
    return facts


def _compact_strings(value: Any, *, limit: int, text_limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [truncate_text(item, text_limit) for item in value[:limit] if str(item or "").strip()]


def _compact_evidence(value: Any, *, limit: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    refs = []
    for item in value[:limit]:
        if not isinstance(item, dict):
            continue
        ref = {key: item.get(key) for key in ("kind", "id", "field") if item.get(key)}
        if ref:
            refs.append(ref)
    return refs
