from __future__ import annotations

import base64
import calendar
import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field, ValidationError

from app.core.context.render import render_provider_capsules
from app.core.relative_time import DAY_ROLLOVER_HOUR, effective_now
from app.core.schemas import (
    AgentResponse,
    AgentToolCall,
    AssistantProposal,
    PlanDraftKind,
    PlanDraftStatus,
    RiskLevel,
)
from app.services.time_parser import parse_datetime

DAY_CODES = {
    "周一": "MO",
    "周二": "TU",
    "周三": "WE",
    "周四": "TH",
    "周五": "FR",
    "周六": "SA",
    "周日": "SU",
    "周天": "SU",
}

TIME_TOKEN = r"(?:上午|早上|下午|晚上|晚|中午)?\s*\d{1,2}(?:(?::|：|\.)\d{1,2}|点\d{0,2})?"


def _looks_like_disable_schedule_block_reminders_text(text: str) -> bool:
    text = str(text or "")
    if not any(token in text for token in ("不用提醒", "不需要提醒", "不要提醒", "别提醒", "关闭提醒", "取消提醒", "不用再提醒")):
        return False
    if any(token in text for token in ("固定", "每周", "重复", "长期日程", "日程安排", "安排")):
        return True
    return "提醒" in text and any(token in text for token in ("上课", "课程", "教室", "日历同步", "同步到日历"))


class CoreAgentProviderError(RuntimeError):
    pass


class CoreAgentProviderUnavailable(CoreAgentProviderError):
    pass


class CoreAgentProvider(Protocol):
    name: str
    model: str | None

    def run(self, request: dict[str, Any]) -> AgentResponse:
        ...


ModelIntentName = Literal[
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


class ModelIntent(BaseModel):
    intent: str
    confidence: float = Field(ge=0, le=1)
    reply: str | None = ""
    entities: dict[str, Any] | None = Field(default_factory=dict)
    needs_confirmation: bool | None = False
    reasoning_summary: str | None = ""


class MockAgentProvider:
    name = "mock_provider"
    model = "deterministic-mock"

    def __init__(self, tz: ZoneInfo | None = None):
        self.tz = tz or ZoneInfo("Asia/Shanghai")

    def run(self, request: dict[str, Any]) -> AgentResponse:
        text = str(request.get("raw_text") or "").strip()
        now = datetime.now(self.tz)
        tomorrow = now + timedelta(days=1)
        if text in {"确认", "是的", "可以", "确定", "OK", "ok"}:
            return AgentResponse(
                intent="update_existing",
                confidence=0.95,
                reasoning_summary="用户确认最近待确认操作。",
                reply_to_user="收到，按刚才确认的内容执行。",
                tool_calls=[
                    AgentToolCall(tool_name="resolve_confirmation", risk_level=RiskLevel.low, arguments={})
                ],
            )
        if text in {"取消", "不用了", "算了"}:
            return AgentResponse(
                intent="update_existing",
                confidence=0.9,
                reasoning_summary="用户取消最近待确认操作。",
                reply_to_user="收到，取消刚才的候选。",
                tool_calls=[
                    AgentToolCall(
                        tool_name="resolve_confirmation",
                        risk_level=RiskLevel.low,
                        arguments={"action": "cancel"},
                    )
                ],
            )
        if ("待确认" in text or "待确认项" in text) and any(word in text for word in ("哪些", "什么", "看看", "查")):
            return AgentResponse(
                intent="query_today",
                confidence=0.9,
                reasoning_summary="查询最近待确认项。",
                reply_to_user="我查一下最近待确认项。",
                tool_calls=[AgentToolCall(tool_name="query_pending_confirmations", risk_level=RiskLevel.low)],
            )
        if "小王" in text and any(word in text for word in ("任务", "日程", "相关", "事项")):
            return AgentResponse(
                intent="query_week",
                confidence=0.86,
                reasoning_summary="按关键词查询小王相关事项。",
                reply_to_user="我查一下小王相关事项。",
                tool_calls=[AgentToolCall(tool_name="query_tasks", risk_level=RiskLevel.low, arguments={"query": "小王"})],
            )
        if any(word in text for word in ("有空", "空闲", "啥时间", "什么时候有空", "被占", "能不能安排")):
            return AgentResponse(
                intent="query_availability",
                confidence=0.88,
                reasoning_summary="用户询问空闲或占用时间，查询日程安排。",
                reply_to_user="我帮你算一下空闲时间。",
                tool_calls=[
                    AgentToolCall(
                        tool_name="query_availability",
                        risk_level=RiskLevel.low,
                        arguments=self._availability_args(text),
                    )
                ],
            )
        if "每周" not in text and any(day in text for day in ("周一", "周二", "周三", "周四", "周五", "周六", "周天", "周日")) and any(
            word in text for word in ("安排", "任务", "日程")
        ):
            return AgentResponse(
                intent="query_availability",
                confidence=0.84,
                reasoning_summary="用户查询某天安排，展开日程安排。",
                reply_to_user="我查一下那天的安排。",
                tool_calls=[
                    AgentToolCall(
                        tool_name="query_availability",
                        risk_level=RiskLevel.low,
                        arguments={**self._availability_args(text), "focus": "busy"},
                    )
                ],
            )
        if "今天" in text and "任务" in text:
            return AgentResponse(
                intent="query_today",
                confidence=0.95,
                reasoning_summary="查询今天任务，不允许创建。",
                reply_to_user="我查一下今天任务。",
                tool_calls=[AgentToolCall(tool_name="query_today", risk_level=RiskLevel.low)],
            )
        if "明天" in text and any(word in text for word in ("任务", "安排", "日程")):
            return AgentResponse(
                intent="query_tomorrow",
                confidence=0.92,
                reasoning_summary="查询明天安排，不允许创建。",
                reply_to_user="我查一下明天安排。",
                tool_calls=[AgentToolCall(tool_name="query_tomorrow", risk_level=RiskLevel.low)],
            )
        if "本周" in text and any(word in text for word in ("任务", "安排", "日程", "事项")):
            return AgentResponse(
                intent="query_week",
                confidence=0.92,
                reasoning_summary="查询本周事项，不允许创建。",
                reply_to_user="我查一下本周事项。",
                tool_calls=[AgentToolCall(tool_name="query_week", risk_level=RiskLevel.low)],
            )
        if any(word in text for word in ("完成", "做完了", "已完成")) and "任务" in text:
            query = text.replace("完成", "").replace("做完了", "").replace("已完成", "").replace("任务", "").strip() or text
            return AgentResponse(
                intent="complete_item",
                confidence=0.82,
                reasoning_summary="用户要完成任务，唯一匹配时可执行。",
                reply_to_user="我会匹配要完成的任务。",
                tool_calls=[AgentToolCall(tool_name="complete_task", risk_level=RiskLevel.low, arguments={"query": query})],
            )
        if "取消" in text and "任务" in text:
            query = text.replace("取消", "").replace("任务", "").strip() or text
            return AgentResponse(
                intent="update_existing",
                confidence=0.78,
                reasoning_summary="用户要取消任务，属于高风险写操作，需要确认。",
                reply_to_user="我会先匹配任务，再请你确认取消。",
                tool_calls=[
                    AgentToolCall(
                        tool_name="cancel_task",
                        risk_level=RiskLevel.high,
                        requires_confirmation=True,
                        arguments={"query": query},
                    )
                ],
            )
        if _looks_like_disable_schedule_block_reminders_text(text):
            return AgentResponse(
                intent="update_existing",
                confidence=0.9,
                reasoning_summary="用户要保留固定安排，但关闭固定安排提醒。",
                reply_to_user="我会关闭固定安排提醒，日程安排仍会保留。",
                tool_calls=[
                    AgentToolCall(
                        tool_name="disable_schedule_block_reminders",
                        risk_level=RiskLevel.low,
                        requires_confirmation=False,
                        arguments={"scope": "all", "query": text},
                    )
                ],
            )
        if self._mock_pending_plan(request) and text not in {"确认", "取消"}:
            return AgentResponse(
                intent="create_candidates",
                confidence=0.9,
                reasoning_summary="用户在完善长期日程草案。",
                reply_to_user="我会更新这张草案。",
                tool_calls=[
                    AgentToolCall(
                        tool_name="refine_plan_draft",
                        risk_level=RiskLevel.low,
                        arguments={"raw_text": text, "kind": "course_timetable"},
                    )
                ],
            )
        if any(token in text for token in ("课程表", "课表", "上课时间", "节次", "教学周")):
            return AgentResponse(
                intent="create_candidates",
                confidence=0.9,
                reasoning_summary="识别为课程表导入，需要先形成可确认草案。",
                reply_to_user="我会先生成课程表草案，确认周次、节次和课程后再写入日历。",
                tool_calls=[
                    AgentToolCall(
                        tool_name="start_plan_refinement",
                        risk_level=RiskLevel.low,
                        arguments={"kind": "course_timetable", "raw_text": text, "attachment_refs": request.get("attachment_refs") or []},
                    )
                ],
            )
        if ("每周" in text and any(word in text for word in ("固定", "课", "上课", "实验课"))) or ("每晚" in text and "睡觉" in text):
            blocks = self._extract_schedule_blocks(text)
            if not blocks:
                blocks.append(
                    {
                        "title": "日程安排",
                        "recurrence_rule": "FREQ=WEEKLY",
                        "start_time": "00:00",
                        "end_time": "00:00",
                        "timezone": "Asia/Shanghai",
                    }
                )
            return AgentResponse(
                intent="schedule_blocks",
                confidence=0.88,
                reasoning_summary="识别为重复日程安排，不能拆成普通任务。",
                reply_to_user=f"我识别到 {len(blocks)} 个日程安排，需要你确认后保存。",
                tool_calls=[
                    AgentToolCall(
                        tool_name="create_schedule_block_candidates",
                        risk_level=RiskLevel.medium,
                        requires_confirmation=True,
                        arguments={"blocks": blocks},
                    )
                ],
            )
        if "小王补课" in text and "改到" in text:
            hour = 19
            if "晚上7点" in text or "晚7点" in text:
                hour = 19
            return AgentResponse(
                intent="update_existing",
                confidence=0.86,
                reasoning_summary="用户要修改已有日程时间，需要确认。",
                reply_to_user="我会先匹配小王补课日程，再请你确认修改。",
                tool_calls=[
                    AgentToolCall(
                        tool_name="update_calendar_event",
                        risk_level=RiskLevel.medium,
                        requires_confirmation=True,
                        arguments={
                            "query": "小王补课",
                            "start_at": tomorrow.replace(hour=hour, minute=0, second=0, microsecond=0).isoformat(),
                            "end_at": tomorrow.replace(hour=hour + 1, minute=0, second=0, microsecond=0).isoformat(),
                        },
                    )
                ],
            )
        if "任务" in text and "改到" in text:
            query = text.split("改到", 1)[0].replace("把", "").replace("任务", "").strip() or text
            return AgentResponse(
                intent="update_existing",
                confidence=0.76,
                reasoning_summary="用户要修改任务时间，需要确认。",
                reply_to_user="我会先匹配任务，再请你确认修改时间。",
                tool_calls=[
                    AgentToolCall(
                        tool_name="update_task",
                        risk_level=RiskLevel.medium,
                        requires_confirmation=True,
                        arguments={
                            "query": query,
                            "due_at": tomorrow.replace(hour=19, minute=0, second=0, microsecond=0).isoformat(),
                        },
                    )
                ],
            )
        if "小王补课" in text and "资料发家长" in text:
            return AgentResponse(
                intent="create_candidates",
                confidence=0.9,
                reasoning_summary="包含一个明确日程和一个普通待办，均需确认。",
                reply_to_user="我识别到 1 个日程候选和 1 个任务候选，需要你确认后创建。",
                tool_calls=[
                    AgentToolCall(
                        tool_name="create_calendar_event_candidate",
                        risk_level=RiskLevel.medium,
                        requires_confirmation=True,
                        arguments={
                            "title": "给小王补课",
                            "description": "明天下午3点给小王补课",
                            "start_at": tomorrow.replace(hour=15, minute=0, second=0, microsecond=0).isoformat(),
                            "end_at": tomorrow.replace(hour=16, minute=0, second=0, microsecond=0).isoformat(),
                            "confidence": 0.9,
                        },
                    ),
                    AgentToolCall(
                        tool_name="create_task_candidate",
                        risk_level=RiskLevel.medium,
                        requires_confirmation=True,
                        arguments={
                            "title": "把资料发家长",
                            "description": "今晚把资料发家长",
                            "due_at": now.replace(hour=21, minute=0, second=0, microsecond=0).isoformat(),
                            "priority": "P1",
                            "confidence": 0.85,
                        },
                    ),
                ],
            )
        if self._mock_pending_habit(request) and text not in {"确认", "取消"}:
            return AgentResponse(
                intent="create_candidates",
                confidence=0.9,
                reasoning_summary="用户在完善养成卡。",
                reply_to_user="我会更新养成卡。",
                tool_calls=[AgentToolCall(tool_name="refine_habit_plan", risk_level=RiskLevel.low, arguments={"raw_text": text})],
            )
        if any(token in text for token in ("锻炼", "健身", "保持健康", "习惯养成")) and any(token in text for token in ("想", "希望", "帮我", "计划")):
            return AgentResponse(
                intent="create_candidates",
                confidence=0.88,
                reasoning_summary="识别为需要先澄清的习惯养成计划。",
                reply_to_user="我先帮你做一张养成卡，补齐细节后再生成日程确认卡。",
                assistant_proposal=AssistantProposal(
                    kind=PlanDraftKind.habit.value,
                    status=PlanDraftStatus.refining.value,
                    user_goal=text,
                    context_summary="Mock provider generated a habit proposal.",
                    ai_assumptions=["这是习惯养成目标，需要先澄清方式、时长、频率和周期。"],
                    missing_info=["执行方式", "每次时长", "偏好时间", "频率", "持续周期"],
                    candidate_plans=[{"title": text, "details": {}}],
                    risks=["目标过粗时直接排日程容易不符合真实偏好。"],
                    next_step_suggestion="请补充锻炼方式、每次时长、频率和持续周期。",
                    confidence=0.88,
                ),
                tool_calls=[],
            )
        if any(token in text for token in ("长期", "复习", "学习计划", "备考")) and any(
            token in text for token in ("想", "帮我", "计划", "安排", "养成")
        ):
            return AgentResponse(
                intent="create_candidates",
                confidence=0.86,
                reasoning_summary="Mock provider generated a planner proposal for a long-term study goal.",
                reply_to_user="我会先生成计划草案，补齐细节后再给你日程确认卡。",
                assistant_proposal=AssistantProposal(
                    kind=PlanDraftKind.long_term_schedule.value,
                    status=PlanDraftStatus.refining.value,
                    user_goal=text,
                    context_summary="Mock provider generated a long-term study proposal.",
                    ai_assumptions=["这是长期复习目标，需要先明确频率、时长、偏好时间和周期。"],
                    missing_info=["执行方式", "每次时长", "偏好时间", "频率", "持续周期"],
                    candidate_plans=[{"title": text, "details": {"method": "复习"}}],
                    risks=["目标过粗时直接排日程容易不符合真实偏好。"],
                    next_step_suggestion="请补充每天/每周频率、每次时长、偏好时间和持续周期。",
                    confidence=0.86,
                ),
                tool_calls=[],
            )
        return AgentResponse(
            intent="unknown",
            confidence=0.2,
            reasoning_summary="mock provider 无法可靠判断。",
            reply_to_user="我还不确定你要记录、查询还是修改哪件事，可以再说具体一点。",
            tool_calls=[AgentToolCall(tool_name="send_feishu_reply", arguments={"text": "我还不确定你的意思，可以再说具体一点。"})],
        )

    def _mock_pending_habit(self, request: dict[str, Any]) -> bool:
        pending = request.get("pending_confirmations")
        if not isinstance(pending, list):
            return False
        return any(
            isinstance(item, dict) and item.get("confirmation_type") in {"habit_refinement", "habit_schedule"}
            for item in pending
        )

    def _mock_pending_plan(self, request: dict[str, Any]) -> bool:
        pending = request.get("pending_confirmations")
        if not isinstance(pending, list):
            return False
        return any(
            isinstance(item, dict)
            and item.get("confirmation_type") in {"course_timetable_refinement", "course_timetable_schedule", "plan_refinement", "plan_schedule"}
            for item in pending
        )

    def _extract_schedule_blocks(self, text: str) -> list[dict[str, str]]:
        blocks = self._extract_compact_weekly_blocks(text)
        if blocks:
            return self._dedupe_blocks(blocks)
        segments = self._day_segments(text)
        for day, segment in segments.items():
            blocks.extend(self._extract_day_blocks(day, segment))
        if "每晚" in text and "睡觉" in text and not any(block["title"] == "睡觉" for block in blocks):
            blocks.append(
                {
                    "title": "睡觉",
                    "recurrence_rule": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR,SA,SU",
                    "start_time": "00:00",
                    "end_time": "08:00",
                    "timezone": "Asia/Shanghai",
                }
            )
        return self._dedupe_blocks(blocks)

    def _availability_args(self, text: str) -> dict[str, str]:
        args: dict[str, str] = {}
        if "明天" in text:
            args["day"] = "tomorrow"
        elif "今天" in text:
            args["day"] = "today"
        elif "周六" in text:
            args["day"] = "saturday"
        elif "周天" in text or "周日" in text:
            args["day"] = "sunday"
        elif "周一" in text:
            args["day"] = "monday"
        elif "周二" in text:
            args["day"] = "tuesday"
        elif "周三" in text:
            args["day"] = "wednesday"
        elif "周四" in text:
            args["day"] = "thursday"
        elif "周五" in text:
            args["day"] = "friday"
        else:
            args["day"] = "tomorrow"
        if "下午" in text:
            args["window_start"] = "12:00"
            args["window_end"] = "18:00"
        else:
            args["window_start"] = "08:00"
            args["window_end"] = "24:00"
        if "被占" in text:
            args["focus"] = "busy"
        elif "能不能安排" in text:
            args["focus"] = "can_schedule"
        else:
            args["focus"] = "free"
        return args

    def _extract_compact_weekly_blocks(self, text: str) -> list[dict[str, str]]:
        blocks: list[dict[str, str]] = []
        if "一三五" in text:
            blocks.append(self._block("固定上课", "MO,WE,FR", "19:00", "21:00"))
        if "周二" in text and "实验课" in text and ("一三五" in text or "2点到5" in text or "2:00到5" in text):
            blocks.append(self._block("实验课", "TU", "14:00", "17:00"))
        return blocks

    def _day_segments(self, text: str) -> dict[str, str]:
        matches = list(re.finditer(r"周[一二三四五六日天]", text))
        segments: dict[str, str] = {}
        for index, match in enumerate(matches):
            day = match.group(0)
            start = match.start()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            segments[day] = text[start:end]
        return segments

    def _extract_day_blocks(self, day: str, segment: str) -> list[dict[str, str]]:
        day_code = DAY_CODES[day]
        blocks: list[dict[str, str]] = []
        external_matches = list(
            re.finditer(
                rf"(?P<start>{TIME_TOKEN}).{{0,12}}出发.*?(?P<end>{TIME_TOKEN}).{{0,8}}回来",
                segment,
            )
        )
        external_spans = [match.span() for match in external_matches]
        for match in external_matches:
            title = f"{day}驾校" if "驾校" in match.group(0) else f"{day}家教/外出"
            start, end = self._match_times(segment, match, default_title_period="下午")
            blocks.append(self._block(title, day_code, start, end))

        for match in re.finditer(
            rf"(?P<start>{TIME_TOKEN}).{{0,16}}(?:到|上到)(?P<end>{TIME_TOKEN}).{{0,10}}(?:有课|上课|课)",
            segment,
        ):
            if any(match.start() >= span[0] and match.end() <= span[1] for span in external_spans):
                continue
            if "出发" in match.group(0) or "回来" in match.group(0):
                continue
            start, end = self._match_times(segment, match, default_title_period="上午")
            if start == end:
                continue
            blocks.append(self._block(f"{day}上课", day_code, start, end))

        if "上午有课" in segment and not any(block["title"] == f"{day}上课" for block in blocks):
            blocks.append(self._block(f"{day}上课", day_code, "08:00", "12:00"))
        return blocks

    def _match_times(self, segment: str, match: re.Match, *, default_title_period: str) -> tuple[str, str]:
        start_context = segment[max(0, match.start("start") - 12) : match.start("start")]
        end_context = segment[max(0, match.start("end") - 12) : match.start("end")]
        start_period = self._period_for(match.group("start"), start_context, default_title_period)
        end_period = self._period_for(match.group("end"), end_context, start_period)
        return self._parse_time(match.group("start"), start_period), self._parse_time(match.group("end"), end_period)

    def _period_for(self, token: str, context: str, default: str) -> str:
        combined = f"{context}{token}"
        matches = [(combined.rfind(period), period) for period in ("上午", "早上", "下午", "晚上", "中午", "晚")]
        matches = [item for item in matches if item[0] >= 0]
        if matches:
            return max(matches, key=lambda item: item[0])[1]
        return default

    def _parse_time(self, token: str, period: str) -> str:
        found = re.search(r"(\d{1,2})(?:(?::|：|\.)(\d{1,2})|点(\d{1,2})?)?", token)
        if not found:
            return "00:00"
        hour = int(found.group(1))
        minute = int(found.group(2) or found.group(3) or 0)
        if period in {"下午", "晚上", "晚", "中午"} and hour < 12:
            hour += 12
        if period in {"上午", "早上"} and hour == 12:
            hour = 12
        return f"{hour:02d}:{minute:02d}"

    def _block(self, title: str, byday: str, start: str, end: str) -> dict[str, str]:
        return {
            "title": title,
            "recurrence_rule": f"FREQ=WEEKLY;BYDAY={byday}",
            "start_time": start,
            "end_time": end,
            "timezone": "Asia/Shanghai",
        }

    def _dedupe_blocks(self, blocks: list[dict[str, str]]) -> list[dict[str, str]]:
        seen: set[tuple[str, str, str, str]] = set()
        out: list[dict[str, str]] = []
        for block in blocks:
            key = (block["title"], block["recurrence_rule"], block["start_time"], block["end_time"])
            if key not in seen:
                seen.add(key)
                out.append(block)
        return out


class CodexCliProvider:
    name = "codex_cli_provider"

    def __init__(self, command_path: str, timeout_seconds: int = 300, workdir: str | None = None, model: str | None = None):
        self.command_path = command_path
        self.timeout_seconds = timeout_seconds
        self.workdir = workdir
        self.model = model
        self.schema_path = Path(__file__).with_name("agent_response_schema.json")

    def run(self, request: dict[str, Any]) -> AgentResponse:
        path = Path(self.command_path)
        if not path.exists():
            raise CoreAgentProviderUnavailable(f"Codex CLI not found: {self.command_path}")
        prompt = self._prompt(request)
        with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False, encoding="utf-8") as output:
            output_path = Path(output.name)
        args = [
            "-a",
            "never",
            "-c",
            'model_reasoning_effort="low"',
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--output-schema",
            str(self.schema_path),
            "--output-last-message",
            str(output_path),
        ]
        command = self._command(path, args)
        try:
            completed = subprocess.run(
                command,
                input=prompt,
                text=True,
                encoding="utf-8",
                env=self._env(),
                capture_output=True,
                cwd=self.workdir,
                timeout=self.timeout_seconds,
                check=False,
            )
            if completed.returncode != 0:
                raise CoreAgentProviderError(
                    f"Codex CLI failed with {completed.returncode}: {completed.stderr or completed.stdout}"
                )
            text = output_path.read_text(encoding="utf-8").strip() or completed.stdout.strip()
            try:
                return AgentResponse.model_validate(json.loads(text))
            except (json.JSONDecodeError, ValidationError) as exc:
                raise CoreAgentProviderError(f"invalid AgentResponse JSON: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise CoreAgentProviderUnavailable("Codex CLI timed out") from exc
        finally:
            output_path.unlink(missing_ok=True)

    def _command(self, path: Path, args: list[str]) -> list[str]:
        if path.suffix.lower() == ".ps1":
            return ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path), *args]
        return [str(path), *args]

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["LC_ALL"] = "C.UTF-8"
        env["LANG"] = "C.UTF-8"
        return env

    def _prompt(self, request: dict[str, Any]) -> str:
        return (
            "你是飞书私人助理的 Agent Orchestrator。只输出符合 schema 的 JSON。\n"
            "LLM 不允许写数据库，只能输出 tool_calls。查询类消息不能创建任务。"
            "删除、批量修改、低置信度修改、重复日历创建必须确认。\n\n"
            f"AgentRequest:\n{json.dumps(request, ensure_ascii=True, indent=2, default=str)}\n"
        )


class OpenAICompatibleChatProvider:
    name = "openai_compatible_provider"

    def __init__(
        self,
        *,
        base_url: str,
        model: str | None,
        api_key: str | None = None,
        timeout_seconds: int = 120,
        response_format: str = "json_object",
        max_tokens: int | None = None,
        max_image_bytes: int = 8 * 1024 * 1024,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.response_format = response_format.lower()
        self.max_tokens = max_tokens
        self.max_image_bytes = max_image_bytes
        self.schema_path = Path(__file__).with_name("agent_response_schema.json")

    def run(self, request: dict[str, Any]) -> AgentResponse:
        model = self.model or self._first_available_model()
        payload: dict[str, Any] = {
            "model": model,
            "messages": self._messages(request),
            "temperature": 0.1,
            "stream": False,
        }
        self._apply_chat_options(payload)
        formatted = self._with_response_format(payload)
        data = self._post_chat_completion(formatted, fallback_payload=payload if formatted is not payload else None)
        content = self._message_content(data)
        try:
            intent = self._parse_model_intent(content, allow_repair=True)
        except (CoreAgentProviderError, json.JSONDecodeError, ValidationError) as exc:
            return self._invalid_model_response(exc)
        intent = self._adjudicate_time_budget_intent(intent, request, model)
        intent = self._refine_entities(intent, request, model)
        return self._intent_to_agent_response(intent, request)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _first_available_model(self) -> str:
        try:
            data = self._request_json("GET", f"{self.base_url}/models")
        except (CoreAgentProviderError, ValueError) as exc:
            raise CoreAgentProviderUnavailable(
                f"{self.name} could not list models at {self.base_url}/models; set LM_STUDIO_MODEL explicitly or start LM Studio server"
            ) from exc
        models = data.get("data") if isinstance(data, dict) else None
        if not isinstance(models, list) or not models:
            raise CoreAgentProviderUnavailable(f"{self.name} returned no models from {self.base_url}/models")
        model_id = models[0].get("id") if isinstance(models[0], dict) else None
        if not model_id:
            raise CoreAgentProviderUnavailable(f"{self.name} returned a model without an id")
        self.model = str(model_id)
        return self.model

    def _post_chat_completion(
        self,
        payload: dict[str, Any],
        *,
        fallback_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            return self._request_json("POST", f"{self.base_url}/chat/completions", payload)
        except CoreAgentProviderError as first_exc:
            if fallback_payload is not None:
                try:
                    return self._request_json("POST", f"{self.base_url}/chat/completions", fallback_payload)
                except CoreAgentProviderError as fallback_exc:
                    raise fallback_exc from first_exc
            raise first_exc
        except TimeoutError as exc:
            raise CoreAgentProviderUnavailable(
                f"{self.name} is unavailable at {self.base_url}; start LM Studio Local Server"
            ) from exc
        except ValueError as exc:
            raise CoreAgentProviderError(f"{self.name} chat completion failed: {exc}") from exc

    def _request_json(self, method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=body, headers=self._headers(), method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            message = f"{self.name} HTTP {exc.code} from {url}"
            if detail:
                message = f"{message}: {detail}"
            raise CoreAgentProviderError(message) from exc
        except urllib.error.URLError as exc:
            raise CoreAgentProviderUnavailable(
                f"{self.name} is unavailable at {self.base_url}; start LM Studio Local Server"
            ) from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CoreAgentProviderError(f"{self.name} returned non-JSON response from {url}") from exc
        if not isinstance(data, dict):
            raise CoreAgentProviderError(f"{self.name} returned a non-object response from {url}")
        return data

    def _with_response_format(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.response_format in {"", "none", "off", "disabled"}:
            return payload
        payload = {**payload}
        if self.response_format == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "model_intent",
                    "strict": True,
                    "schema": ModelIntent.model_json_schema(),
                },
            }
        else:
            payload["response_format"] = {"type": "json_object"}
        return payload

    def _apply_chat_options(self, payload: dict[str, Any]) -> None:
        if self.max_tokens is not None:
            payload["max_tokens"] = self.max_tokens

    def _adjudicate_time_budget_intent(self, intent: ModelIntent, request: dict[str, Any], model: str) -> ModelIntent:
        intent_name = self._normalize_intent_name(intent.intent, str(request.get("raw_text") or ""))
        if intent_name not in {"query_time_budget_plan", "schedule_time_budget_plan"}:
            return intent
        if not request.get("recent_assistant_turns"):
            return intent
        payload: dict[str, Any] = {
            "model": model,
            "messages": self._time_budget_adjudication_messages(intent, request),
            "temperature": 0.0,
            "stream": False,
        }
        self._apply_chat_options(payload)
        formatted = self._with_response_format(payload)
        try:
            data = self._post_chat_completion(formatted, fallback_payload=payload if formatted is not payload else None)
            adjudicated = self._parse_model_intent(
                self._message_content(data),
                fallback_confidence=intent.confidence,
                allow_repair=True,
            )
        except (CoreAgentProviderError, json.JSONDecodeError, ValidationError):
            return intent
        adjudicated_name = self._normalize_intent_name(adjudicated.intent, str(request.get("raw_text") or ""))
        if adjudicated_name not in {"query_time_budget_plan", "schedule_time_budget_plan"}:
            return intent
        original_entities = intent.entities if isinstance(intent.entities, dict) else {}
        adjudicated_entities = adjudicated.entities if isinstance(adjudicated.entities, dict) else {}
        return ModelIntent(
            intent=adjudicated_name,
            confidence=adjudicated.confidence,
            reply=adjudicated.reply if adjudicated.reply is not None else intent.reply,
            entities={**original_entities, **adjudicated_entities},
            needs_confirmation=adjudicated.needs_confirmation,
            reasoning_summary=adjudicated.reasoning_summary or intent.reasoning_summary,
        )

    def _time_budget_adjudication_messages(self, intent: ModelIntent, request: dict[str, Any]) -> list[dict[str, str]]:
        context = {
            "raw_text": request.get("raw_text"),
            "recent_user_messages": self._compact_recent_messages(request.get("recent_user_messages"), limit=3),
            "recent_assistant_turns": self._compact_assistant_turns(request.get("recent_assistant_turns"), limit=2),
            "long_term_tasks": self._compact_items(request.get("long_term_tasks"), limit=8),
            "first_stage_intent": intent.model_dump(mode="json", exclude_none=True),
        }
        return [
            {
                "role": "system",
                "content": (
                    "Decide between exactly two intents for a cumulative time-budget task. Return JSON only. "
                    "Use query_time_budget_plan when the user is only asking for current status or existing slots. "
                    "Use schedule_time_budget_plan when the user asks to create, split, arrange, write, or proceed with calendar slots. "
                    "If the assistant just said the task has no calendar slots yet and must be split into schedule candidates, "
                    "and the current user asks to do that, choose schedule_time_budget_plan."
                ),
            },
            {
                "role": "user",
                "content": (
                    "Return {\"intent\":\"query_time_budget_plan\"|\"schedule_time_budget_plan\","
                    "\"confidence\":0-1,\"entities\":{},\"reasoning_summary\":\"short\"}.\n"
                    f"Context JSON:\n{json.dumps(context, ensure_ascii=False, separators=(',', ':'), default=str)}"
                ),
            },
        ]

    def _messages(self, request: dict[str, Any]) -> list[dict[str, Any]]:
        context = self._intent_context(request)
        intents = ", ".join(context.get("available_intents") or [])
        user_text = (
            f"Intent options: {intents}\n"
            "Intent guide: read-only questions use query_*; creating/updating/canceling data needs confirmation; "
            "a new cumulative hours goal is create_time_budget_plan; asking where an existing hours goal is scheduled is query_time_budget_plan; "
            "putting an existing hours goal into calendar is schedule_time_budget_plan; recurring unavailable time is create_schedule_block; "
            "if fixed or weekly arrangements should remain but no longer remind, use disable_schedule_block_reminders, not cancel_schedule_block; "
            "course timetable image imports are start_plan_refinement with kind=course_timetable, not time-budget plans; "
            "course timetables, school schedules, classes, lessons, and fixed weekly availability are schedule blocks or calendar events, not long-term study tasks; "
            "unclear long-term habits or schedules should become plan drafts before calendar writes; "
            "Use recent_assistant_turns and recent_user_messages to resolve follow-ups; short messages may provide context for a recent attachment or plan; "
            "If the user corrects your interpretation, classify according to the corrected meaning and do not repeat the rejected meaning; "
            "inspect attached images when present, but do not treat attachment-only messages as confirmation or cancellation; "
            f"before {DAY_ROLLOVER_HOUR:02d}:00, relative dates follow the user's pre-sleep day.\n"
            "Return a compact object such as {\"intent\":\"create_time_budget_plan\",\"confidence\":0.9,\"entities\":{}}.\n\n"
            f"Context JSON:\n{json.dumps(context, ensure_ascii=False, separators=(',', ':'), default=str)}"
        )
        return [
            {
                "role": "system",
                "content": (
                    "Classify one Feishu assistant message. Return JSON only. "
                    "Required keys: intent, confidence. Optional keys: reply, entities, needs_confirmation, reasoning_summary. "
                    "Do not call tools or write data; the backend executes after validation. "
                    "If the message has image content, use the image as ordinary user-provided context."
                ),
            },
            {
                "role": "user",
                "content": self._chat_user_content(user_text, request),
            },
        ]

    def _refine_entities(self, intent: ModelIntent, request: dict[str, Any], model: str) -> ModelIntent:
        intent_name = self._normalize_intent_name(intent.intent, str(request.get("raw_text") or ""))
        if intent_name in {"confirm", "cancel", "smalltalk", "clarify", "unknown"}:
            return intent
        payload: dict[str, Any] = {
            "model": model,
            "messages": self._entity_messages(intent, request),
            "temperature": 0.0,
            "stream": False,
        }
        self._apply_chat_options(payload)
        formatted = self._with_response_format(payload)
        try:
            data = self._post_chat_completion(formatted, fallback_payload=payload if formatted is not payload else None)
            refined = self._parse_model_intent(
                self._message_content(data),
                fixed_intent=intent_name,
                fallback_confidence=intent.confidence,
                allow_repair=True,
            )
        except (CoreAgentProviderError, json.JSONDecodeError, ValidationError) as exc:
            summary = intent.reasoning_summary or "First-stage intent accepted."
            return ModelIntent(
                intent=intent.intent,
                confidence=intent.confidence,
                reply=intent.reply,
                entities=intent.entities if isinstance(intent.entities, dict) else {},
                needs_confirmation=intent.needs_confirmation,
                reasoning_summary=f"{summary} Entity refinement skipped: {exc}",
            )
        original_entities = intent.entities if isinstance(intent.entities, dict) else {}
        refined_entities = refined.entities if isinstance(refined.entities, dict) else {}
        return ModelIntent(
            intent=intent.intent,
            confidence=min(intent.confidence, refined.confidence),
            reply=refined.reply if refined.reply is not None else intent.reply,
            entities={**original_entities, **refined_entities},
            needs_confirmation=refined.needs_confirmation if refined.needs_confirmation is not None else intent.needs_confirmation,
            reasoning_summary=refined.reasoning_summary or intent.reasoning_summary,
        )

    def _entity_messages(self, intent: ModelIntent, request: dict[str, Any]) -> list[dict[str, Any]]:
        intent_name = self._normalize_intent_name(intent.intent, str(request.get("raw_text") or ""))
        context = self._entity_context(request, intent_name)
        user_text = (
            f"Fixed intent: {intent_name}\n"
            "Return JSON:\n"
            "{\n"
            f'  "intent": "{intent_name}",\n'
            '  "confidence": number from 0 to 1,\n'
            '  "reply": optional short Chinese reply,\n'
            '  "entities": object\n'
            "}\n\n"
            "Entity requirements by intent:\n"
            "- schedule_time_budget_plan/query_time_budget_plan: choose action_item_id from long_term_tasks when possible; include query only if no id is available; optional daily_minutes, session_minutes, min_session_minutes, buffer_minutes, window_start, window_end.\n"
            "- start_plan_refinement/refine_plan_draft: kind, raw_text, optional plan_id, attachment_refs, extracted_payload. For course_timetable include period_map, term_anchor, courses when visible.\n"
            "- generate_plan_schedule_confirmation: plan_id when an active plan draft is ready.\n"
            "- create_time_budget_plan: title, estimated_minutes, due_at, optional start_date/description.\n"
            "- create_task/update_task/cancel_task/complete_task: title or query, action_item_id if an existing task is referenced, plus changed fields.\n"
            "- create_calendar_event/update_calendar_event/cancel_calendar_event: title/query, calendar_event_id when possible, start_at, end_at, location.\n"
            "- create_schedule_block/update_schedule_block/cancel_schedule_block: blocks or schedule_block_id/query and changed fields. Each new block needs title, recurrence_rule, start_time, end_time, timezone.\n"
            "- disable_schedule_block_reminders: scope ('all' for all fixed arrangements) or schedule_block_id/query. Do not cancel the schedule block.\n"
            "- query_availability: day, window_start, window_end, focus.\n\n"
            f"First-stage intent JSON:\n{intent.model_dump_json(exclude_none=True)}\n\n"
            f"Context JSON:\n{json.dumps(context, ensure_ascii=False, separators=(',', ':'), default=str)}"
        )
        return [
            {
                "role": "system",
                "content": (
                    "You are the second-stage entity extractor for a Feishu personal assistant. "
                    "The intent is already fixed. Return exactly one JSON object. "
                    "The object may contain only intent, confidence, entities, and reply. Keep reply empty unless clarification is needed. "
                    "Extract only structured entities needed by that intent. Prefer ids from AgentContextPack when an existing item is referenced. "
                    "Do not invent ids, tools, Feishu cards, database writes, or completed-action wording."
                ),
            },
            {
                "role": "user",
                "content": self._chat_user_content(user_text, request),
            },
        ]

    def _intent_context(self, request: dict[str, Any]) -> dict[str, Any]:
        return {
            "raw_text": request.get("raw_text"),
            "content_type": request.get("content_type"),
            "attachment_refs": self._compact_attachments(request.get("attachment_refs")),
            "now": request.get("now"),
            "recent_user_messages": self._compact_recent_messages(request.get("recent_user_messages"), limit=2),
            "recent_assistant_turns": self._compact_assistant_turns(request.get("recent_assistant_turns"), limit=2),
            "pending_confirmations": self._compact_pending_confirmations(request.get("pending_confirmations"), limit=2),
            "active_plan_drafts": self._compact_items(request.get("active_plan_drafts"), limit=3),
            "context_capsules": self._compact_context_capsules(request, limit=5),
            "long_term_tasks": self._compact_items(request.get("long_term_tasks"), limit=5),
            "available_intents": request.get("available_intents") or [],
        }

    def _entity_context(self, request: dict[str, Any], intent_name: str) -> dict[str, Any]:
        context: dict[str, Any] = {
            "raw_text": request.get("raw_text"),
            "content_type": request.get("content_type"),
            "attachment_refs": self._compact_attachments(request.get("attachment_refs")),
            "now": request.get("now"),
            "recent_user_messages": self._compact_recent_messages(request.get("recent_user_messages"), limit=3),
            "recent_assistant_turns": self._compact_assistant_turns(request.get("recent_assistant_turns"), limit=2),
            "pending_confirmations": self._compact_pending_confirmations(request.get("pending_confirmations"), limit=3),
            "active_plan_drafts": self._compact_items(request.get("active_plan_drafts"), limit=3),
            "context_capsules": self._compact_context_capsules(request, intent_name=intent_name, limit=6),
            "long_term_tasks": self._compact_items(request.get("long_term_tasks"), limit=8),
        }
        if intent_name in {
            "update_calendar_event",
            "cancel_calendar_event",
            "update_schedule_block",
            "cancel_schedule_block",
            "query_availability",
            "query_today_plan",
            "query_tomorrow_plan",
            "query_week_plan",
        }:
            context["today"] = self._compact_items(request.get("today"), limit=4)
            context["tomorrow"] = self._compact_items(request.get("tomorrow"), limit=4)
            context["next_7_days"] = self._compact_items(request.get("next_7_days"), limit=6)
            context["schedule_blocks"] = self._compact_items(request.get("schedule_blocks"), limit=6)
        return context

    def _chat_user_content(self, text: str, request: dict[str, Any]) -> str | list[dict[str, Any]]:
        image_parts = self._image_content_parts(request)
        if not image_parts:
            return text
        return [{"type": "text", "text": text}, *image_parts]

    def _image_content_parts(self, request: dict[str, Any]) -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = []
        seen: set[str] = set()
        for attachment in self._vision_attachments(request):
            data_url = self._attachment_data_url(attachment)
            if not data_url or data_url in seen:
                continue
            seen.add(data_url)
            parts.append({"type": "image_url", "image_url": {"url": data_url}})
            if len(parts) >= 3:
                break
        return parts

    def _vision_attachments(self, request: dict[str, Any]) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        current = request.get("attachment_refs")
        if isinstance(current, list):
            candidates.extend(item for item in current if isinstance(item, dict))
        recent = request.get("recent_user_messages")
        if isinstance(recent, list):
            for message in recent[:2]:
                if not isinstance(message, dict):
                    continue
                attachments = message.get("attachment_refs")
                if isinstance(attachments, list):
                    candidates.extend(item for item in attachments if isinstance(item, dict))
        out: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        for item in candidates:
            if str(item.get("kind") or "") != "image":
                continue
            local_path = str(item.get("local_path") or "")
            if not local_path or local_path in seen_paths:
                continue
            seen_paths.add(local_path)
            out.append(item)
        return out

    def _attachment_data_url(self, attachment: dict[str, Any]) -> str | None:
        local_path = attachment.get("local_path")
        if not isinstance(local_path, str) or not local_path:
            return None
        path = Path(local_path)
        try:
            size = path.stat().st_size
        except OSError:
            return None
        if size <= 0 or size > self.max_image_bytes:
            return None
        try:
            data = path.read_bytes()
        except OSError:
            return None
        mime_type = str(attachment.get("mime_type") or self._mime_from_path(path))
        encoded = base64.b64encode(data).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    def _mime_from_path(self, path: Path) -> str:
        suffix = path.suffix.lower()
        return {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".gif": "image/gif",
            ".webp": "image/webp",
            ".bmp": "image/bmp",
        }.get(suffix, "image/png")

    def _compact_attachments(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        allowed = {
            "kind",
            "image_key",
            "file_key",
            "file_name",
            "filename",
            "mime_type",
            "size_bytes",
            "download_status",
            "download_error",
            "local_path",
        }
        attachments: list[dict[str, Any]] = []
        for item in value[:3]:
            if not isinstance(item, dict):
                continue
            compact = {}
            for key in allowed:
                item_value = item.get(key)
                if item_value is None or item_value == "" or item_value == []:
                    continue
                compact[key] = item_value
            if compact:
                attachments.append(compact)
        return attachments

    def _compact_recent_messages(self, value: Any, *, limit: int) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        messages: list[dict[str, Any]] = []
        for item in value[:limit]:
            if not isinstance(item, dict):
                continue
            messages.append(
                {
                    "raw_text": self._short_text(item.get("raw_text"), 120),
                    "content_type": item.get("content_type"),
                    "attachment_refs": self._compact_attachments(item.get("attachment_refs")),
                    "created_at": item.get("created_at"),
                }
            )
        return messages

    def _compact_assistant_turns(self, value: Any, *, limit: int) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        turns: list[dict[str, Any]] = []
        for item in value[:limit]:
            if not isinstance(item, dict):
                continue
            turns.append(
                {
                    "intent": item.get("intent"),
                    "reply_text": self._short_text(item.get("reply_text"), 140),
                    "tool_names": (item.get("tool_names") or [])[:5],
                    "created_at": item.get("created_at"),
                }
            )
        return turns

    def _compact_pending_confirmations(self, value: Any, *, limit: int) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        confirmations: list[dict[str, Any]] = []
        for item in value[:limit]:
            if not isinstance(item, dict):
                continue
            confirmations.append(
                {
                    "id": item.get("id"),
                    "type": item.get("confirmation_type"),
                    "status": item.get("status"),
                    "candidate_count": item.get("candidate_count"),
                    "candidate_titles": [
                        self._short_text(title, 60)
                        for title in (item.get("candidate_titles") or [])[:3]
                    ],
                }
            )
        return confirmations

    def _compact_context_capsules(self, request: dict[str, Any], *, limit: int, intent_name: str | None = None) -> list[dict[str, Any]]:
        return render_provider_capsules(
            request.get("context_v2"),
            raw_text=request.get("raw_text"),
            intent_name=intent_name,
            stage="entity" if intent_name else "intent",
            limit=limit,
        )

    def _compact_items(self, value: Any, *, limit: int) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        items: list[dict[str, Any]] = []
        fields = (
            "id",
            "kind",
            "title",
            "status",
            "start_at",
            "end_at",
            "due_at",
            "display_time",
            "recurrence_rule",
            "estimated_minutes",
            "missing_fields",
            "payload_summary",
        )
        for item in value[:limit]:
            if not isinstance(item, dict):
                continue
            compact: dict[str, Any] = {}
            for key in fields:
                item_value = item.get(key)
                if item_value is None or item_value == "" or item_value == []:
                    continue
                compact[key] = item_value
            if "title" in compact:
                compact["title"] = self._short_text(compact["title"], 80)
            items.append(compact)
        return items

    def _short_text(self, value: Any, limit: int) -> str:
        text = str(value or "").strip()
        if len(text) <= limit:
            return text
        return f"{text[:limit]}..."

    def _should_refine_habit_plan(self, raw_text: str, request: dict[str, Any], intent_name: str) -> bool:
        if intent_name in {"confirm", "cancel"} or self._is_plain_confirmation_text(raw_text):
            return False
        return self._pending_habit_confirmation_type(request) in {"habit_refinement", "habit_schedule"}

    def _pending_habit_confirmation_type(self, request: dict[str, Any]) -> str | None:
        pending = request.get("pending_confirmations")
        if not isinstance(pending, list):
            return None
        for item in pending:
            if not isinstance(item, dict):
                continue
            confirmation_type = str(item.get("confirmation_type") or item.get("type") or "")
            if confirmation_type in {"habit_refinement", "habit_schedule"}:
                return confirmation_type
        return None

    def _pending_plan_confirmation_type(self, request: dict[str, Any]) -> str | None:
        pending = request.get("pending_confirmations")
        if not isinstance(pending, list):
            return None
        plan_types = {
            "habit_refinement",
            "habit_schedule",
            "course_timetable_refinement",
            "course_timetable_schedule",
            "plan_refinement",
            "plan_schedule",
        }
        for item in pending:
            if not isinstance(item, dict):
                continue
            confirmation_type = str(item.get("confirmation_type") or item.get("type") or "")
            if confirmation_type in plan_types:
                return confirmation_type
        return None

    def _active_plan_draft(self, request: dict[str, Any]) -> dict[str, Any] | None:
        drafts = request.get("active_plan_drafts")
        if not isinstance(drafts, list):
            return None
        for item in drafts:
            if isinstance(item, dict):
                return item
        return None

    def _should_refine_plan_draft(self, raw_text: str, request: dict[str, Any], intent_name: str) -> bool:
        if intent_name in {"confirm", "cancel"} or self._is_plain_confirmation_text(raw_text):
            return False
        pending_type = self._pending_plan_confirmation_type(request)
        if pending_type and pending_type not in {"habit_refinement", "habit_schedule"}:
            return True
        draft = self._active_plan_draft(request)
        return bool(draft and draft.get("kind") != "habit")

    def _pending_or_active_plan_id(self, request: dict[str, Any]) -> str | None:
        draft = self._active_plan_draft(request)
        if isinstance(draft, dict) and draft.get("id"):
            return str(draft["id"])
        return None

    def _looks_like_course_timetable_request(self, raw_text: str, request: dict[str, Any]) -> bool:
        text = str(raw_text or "")
        if any(token in text for token in ("课程表", "课表", "上课时间", "节次", "第几周", "教学周")):
            return True
        if self._looks_like_correction_to_schedule(text):
            return True
        has_image = self._current_image_attachments_have_local_bytes(request)
        return bool(has_image and any(token in text for token in ("安排进日程", "日程", "课程", "上课", "图片")))

    def _looks_like_disable_schedule_block_reminders(self, raw_text: str) -> bool:
        return _looks_like_disable_schedule_block_reminders_text(raw_text)

    def _plan_refinement_args(
        self,
        raw_text: str,
        request: dict[str, Any],
        entities: dict[str, Any],
        *,
        kind: str,
    ) -> dict[str, Any]:
        extracted_payload = entities.get("extracted_payload") or entities.get("payload") or entities.get(kind)
        if not isinstance(extracted_payload, dict):
            extracted_payload = {}
        for key in ("period_map", "term_anchor", "courses", "confidence", "title"):
            if key in entities and key not in extracted_payload:
                extracted_payload[key] = entities[key]
        return {
            "kind": kind,
            "raw_text": raw_text,
            "plan_id": self._entity_str(entities, "plan_id") or self._pending_or_active_plan_id(request),
            "attachment_refs": request.get("attachment_refs") or [],
            "extracted_payload": extracted_payload,
        }

    def _looks_like_habit_plan_request(self, raw_text: str) -> bool:
        text = str(raw_text or "").strip()
        if not text or self._looks_like_time_budget_plan(text):
            return False
        habit_terms = (
            "习惯",
            "养成",
            "坚持",
            "保持",
            "锻炼",
            "运动",
            "健身",
            "健康",
            "早睡",
            "早起",
            "阅读",
            "读书",
            "背单词",
            "冥想",
        )
        request_terms = ("想", "希望", "打算", "我要", "帮我", "安排", "计划", "长期")
        if any(term in text for term in habit_terms) and any(term in text for term in request_terms):
            return True
        if any(term in text for term in ("长期任务", "长期计划", "长期安排", "长期日程")):
            return True
        return bool(re.search(r"(每天|每日|每周).{0,12}(锻炼|运动|跑步|阅读|背单词|冥想)", text))

    def _is_plain_confirmation_text(self, raw_text: str) -> bool:
        return str(raw_text or "").strip() in {"确认", "是的", "可以", "确定", "OK", "ok", "取消", "不用了", "算了"}

    def _is_attachment_only_message(self, raw_text: str, request: dict[str, Any]) -> bool:
        text = str(raw_text or "").strip()
        if text and not re.fullmatch(r"\[(?:image|file|audio) attachment\]", text):
            return False
        content_type = str(request.get("content_type") or "")
        attachments = request.get("attachment_refs")
        return content_type in {"image", "file", "audio"} or bool(attachments)

    def _unreadable_attachment_only_message(self, raw_text: str, request: dict[str, Any]) -> bool:
        if not self._is_attachment_only_message(raw_text, request):
            return False
        content_type = str(request.get("content_type") or "")
        if content_type == "image":
            return not self._current_image_attachments_have_local_bytes(request)
        return content_type in {"file", "audio"}

    def _current_image_attachments_have_local_bytes(self, request: dict[str, Any]) -> bool:
        attachments = request.get("attachment_refs")
        if not isinstance(attachments, list):
            return False
        for attachment in attachments:
            if not isinstance(attachment, dict) or str(attachment.get("kind") or "") != "image":
                continue
            local_path = attachment.get("local_path")
            if isinstance(local_path, str) and local_path and Path(local_path).exists():
                return True
        return False

    def _unreadable_attachment_reply(self, request: dict[str, Any]) -> str:
        errors = self._attachment_download_errors(request.get("attachment_refs"))
        if errors:
            return f"我收到了附件，但当前读不到内容：{errors[0]}。请开通飞书消息资源读取权限后重发，或直接把图片里的关键信息发成文字。"
        return "我收到了附件，但当前还没有可读内容。请重发可读取的图片，或直接把图片里的关键信息发成文字。"

    def _attachment_download_errors(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        errors: list[str] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            error = item.get("download_error")
            if error:
                errors.append(self._short_text(error, 180))
        return errors

    def _single_long_term_task_id(self, request: dict[str, Any]) -> str | None:
        tasks = request.get("long_term_tasks")
        if not isinstance(tasks, list) or len(tasks) != 1:
            return None
        item = tasks[0]
        if not isinstance(item, dict):
            return None
        task_id = item.get("id")
        return str(task_id) if task_id else None

    def _looks_like_correction_to_schedule(self, raw_text: str) -> bool:
        text = str(raw_text or "")
        if "不是" not in text:
            return False
        return any(token in text for token in ("日程", "课程表", "课表", "上课", "固定安排"))

    def _normalized_schedule_blocks(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        blocks: list[dict[str, Any]] = []
        for raw in value:
            if not isinstance(raw, dict):
                continue
            block = dict(raw)
            if not block.get("start_time") and block.get("start_at"):
                block["start_time"] = self._time_only(block.get("start_at"))
            if not block.get("end_time") and block.get("end_at"):
                block["end_time"] = self._time_only(block.get("end_at"))
            block.setdefault("timezone", "Asia/Shanghai")
            if not block.get("title") or not block.get("recurrence_rule") or not block.get("start_time") or not block.get("end_time"):
                continue
            blocks.append(
                {
                    key: block[key]
                    for key in ("title", "recurrence_rule", "start_time", "end_time", "timezone")
                    if block.get(key)
                }
            )
        return blocks

    def _time_only(self, value: Any) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        match = re.search(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)", text)
        if not match:
            return None
        return f"{int(match.group(1)):02d}:{match.group(2)}"

    def _compact_request(self, request: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "context_schema_version",
            "project_brief",
            "safety_rules",
            "raw_text",
            "content_type",
            "sender_id",
            "chat_id",
            "capture_id",
            "source",
            "source_message_id",
            "now",
            "attachment_refs",
            "recent_user_messages",
            "recent_assistant_turns",
            "pending_confirmations",
            "active_plan_drafts",
            "today",
            "tomorrow",
            "next_7_days",
            "long_term_tasks",
            "schedule_blocks",
            "available_intents",
            "context_limits",
            "context_v2",
        }
        return {key: request.get(key) for key in allowed if key in request}

    def _intent_to_agent_response(self, intent: ModelIntent, request: dict[str, Any]) -> AgentResponse:
        entities = intent.entities if isinstance(intent.entities, dict) else {}
        raw_text = str(request.get("raw_text") or "")
        reply = (intent.reply or "").strip()
        intent_name = self._normalize_intent_name(intent.intent, raw_text)
        has_complete_time_budget_entities = bool(
            intent_name == "create_time_budget_plan"
            and self._time_budget_plan_from_entities(entities, raw_text, request, intent.confidence)
        )

        if self._looks_like_disable_schedule_block_reminders(raw_text):
            return AgentResponse(
                intent="update_existing",
                confidence=max(intent.confidence, 0.86),
                reasoning_summary="User wants fixed arrangements kept but reminders disabled.",
                reply_to_user=reply or "我会关闭固定安排提醒；安排仍会保留在查询和日历同步中。",
                tool_calls=[
                    AgentToolCall(
                        tool_name="disable_schedule_block_reminders",
                        risk_level=RiskLevel.low,
                        requires_confirmation=False,
                        arguments={"scope": "all", "query": raw_text},
                    )
                ],
            )

        if self._should_refine_plan_draft(raw_text, request, intent_name):
            return AgentResponse(
                intent="create_candidates",
                confidence=max(intent.confidence, 0.82),
                reasoning_summary=intent.reasoning_summary or "User is refining an active plan draft.",
                reply_to_user="我会更新这张长期日程草案；信息够了再生成日程确认卡。",
                tool_calls=[
                    AgentToolCall(
                        tool_name="refine_plan_draft",
                        risk_level=RiskLevel.low,
                        arguments=self._plan_refinement_args(raw_text, request, entities, kind="course_timetable"),
                    )
                ],
            )
        if intent_name in {"start_plan_refinement", "refine_plan_draft"} or self._looks_like_course_timetable_request(raw_text, request):
            tool_name = "refine_plan_draft" if intent_name == "refine_plan_draft" else "start_plan_refinement"
            return AgentResponse(
                intent="create_candidates",
                confidence=max(intent.confidence, 0.82),
                reasoning_summary=intent.reasoning_summary or "User wants to import or refine a course timetable plan draft.",
                reply_to_user="我会先生成课程表草案，确认周次、节次和课程后再写入日历。",
                tool_calls=[
                    AgentToolCall(
                        tool_name=tool_name,
                        risk_level=RiskLevel.low,
                        arguments=self._plan_refinement_args(raw_text, request, entities, kind="course_timetable"),
                    )
                ],
            )
        if intent_name == "generate_plan_schedule_confirmation":
            return AgentResponse(
                intent="create_candidates",
                confidence=max(intent.confidence, 0.82),
                reasoning_summary=intent.reasoning_summary or "User wants to generate a schedule confirmation from a plan draft.",
                reply_to_user="我会根据草案生成日程确认卡。",
                tool_calls=[
                    AgentToolCall(
                        tool_name="generate_plan_schedule_confirmation",
                        risk_level=RiskLevel.low,
                        arguments={"plan_id": self._entity_str(entities, "plan_id") or self._pending_or_active_plan_id(request)},
                    )
                ],
            )
        if self._should_refine_habit_plan(raw_text, request, intent_name):
            return AgentResponse(
                intent="create_candidates",
                confidence=max(intent.confidence, 0.82),
                reasoning_summary=intent.reasoning_summary or "User is refining a pending habit plan.",
                reply_to_user="我会更新养成卡；信息够了就生成日程确认卡。",
                tool_calls=[
                    AgentToolCall(
                        tool_name="refine_habit_plan",
                        risk_level=RiskLevel.low,
                        arguments={"raw_text": raw_text},
                    )
                ],
            )
        if not has_complete_time_budget_entities and self._looks_like_habit_plan_request(raw_text):
            return AgentResponse(
                intent="create_candidates",
                confidence=max(intent.confidence, 0.82),
                reasoning_summary=intent.reasoning_summary or "User wants to build a long-term habit plan.",
                reply_to_user="我先帮你做一张养成卡，补齐细节后再生成日程确认卡。",
                tool_calls=[
                    AgentToolCall(
                        tool_name="start_habit_refinement",
                        risk_level=RiskLevel.low,
                        arguments={"raw_text": raw_text},
                    )
                ],
            )

        if self._unreadable_attachment_only_message(raw_text, request):
            return self._clarify_response(
                intent,
                self._unreadable_attachment_reply(request),
            )

        if intent.confidence < 0.45:
            if self._looks_like_correction_to_schedule(raw_text):
                return self._clarify_response(
                    intent,
                    "明白，这是课程表/固定日程安排，我不会按长期学习任务处理。当前还缺可读的课程表图片内容或每门课的星期与节次，补齐后我再生成日程确认卡。",
                )
            return self._clarify_response(intent, "我还不确定你要我记录、查询还是修改哪件事，可以再说具体一点。")

        if intent_name in {"confirm", "cancel"} and self._is_attachment_only_message(raw_text, request):
            return self._clarify_response(intent, reply or "我看到了附件，但不能把附件本身当作确认或取消。请告诉我希望怎么处理这份内容。")

        if intent_name == "confirm":
            return AgentResponse(
                intent="update_existing",
                confidence=intent.confidence,
                reasoning_summary=intent.reasoning_summary or "User confirmed the latest pending confirmation.",
                reply_to_user=reply or "收到，按刚才确认的内容执行。",
                tool_calls=[AgentToolCall(tool_name="resolve_confirmation", risk_level=RiskLevel.low)],
            )
        if intent_name == "cancel":
            return AgentResponse(
                intent="update_existing",
                confidence=intent.confidence,
                reasoning_summary=intent.reasoning_summary or "User canceled the latest pending confirmation.",
                reply_to_user=reply or "收到，取消刚才的候选操作。",
                tool_calls=[
                    AgentToolCall(
                        tool_name="resolve_confirmation",
                        risk_level=RiskLevel.low,
                        arguments={"action": "cancel"},
                    )
                ],
            )
        if intent_name == "query_pending_confirmations":
            return AgentResponse(
                intent="query_today",
                confidence=intent.confidence,
                reasoning_summary=intent.reasoning_summary or "User asked to list pending confirmations.",
                reply_to_user=reply or "我查一下最近待确认项。",
                tool_calls=[AgentToolCall(tool_name="query_pending_confirmations", risk_level=RiskLevel.low)],
            )
        if intent_name == "query_today_plan":
            return self._read_response(intent, "query_today", reply or "我查一下今天的安排。")
        if intent_name == "query_tomorrow_plan":
            return self._read_response(intent, "query_tomorrow", reply or "我查一下明天的安排。")
        if intent_name == "query_week_plan":
            return self._read_response(intent, "query_week", reply or "我查一下未来 7 天的安排。")
        if intent_name == "query_availability":
            return AgentResponse(
                intent="query_availability",
                confidence=intent.confidence,
                reasoning_summary=intent.reasoning_summary or "User asked for availability.",
                reply_to_user=reply or "我帮你算一下空闲时间。",
                tool_calls=[
                    AgentToolCall(
                        tool_name="query_availability",
                        risk_level=RiskLevel.low,
                        arguments=self._availability_entities(entities, raw_text),
                    )
                ],
            )
        if intent_name == "query_time_budget_plan":
            query = self._entity_str(entities, "query") or self._entity_str(entities, "title") or raw_text.strip()
            return AgentResponse(
                intent="query_today",
                confidence=intent.confidence,
                reasoning_summary=intent.reasoning_summary or "User asked how a cumulative time-budget task is scheduled.",
                reply_to_user=reply or "我查一下这个长期学习安排目前有没有拆到日历。",
                tool_calls=[
                    AgentToolCall(
                        tool_name="explain_time_budget_plan",
                        risk_level=RiskLevel.low,
                        arguments={
                            key: value
                            for key, value in {
                                "query": query,
                                "action_item_id": self._entity_str(entities, "action_item_id"),
                            }.items()
                            if value
                        },
                    )
                ],
            )
        if intent_name == "schedule_time_budget_plan":
            query = self._entity_str(entities, "query") or self._entity_str(entities, "title")
            action_item_id = self._entity_str(entities, "action_item_id")
            if not action_item_id and not query:
                action_item_id = self._single_long_term_task_id(request)
            if not action_item_id and not query:
                return self._clarify_response(
                    intent,
                    reply or "要把长期时间目标排进日历，需要先明确是哪一个长期任务；如果这是课程表，请按重复日程安排处理。",
                )
            args = {
                "action_item_id": action_item_id,
                "query": query,
                "daily_minutes": self._entity_int(entities, "daily_minutes"),
                "session_minutes": self._entity_int(entities, "session_minutes"),
                "min_session_minutes": self._entity_int(entities, "min_session_minutes"),
                "buffer_minutes": self._entity_int(entities, "buffer_minutes"),
                "window_start": self._entity_str(entities, "window_start"),
                "window_end": self._entity_str(entities, "window_end"),
            }
            return self._proposal_response(
                intent,
                kind=PlanDraftKind.long_term_schedule.value,
                user_goal=query or action_item_id or raw_text.strip(),
                reply_to_user="我会按当前空闲时间生成日历候选，确认后写入日历。",
                tool_calls=[
                    AgentToolCall(
                        tool_name="schedule_time_budget_plan",
                        risk_level=RiskLevel.low,
                        arguments={key: value for key, value in args.items() if value is not None},
                    )
                ],
            )
        if intent_name in {"create_task", "create_calendar_event"}:
            compound_calls = self._compound_event_and_reminder_calls(raw_text, request, intent.confidence)
            if compound_calls:
                return AgentResponse(
                    intent="create_candidates",
                    confidence=max(intent.confidence, 0.82),
                    reasoning_summary=intent.reasoning_summary
                    or "Mapped model create intent to a calendar event candidate and reminder task candidate.",
                    reply_to_user=reply or f"我识别到 {len(compound_calls)} 个候选，确认后再创建。",
                    tool_calls=compound_calls,
                )
        if intent_name == "create_task":
            title = self._entity_str(entities, "title") or raw_text.strip()
            if not title:
                return self._clarify_response(intent, "要创建任务的话，请告诉我任务标题。")
            args: dict[str, Any] = {
                "title": title,
                "description": self._entity_str(entities, "description") or raw_text.strip() or None,
                "due_at": self._entity_str(entities, "due_at") or self._parse_due_at(raw_text, request),
                "priority": self._entity_str(entities, "priority") or "P3",
                "estimated_minutes": self._entity_int(entities, "estimated_minutes"),
                "confidence": intent.confidence,
            }
            return self._candidate_response(
                intent,
                "create_candidates",
                "create_task_candidate",
                args,
                reply or "我识别到一个任务候选，确认后再创建。",
            )
        if intent_name == "create_calendar_event":
            args = {
                "title": self._entity_str(entities, "title") or raw_text.strip(),
                "description": self._entity_str(entities, "description") or raw_text.strip() or None,
                "start_at": self._entity_str(entities, "start_at"),
                "end_at": self._entity_str(entities, "end_at"),
                "location": self._entity_str(entities, "location"),
                "confidence": intent.confidence,
            }
            if not args["title"] or not args["start_at"] or not args["end_at"]:
                return self._clarify_response(intent, "我还缺少日程标题、开始时间或结束时间，不能直接生成候选。")
            return self._candidate_response(
                intent,
                "create_candidates",
                "create_calendar_event_candidate",
                args,
                reply or "我识别到一个日程候选，确认后再创建。",
            )
        if intent_name == "create_schedule_block":
            blocks = self._normalized_schedule_blocks(entities.get("blocks"))
            if not isinstance(blocks, list) or not blocks:
                return self._clarify_response(intent, "我识别到重复日程安排意图，但还缺少重复规则或起止时间。")
            return self._candidate_response(
                intent,
                "schedule_blocks",
                "create_schedule_block_candidates",
                {"blocks": blocks},
                reply or f"我识别到 {len(blocks)} 个日程安排候选，确认后再保存。",
            )
        if intent_name == "create_time_budget_plan":
            time_budget_plan = self._time_budget_plan_from_entities(entities, raw_text, request, intent.confidence)
            if not time_budget_plan:
                time_budget_plan = self._time_budget_plan_from_text(raw_text, request, intent.confidence)
            if not time_budget_plan:
                return self._clarify_response(
                    intent,
                    reply or "我识别到这是累计时间计划，但还缺少学习主题、总时长或截止时间。",
                )
            return self._candidate_response(
                intent,
                "create_candidates",
                "create_task_candidate",
                time_budget_plan,
                "我识别到一个长期学习安排候选，确认后再创建。",
                risk=RiskLevel.medium,
            )
        if intent_name == "complete_task":
            query = self._entity_str(entities, "query") or self._entity_str(entities, "title") or raw_text.strip()
            if not query:
                return self._clarify_response(intent, "请告诉我要标记完成的是哪一个任务。")
            return AgentResponse(
                intent="complete_item",
                confidence=intent.confidence,
                reasoning_summary=intent.reasoning_summary or "User wants to complete a task.",
                reply_to_user=reply or "我会匹配要完成的任务。",
                tool_calls=[AgentToolCall(tool_name="complete_task", risk_level=RiskLevel.low, arguments={"query": query})],
            )
        if intent_name == "update_task":
            query = self._entity_str(entities, "query") or self._entity_str(entities, "title")
            patch = {key: entities.get(key) for key in ("title", "description", "status", "priority", "due_at", "estimated_minutes") if entities.get(key)}
            if not query or not patch:
                return self._clarify_response(intent, "请告诉我要修改哪个任务，以及要改成什么。")
            return self._candidate_response(
                intent,
                "update_existing",
                "update_task",
                {"query": query, **patch},
                reply or "我会先匹配任务，再请你确认修改。",
                risk=RiskLevel.medium,
            )
        if intent_name == "update_calendar_event":
            query = self._entity_str(entities, "query") or self._entity_str(entities, "title")
            event_id = self._entity_str(entities, "calendar_event_id") or self._entity_str(entities, "event_id")
            patch = {
                key: entities.get(key)
                for key in ("title", "description", "start_at", "end_at", "location")
                if entities.get(key)
            }
            if not (query or event_id) or not patch:
                return self._clarify_response(intent, "请告诉我要修改哪条日程，以及要改成什么。")
            return self._candidate_response(
                intent,
                "update_existing",
                "update_calendar_event",
                {"query": query, "event_id": event_id, **patch},
                reply or "我会先匹配日程，再请你确认修改。",
                risk=RiskLevel.medium,
            )
        if intent_name == "update_schedule_block":
            query = self._entity_str(entities, "query") or self._entity_str(entities, "title")
            block_id = self._entity_str(entities, "schedule_block_id") or self._entity_str(entities, "block_id")
            patch = {
                key: entities.get(key)
                for key in ("title", "recurrence_rule", "start_time", "end_time", "timezone", "status")
                if entities.get(key)
            }
            if not (query or block_id) or not patch:
                return self._clarify_response(intent, "请告诉我要修改哪条日程安排，以及新的标题、重复规则或起止时间。")
            if set(patch) == {"title"} and not block_id:
                return self._clarify_response(intent, "请补充新的开始和结束时间，或明确只改这条日程安排的标题。")
            return self._candidate_response(
                intent,
                "update_existing",
                "update_schedule_block",
                {"query": query, "schedule_block_id": block_id, **patch},
                reply or "我会先匹配日程安排，再请你确认修改。",
                risk=RiskLevel.medium,
            )
        if intent_name == "disable_schedule_block_reminders":
            query = self._entity_str(entities, "query") or self._entity_str(entities, "title") or raw_text.strip()
            scope = self._entity_str(entities, "scope") or ("all" if self._looks_like_disable_schedule_block_reminders(raw_text) else "")
            block_id = self._entity_str(entities, "schedule_block_id") or self._entity_str(entities, "block_id")
            if not (query or scope or block_id):
                return self._clarify_response(intent, "请告诉我要关闭哪条固定安排的提醒，或说明关闭所有固定安排提醒。")
            return AgentResponse(
                intent="update_existing",
                confidence=intent.confidence,
                reasoning_summary=intent.reasoning_summary or "User wants to disable schedule-block reminders without canceling the schedule blocks.",
                reply_to_user=reply or "我会关闭固定安排提醒；安排仍会保留在查询和日历同步中。",
                tool_calls=[
                    AgentToolCall(
                        tool_name="disable_schedule_block_reminders",
                        risk_level=RiskLevel.low,
                        requires_confirmation=False,
                        arguments={
                            key: value
                            for key, value in {"query": query, "schedule_block_id": block_id, "scope": scope}.items()
                            if value
                        },
                    )
                ],
            )
        if intent_name == "cancel_task":
            query = self._entity_str(entities, "query") or self._entity_str(entities, "title") or raw_text.strip()
            if not query:
                return self._clarify_response(intent, "请告诉我要取消的是哪一个任务。")
            return self._candidate_response(
                intent,
                "update_existing",
                "cancel_task",
                {"query": query},
                reply or "我会先匹配任务，再请你确认取消。",
                risk=RiskLevel.high,
            )
        if intent_name == "cancel_calendar_event":
            query = self._entity_str(entities, "query") or self._entity_str(entities, "title")
            event_id = self._entity_str(entities, "calendar_event_id") or self._entity_str(entities, "event_id")
            if not (query or event_id):
                return self._clarify_response(intent, "请告诉我要取消哪一条日程。")
            return self._candidate_response(
                intent,
                "update_existing",
                "cancel_calendar_event",
                {"query": query, "calendar_event_id": event_id},
                reply or "我会先匹配日程，再请你确认取消。",
                risk=RiskLevel.high,
            )
        if intent_name == "cancel_schedule_block":
            query = self._entity_str(entities, "query") or self._entity_str(entities, "title")
            block_id = self._entity_str(entities, "schedule_block_id") or self._entity_str(entities, "block_id")
            if not (query or block_id):
                return self._clarify_response(intent, "请告诉我要取消哪一条日程安排。")
            return self._candidate_response(
                intent,
                "update_existing",
                "cancel_schedule_block",
                {"query": query, "schedule_block_id": block_id},
                reply or "我会先匹配日程安排，再请你确认取消。",
                risk=RiskLevel.high,
            )
        if intent_name == "smalltalk":
            return AgentResponse(
                intent="smalltalk",
                confidence=intent.confidence,
                reasoning_summary=intent.reasoning_summary or "Smalltalk.",
                reply_to_user=reply or "我在。",
                tool_calls=[
                    AgentToolCall(
                        tool_name="send_feishu_reply",
                        risk_level=RiskLevel.low,
                        arguments={"text": reply or "我在。"},
                    )
                ],
            )
        return self._clarify_response(intent, reply or "我还不确定你的意思，可以再说具体一点。")

    def _read_response(self, intent: ModelIntent, tool_name: Literal["query_today", "query_tomorrow", "query_week"], reply: str) -> AgentResponse:
        return AgentResponse(
            intent=tool_name,
            confidence=intent.confidence,
            reasoning_summary=intent.reasoning_summary or f"Read-only {tool_name}.",
            reply_to_user=reply,
            tool_calls=[AgentToolCall(tool_name=tool_name, risk_level=RiskLevel.low)],
        )

    def _proposal_response(
        self,
        intent: ModelIntent,
        *,
        kind: str,
        user_goal: str,
        reply: str | None = None,
        missing_info: list[str] | None = None,
        candidate_plan: dict[str, Any] | None = None,
        assumptions: list[str] | None = None,
        risks: list[str] | None = None,
        reply_to_user: str | None = None,
        tool_calls: list[AgentToolCall] | None = None,
    ) -> AgentResponse:
        if candidate_plan is None and tool_calls:
            first_call = tool_calls[0]
            if first_call.tool_name == "schedule_time_budget_plan":
                candidate_plan = {
                    "type": "time_budget_schedule",
                    "title": first_call.arguments.get("query") or user_goal,
                    "arguments": dict(first_call.arguments),
                    "details": dict(first_call.arguments),
                }
        candidate_plan = candidate_plan or {"title": user_goal, "details": {}}
        missing_info = list(missing_info or [])
        reply_text = reply or reply_to_user or "我会先生成计划草案，确认后才会写入。"
        return AgentResponse(
            intent="create_candidates",
            confidence=max(intent.confidence, 0.72),
            reasoning_summary=intent.reasoning_summary or f"Mapped model intent {intent.intent} to AssistantProposal.",
            reply_to_user=reply_text,
            assistant_proposal=AssistantProposal(
                kind=kind,
                status=PlanDraftStatus.refining.value if missing_info else PlanDraftStatus.ready_for_schedule.value,
                user_goal=user_goal,
                context_summary="Provider generated a planner proposal from the current message and context.",
                ai_assumptions=assumptions or ["复杂长期安排先生成计划草案，确认前不写入任务或日历。"],
                missing_info=missing_info,
                candidate_plans=[candidate_plan],
                schedule_preview=[],
                risks=risks or ["计划可能需要根据用户偏好继续调整。"],
                next_step_suggestion="请补充缺失信息；信息完整后我再生成日程确认卡。" if missing_info else "请确认日程预览，确认后才会写入。",
                confidence=max(intent.confidence, 0.72),
            ),
            tool_calls=[],
        )

    def _candidate_response(
        self,
        intent: ModelIntent,
        agent_intent: Literal["create_candidates", "update_existing", "schedule_blocks"],
        tool_name: Literal[
            "create_task_candidate",
            "create_calendar_event_candidate",
            "create_schedule_block_candidates",
            "update_task",
            "update_calendar_event",
            "cancel_task",
            "cancel_calendar_event",
            "cancel_schedule_block",
            "update_schedule_block",
        ],
        arguments: dict[str, Any],
        reply: str,
        *,
        risk: RiskLevel = RiskLevel.medium,
    ) -> AgentResponse:
        clean_args = {key: value for key, value in arguments.items() if value is not None}
        if tool_name == "create_task_candidate" and self._looks_like_time_budget_candidate(clean_args):
            return self._proposal_response(
                intent,
                kind=PlanDraftKind.long_term_schedule.value,
                user_goal=str(clean_args.get("title") or reply),
                reply="我会先生成长期计划草案，确认前不会创建任务或日历。",
                missing_info=["频率", "偏好时间", "持续周期"],
                candidate_plan={
                    "type": "time_budget_goal",
                    "title": clean_args.get("title") or "长期时间目标",
                    "arguments": clean_args,
                    "details": {
                        "estimated_minutes": clean_args.get("estimated_minutes"),
                        "due_at": clean_args.get("due_at"),
                    },
                },
                assumptions=["这是长期累计时间目标，需要进一步确认如何落到日历。"],
                risks=["只记录总时长但不澄清频率，后续排程可能不符合真实偏好。"],
            )
        return AgentResponse(
            intent=agent_intent,
            confidence=intent.confidence,
            reasoning_summary=intent.reasoning_summary or f"Mapped model intent {intent.intent} to backend candidate tool.",
            reply_to_user=reply,
            tool_calls=[
                AgentToolCall(
                    tool_name=tool_name,
                    risk_level=risk,
                    requires_confirmation=True,
                    arguments=clean_args,
                )
            ],
        )

    def _looks_like_time_budget_candidate(self, arguments: dict[str, Any]) -> bool:
        text = f"{arguments.get('title') or ''}\n{arguments.get('description') or ''}"
        return bool(arguments.get("estimated_minutes") and any(token in text for token in ("累计", "总量", "长期", "time-budget")))

    def _clarify_response(self, intent: ModelIntent, reply: str) -> AgentResponse:
        text = reply.strip() or "我还不确定你的意思，可以再说具体一点。"
        return AgentResponse(
            intent="unknown",
            confidence=min(intent.confidence, 0.44),
            reasoning_summary=intent.reasoning_summary or "Clarification required; no state change.",
            reply_to_user=text,
            tool_calls=[AgentToolCall(tool_name="send_feishu_reply", risk_level=RiskLevel.low, arguments={"text": text})],
        )

    def _invalid_model_response(self, exc: Exception) -> AgentResponse:
        text = "模型输出格式不合法，我没有写入任何数据。请再说具体一点。"
        return AgentResponse(
            intent="unknown",
            confidence=0.0,
            reasoning_summary=f"Invalid ModelIntent JSON; no state change. {exc}",
            reply_to_user=text,
            tool_calls=[AgentToolCall(tool_name="send_feishu_reply", risk_level=RiskLevel.low, arguments={"text": text})],
        )

    def _availability_entities(self, entities: dict[str, Any], raw_text: str) -> dict[str, str]:
        args: dict[str, str] = {}
        day = self._entity_str(entities, "day")
        if day:
            args["day"] = day
        elif "后天" in raw_text:
            args["day"] = "after_tomorrow"
        elif "今天" in raw_text or "今晚" in raw_text:
            args["day"] = "today"
        elif "周六" in raw_text:
            args["day"] = "saturday"
        elif "周日" in raw_text or "周天" in raw_text:
            args["day"] = "sunday"
        elif "周一" in raw_text:
            args["day"] = "monday"
        elif "周二" in raw_text:
            args["day"] = "tuesday"
        elif "周三" in raw_text:
            args["day"] = "wednesday"
        elif "周四" in raw_text:
            args["day"] = "thursday"
        elif "周五" in raw_text:
            args["day"] = "friday"
        else:
            args["day"] = "tomorrow"
        if self._entity_str(entities, "window_start"):
            args["window_start"] = str(entities["window_start"])
        elif "今晚" in raw_text or "晚上" in raw_text:
            args["window_start"] = "18:00"
        else:
            args["window_start"] = "08:00"
        if self._entity_str(entities, "window_end"):
            args["window_end"] = str(entities["window_end"])
        else:
            args["window_end"] = "24:00"
        args["focus"] = self._entity_str(entities, "focus") or ("busy" if "占" in raw_text else "free")
        return args

    def _time_budget_plan_from_entities(
        self,
        entities: dict[str, Any],
        raw_text: str,
        request: dict[str, Any],
        confidence: float,
    ) -> dict[str, Any] | None:
        title = (
            self._entity_str(entities, "title")
            or self._entity_str(entities, "task_title")
            or self._entity_str(entities, "goal")
            or self._entity_str(entities, "goal_description")
            or self._entity_str(entities, "subject")
        )
        minutes = (
            self._entity_int(entities, "estimated_minutes")
            or self._entity_int(entities, "total_minutes")
            or self._entity_int(entities, "duration_minutes")
            or self._entity_int(entities, "target_minutes")
            or self._entity_int(entities, "minimum_minutes")
        )
        if minutes is None:
            hours = (
                self._entity_float(entities, "estimated_hours")
                or self._entity_float(entities, "total_hours")
                or self._entity_float(entities, "hours")
                or self._entity_float(entities, "target_hours")
                or self._entity_float(entities, "minimum_hours")
            )
            if hours is not None:
                minutes = int(hours * 60)
        due_at = self._time_budget_due_at(raw_text, request) or self._normalize_entity_datetime(
            self._entity_str(entities, "due_at")
            or self._entity_str(entities, "deadline")
            or self._entity_str(entities, "end_date"),
            request,
            end_of_day=True,
        )
        if not title or not minutes or not due_at:
            return None

        start_date = self._normalize_entity_datetime(
            self._entity_str(entities, "start_date") or self._entity_str(entities, "start_at"),
            request,
            end_of_day=False,
        )
        hours_display = minutes // 60 if minutes % 60 == 0 else round(minutes / 60, 1)
        title = title.strip(" ，。！？")
        title_with_budget = title if "累计" in title else f"{title}（累计不少于{hours_display}小时）"
        description_parts = [self._entity_str(entities, "description") or raw_text.strip() or "长期学习安排"]
        if start_date:
            description_parts.append(f"开始：{start_date[:10]}")
        description_parts.append(f"截止：{due_at[:10]}")
        description_parts.append(f"累计不少于：{hours_display}小时")
        return {
            "title": title_with_budget,
            "description": "；".join(part for part in description_parts if part),
            "due_at": due_at,
            "priority": self._entity_str(entities, "priority") or "P2",
            "estimated_minutes": minutes,
            "confidence": max(confidence, 0.78),
        }

    def _time_budget_plan_from_text(self, raw_text: str, request: dict[str, Any], confidence: float) -> dict[str, Any] | None:
        source_text = raw_text.strip()
        if not self._looks_like_time_budget_plan(source_text):
            previous = self._recent_time_budget_message(request)
            if not previous or not self._looks_like_time_budget_followup(source_text):
                return None
            source_text = f"{previous}，{source_text}"

        title = self._time_budget_title(source_text)
        minutes = self._time_budget_minutes(source_text)
        due_at = self._time_budget_due_at(source_text, request)
        if not title or not minutes or not due_at:
            return None

        start_date = self._time_budget_start_date(source_text, request)
        hours = minutes // 60 if minutes % 60 == 0 else round(minutes / 60, 1)
        title_with_budget = f"{title}（累计不少于{hours}小时）"
        description_parts = ["长期学习安排"]
        if start_date:
            description_parts.append(f"开始：{start_date}")
        description_parts.append(f"截止：{due_at[:10]}")
        description_parts.append(f"累计不少于：{hours}小时")
        return {
            "title": title_with_budget,
            "description": "；".join(description_parts),
            "due_at": due_at,
            "priority": "P2",
            "estimated_minutes": minutes,
            "confidence": max(confidence, 0.78),
        }

    def _looks_like_time_budget_plan(self, text: str) -> bool:
        return bool(
            re.search(r"\d+(?:\.\d+)?\s*(?:小时|h|H)", text)
            and any(token in text for token in ("不少于", "至少", "累计", "总共", "总计"))
            and any(token in text for token in ("学习", "长期安排", "长期任务", "长期计划"))
        )

    def _looks_like_time_budget_followup(self, text: str) -> bool:
        return any(token in text for token in ("开始", "截止", "到", "最后一天", "月底")) and any(
            token in text for token in ("明天", "今天", "六月", "6月", "七月", "7月")
        )

    def _recent_time_budget_message(self, request: dict[str, Any]) -> str | None:
        messages = request.get("recent_user_messages")
        if not isinstance(messages, list):
            return None
        for item in messages[:3]:
            if not isinstance(item, dict):
                continue
            text = str(item.get("raw_text") or "")
            if self._looks_like_time_budget_plan(text):
                return text
        return None

    def _time_budget_title(self, text: str) -> str | None:
        match = re.search(r"(?:长期安排|长期任务|长期计划)[，,：:\s]*(?P<title>[^，,。；;]+?学习)", text)
        if not match:
            match = re.search(r"(?P<title>[\u4e00-\u9fffA-Za-z0-9]+学习)", text)
        if not match:
            return None
        title = match.group("title").strip()
        title = re.sub(r"^(添加|新增|创建|一个)", "", title).strip()
        return title or None

    def _time_budget_minutes(self, text: str) -> int | None:
        match = re.search(r"(?P<hours>\d+(?:\.\d+)?)\s*(?:小时|h|H)", text)
        if not match:
            return None
        return int(float(match.group("hours")) * 60)

    def _time_budget_due_at(self, text: str, request: dict[str, Any]) -> str | None:
        end_date = self._time_budget_end_date(text, request)
        if not end_date:
            return None
        return end_date.replace(hour=23, minute=59, second=0, microsecond=0).isoformat()

    def _time_budget_start_date(self, text: str, request: dict[str, Any]) -> str | None:
        base = self._request_effective_now(request)
        if "明天开始" in text:
            return (base + timedelta(days=1)).date().isoformat()
        if "今天开始" in text:
            return base.date().isoformat()
        return None

    def _time_budget_end_date(self, text: str, request: dict[str, Any]) -> datetime | None:
        base = self._request_effective_now(request)
        month = self._month_from_text(text, markers=("最后一天", "月底", "截止"))
        if month:
            return self._last_day_of_month(base, month)
        before_month = self._month_before_from_text(text)
        if before_month:
            month = before_month - 1 or 12
            return self._last_day_of_month(base, month)
        parsed = self._parse_due_at(text, request)
        if not parsed:
            return None
        try:
            return datetime.fromisoformat(parsed)
        except ValueError:
            return None

    def _request_effective_now(self, request: dict[str, Any]) -> datetime:
        now_text = str(request.get("now") or "")
        try:
            actual_now = datetime.fromisoformat(now_text) if now_text else None
        except ValueError:
            actual_now = None
        tz = actual_now.tzinfo if actual_now and actual_now.tzinfo else ZoneInfo("Asia/Shanghai")
        if not isinstance(tz, ZoneInfo):
            tz = ZoneInfo("Asia/Shanghai")
        return effective_now(tz, actual_now)

    def _last_day_of_month(self, base: datetime, month: int) -> datetime:
        year = base.year
        candidate = base.replace(
            year=year,
            month=month,
            day=calendar.monthrange(year, month)[1],
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        if candidate.date() < base.date():
            year += 1
            candidate = candidate.replace(year=year, day=calendar.monthrange(year, month)[1])
        return candidate

    def _month_from_text(self, text: str, *, markers: tuple[str, ...]) -> int | None:
        if not any(marker in text for marker in markers):
            return None
        match = re.search(r"(?P<month>\d{1,2})\s*月份?\s*(?:最后一天|月底|截止)", text)
        if match:
            month = int(match.group("month"))
            return month if 1 <= month <= 12 else None
        for name, month in self._chinese_months().items():
            if f"{name}月最后一天" in text or f"{name}月底" in text or f"{name}月截止" in text:
                return month
        return None

    def _month_before_from_text(self, text: str) -> int | None:
        match = re.search(r"(?P<month>\d{1,2})\s*月份?前", text)
        if match:
            month = int(match.group("month"))
            return month if 1 <= month <= 12 else None
        for name, month in self._chinese_months().items():
            if f"{name}月份前" in text or f"{name}月前" in text:
                return month
        return None

    def _chinese_months(self) -> dict[str, int]:
        return {
            "一": 1,
            "二": 2,
            "三": 3,
            "四": 4,
            "五": 5,
            "六": 6,
            "七": 7,
            "八": 8,
            "九": 9,
            "十": 10,
            "十一": 11,
            "十二": 12,
        }

    def _normalize_intent_name(self, value: str, raw_text: str) -> str:
        normalized = str(value or "").strip().lower()
        allowed = {
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
        }
        if normalized in allowed:
            return normalized
        if normalized in {"query_plan", "query_tasks", "query_task", "query_schedule", "query_calendar"}:
            return "query_week_plan"
        if normalized in {"query_pending_confirmations", "query_confirmations", "pending_confirmations"}:
            return "query_pending_confirmations"
        if normalized in {"disable_schedule_reminders", "disable_schedule_block_reminder", "disable_reminders", "mute_schedule_block"}:
            return "disable_schedule_block_reminders"
        if normalized in {"query_time_budget_plan", "explain_time_budget_plan", "query_time_budget", "time_budget_status"}:
            return "query_time_budget_plan"
        if normalized in {"schedule_time_budget_plan", "schedule_time_budget", "plan_time_budget_calendar", "calendar_time_budget"}:
            return "schedule_time_budget_plan"
        if normalized in {"course_timetable", "create_course_timetable", "import_course_timetable", "start_plan_refinement"}:
            return "start_plan_refinement"
        if normalized in {"refine_plan_draft", "refine_plan", "update_plan_draft"}:
            return "refine_plan_draft"
        if normalized in {"generate_plan_schedule_confirmation", "plan_schedule_confirmation"}:
            return "generate_plan_schedule_confirmation"
        if normalized in {"create_todo", "add_task", "record_task"}:
            return "create_task"
        return "unknown"

    def _compound_event_and_reminder_calls(
        self,
        raw_text: str,
        request: dict[str, Any],
        confidence: float,
    ) -> list[AgentToolCall]:
        if "提醒我" not in raw_text:
            return []
        segments = [part.strip() for part in re.split(r"[，,。；;]\s*", raw_text) if part.strip()]
        reminder_segment = next((part for part in segments if "提醒我" in part), "")
        event_segment = next((part for part in segments if part != reminder_segment and re.search(r"(要去|参加|考)", part)), "")
        if not reminder_segment or not event_segment:
            return []

        reminder_time = self._parse_due_at(reminder_segment, request)
        event_start = self._parse_due_at(event_segment, request)
        reminder_title = self._reminder_title(reminder_segment)
        event_title = self._event_title(event_segment)
        if not reminder_time or not event_start or not reminder_title or not event_title:
            return []
        event_end = (datetime.fromisoformat(event_start) + timedelta(hours=2)).isoformat()
        return [
            AgentToolCall(
                tool_name="create_calendar_event_candidate",
                risk_level=RiskLevel.medium,
                requires_confirmation=True,
                arguments={
                    "title": event_title,
                    "description": event_segment,
                    "start_at": event_start,
                    "end_at": event_end,
                    "confidence": confidence,
                },
            ),
            AgentToolCall(
                tool_name="create_task_candidate",
                risk_level=RiskLevel.medium,
                requires_confirmation=True,
                arguments={
                    "title": reminder_title,
                    "description": reminder_segment,
                    "due_at": reminder_time,
                    "priority": "P2",
                    "confidence": confidence,
                },
            ),
        ]

    def _reminder_title(self, text: str) -> str:
        title = text.split("提醒我", 1)[-1]
        title = re.sub(r"^(要|去|把|先|记得)", "", title).strip()
        return title.strip(" ，。！？")

    def _event_title(self, text: str) -> str:
        match = re.search(r"(考[^，,。；;]+)", text)
        if match:
            return match.group(1).strip()
        cleaned = re.sub(r"^我", "", text)
        cleaned = re.sub(r"(今天|明天|后天|大后天|上午|下午|晚上|早上|中午|\d{1,2}点|\d{1,2}[:：]\d{2})", "", cleaned)
        cleaned = cleaned.replace("要去", "").replace("参加", "").strip(" ，。！？")
        return cleaned

    def _entity_str(self, entities: dict[str, Any], key: str) -> str | None:
        value = entities.get(key)
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    def _entity_int(self, entities: dict[str, Any], key: str) -> int | None:
        value = entities.get(key)
        if value in {None, ""}:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _entity_float(self, entities: dict[str, Any], key: str) -> float | None:
        value = entities.get(key)
        if value in {None, ""}:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _normalize_entity_datetime(self, value: str | None, request: dict[str, Any], *, end_of_day: bool) -> str | None:
        if not value:
            return None
        parsed = self._parse_due_at(value, request)
        if parsed:
            dt = datetime.fromisoformat(parsed)
        else:
            try:
                dt = datetime.fromisoformat(value)
            except ValueError:
                return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        if end_of_day and dt.hour == 0 and dt.minute == 0 and dt.second == 0:
            dt = dt.replace(hour=23, minute=59)
        return dt.isoformat()

    def _parse_due_at(self, raw_text: str, request: dict[str, Any]) -> str | None:
        now_text = str(request.get("now") or "")
        try:
            actual_now = datetime.fromisoformat(now_text) if now_text else None
        except ValueError:
            actual_now = None
        tz = actual_now.tzinfo if actual_now and actual_now.tzinfo else ZoneInfo("Asia/Shanghai")
        if not isinstance(tz, ZoneInfo):
            tz = ZoneInfo("Asia/Shanghai")
        parsed = parse_datetime(raw_text, tz, effective_now(tz, actual_now))
        return parsed.value.isoformat() if parsed else None

    def _parse_model_intent(
        self,
        content: str,
        *,
        fixed_intent: str | None = None,
        fallback_confidence: float | None = None,
        allow_repair: bool = False,
    ) -> ModelIntent:
        text = self._json_text(content, allow_repair=allow_repair)
        data = json.loads(text)
        if not isinstance(data, dict):
            raise CoreAgentProviderError(f"{self.name} returned a non-object ModelIntent")

        if fixed_intent:
            data["intent"] = fixed_intent
        elif not str(data.get("intent") or "").strip():
            raise CoreAgentProviderError(f"{self.name} ModelIntent is missing intent")

        confidence = data.get("confidence")
        if confidence in {None, ""}:
            data["confidence"] = fallback_confidence if fallback_confidence is not None else 0.5

        entities = data.get("entities")
        if not isinstance(entities, dict):
            data["entities"] = {}

        if data.get("reply") is None:
            data["reply"] = ""
        if data.get("reasoning_summary") is None:
            data["reasoning_summary"] = ""

        needs_confirmation = data.get("needs_confirmation")
        if not isinstance(needs_confirmation, bool):
            intent_name = self._normalize_intent_name(str(data.get("intent") or ""), "")
            data["needs_confirmation"] = self._default_needs_confirmation(intent_name)

        return ModelIntent.model_validate(data)

    def _default_needs_confirmation(self, intent_name: str) -> bool:
        return intent_name in {
            "schedule_time_budget_plan",
            "create_task",
            "create_calendar_event",
            "create_schedule_block",
            "create_time_budget_plan",
            "complete_task",
            "update_task",
            "update_calendar_event",
            "update_schedule_block",
            "cancel_task",
            "cancel_calendar_event",
            "cancel_schedule_block",
            "confirm_plan_schedule",
        }

    def _message_content(self, data: dict[str, Any]) -> str:
        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices:
            raise CoreAgentProviderError(f"{self.name} response has no choices")
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            content = "".join(str(part.get("text") or "") if isinstance(part, dict) else str(part) for part in content)
        if not isinstance(content, str) or not content.strip():
            raise CoreAgentProviderError(f"{self.name} response content is empty")
        return content

    def _json_text(self, text: str, *, allow_repair: bool = False) -> str:
        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
            stripped = re.sub(r"\s*```$", "", stripped)
        try:
            json.loads(stripped)
            return stripped
        except json.JSONDecodeError:
            pass
        start = stripped.find("{")
        if start < 0:
            raise CoreAgentProviderError(f"{self.name} did not return a JSON object")
        stack: list[str] = []
        in_string = False
        escaped = False
        for index, char in enumerate(stripped[start:], start=start):
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if char == "{":
                stack.append("{")
            elif char == "[":
                stack.append("[")
            elif char == "}":
                if stack and stack[-1] == "{":
                    stack.pop()
                if not stack:
                    return stripped[start : index + 1]
            elif char == "]":
                if stack and stack[-1] == "[":
                    stack.pop()
                if not stack:
                    return stripped[start : index + 1]
        if allow_repair and stack:
            candidate = stripped[start:]
            if in_string:
                candidate += '"'
            for opener in reversed(stack):
                candidate += "}" if opener == "{" else "]"
            candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
            try:
                json.loads(candidate)
            except json.JSONDecodeError as exc:
                raise CoreAgentProviderError(f"{self.name} returned incomplete JSON") from exc
            return candidate
        raise CoreAgentProviderError(f"{self.name} returned incomplete JSON")


class LmStudioProvider(OpenAICompatibleChatProvider):
    name = "lm_studio_provider"

    def __init__(
        self,
        *,
        base_url: str,
        model: str | None,
        api_key: str | None = None,
        timeout_seconds: int = 120,
        response_format: str = "none",
        max_tokens: int | None = 512,
        context_length: int | None = None,
        use_native_chat: bool = False,
        max_image_bytes: int = 8 * 1024 * 1024,
    ):
        super().__init__(
            base_url=base_url,
            model=model,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            response_format=response_format,
            max_tokens=max_tokens,
            max_image_bytes=max_image_bytes,
        )
        self.context_length = context_length
        self.use_native_chat = use_native_chat
        self._context_checked = False

    def run(self, request: dict[str, Any]) -> AgentResponse:
        if not self.use_native_chat:
            self._ensure_loaded_context()
        return super().run(request)

    def _post_chat_completion(
        self,
        payload: dict[str, Any],
        *,
        fallback_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self.use_native_chat:
            return super()._post_chat_completion(payload, fallback_payload=fallback_payload)
        try:
            return self._post_native_chat(payload)
        except (CoreAgentProviderError, TimeoutError, ValueError):
            return super()._post_chat_completion(payload, fallback_payload=fallback_payload)

    def _post_native_chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        native_payload = self._native_chat_payload(payload)
        data = self._request_json("POST", self._native_chat_url(), native_payload)
        content = self._native_chat_content(data)
        return {
            "choices": [
                {
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": data.get("finish_reason") or data.get("stop_reason"),
                }
            ],
            "usage": data.get("usage") or data.get("stats") or {},
        }

    def _ensure_loaded_context(self) -> None:
        if self._context_checked or not self.context_length or not self.model:
            return
        self._context_checked = True
        try:
            models = self._request_json("GET", self._native_models_url())
            if self._loaded_context_length(models, self.model) >= self.context_length:
                return
            base_model = self.model.split(":", 1)[0]
            data = self._request_json(
                "POST",
                self._native_load_url(),
                {
                    "model": base_model,
                    "context_length": self.context_length,
                    "flash_attention": True,
                    "echo_load_config": True,
                },
            )
        except (CoreAgentProviderError, TimeoutError, ValueError):
            return
        instance_id = data.get("instance_id") if isinstance(data, dict) else None
        if isinstance(instance_id, str) and instance_id:
            self.model = instance_id

    def _loaded_context_length(self, data: dict[str, Any], model_id: str) -> int:
        models = data.get("models") if isinstance(data, dict) else None
        if not isinstance(models, list):
            return 0
        for model in models:
            if not isinstance(model, dict):
                continue
            instances = model.get("loaded_instances")
            if not isinstance(instances, list):
                continue
            for instance in instances:
                if not isinstance(instance, dict) or instance.get("id") != model_id:
                    continue
                config = instance.get("config")
                if not isinstance(config, dict):
                    return 0
                try:
                    return int(config.get("context_length") or 0)
                except (TypeError, ValueError):
                    return 0
        return 0

    def _native_chat_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        messages = payload.get("messages")
        if not isinstance(messages, list):
            messages = []
        system_parts: list[str] = []
        input_parts: list[str] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            content = self._text_from_chat_content(message.get("content"))
            if not content:
                continue
            if message.get("role") == "system":
                system_parts.append(content)
            else:
                input_parts.append(content)
        native_payload: dict[str, Any] = {
            "model": payload.get("model"),
            "input": "\n\n".join(input_parts),
            "system_prompt": "\n\n".join(system_parts),
            "temperature": payload.get("temperature", 0.1),
            "stream": False,
            "store": False,
        }
        max_tokens = payload.get("max_tokens") or self.max_tokens
        if max_tokens is not None:
            native_payload["max_output_tokens"] = max_tokens
        if self.context_length is not None:
            native_payload["context_length"] = self.context_length
        return {key: value for key, value in native_payload.items() if value not in {None, ""}}

    def _text_from_chat_content(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(str(part.get("text") or ""))
            return "\n".join(part for part in parts if part)
        return str(content or "")

    def _native_chat_url(self) -> str:
        root = self.base_url
        if root.endswith("/api/v1"):
            return f"{root}/chat"
        if root.endswith("/v1"):
            root = root[: -len("/v1")]
        return f"{root}/api/v1/chat"

    def _native_models_url(self) -> str:
        root = self.base_url
        if root.endswith("/api/v1"):
            return f"{root}/models"
        if root.endswith("/v1"):
            root = root[: -len("/v1")]
        return f"{root}/api/v1/models"

    def _native_load_url(self) -> str:
        root = self.base_url
        if root.endswith("/api/v1"):
            return f"{root}/models/load"
        if root.endswith("/v1"):
            root = root[: -len("/v1")]
        return f"{root}/api/v1/models/load"

    def _native_chat_content(self, data: dict[str, Any]) -> str:
        if isinstance(data.get("content"), str):
            return str(data["content"])
        if isinstance(data.get("response"), str):
            return str(data["response"])
        output = data.get("output")
        if isinstance(output, str):
            return output
        if isinstance(output, list):
            parts: list[str] = []
            for item in output:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict) and isinstance(item.get("content"), str):
                    parts.append(str(item["content"]))
            if parts:
                return "".join(parts)
        raise CoreAgentProviderError(f"{self.name} native chat response content is empty")


class OpenAIApiProvider:
    name = "openai_api_provider"
    model = None

    def run(self, _request: dict[str, Any]) -> AgentResponse:
        raise CoreAgentProviderUnavailable("openai_api_provider is a stub; configure API integration later")


class LocalMultimodalProvider:
    name = "local_multimodal_provider"
    model = None

    def run(self, _request: dict[str, Any]) -> AgentResponse:
        raise CoreAgentProviderUnavailable("local_multimodal_provider is a stub; image/audio understanding is not enabled")
