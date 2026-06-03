from __future__ import annotations

from app.core.schemas import AgentResponse, AgentToolCall, RiskLevel

QUERY_INTENTS = {"query_today", "query_tomorrow", "query_week", "query_availability"}
WRITE_TOOLS = {
    "create_task_candidate",
    "create_calendar_event_candidate",
    "start_plan_refinement",
    "refine_plan_draft",
    "generate_plan_schedule_confirmation",
    "confirm_plan_schedule",
    "update_task",
    "complete_task",
    "cancel_task",
    "cancel_calendar_event",
    "update_calendar_event",
    "create_schedule_block_candidates",
    "schedule_habit_plan",
    "update_schedule_block",
    "disable_schedule_block_reminders",
    "cancel_schedule_block",
    "sync_feishu_task",
    "sync_feishu_calendar",
}
ALWAYS_CONFIRM = {
    "create_task_candidate",
    "create_calendar_event_candidate",
    "confirm_plan_schedule",
    "update_task",
    "update_calendar_event",
    "cancel_task",
    "cancel_calendar_event",
    "create_schedule_block_candidates",
    "schedule_habit_plan",
    "update_schedule_block",
    "cancel_schedule_block",
    "delete_task",
    "batch_update",
}


class PolicyViolation(RuntimeError):
    pass


class RiskPolicy:
    def validate_response(self, response: AgentResponse) -> None:
        if response.assistant_proposal:
            for call in response.tool_calls:
                if call.tool_name in WRITE_TOOLS:
                    raise PolicyViolation(f"assistant proposal cannot include write tool: {call.tool_name}")
        if response.intent in QUERY_INTENTS:
            for call in response.tool_calls:
                if call.tool_name in WRITE_TOOLS:
                    raise PolicyViolation(f"query intent cannot call write tool: {call.tool_name}")
        if response.intent == "unknown":
            for call in response.tool_calls:
                if call.tool_name not in {"send_feishu_reply", "ask_confirmation"}:
                    raise PolicyViolation("unknown intent cannot write state")

    def normalize_call(self, call: AgentToolCall) -> AgentToolCall:
        if call.tool_name in ALWAYS_CONFIRM:
            call.requires_confirmation = True
            if call.risk_level == RiskLevel.low:
                call.risk_level = RiskLevel.medium
        if call.risk_level in {RiskLevel.medium, RiskLevel.high} and call.tool_name in WRITE_TOOLS:
            call.requires_confirmation = True
        return call
