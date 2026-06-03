from __future__ import annotations

from typing import Any

from app.core.context.budget import truncate_text

SCHEDULE_FACT_INTENTS = {
    "query_availability",
    "schedule_time_budget_plan",
    "create_calendar_event",
    "update_calendar_event",
}

SCHEDULE_AVAILABILITY_TEXT_KEYWORDS = (
    "availability",
    "available",
    "free",
    "busy",
    "time slot",
    "\u7a7a\u95f2",
    "\u6709\u7a7a",
    "\u6709\u6ca1\u6709\u65f6\u95f4",
    "\u5fd9",
    "\u51b2\u7a81",
    "\u65f6\u95f4\u6bb5",
)

SCHEDULE_RELEVANCE_TEXT_KEYWORDS = (
    *SCHEDULE_AVAILABILITY_TEXT_KEYWORDS,
    "calendar",
    "schedule",
    "\u65e5\u7a0b",
    "\u6392\u7a0b",
    "\u5b89\u6392",
    "\u9884\u7ea6",
    "\u4f1a\u8bae",
    "\u4e0a\u8bfe",
    "\u8bfe\u7a0b",
    "\u8bfe\u8868",
)


def should_include_schedule_context(*, raw_text: Any = None, intent_name: str | None = None, purpose: str | None = None) -> bool:
    if intent_name in SCHEDULE_FACT_INTENTS or purpose in SCHEDULE_FACT_INTENTS:
        return True
    text = str(raw_text or "").lower()
    if not text.strip():
        return False
    return any(keyword in text for keyword in SCHEDULE_AVAILABILITY_TEXT_KEYWORDS)


def should_run_schedule_compressor(*, raw_text: Any = None, purpose: str | None = None) -> bool:
    if purpose in SCHEDULE_FACT_INTENTS:
        return True
    text = str(raw_text or "").lower()
    if not text.strip():
        return False
    return any(keyword in text for keyword in SCHEDULE_RELEVANCE_TEXT_KEYWORDS)


def render_provider_capsules(
    context_v2: Any,
    *,
    raw_text: Any = None,
    intent_name: str | None = None,
    stage: str = "intent",
    limit: int = 6,
) -> list[dict[str, Any]]:
    if not isinstance(context_v2, dict):
        return []
    raw_capsules = context_v2.get("capsules")
    if not isinstance(raw_capsules, list):
        return []
    include_schedule_facts = should_include_schedule_context(raw_text=raw_text, intent_name=intent_name, purpose=stage)
    rendered: list[dict[str, Any]] = []
    plan_draft_capsules = 0
    for item in raw_capsules:
        if not isinstance(item, dict):
            continue
        domain = str(item.get("domain") or "")
        if domain == "schedule" and not include_schedule_facts:
            continue
        if domain == "plan_draft":
            plan_draft_capsules += 1
            if plan_draft_capsules > 2:
                continue
        compact = _base_capsule(item)
        if domain == "plan_draft":
            compact["facts"] = _compact_plan_draft_facts(item.get("facts"), limit=2)
        elif domain == "schedule":
            compact["facts"] = _compact_schedule_facts(item.get("facts"))
        elif domain == "confirmation":
            compact.pop("missing_info", None)
        rendered.append(compact)
        if len(rendered) >= limit:
            break
    return [capsule for capsule in rendered if capsule.get("summary") or capsule.get("facts") or capsule.get("evidence_refs")]


def _base_capsule(item: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key in (
        "capsule_id",
        "domain",
        "purpose",
        "summary",
        "missing_info",
        "decision_hints",
        "forbidden_actions",
        "evidence_refs",
        "confidence",
        "freshness",
    ):
        value = item.get(key)
        if value not in (None, "", []):
            compact[key] = value
    if "summary" in compact:
        compact["summary"] = truncate_text(compact["summary"], 240)
    if isinstance(compact.get("missing_info"), list):
        compact["missing_info"] = [truncate_text(text, 80) for text in compact["missing_info"][:6] if str(text or "").strip()]
    if isinstance(compact.get("decision_hints"), list):
        compact["decision_hints"] = [truncate_text(text, 120) for text in compact["decision_hints"][:4] if str(text or "").strip()]
    if isinstance(compact.get("forbidden_actions"), list):
        compact["forbidden_actions"] = [truncate_text(text, 120) for text in compact["forbidden_actions"][:4] if str(text or "").strip()]
    if isinstance(compact.get("evidence_refs"), list):
        compact["evidence_refs"] = _compact_evidence(compact["evidence_refs"], limit=5)
    return compact


def _compact_plan_draft_facts(value: Any, *, limit: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    facts: list[dict[str, Any]] = []
    for item in value[:limit]:
        if not isinstance(item, dict):
            continue
        fact = {
            key: item.get(key)
            for key in ("plan_draft_id", "kind", "status", "title", "missing_fields", "planned_event_count", "created_at")
            if item.get(key) not in (None, "", [])
        }
        if "title" in fact:
            fact["title"] = truncate_text(fact["title"], 100)
        if isinstance(fact.get("missing_fields"), list):
            fact["missing_fields"] = [truncate_text(text, 60) for text in fact["missing_fields"][:6]]
        proposal = item.get("assistant_proposal")
        if isinstance(proposal, dict):
            fact["assistant_proposal"] = {
                key: truncate_text(proposal.get(key), 120)
                for key in ("user_goal", "next_step_suggestion")
                if proposal.get(key)
            }
            missing_info = proposal.get("missing_info")
            if isinstance(missing_info, list):
                fact["assistant_proposal"]["missing_info"] = [truncate_text(text, 60) for text in missing_info[:4]]
        facts.append(fact)
    return facts


def _compact_schedule_facts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    facts: list[dict[str, Any]] = []
    for item in value[:7]:
        if not isinstance(item, dict):
            continue
        facts.append(
            {
                "date": item.get("date"),
                "weekday": item.get("weekday"),
                "busy_count": item.get("busy_count", 0),
                "free_count": item.get("free_count", 0),
                "busy": _compact_ranges(item.get("busy"), include_title=True, limit=5),
                "free": _compact_ranges(item.get("free"), include_title=False, limit=5),
            }
        )
    return facts


def _compact_ranges(value: Any, *, include_title: bool, limit: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    ranges: list[dict[str, Any]] = []
    for item in value[:limit]:
        if not isinstance(item, dict):
            continue
        out = {key: item.get(key) for key in ("start", "end") if item.get(key)}
        if include_title:
            for key in ("title", "kind", "id"):
                if item.get(key):
                    out[key] = truncate_text(item.get(key), 80) if key == "title" else item.get(key)
        if out:
            ranges.append(out)
    return ranges


def _compact_evidence(value: Any, *, limit: int) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    refs: list[dict[str, Any]] = []
    for item in value[:limit]:
        if not isinstance(item, dict):
            continue
        ref = {key: item.get(key) for key in ("kind", "id", "field") if item.get(key)}
        if ref:
            refs.append(ref)
    return refs
