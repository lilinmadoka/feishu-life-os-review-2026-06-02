from fastapi.testclient import TestClient

from app.config import get_settings
from app.dependencies import get_feishu_client, get_repo
from app.main import create_app


def reset_settings():
    get_settings.cache_clear()
    get_repo.cache_clear()
    get_feishu_client.cache_clear()


def test_local_docs_are_available_without_admin_token(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "lifeos.sqlite3"))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("PUBLIC_TUNNEL_PROTECTION", "true")
    monkeypatch.setenv("ADMIN_API_TOKEN", "secret")
    reset_settings()
    client = TestClient(create_app())
    response = client.get("/docs")
    assert response.status_code == 200


def test_cloudflare_public_docs_are_blocked_without_admin_token(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "lifeos.sqlite3"))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("PUBLIC_TUNNEL_PROTECTION", "true")
    monkeypatch.setenv("ADMIN_API_TOKEN", "secret")
    reset_settings()
    client = TestClient(create_app())
    response = client.get("/docs", headers={"cf-connecting-ip": "203.0.113.10"})
    assert response.status_code == 403
    assert response.json()["detail"] == "public tunnel access denied"


def test_cloudflare_public_docs_can_use_admin_token(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "lifeos.sqlite3"))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("PUBLIC_TUNNEL_PROTECTION", "true")
    monkeypatch.setenv("ADMIN_API_TOKEN", "secret")
    reset_settings()
    client = TestClient(create_app())
    response = client.get(
        "/docs",
        headers={"cf-connecting-ip": "203.0.113.10", "x-admin-token": "secret"},
    )
    assert response.status_code == 200


def test_cloudflare_public_health_is_allowed(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "lifeos.sqlite3"))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("PUBLIC_TUNNEL_PROTECTION", "true")
    monkeypatch.setenv("ADMIN_API_TOKEN", "secret")
    reset_settings()
    client = TestClient(create_app())
    response = client.get("/health", headers={"cf-connecting-ip": "203.0.113.10"})
    assert response.status_code == 200
