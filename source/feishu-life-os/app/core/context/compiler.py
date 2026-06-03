from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.core.context.budget import MAX_CONTEXT_BYTES, estimate_tokens
from app.core.context.compressors import (
    ActivePlanDraftCompressor,
    PendingConfirmationCompressor,
    ScheduleAvailabilityCompressor,
)
from app.core.context.schemas import AgentContextPackV2, CompiledContext, ContextCapsule
from app.core.context_builder import build_agent_context
from app.core.store import StateStore


class ContextCompiler:
    def __init__(self, store: StateStore, tz: ZoneInfo, *, max_bytes: int = MAX_CONTEXT_BYTES) -> None:
        self.store = store
        self.tz = tz
        self.max_bytes = max_bytes
        self.compressors = [
            PendingConfirmationCompressor(),
            ActivePlanDraftCompressor(),
            ScheduleAvailabilityCompressor(),
        ]

    def compile(self, capture: dict[str, Any], *, purpose: str = "general") -> CompiledContext:
        legacy = build_agent_context(self.store, capture, self.tz)
        capsules: list[ContextCapsule] = []
        for compressor in self.compressors:
            capsules.extend(compressor.compress(store=self.store, legacy_pack=legacy, purpose=purpose))
        v2 = AgentContextPackV2(
            current_message={
                "raw_text": legacy.raw_text,
                "content_type": legacy.content_type,
                "attachment_refs": legacy.attachment_refs,
                "sender_id": legacy.sender_id,
                "chat_id": legacy.chat_id,
                "capture_id": legacy.capture_id,
                "source": legacy.source,
                "source_message_id": legacy.source_message_id,
                "now": legacy.now,
            },
            system_brief=legacy.project_brief,
            safety_rules=legacy.safety_rules,
            available_intents=legacy.available_intents,
            capsules=capsules,
            context_trace={
                "compiled_at": datetime.now(self.tz).isoformat(),
                "compiler_version": 1,
                "legacy_context_schema_version": legacy.context_schema_version,
                "compressors": [compressor.domain for compressor in self.compressors],
                "capsule_count": len(capsules),
            },
            budgets={
                "max_provider_request_bytes": self.max_bytes,
                "legacy_context_limits": legacy.context_limits,
                "capsule_token_estimate": sum(capsule.token_estimate or estimate_tokens(capsule.model_dump(mode="json")) for capsule in capsules),
            },
        )
        return CompiledContext(legacy_pack=legacy, v2_pack=v2)
