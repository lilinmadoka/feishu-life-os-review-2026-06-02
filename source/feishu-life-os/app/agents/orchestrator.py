from __future__ import annotations

import asyncio
from typing import Any
from zoneinfo import ZoneInfo

from app.adapters.feishu_client import FeishuClient
from app.agents.models import (
    AgentIntent,
    AgentMessageType,
    AgentRequest,
    AgentResponse,
    AgentToolCall,
    AgentToolName,
)
from app.agents.providers.base import AgentProvider, AgentProviderError, AgentProviderUnavailable
from app.agents.tools import AgentToolExecutor
from app.database import Repository
from app.models import (
    AgentRunCreate,
    CaptureCreate,
    CaptureStatus,
    SourceType,
)
from app.services.review_service import ReviewService
from app.services.sync_service import SyncService

PROJECT_BRIEF = (
    "这是用户的 Agent-first 个人事务助理。飞书私聊是主产品入口；多维表只是后台展示和审计层，"
    "不要把用户消息当成表格搬运任务。"
)

SAFETY_RULES = [
    "删除、批量修改、低置信度修改必须先 ask_confirmation。",
    "查询类消息不能创建任务，也不能写成多维表任务输入。",
    "修改任务时只有单一明确匹配才允许直接执行。",
    "当用户只回复 A/B/C、数字、是的、确认时，必须先查看 pending_summary 判断是否在回答上一轮确认。",
    "图片、文件和复杂聊天记录先保存附件信息；多模态能力不足时要说明处理状态。",
]


class AgentOrchestrator:
    def __init__(
        self,
        repo: Repository,
        provider: AgentProvider,
        review_service: ReviewService,
        sync: SyncService,
        feishu: FeishuClient,
        tz: ZoneInfo,
    ):
        self.repo = repo
        self.provider = provider
        self.review_service = review_service
        self.sync = sync
        self.feishu = feishu
        self.tz = tz
        self.tools = AgentToolExecutor(repo, sync, review_service, tz)

    async def handle_feishu_message(
        self,
        *,
        raw_text: str,
        message_type: AgentMessageType,
        open_id: str | None,
        message_id: str | None,
        attachments: list[dict[str, Any]],
        raw_event: dict[str, Any],
    ) -> dict[str, Any]:
        queued = await self.enqueue_feishu_message(
            raw_text=raw_text,
            message_type=message_type,
            open_id=open_id,
            message_id=message_id,
            attachments=attachments,
            raw_event=raw_event,
        )
        request = queued.pop("_agent_request", None)
        if not request:
            return queued
        result = await self.process_agent_run(
            agent_run_id=queued["agent_run_id"],
            capture_id=queued["capture_id"],
            open_id=open_id,
            request=request,
        )
        return {**queued, **result}

    async def enqueue_feishu_message(
        self,
        *,
        raw_text: str,
        message_type: AgentMessageType,
        open_id: str | None,
        message_id: str | None,
        attachments: list[dict[str, Any]],
        raw_event: dict[str, Any],
    ) -> dict[str, Any]:
        if message_id:
            existing = self.repo.get_capture_by_source(SourceType.feishu_event.value, message_id)
            if existing:
                return {"ok": True, "duplicate": True, "capture_id": existing.id}

        capture = self.repo.create_capture(
            CaptureCreate(
                raw_text=raw_text or f"[{message_type.value} message]",
                source_type=SourceType.feishu_event,
                source_ref=message_id,
                attachments=attachments,
                metadata={
                    "agent_first": True,
                    "message_type": message_type.value,
                    "open_id": open_id,
                    "raw_event": raw_event,
                },
            ),
            normalized_text=raw_text.strip() or f"[{message_type.value} message]",
        )
        request = self.build_request(
            raw_text=raw_text,
            message_type=message_type,
            open_id=open_id,
            message_id=message_id,
            capture_id=capture.id,
            attachments=attachments,
        )
        run = self.repo.create_agent_run(
            AgentRunCreate(
                capture_id=capture.id,
                source_ref=message_id,
                provider=self.provider.name,
                request_json=request.model_dump(mode="json"),
            )
        )
        return {
            "ok": True,
            "queued": True,
            "capture_id": capture.id,
            "agent_run_id": run.id,
            "_agent_request": request,
        }

    async def process_agent_run(
        self,
        *,
        agent_run_id: str,
        capture_id: str,
        open_id: str | None,
        request: AgentRequest,
    ) -> dict[str, Any]:
        ack_audit: dict[str, Any] | None = None
        try:
            response = self._fast_path_response(request)
            if response is None:
                ack_result = await self._safe_send_reply(open_id, "已收到，我正在整理，稍后回复。")
                ack_audit = self._reply_audit(ack_result)
                response = await asyncio.to_thread(self.provider.run, request)
        except AgentProviderUnavailable as exc:
            self.repo.update_capture_status(capture_id, CaptureStatus.needs_review, confidence=0)
            reply_text = "智能处理器未启动/不可用，已记录消息但不会自动处理。"
            reply_result = await self._safe_send_reply(open_id, reply_text)
            tool_results = [self._reply_audit(reply_result)]
            if ack_audit:
                tool_results.insert(0, ack_audit)
            self.repo.fail_agent_run(
                agent_run_id,
                str(exc),
                tool_results_json=tool_results,
            )
            return {
                "ok": True,
                "agent_status": "unavailable",
                "reply": reply_result,
            }
        except AgentProviderError as exc:
            self.repo.update_capture_status(capture_id, CaptureStatus.needs_review, confidence=0)
            reply_text = "智能处理器返回结果异常，已记录消息，暂时不会自动修改任务。"
            reply_result = await self._safe_send_reply(open_id, reply_text)
            tool_results = [self._reply_audit(reply_result)]
            if ack_audit:
                tool_results.insert(0, ack_audit)
            self.repo.fail_agent_run(
                agent_run_id,
                str(exc),
                tool_results_json=tool_results,
            )
            return {
                "ok": True,
                "agent_status": "failed",
                "reply": reply_result,
            }
        except Exception as exc:  # noqa: BLE001 - background processing must persist unexpected failures
            self.repo.update_capture_status(capture_id, CaptureStatus.needs_review, confidence=0)
            reply_text = "系统处理时遇到异常，已记录消息，暂时不会自动修改任务。"
            reply_result = await self._safe_send_reply(open_id, reply_text)
            tool_results = [self._reply_audit(reply_result)]
            if ack_audit:
                tool_results.insert(0, ack_audit)
            self.repo.fail_agent_run(
                agent_run_id,
                str(exc),
                tool_results_json=tool_results,
            )
            return {"ok": True, "agent_status": "failed", "reply": reply_result}

        tool_results = await self.tools.execute_all(response.tool_calls, capture_id=capture_id)
        final_reply = self._compose_reply(response, tool_results)
        reply_result = await self._safe_send_reply(open_id, final_reply)
        persisted_results = [result.model_dump(mode="json") for result in tool_results]
        if ack_audit:
            persisted_results.insert(0, ack_audit)
        persisted_results.append(self._reply_audit(reply_result))
        self.repo.update_capture_status(capture_id, CaptureStatus.parsed, confidence=response.confidence)
        self.repo.complete_agent_run(
            agent_run_id,
            response.model_dump(mode="json"),
            persisted_results,
        )
        return {
            "ok": True,
            "intent": response.intent.value,
            "tool_results": [result.model_dump(mode="json") for result in tool_results],
            "reply": reply_result,
        }

    def _fast_path_response(self, request: AgentRequest) -> AgentResponse | None:
        text = request.raw_text.strip()
        compact = text.replace(" ", "")
        if request.message_type == AgentMessageType.text and any(
            phrase in compact
            for phrase in (
                "今天任务",
                "今日任务",
                "今天有什么任务",
                "今天还有什么任务",
                "今天还有啥任务",
                "今天有啥任务",
            )
        ):
            return AgentResponse(
                intent=AgentIntent.query,
                reply_text="我先查今天任务。",
                tool_calls=[AgentToolCall(name=AgentToolName.query_today)],
                needs_confirmation=False,
                confidence=0.9,
                reason_summary="命中极简单今天任务查询兜底，避免误创建任务。",
            )
        if request.message_type == AgentMessageType.text and any(
            phrase in compact
            for phrase in (
                "明天任务",
                "明天有什么任务",
                "明天还有什么任务",
                "明天有啥任务",
                "明天还有啥任务",
            )
        ):
            return AgentResponse(
                intent=AgentIntent.query,
                reply_text="我先查明天任务。",
                tool_calls=[AgentToolCall(name=AgentToolName.query_tomorrow)],
                needs_confirmation=False,
                confidence=0.9,
                reason_summary="命中极简单明天任务查询兜底，避免误创建任务。",
            )
        return None

    def build_request(
        self,
        *,
        raw_text: str,
        message_type: AgentMessageType,
        open_id: str | None,
        message_id: str | None,
        capture_id: str | None,
        attachments: list[dict[str, Any]],
    ) -> AgentRequest:
        review = self.review_service.daily()
        return AgentRequest(
            raw_text=raw_text,
            message_type=message_type,
            open_id=open_id,
            message_id=message_id,
            capture_id=capture_id,
            recent_captures=[
                capture.model_dump(mode="json", exclude={"metadata", "attachments"})
                for capture in self.repo.list_captures(limit=8)
            ],
            recent_actions=[action.model_dump(mode="json") for action in self.repo.list_actions(limit=30)],
            today_summary=[action.model_dump(mode="json") for action in review.sections.get("today", [])],
            overdue_summary=[action.model_dump(mode="json") for action in review.sections.get("overdue", [])],
            pending_summary=self._pending_confirmation_summary(),
            available_tools=[tool.value for tool in AgentToolName],
            project_brief=PROJECT_BRIEF,
            safety_rules=SAFETY_RULES,
            attachments=attachments,
        )

    def _compose_reply(self, response: AgentResponse, tool_results) -> str:
        tool_replies = [result.reply_text for result in tool_results if result.reply_text]
        if tool_replies:
            return "\n".join(tool_replies)
        errors = [result.error for result in tool_results if result.error]
        if errors:
            return "我处理时遇到问题，已记录下来：" + "；".join(errors[:3])
        if response.reply_text:
            return response.reply_text
        return "已收到。"

    def _pending_confirmation_summary(self) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for run in self.repo.list_agent_runs(status=None, limit=12):
            tool_prompts = []
            for result in run.tool_results_json:
                if result.get("needs_confirmation"):
                    tool_prompts.append(
                        {
                            "name": result.get("name"),
                            "reply_text": result.get("reply_text"),
                            "result": result.get("result", {}),
                        }
                    )
            response_needs_confirmation = bool(run.response_json.get("needs_confirmation"))
            if not response_needs_confirmation and not tool_prompts:
                continue
            raw_text = None
            if run.capture_id:
                try:
                    raw_text = self.repo.get_capture(run.capture_id).raw_text
                except KeyError:
                    raw_text = None
            summaries.append(
                {
                    "agent_run_id": run.id,
                    "capture_id": run.capture_id,
                    "raw_text": raw_text,
                    "intent": run.response_json.get("intent"),
                    "reply_text": run.response_json.get("reply_text"),
                    "tool_confirmations": tool_prompts[:3],
                    "created_at": run.created_at.isoformat(),
                }
            )
            if len(summaries) >= 3:
                break
        return summaries

    async def _safe_send_reply(self, open_id: str | None, text: str) -> dict[str, Any]:
        if not open_id:
            return {"ok": False, "error": "missing open_id", "text": text}
        try:
            data = await self.feishu.send_app_text(open_id, text)
            return {"ok": True, "response": data, "text": text}
        except Exception as exc:  # noqa: BLE001 - event callbacks should not retry only because reply failed
            return {"ok": False, "error": str(exc) or repr(exc), "text": text}

    def _reply_audit(self, reply_result: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": "send_feishu_reply",
            "ok": bool(reply_result.get("ok")),
            "result": reply_result,
            "error": reply_result.get("error"),
        }
