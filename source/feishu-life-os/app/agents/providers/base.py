from __future__ import annotations

from typing import Protocol

from app.agents.models import AgentRequest, AgentResponse


class AgentProviderError(RuntimeError):
    pass


class AgentProviderUnavailable(AgentProviderError):
    pass


class AgentProvider(Protocol):
    name: str

    def run(self, request: AgentRequest) -> AgentResponse:
        ...

