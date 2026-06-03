from __future__ import annotations

from typing import Protocol

from app.core.context.schemas import ContextCapsule
from app.core.context_builder import AgentContextPack
from app.core.store import StateStore


class ContextCompressor(Protocol):
    domain: str

    def compress(self, *, store: StateStore, legacy_pack: AgentContextPack, purpose: str) -> list[ContextCapsule]:
        ...
