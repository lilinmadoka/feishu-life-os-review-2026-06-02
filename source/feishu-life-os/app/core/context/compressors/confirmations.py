from __future__ import annotations

from app.core.context.budget import estimate_tokens, truncate_text
from app.core.context.schemas import ContextCapsule
from app.core.context_builder import AgentContextPack
from app.core.store import StateStore


class PendingConfirmationCompressor:
    domain = "confirmation"

    def compress(self, *, store: StateStore, legacy_pack: AgentContextPack, purpose: str) -> list[ContextCapsule]:
        if not legacy_pack.pending_confirmations:
            return []
        facts = []
        evidence_refs = []
        for item in legacy_pack.pending_confirmations:
            fact = {
                "confirmation_id": item.id,
                "type": item.confirmation_type,
                "status": item.status,
                "candidate_count": item.candidate_count,
                "candidate_titles": [truncate_text(title, 80) for title in item.candidate_titles[:3]],
                "created_at": item.created_at,
                "expires_at": item.expires_at,
            }
            facts.append(fact)
            evidence_refs.append({"kind": "confirmation", "id": item.id})
        latest = facts[0]
        summary = (
            f"There are {len(facts)} pending confirmation(s). Latest is {latest['type']} "
            f"with {latest['candidate_count']} candidate(s): {', '.join(latest['candidate_titles'])}."
        )
        capsule = ContextCapsule(
            capsule_id="cap_confirmation_pending",
            domain=self.domain,
            purpose=purpose,
            summary=truncate_text(summary, 360),
            facts=facts,
            decision_hints=[
                "If the user only says confirm/确认, resolve the latest pending confirmation.",
                "If the user only says cancel/取消, cancel the latest pending confirmation.",
            ],
            forbidden_actions=[
                "Do not create new items when the user only confirms or cancels.",
                "Do not infer confirmation from an attachment-only message.",
            ],
            evidence_refs=evidence_refs,
            relevance_score=0.95,
            confidence=0.9,
            freshness="live",
        )
        capsule.token_estimate = estimate_tokens(capsule.model_dump(mode="json"))
        return [capsule]
