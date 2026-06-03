from __future__ import annotations

from collections.abc import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.config import get_settings

CF_PUBLIC_HEADERS = {"cf-connecting-ip", "cf-ray", "cf-visitor", "cf-ipcountry"}
PUBLIC_ALLOWLIST_PREFIXES = ("/api/feishu/events", "/api/v2/feishu/events", "/api/v2/feishu/card", "/health")


class PublicTunnelProtectionMiddleware(BaseHTTPMiddleware):
    """Restrict routes exposed through Cloudflare Tunnel.

    Local requests remain unrestricted for development. Public Cloudflare requests
    can only hit health, Feishu events, or authenticated admin endpoints.
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        settings = get_settings()
        if not settings.public_tunnel_protection or not _is_cloudflare_request(request):
            return await call_next(request)

        path = request.url.path
        if path.startswith(PUBLIC_ALLOWLIST_PREFIXES):
            return await call_next(request)

        token = request.headers.get("x-admin-token")
        if settings.admin_api_token and token == settings.admin_api_token:
            return await call_next(request)

        return JSONResponse(
            status_code=403,
            content={
                "detail": "public tunnel access denied",
                "allowed_public_paths": list(PUBLIC_ALLOWLIST_PREFIXES),
            },
        )


def _is_cloudflare_request(request: Request) -> bool:
    headers = {key.lower() for key in request.headers}
    return bool(headers & CF_PUBLIC_HEADERS)
