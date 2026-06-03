from __future__ import annotations

from app.agents.models import AgentIntent, AgentRequest, AgentResponse, AgentToolCall, AgentToolName


class RulesFallbackAgentProvider:
    """Explicit fallback provider for health checks and trivial temporary commands."""

    name = "rules_fallback"

    def run(self, request: AgentRequest) -> AgentResponse:
        text = request.raw_text.strip()
        if any(keyword in text for keyword in ("今天任务", "今天还有什么任务", "今天有什么任务")):
            return AgentResponse(
                intent=AgentIntent.query,
                reply_text="我先查一下今天任务。",
                tool_calls=[AgentToolCall(name=AgentToolName.query_today)],
                confidence=0.6,
                reason_summary="命中临时兜底的今天任务查询。",
            )
        return AgentResponse(
            intent=AgentIntent.system,
            reply_text="已收到。我现在只能做最低限度兜底，智能处理器恢复后会继续处理。",
            tool_calls=[],
            confidence=0.2,
            reason_summary="规则兜底无法可靠判断意图。",
        )

