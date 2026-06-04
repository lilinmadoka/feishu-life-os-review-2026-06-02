from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

RAW_TEXT_LIMIT = 160
MASKED_KEYS = {
    "sender_id",
    "open_id",
    "open_ids",
    "user_id",
    "user_ids",
    "union_id",
    "union_ids",
    "chat_id",
    "operator_id",
    "tenant_key",
}
RAW_TEXT_KEYS = {"raw_text", "text", "message"}
TEXT_SUMMARY_KEYS = {
    "prompt",
    "system_prompt",
    "full_prompt",
    "provider_input",
    "reply_text",
    "reply_to_user",
    "reasoning_summary",
}
PATH_KEYS = {"local_path", "path", "content_ref"}


def _safe_string(value: Any) -> str:
    try:
        return str(value or "")
    except Exception:  # noqa: BLE001 - redaction must never expose or fail on bad values.
        return f"<unprintable:{type(value).__name__}>"


def hash_identifier(value: Any) -> str | None:
    text = _safe_string(value).strip()
    if not text:
        return None
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"sha256:{digest}"


def redact_text(value: Any, *, limit: int = RAW_TEXT_LIMIT) -> dict[str, Any]:
    text = _safe_string(value)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16] if text else None
    truncated = text[:limit]
    return {
        "text": truncated,
        "truncated": len(text) > limit,
        "char_count": len(text),
        "hash": f"sha256:{digest}" if digest else None,
    }


def redact_value(key: str, value: Any) -> Any:
    key_lower = key.lower()
    if (
        key_lower in MASKED_KEYS
        or key_lower.endswith("_open_id")
        or key_lower.endswith("_open_ids")
        or key_lower.endswith("_user_id")
        or key_lower.endswith("_user_ids")
        or key_lower.endswith("_union_id")
        or key_lower.endswith("_union_ids")
    ):
        if isinstance(value, list):
            return [{"hash": hash_identifier(item)} for item in value[:20]]
        return {"hash": hash_identifier(value)}
    if key_lower in RAW_TEXT_KEYS or key_lower in TEXT_SUMMARY_KEYS:
        return redact_text(value)
    if key_lower in PATH_KEYS:
        text = _safe_string(value)
        return {"basename": Path(text).name if text else "", "hash": hash_identifier(text)}
    if isinstance(value, dict):
        return redact_mapping(value)
    if isinstance(value, list):
        return [redact_value(key, item) for item in value[:20]]
    if isinstance(value, str) and len(value) > 300:
        return f"{value[:297]}..."
    return value


def redact_mapping(value: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {_safe_string(key): redact_value(_safe_string(key), item) for key, item in value.items()}
