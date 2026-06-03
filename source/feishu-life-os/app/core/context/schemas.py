from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.core.context_builder import AgentContextPack


class ContextCapsule(BaseModel):
    capsule_id: str
    domain: str
    purpose: str
    summary: str
    facts: list[dict[str, Any]] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    missing_info: list[str] = Field(default_factory=list)
    decision_hints: list[str] = Field(default_factory=list)
    forbidden_actions: list[str] = Field(default_factory=list)
    evidence_refs: list[dict[str, Any]] = Field(default_factory=list)
    relevance_score: float = Field(default=0.5, ge=0, le=1)
    confidence: float = Field(default=0.5, ge=0, le=1)
    freshness: str = "live"
    expires_at: datetime | None = None
    token_estimate: int | None = None


class AgentContextPackV2(BaseModel):
    context_schema_version: int = 2
    current_message: dict[str, Any]
    system_brief: str
    safety_rules: list[str]
    available_intents: list[str]
    capsules: list[ContextCapsule] = Field(default_factory=list)
    context_trace: dict[str, Any] = Field(default_factory=dict)
    budgets: dict[str, Any] = Field(default_factory=dict)


class CompiledContext(BaseModel):
    legacy_pack: AgentContextPack
    v2_pack: AgentContextPackV2

    def provider_request(self, *, max_bytes: int) -> dict[str, Any]:
        from app.core.context.budget import fit_provider_request

        request = self.legacy_pack.model_dump(mode="json")
        request["context_v2"] = self.v2_pack.model_dump(mode="json")
        return fit_provider_request(request, max_bytes=max_bytes)
