from __future__ import annotations

import json
import logging
import time
from typing import Any
from zoneinfo import ZoneInfo

from app.core.context import ContextCompiler
from app.core.feishu_native import FeishuNativeAdapter
from app.core.observability import NullTraceEmitter, TraceEmitter
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
    def __init__(
        self,
        store: StateStore,
        provider: CoreAgentProvider,
        feishu: FeishuNativeAdapter,
        tz: ZoneInfo,
        trace_emitter: TraceEmitter | None = None,
    ):
        self.store = store
        self.provider = provider
        self.feishu = feishu
        self.tz = tz
        self.trace = trace_emitter or NullTraceEmitter()
        self.router = ToolRouter(store, feishu, tz)
        self.planner = PlannerService(store, feishu, tz, self.router)
        self.policy = RiskPolicy()
        self.context_compiler = ContextCompiler(store, tz)

    async def process_capture(self, capture_input: CaptureIn) -> OrchestratorResult:
        trace = self.trace.start_trace(
            workflow_type=self._workflow_type(capture_input.source),
            sender_id=capture_input.sender_id,
            attrs={
                "source": capture_input.source,
                "content_type": capture_input.content_type,
                "raw_text": capture_input.raw_text,
                "source_message_id": capture_input.source_message_id,
            },
        )
        trace_id = trace.trace_id
        try:
            self.store.migrate()
            with self.trace.span(trace_id, "capture.lookup", component="orchestrator", lane="ingest") as lookup_span:
                existing = self.store.find_capture_by_source_message(capture_input.source, capture_input.source_message_id)
                lookup_span.add_attrs({"found": bool(existing), "source": capture_input.source})
            if existing and existing.processed_status != ProcessedStatus.failed:
                self.trace.update_trace(
                    trace_id,
                    capture_id=existing.id,
                    root_entity_type="capture",
                    root_entity_id=existing.id,
                    attrs={"duplicate_ignored": True},
                )
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
                self.trace.end_trace(trace_id, status="ok", summary="duplicate ignored", attrs={"capture_id": existing.id})
                return OrchestratorResult(capture_id=existing.id, agent_run_id="", reply_text="重复消息已忽略。")

            with self.trace.span(trace_id, "capture.create", component="orchestrator", lane="ingest") as capture_span:
                capture = self.store.create_capture(capture_input)
                capture_span.add_attrs({"capture_id": capture.id})
            self.trace.update_trace(trace_id, capture_id=capture.id, root_entity_type="capture", root_entity_id=capture.id)

            with self.trace.span(trace_id, "context.compile", component="context", lane="context") as context_span:
                request = self._build_agent_request(capture.model_dump(mode="json"))
                context_v2 = request.get("context_v2") if isinstance(request.get("context_v2"), dict) else {}
                context_span.add_attrs(
                    {
                        "context_size_bytes": len(json.dumps(request, ensure_ascii=False, default=str).encode("utf-8")),
                        "capsule_count": len(context_v2.get("capsules") or []),
                        "context_schema_version": request.get("context_schema_version"),
                    }
                )

            run = self.store.create_agent_run(
                capture_id=capture.id,
                provider=self.provider.name,
                model=getattr(self.provider, "model", None),
                input_json=request,
            )
            self.trace.update_trace(trace_id, agent_run_id=run.id)
            started = time.perf_counter()

            try:
                with self.trace.span(
                    trace_id,
                    "provider.run",
                    component="provider",
                    lane="model",
                    attrs={"provider_name": self.provider.name, "model": getattr(self.provider, "model", None)},
                ) as provider_span:
                    response = self.provider.run(request)
                    provider_span.add_attrs(
                        {
                            "intent": response.intent,
                            "confidence": response.confidence,
                            "tool_call_count": len(response.tool_calls),
                            "has_assistant_proposal": response.assistant_proposal is not None,
                        }
                    )
                with self.trace.span(trace_id, "policy.validate_response", component="policy", lane="guard") as policy_span:
                    self.policy.validate_response(response)
                    policy_span.add_attrs({"intent": response.intent, "tool_call_count": len(response.tool_calls)})
            except (CoreAgentProviderUnavailable, CoreAgentProviderError, PolicyViolation) as exc:
                latency_ms = int((time.perf_counter() - started) * 1000)
                self.store.fail_agent_run(run.id, str(exc), latency_ms)
                self.store.update_capture_status(capture.id, ProcessedStatus.needs_review)
                reply = "智能处理器不可用或返回结果不符合安全策略，已记录消息但不会自动处理。"
                with self.trace.span(trace_id, "final_reply.complete_run", component="orchestrator", lane="state"):
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
                self.trace.end_trace(trace_id, status="failed", summary=str(exc), attrs={"capture_id": capture.id, "agent_run_id": run.id})
                return OrchestratorResult(capture_id=capture.id, agent_run_id=run.id, reply_text=reply)

            with self.trace.span(trace_id, "planner.plan_response", component="planner", lane="planner") as planner_span:
                planning = await self.planner.plan_response(
                    response,
                    request,
                    agent_run_id=run.id,
                    capture_id=capture.id,
                    sender_id=capture.sender_id,
                )
                planner_span.add_attrs(
                    {
                        "proposal_id": planning.proposal_id,
                        "confirmation_id": planning.confirmation_id,
                        "tool_call_count": len(planning.tool_calls),
                        "card_sent": planning.card_sent,
                    }
                )

            tool_results = list(planning.tool_results)
            tool_reply = planning.reply_text
            confirmation_id = planning.confirmation_id
            executed_calls = list(planning.tool_calls)
            with self.trace.span(
                trace_id,
                "tool_router.execute_calls",
                component="tool_router",
                lane="execute",
                status="running" if executed_calls else "skipped",
                attrs={"tool_call_count": len(executed_calls), "tool_names": [call.tool_name for call in executed_calls]},
            ) as router_span:
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
                    router_span.add_attrs({"result_count": len(routed_results), "confirmation_id": routed_confirmation_id})

            final_reply = tool_reply or response.reply_to_user or "已收到。"
            with self.trace.span(trace_id, "final_reply.complete_run", component="orchestrator", lane="state") as final_span:
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
                final_span.add_attrs(
                    {
                        "latency_ms": latency_ms,
                        "confirmation_id": confirmation_id,
                        "direct_reply_sent": direct_reply_sent,
                        "reply_text": final_reply,
                    }
                )

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
            self.trace.end_trace(
                trace_id,
                status="ok",
                summary=f"{response.intent or 'unknown'} processed",
                attrs={"capture_id": capture.id, "agent_run_id": run.id, "intent": response.intent},
            )
            return OrchestratorResult(
                capture_id=capture.id,
                agent_run_id=run.id,
                reply_text=final_reply,
                tool_results=tool_results,
                confirmation_id=confirmation_id,
                proposal_id=planning.proposal_id,
            )
        except Exception as exc:
            self.trace.end_trace(trace_id, status="failed", summary=str(exc))
            raise

    def _build_agent_request(self, capture: dict[str, Any]) -> dict[str, Any]:
        return self.context_compiler.compile(capture).provider_request(max_bytes=self.context_compiler.max_bytes)

    def _is_fallback_provider(self) -> bool:
        return self.provider.name in {"mock_provider", "rules_provider", "rules_fallback"}

    def _workflow_type(self, source: str) -> str:
        if source == "feishu":
            return "feishu_message"
        if source == "local_api":
            return "local_agent_message"
        return f"{source}_message" if source else "agent_message"

    def _log_runtime(self, payload: dict[str, Any]) -> None:
        LOGGER.info("agent_runtime %s", json.dumps(payload, ensure_ascii=False, default=str))
