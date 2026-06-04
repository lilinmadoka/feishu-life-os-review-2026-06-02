from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError

from app.core.decision_schemas import AssistantDecision
from app.core.schemas import AgentResponse


@runtime_checkable
class DecisionProvider(Protocol):
    name: str
    model: str | None
    last_used_legacy_adapter: bool

    def run_decision(self, request: dict[str, Any]) -> AssistantDecision:
        ...


class ModelDecisionProvider:
    """Thin model-first adapter over the existing provider interface."""

    def __init__(self, provider: Any):
        self.provider = provider
        self.name = f"model_decision:{getattr(provider, 'name', provider.__class__.__name__)}"
        self.model = getattr(provider, "model", None)
        self.last_used_legacy_adapter = False

    def run_decision(self, request: dict[str, Any]) -> AssistantDecision:
        native = getattr(self.provider, "run_decision", None)
        if callable(native):
            self.last_used_legacy_adapter = False
            return native(request)

        self.last_used_legacy_adapter = True
        response = self.provider.run(request)
        if not isinstance(response, AgentResponse):
            try:
                response = AgentResponse.model_validate(response)
            except ValidationError as exc:
                return AssistantDecision(
                    action="reply",
                    confidence=0.0,
                    reasoning_summary=f"legacy provider returned invalid AgentResponse: {exc}",
                    reply_to_user="模型输出无法安全解析，已记录但不会写入任何数据。",
                )
        return self._decision_from_legacy_response(response)

    def _decision_from_legacy_response(self, response: AgentResponse) -> AssistantDecision:
        return AssistantDecision(
            action="query",
            confidence=response.confidence,
            reasoning_summary="Legacy AgentResponse wrapped for model-first runtime compatibility.",
            reply_to_user=response.reply_to_user,
            referenced_context=[],
            query={"legacy_agent_response": response.model_dump(mode="json")},
        )
