from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

RAW_TEXT_LIMIT = 160
MASKED_KEYS = {
    "sender_id",
    "open_id",
    "user_id",
    "union_id",
    "chat_id",
    "operator_id",
    "tenant_key",
}
RAW_TEXT_KEYS = {"raw_text", "text", "message"}
PATH_KEYS = {"local_path", "path", "content_ref"}


def hash_identifier(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"sha256:{digest}"


def redact_text(value: Any, *, limit: int = RAW_TEXT_LIMIT) -> dict[str, Any]:
    text = str(value or "")
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
    if key_lower in MASKED_KEYS or key_lower.endswith("_open_id") or key_lower.endswith("_user_id"):
        return {"hash": hash_identifier(value)}
    if key_lower in RAW_TEXT_KEYS:
        return redact_text(value)
    if key_lower in PATH_KEYS:
        text = str(value or "")
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
    return {str(key): redact_value(str(key), item) for key, item in value.items()}
