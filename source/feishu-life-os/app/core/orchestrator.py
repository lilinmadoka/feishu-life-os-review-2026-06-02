from __future__ import annotations

import json
import logging
import time
from typing import Any
from zoneinfo import ZoneInfo

from app.core.context_builder import build_agent_context
from app.core.feishu_native import FeishuNativeAdapter
from app.core.planner import PlannerService
from app.core.policy import PolicyViolation, RiskPolicy
from app.core.providers import (
    CoreAgentProvider,
    CoreAgentProviderError,
    CoreAgentProviderUnavailable,
)
from app.core.schemas import CaptureIn, OrchestratorResult, ProcessedStatus
from app.core.store import StateStore
from app.core.tools import ToolRouter

LOGGER = logging.getLogger("lifeos.agent_runtime")


class CoreAgentOrchestrator:
    def __init__(self, store: StateStore, provider: CoreAgentProvider, feishu: FeishuNativeAdapter, tz: ZoneInfo):
        self.store = store
        self.provider = provider
        self.feishu = feishu
        self.tz = tz
        self.router = ToolRouter(store, feishu, tz)
        self.planner = PlannerService(store, feishu, tz, self.router)
        self.policy = RiskPolicy()

    async def process_capture(self, capture_input: CaptureIn) -> OrchestratorResult:
        self.store.migrate()
        existing = self.store.find_capture_by_source_message(capture_input.source, capture_input.source_message_id)
        if existing and existing.processed_status != ProcessedStatus.failed:
            self._log_runtime(
                {
                    "status": "duplicate_ignored",
                    "capture_id": existing.id,
                    "event_id": capture_input.source_event_id,
                    "message_id": capture_input.source_message_id,
                    "provider_name": self.provider.name,
                    "agent_run_id": None,
                    "used_fallback": self._is_fallback_provider(),
                    "tool_calls": [],
                    "reply_text": "",
                }
            )
            return OrchestratorResult(capture_id=existing.id, agent_run_id="", reply_text="重复消息已忽略。")
        capture = self.store.create_capture(capture_input)
        request = self._build_agent_request(capture.model_dump(mode="json"))
        run = self.store.create_agent_run(
            capture_id=capture.id,
            provider=self.provider.name,
            model=getattr(self.provider, "model", None),
            input_json=request,
        )
        started = time.perf_counter()
        try:
            response = self.provider.run(request)
            self.policy.validate_response(response)
        except (CoreAgentProviderUnavailable, CoreAgentProviderError, PolicyViolation) as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            self.store.fail_agent_run(run.id, str(exc), latency_ms)
            self.store.update_capture_status(capture.id, ProcessedStatus.needs_review)
            reply = "智能处理器不可用或返回结果不符合安全策略，已记录消息但不会自动处理。"
            await self.feishu.send_text(capture.sender_id, reply)
            self._log_runtime(
                {
                    "status": "failed",
                    "capture_id": capture.id,
                    "event_id": capture.source_event_id,
                    "message_id": capture.source_message_id,
                    "provider_name": self.provider.name,
                    "intent": None,
                    "agent_run_id": run.id,
                    "used_fallback": self._is_fallback_provider(),
                    "tool_calls": [],
                    "reply_text": reply,
                    "error": str(exc),
                }
            )
            return OrchestratorResult(capture_id=capture.id, agent_run_id=run.id, reply_text=reply)

        planning = await self.planner.plan_response(
            response,
            request,
            agent_run_id=run.id,
            capture_id=capture.id,
            sender_id=capture.sender_id,
        )
        tool_results = list(planning.tool_results)
        tool_reply = planning.reply_text
        confirmation_id = planning.confirmation_id
        executed_calls = list(planning.tool_calls)
        if executed_calls:
            routed_results, routed_reply, routed_confirmation_id = await self.router.execute_calls(
                executed_calls,
                agent_run_id=run.id,
                capture_id=capture.id,
                sender_id=capture.sender_id,
            )
            tool_results.extend(routed_results)
            tool_reply = routed_reply or tool_reply
            confirmation_id = routed_confirmation_id or confirmation_id
        final_reply = tool_reply or response.reply_to_user or "已收到。"
        direct_reply_sent = any(result.get("tool_name") == "send_feishu_reply" for result in tool_results)
        if not confirmation_id and not direct_reply_sent and not planning.card_sent:
            await self.feishu.send_text(capture.sender_id, final_reply)
        latency_ms = int((time.perf_counter() - started) * 1000)
        self.store.complete_agent_run(
            run.id,
            output_json=response.model_dump(mode="json"),
            tool_calls_json=[call.model_dump(mode="json") for call in executed_calls],
            latency_ms=latency_ms,
        )
        self.store.update_capture_status(capture.id, ProcessedStatus.processed)
        self._log_runtime(
            {
                "status": "done",
                "capture_id": capture.id,
                "event_id": capture.source_event_id,
                "message_id": capture.source_message_id,
                "provider_name": self.provider.name,
                "intent": response.intent,
                "agent_run_id": run.id,
                "used_fallback": self._is_fallback_provider(),
                "tool_calls": [call.model_dump(mode="json") for call in executed_calls],
                "proposal_id": planning.proposal_id,
                "reply_text": final_reply,
            }
        )
        return OrchestratorResult(
            capture_id=capture.id,
            agent_run_id=run.id,
            reply_text=final_reply,
            tool_results=tool_results,
            confirmation_id=confirmation_id,
            proposal_id=planning.proposal_id,
        )

    def _build_agent_request(self, capture: dict[str, Any]) -> dict[str, Any]:
        return build_agent_context(self.store, capture, self.tz).model_dump(mode="json")

    def _is_fallback_provider(self) -> bool:
        return self.provider.name in {"mock_provider", "rules_provider", "rules_fallback"}

    def _log_runtime(self, payload: dict[str, Any]) -> None:
        LOGGER.info("agent_runtime %s", json.dumps(payload, ensure_ascii=False, default=str))
