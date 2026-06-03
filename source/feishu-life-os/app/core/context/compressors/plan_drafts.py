from __future__ import annotations

from typing import Any

from app.core.context.budget import estimate_tokens, truncate_text
from app.core.context.schemas import ContextCapsule
from app.core.context_builder import AgentContextPack
from app.core.store import StateStore


class ActivePlanDraftCompressor:
    domain = "plan_draft"

    def compress(self, *, store: StateStore, legacy_pack: AgentContextPack, purpose: str) -> list[ContextCapsule]:
        if not legacy_pack.active_plan_drafts:
            return []
        facts = []
        evidence_refs = []
        missing_info: list[str] = []
        for draft in legacy_pack.active_plan_drafts:
            proposal = _proposal_summary(draft.payload_summary)
            planned_event_count = _int_value(draft.payload_summary.get("planned_event_count"))
            fact = {
                "plan_draft_id": draft.id,
                "kind": draft.kind,
                "status": draft.status,
                "title": truncate_text(draft.title, 100),
                "missing_fields": draft.missing_fields[:6],
                "planned_event_count": planned_event_count,
                "assistant_proposal": proposal,
                "created_at": draft.created_at,
            }
            facts.append(fact)
            evidence_refs.append({"kind": "plan_draft", "id": draft.id})
            missing_info.extend(str(item) for item in draft.missing_fields[:6])
            if isinstance(proposal.get("missing_info"), list):
                missing_info.extend(str(item) for item in proposal["missing_info"][:6])
        latest = facts[0]
        summary = (
            f"Active plan draft: {latest['title']} ({latest['kind']}, {latest['status']}). "
            f"Missing info: {', '.join(_unique(missing_info)[:6]) or 'none'}."
        )
        capsule = ContextCapsule(
            capsule_id="cap_plan_drafts_active",
            domain=self.domain,
            purpose=purpose,
            summary=truncate_text(summary, 360),
            facts=facts,
            missing_info=_unique(missing_info)[:8],
            decision_hints=[
                "Short follow-up messages should refine the active plan draft when they add missing details.",
                "Generate schedule confirmation only after the active plan has enough scheduling details.",
            ],
            forbidden_actions=[
                "Do not write calendar events directly from an active plan draft.",
                "Do not start a new plan if the user is clearly modifying the active draft.",
            ],
            evidence_refs=evidence_refs,
            relevance_score=0.9,
            confidence=0.86,
            freshness="live",
        )
        capsule.token_estimate = estimate_tokens(capsule.model_dump(mode="json"))
        return [capsule]


def _proposal_summary(payload_summary: dict[str, Any]) -> dict[str, Any]:
    value = payload_summary.get("assistant_proposal")
    if not isinstance(value, dict):
        return {}
    return {
        "user_goal": truncate_text(value.get("user_goal"), 120),
        "missing_info": list(value.get("missing_info") or [])[:6],
        "next_step_suggestion": truncate_text(value.get("next_step_suggestion"), 140),
    }


def _int_value(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in values if item))
