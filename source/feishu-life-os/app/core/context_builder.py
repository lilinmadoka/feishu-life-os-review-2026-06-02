from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from app.core.relative_time import DAY_ROLLOVER_HOUR, effective_now
from app.core.schemas import ActionItem, CalendarEvent, Confirmation, PlanDraft, ScheduleBlock
from app.core.store import StateStore

MAX_CONTEXT_BYTES = 12_000
MAX_RAW_TEXT_CHARS = 1_200
MAX_TEXT_CHARS = 180
MAX_ITEMS_PER_BUCKET = 8
MAX_BLOCKS = 14
MAX_PENDING = 3
MAX_RECENT_MESSAGES = 3
MAX_RECENT_ASSISTANT_TURNS = 2
MAX_LONG_TERM_TASKS = 8


class ContextItem(BaseModel):
    id: str
    title: str
    kind: str
    status: str | None = None
    start_at: str | None = None
    end_at: str | None = None
    due_at: str | None = None
    display_time: str | None = None
    recurrence_rule: str | None = None
    reminder_enabled: bool | None = None
    estimated_minutes: int | None = None


class PendingConfirmationSummary(BaseModel):
    id: str
    confirmation_type: str
    status: str
    created_at: str
    expires_at: str | None = None
    candidate_count: int = 0
    candidate_titles: list[str] = Field(default_factory=list)


class PlanDraftSummary(BaseModel):
    id: str
    kind: str
    status: str
    title: str
    missing_fields: list[str] = Field(default_factory=list)
    payload_summary: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class RecentMessageSummary(BaseModel):
    id: str
    raw_text: str
    content_type: str | None = None
    attachment_refs: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str


class RecentAssistantTurnSummary(BaseModel):
    id: str
    intent: str | None = None
    reply_text: str
    tool_names: list[str] = Field(default_factory=list)
    created_at: str


class AgentContextPack(BaseModel):
    context_schema_version: int = 1
    project_brief: str
    safety_rules: list[str]
    raw_text: str
    content_type: str
    attachment_refs: list[dict[str, Any]] = Field(default_factory=list)
    sender_id: str | None = None
    chat_id: str | None = None
    capture_id: str | None = None
    source: str | None = None
    source_message_id: str | None = None
    now: str
    recent_user_messages: list[RecentMessageSummary] = Field(default_factory=list)
    recent_assistant_turns: list[RecentAssistantTurnSummary] = Field(default_factory=list)
    pending_confirmations: list[PendingConfirmationSummary] = Field(default_factory=list)
    active_plan_drafts: list[PlanDraftSummary] = Field(default_factory=list)
    today: list[ContextItem] = Field(default_factory=list)
    tomorrow: list[ContextItem] = Field(default_factory=list)
    next_7_days: list[ContextItem] = Field(default_factory=list)
    long_term_tasks: list[ContextItem] = Field(default_factory=list)
    schedule_blocks: list[ContextItem] = Field(default_factory=list)
    available_intents: list[str]
    context_limits: dict[str, Any]


PROJECT_BRIEF = (
    "Feishu is the user-facing entry. SQLite is the source of truth. "
    "The model may propose plans, but backend validation, confirmations, writes, schedules, and Feishu calls stay server-side."
)

SAFETY_RULES = [
    "The model must not write data or output Feishu card JSON.",
    "Complex long-term goals should become AssistantProposal drafts before concrete tool calls.",
    "Read/query messages must not create, update, or delete items.",
    "Create/update/cancel operations require backend confirmation unless already resolving a pending confirmation.",
    "Recurring unavailable time should become schedule block candidates, not ordinary tasks.",
    "If the intent is unclear or confidence is low, ask one concise clarification and do not write data.",
]

AVAILABLE_INTENTS = [
    "query_today_plan",
    "query_tomorrow_plan",
    "query_week_plan",
    "query_availability",
    "query_time_budget_plan",
    "schedule_time_budget_plan",
    "start_plan_refinement",
    "refine_plan_draft",
    "generate_plan_schedule_confirmation",
    "create_task",
    "create_calendar_event",
    "create_schedule_block",
    "create_time_budget_plan",
    "complete_task",
    "update_task",
    "update_calendar_event",
    "update_schedule_block",
    "disable_schedule_block_reminders",
    "cancel_task",
    "cancel_calendar_event",
    "cancel_schedule_block",
    "confirm",
    "cancel",
    "smalltalk",
    "clarify",
    "unknown",
]


def build_agent_context(store: StateStore, capture: dict[str, Any], tz: ZoneInfo) -> AgentContextPack:
    actual_now = datetime.now(tz)
    relative_now = effective_now(tz, actual_now)
    start_today = relative_now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_tomorrow = start_today + timedelta(days=1)
    start_after_tomorrow = start_today + timedelta(days=2)
    end_week = start_today + timedelta(days=7)

    pack = AgentContextPack(
        project_brief=PROJECT_BRIEF,
        safety_rules=SAFETY_RULES,
        raw_text=_truncate(str(capture.get("raw_text") or ""), MAX_RAW_TEXT_CHARS),
        content_type=str(capture.get("content_type") or "text"),
        attachment_refs=_attachment_summaries(capture.get("attachment_refs")),
        sender_id=_optional_str(capture.get("sender_id")),
        chat_id=_optional_str(capture.get("chat_id")),
        capture_id=_optional_str(capture.get("id")),
        source=_optional_str(capture.get("source")),
        source_message_id=_optional_str(capture.get("source_message_id")),
        now=actual_now.isoformat(),
        recent_user_messages=[
            _recent_message(item)
            for item in store.list_recent_captures(
                sender_id=_optional_str(capture.get("sender_id")),
                limit=MAX_RECENT_MESSAGES,
                exclude_id=_optional_str(capture.get("id")),
            )
        ],
        recent_assistant_turns=_recent_assistant_turns(store, _optional_str(capture.get("sender_id"))),
        pending_confirmations=[
            _summarize_confirmation(item)
            for item in store.list_pending_confirmations(sender_id=capture.get("sender_id"), limit=MAX_PENDING)
        ],
        active_plan_drafts=[
            _summarize_plan_draft(item)
            for item in store.list_plan_drafts(
                sender_id=_optional_str(capture.get("sender_id")),
                statuses=["refining", "ready_for_schedule", "schedule_pending"],
                limit=3,
            )
        ],
        today=_summarize_range(store, start_today, start_tomorrow),
        tomorrow=_summarize_range(store, start_tomorrow, start_after_tomorrow),
        next_7_days=_summarize_range(store, start_today, end_week),
        long_term_tasks=_summarize_long_term_tasks(store),
        schedule_blocks=[_block_item(item) for item in store.list_schedule_blocks()[:MAX_BLOCKS]],
        available_intents=AVAILABLE_INTENTS,
        context_limits={
            "max_context_bytes": MAX_CONTEXT_BYTES,
            "max_raw_text_chars": MAX_RAW_TEXT_CHARS,
            "max_text_chars": MAX_TEXT_CHARS,
            "max_items_per_bucket": MAX_ITEMS_PER_BUCKET,
            "max_schedule_blocks": MAX_BLOCKS,
            "max_pending_confirmations": MAX_PENDING,
            "max_recent_messages": MAX_RECENT_MESSAGES,
            "max_recent_assistant_turns": MAX_RECENT_ASSISTANT_TURNS,
            "max_long_term_tasks": MAX_LONG_TERM_TASKS,
            "day_rollover_hour": DAY_ROLLOVER_HOUR,
            "relative_base_date": relative_now.date().isoformat(),
        },
    )
    return _fit_context(pack)


def _summarize_range(store: StateStore, start: datetime, end: datetime) -> list[ContextItem]:
    items: list[ContextItem] = []
    for task in store.list_action_items(start=start, end=end)[:MAX_ITEMS_PER_BUCKET]:
        items.append(_task_item(task))
    remaining = MAX_ITEMS_PER_BUCKET - len(items)
    if remaining > 0:
        for event in store.list_calendar_events(start=start, end=end)[:remaining]:
            items.append(_event_item(event))
    return items


def _summarize_long_term_tasks(store: StateStore) -> list[ContextItem]:
    items = []
    for task in store.list_action_items():
        status = task.status.value if hasattr(task.status, "value") else str(task.status)
        if status in {"done", "canceled"}:
            continue
        if task.estimated_minutes:
            items.append(_task_item(task))
        if len(items) >= MAX_LONG_TERM_TASKS:
            break
    return items


def _summarize_confirmation(item: Confirmation) -> PendingConfirmationSummary:
    titles: list[str] = []
    for raw_call in item.proposed_tool_calls_json[:5]:
        if not isinstance(raw_call, dict):
            continue
        args = raw_call.get("arguments") if isinstance(raw_call.get("arguments"), dict) else {}
        title = args.get("title") or args.get("query") or raw_call.get("tool_name")
        if title:
            titles.append(_truncate(str(title), MAX_TEXT_CHARS))
    return PendingConfirmationSummary(
        id=item.id,
        confirmation_type=item.confirmation_type,
        status=item.status.value if hasattr(item.status, "value") else str(item.status),
        created_at=item.created_at.isoformat(),
        expires_at=item.expires_at.isoformat() if item.expires_at else None,
        candidate_count=len(item.proposed_tool_calls_json),
        candidate_titles=titles[:5],
    )


def _summarize_plan_draft(item: PlanDraft) -> PlanDraftSummary:
    payload = item.payload if isinstance(item.payload, dict) else {}
    courses = payload.get("courses") if isinstance(payload.get("courses"), list) else []
    planned_events = payload.get("planned_events") if isinstance(payload.get("planned_events"), list) else []
    summary = {
        "course_count": len(courses),
        "planned_event_count": len(planned_events),
        "term_anchor": payload.get("term_anchor") if isinstance(payload.get("term_anchor"), dict) else None,
    }
    proposal = payload.get("assistant_proposal") if isinstance(payload.get("assistant_proposal"), dict) else None
    if proposal:
        summary["assistant_proposal"] = {
            "user_goal": _truncate(str(proposal.get("user_goal") or ""), MAX_TEXT_CHARS),
            "missing_info": list(proposal.get("missing_info") or [])[:6],
            "next_step_suggestion": _truncate(str(proposal.get("next_step_suggestion") or ""), MAX_TEXT_CHARS),
        }
    return PlanDraftSummary(
        id=item.id,
        kind=item.kind.value if hasattr(item.kind, "value") else str(item.kind),
        status=item.status.value if hasattr(item.status, "value") else str(item.status),
        title=_truncate(item.title, MAX_TEXT_CHARS),
        missing_fields=item.missing_fields[:6],
        payload_summary={key: value for key, value in summary.items() if value not in (None, [], {})},
        created_at=item.created_at.isoformat(),
    )


def _recent_message(item: Any) -> RecentMessageSummary:
    return RecentMessageSummary(
        id=item.id,
        raw_text=_truncate(item.raw_text, MAX_TEXT_CHARS),
        content_type=item.content_type,
        attachment_refs=_attachment_summaries(item.attachment_refs),
        created_at=item.created_at.isoformat(),
    )


def _attachment_summaries(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    out: list[dict[str, Any]] = []
    allowed = {
        "kind",
        "message_id",
        "image_key",
        "file_key",
        "file_name",
        "filename",
        "mime_type",
        "size_bytes",
        "local_path",
        "download_status",
        "download_error",
    }
    for item in value[:3]:
        if not isinstance(item, dict):
            continue
        summary = {}
        for key in allowed:
            item_value = item.get(key)
            if item_value is None or item_value == "" or item_value == []:
                continue
            summary[key] = item_value
        if summary:
            out.append(summary)
    return out


def _recent_assistant_turns(store: StateStore, sender_id: str | None) -> list[RecentAssistantTurnSummary]:
    tool_runs = store.list_tool_runs(limit=50)
    tools_by_run: dict[str, list[Any]] = {}
    for tool in tool_runs:
        if tool.agent_run_id:
            tools_by_run.setdefault(tool.agent_run_id, []).append(tool)

    turns: list[RecentAssistantTurnSummary] = []
    for run in store.list_agent_runs(limit=20):
        if sender_id and run.input_json.get("sender_id") != sender_id:
            continue
        output = run.output_json if isinstance(run.output_json, dict) else {}
        reply = _final_reply_text(output, tools_by_run.get(run.id, []))
        if not reply:
            continue
        turns.append(
            RecentAssistantTurnSummary(
                id=run.id,
                intent=_optional_str(output.get("intent")),
                reply_text=_truncate(reply, MAX_TEXT_CHARS),
                tool_names=[
                    str(name)
                    for name in _tool_names(output, tools_by_run.get(run.id, []))
                ][:5],
                created_at=run.created_at.isoformat(),
            )
        )
        if len(turns) >= MAX_RECENT_ASSISTANT_TURNS:
            break
    return turns


def _final_reply_text(output: dict[str, Any], tool_runs: list[Any]) -> str:
    for tool in tool_runs:
        tool_output = tool.output_json if isinstance(tool.output_json, dict) else {}
        reply = tool_output.get("reply_text")
        if reply:
            return str(reply)
    return str(output.get("reply_to_user") or "")


def _tool_names(output: dict[str, Any], tool_runs: list[Any]) -> list[str]:
    names = [tool.tool_name for tool in tool_runs if getattr(tool, "tool_name", None)]
    if names:
        return names
    calls = output.get("tool_calls")
    if not isinstance(calls, list):
        return []
    return [str(call.get("tool_name")) for call in calls if isinstance(call, dict) and call.get("tool_name")]


def _task_item(item: ActionItem) -> ContextItem:
    return ContextItem(
        id=item.id,
        kind="task",
        title=_truncate(item.title, MAX_TEXT_CHARS),
        status=item.status.value if hasattr(item.status, "value") else str(item.status),
        due_at=item.due_at.isoformat() if item.due_at else None,
        estimated_minutes=item.estimated_minutes,
    )


def _event_item(item: CalendarEvent) -> ContextItem:
    return ContextItem(
        id=item.id,
        kind="calendar_event",
        title=_truncate(item.title, MAX_TEXT_CHARS),
        status=item.status.value if hasattr(item.status, "value") else str(item.status),
        start_at=item.start_at.isoformat(),
        end_at=item.end_at.isoformat(),
    )


def _block_item(item: ScheduleBlock) -> ContextItem:
    return ContextItem(
        id=item.id,
        kind="schedule_block",
        title=_truncate(item.title, MAX_TEXT_CHARS),
        status=item.status.value if hasattr(item.status, "value") else str(item.status),
        display_time=f"{item.start_time}-{item.end_time}",
        recurrence_rule=_truncate(item.recurrence_rule, MAX_TEXT_CHARS),
        reminder_enabled=item.reminder_enabled,
    )


def _fit_context(pack: AgentContextPack) -> AgentContextPack:
    data = pack.model_dump(mode="json")
    if _json_size(data) <= MAX_CONTEXT_BYTES:
        return pack

    data["next_7_days"] = data["next_7_days"][:4]
    data["long_term_tasks"] = data["long_term_tasks"][:4]
    data["schedule_blocks"] = data["schedule_blocks"][:6]
    data["today"] = data["today"][:4]
    data["tomorrow"] = data["tomorrow"][:4]
    data["pending_confirmations"] = data["pending_confirmations"][:2]
    data["recent_user_messages"] = data["recent_user_messages"][:2]
    data["recent_assistant_turns"] = data["recent_assistant_turns"][:1]
    if _json_size(data) <= MAX_CONTEXT_BYTES:
        return AgentContextPack.model_validate(data)

    data["raw_text"] = _truncate(str(data.get("raw_text") or ""), 600)
    if _json_size(data) <= MAX_CONTEXT_BYTES:
        return AgentContextPack.model_validate(data)

    data["safety_rules"] = data["safety_rules"][:3]
    data["next_7_days"] = data["next_7_days"][:2]
    data["long_term_tasks"] = data["long_term_tasks"][:2]
    data["schedule_blocks"] = data["schedule_blocks"][:3]
    data["pending_confirmations"] = data["pending_confirmations"][:1]
    data["recent_user_messages"] = data["recent_user_messages"][:1]
    data["recent_assistant_turns"] = data["recent_assistant_turns"][:1]
    data["raw_text"] = _truncate(str(data.get("raw_text") or ""), 300)
    return AgentContextPack.model_validate(data)


def _json_size(value: dict[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))


def _truncate(value: str, limit: int) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 3)]}..."


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
