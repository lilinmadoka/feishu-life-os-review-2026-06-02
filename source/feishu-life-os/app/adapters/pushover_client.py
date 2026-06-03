from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

import httpx

from app.config import Settings

PUSHOVER_TAG_MAX_LEN = 120
_PUSHOVER_TAG_SAFE_CHARS = re.compile(r"[^A-Za-z0-9_.:-]+")


class PushoverConfigError(RuntimeError):
    pass


def pushover_tag_for_target(target_type: str, target_id: str) -> str:
    safe_type = _PUSHOVER_TAG_SAFE_CHARS.sub("_", target_type).strip("_")
    safe_id = _PUSHOVER_TAG_SAFE_CHARS.sub("_", target_id).strip("_")
    tag = f"lifeos:{safe_type or 'target'}:{safe_id or 'unknown'}"
    return tag[:PUSHOVER_TAG_MAX_LEN]


class PushoverClient:
    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def configured(self) -> bool:
        return bool(self.settings.pushover_user_key and self.settings.pushover_app_token)

    async def send_emergency(
        self,
        title: str,
        message: str,
        *,
        url: str | None = None,
        url_title: str | None = None,
        tags: str | None = None,
    ) -> dict[str, Any]:
        if not self.settings.pushover_user_key or not self.settings.pushover_app_token:
            raise PushoverConfigError("PUSHOVER_USER_KEY / PUSHOVER_APP_TOKEN are required")
        payload: dict[str, Any] = {
            "token": self.settings.pushover_app_token,
            "user": self.settings.pushover_user_key,
            "title": title,
            "message": message,
            "priority": 2,
            "retry": self.settings.pushover_retry_seconds,
            "expire": self.settings.pushover_expire_seconds,
            "sound": self.settings.pushover_sound,
        }
        if url:
            payload["url"] = url
            payload["url_title"] = url_title or "打开"
        if tags:
            payload["tags"] = tags
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            response = await client.post(
                "https://api.pushover.net/1/messages.json",
                content=body,
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            data = response.json()
        if response.is_error or data.get("status") != 1:
            raise RuntimeError(f"Pushover API error: {response.status_code} {data}")
        return data

    async def cancel_emergency_by_tag(self, tag: str) -> dict[str, Any]:
        if not self.settings.pushover_app_token:
            raise PushoverConfigError("PUSHOVER_APP_TOKEN is required")
        encoded_tag = quote(tag, safe="")
        async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
            response = await client.post(
                f"https://api.pushover.net/1/receipts/cancel_by_tag/{encoded_tag}.json",
                data={"token": self.settings.pushover_app_token},
            )
            data = response.json()
        if response.is_error or data.get("status") != 1:
            raise RuntimeError(f"Pushover cancel API error: {response.status_code} {data}")
        return data
