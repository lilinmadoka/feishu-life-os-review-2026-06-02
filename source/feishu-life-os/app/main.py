from __future__ import annotations

from fastapi import FastAPI

from app.config import get_settings
from app.dependencies import get_repo
from app.routers import actions, captures, codex, core_agent, feishu, reviews
from app.security import PublicTunnelProtectionMiddleware


def create_app() -> FastAPI:
    settings = get_settings()
    get_repo().migrate()
    app = FastAPI(
        title="Feishu Life OS",
        version="0.1.0",
        description="Personal capture, triage, reminder, and review system prepared for Feishu integration.",
    )
    app.add_middleware(PublicTunnelProtectionMiddleware)
    app.include_router(captures.router)
    app.include_router(actions.router)
    app.include_router(reviews.router)
    app.include_router(feishu.router)
    app.include_router(core_agent.router)
    app.include_router(codex.router)

    @app.get("/health")
    def health():
        return {"ok": True, "env": settings.app_env, "sync_mode": settings.feishu_sync_mode}

    return app


app = create_app()
