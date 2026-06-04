from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient

from app.config import get_settings
from app.core.feishu_native import MockFeishuNativeAdapter
from app.core.observability import SQLiteTraceEmitter, SQLiteTraceStore
from app.core.observability.redaction import redact_mapping
from app.core.orchestrator import CoreAgentOrchestrator
from app.core.providers import MockAgentProvider
from app.core.schemas import CaptureIn
from app.core.store import StateStore
from app.database import Repository
from app.dependencies import (
    get_core_feishu_adapter,
    get_core_provider,
    get_core_store,
    get_observability_store,
    get_repo,
)
from app.main import create_app

TZ_NAME = "Asia/Shanghai"


class BadString:
    def __str__(self) -> str:
        raise RuntimeError("cannot stringify")


def reset_dependencies() -> None:
    get_settings.cache_clear()
    get_repo.cache_clear()
    get_core_store.cache_clear()
    get_core_provider.cache_clear()
    get_core_feishu_adapter.cache_clear()
    get_observability_store.cache_clear()


def configure_app(monkeypatch, tmp_path, *, enabled: bool) -> TestClient:
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "lifeos.sqlite3"))
    monkeypatch.setenv("DATABASE_URL", "")
    monkeypatch.setenv("TIMEZONE", TZ_NAME)
    monkeypatch.setenv("CORE_AGENT_PROVIDER", "mock_provider")
    monkeypatch.setenv("FEISHU_APP_ID", "")
    monkeypatch.setenv("FEISHU_APP_SECRET", "")
    monkeypatch.setenv("OBSERVABILITY_ENABLED", "true" if enabled else "false")
    monkeypatch.setenv("ADMIN_API_TOKEN", "admin-token")
    reset_dependencies()
    return TestClient(create_app())


def test_disabled_observability_is_noop_and_behavior_unchanged(monkeypatch, tmp_path):
    client = configure_app(monkeypatch, tmp_path, enabled=False)

    response = client.post("/api/v2/agent/messages", json={"raw_text": "今天还有什么任务？", "source_message_id": "obs_disabled"})

    assert response.status_code == 200
    assert response.json()["capture_id"].startswith("cap2_")
    assert get_observability_store().list_traces() == []


def test_enabled_observability_records_trace_and_spans_for_agent_message(monkeypatch, tmp_path):
    client = configure_app(monkeypatch, tmp_path, enabled=True)

    response = client.post("/api/v2/agent/messages", json={"raw_text": "今天还有什么任务？", "source_message_id": "obs_enabled"})

    assert response.status_code == 200
    traces = client.get("/api/v2/observability/traces", headers={"x-admin-token": "admin-token"}).json()["items"]
    assert len(traces) == 1
    trace_id = traces[0]["trace_id"]
    detail = client.get(f"/api/v2/observability/traces/{trace_id}", headers={"x-admin-token": "admin-token"}).json()
    span_names = {span["name"] for span in detail["spans"]}

    assert detail["trace"]["status"] == "ok"
    assert detail["trace"]["capture_id"] == response.json()["capture_id"]
    assert {
        "capture.lookup",
        "capture.create",
        "context.compile",
        "provider.run",
        "policy.validate_response",
        "planner.plan_response",
        "tool_router.execute_calls",
        "final_reply.complete_run",
    }.issubset(span_names)
    assert "local_user" not in json.dumps(detail, ensure_ascii=False)

    artifacts = client.get(f"/api/v2/observability/traces/{trace_id}/artifacts", headers={"x-admin-token": "admin-token"}).json()
    context_artifact = next(item for item in artifacts["artifacts"] if item["kind"] == "context_v2")
    provider_artifact = next(item for item in artifacts["artifacts"] if item["kind"] == "provider_output")
    assert context_artifact["redaction"] == "summary_only"
    assert "capsules_generated" in context_artifact["payload_json"]
    assert "capsules" in context_artifact["payload_json"]
    assert provider_artifact["payload_json"]["intent"] == "query_today"
    assert provider_artifact["payload_json"]["tool_names"] == ["query_today"]
    assert any(diff["entity_type"] == "agent_run" and diff["operation"] == "complete" for diff in artifacts["state_diffs"])

    timeline = client.get(f"/api/v2/observability/traces/{trace_id}/timeline", headers={"x-admin-token": "admin-token"}).json()
    graph = client.get(f"/api/v2/observability/traces/{trace_id}/graph", headers={"x-admin-token": "admin-token"}).json()
    assert {lane["name"] for lane in timeline["lanes"]} >= {"ingest", "context", "model", "guard", "planner", "execute", "state"}
    assert graph["nodes"]


def test_observability_ui_requires_admin_token_and_serves_static_dashboard(monkeypatch, tmp_path):
    monkeypatch.setenv("OBSERVABILITY_UI_REQUIRE_ADMIN_TOKEN", "false")
    client = configure_app(monkeypatch, tmp_path, enabled=True)

    assert client.get("/api/v2/observability/ui").status_code == 403
    assert client.get("/api/v2/observability/ui?admin_token=wrong").status_code == 403
    response = client.get("/api/v2/observability/ui", headers={"x-admin-token": "admin-token"})

    assert response.status_code == 200
    assert "Visual Observability" in response.text
    assert "/api/v2/observability/traces" in response.text
    assert "https://" not in response.text

    query_response = client.get("/api/v2/observability/ui?admin_token=admin-token")
    assert query_response.status_code == 200
    assert "Visual Observability" in query_response.text


def test_observability_routes_require_admin_token(monkeypatch, tmp_path):
    client = configure_app(monkeypatch, tmp_path, enabled=True)

    response = client.get("/api/v2/observability/traces")

    assert response.status_code == 403


def test_trace_write_failure_does_not_fail_main_request(tmp_path):
    class BrokenTraceStore:
        def __getattr__(self, name):
            def fail(*args, **kwargs):
                raise RuntimeError(f"broken {name}")

            return fail

    repo = Repository(str(tmp_path / "lifeos.sqlite3"))
    repo.migrate()
    store = StateStore(repo)
    store.migrate()
    orchestrator = CoreAgentOrchestrator(
        store,
        MockAgentProvider(get_settings().tzinfo),
        MockFeishuNativeAdapter(),
        get_settings().tzinfo,
        trace_emitter=SQLiteTraceEmitter(BrokenTraceStore()),
    )

    result = asyncio.run(
        orchestrator.process_capture(
            CaptureIn(
                source="test",
                source_message_id="broken_obs",
                sender_id="ou_test",
                chat_id="chat_test",
                raw_text="今天还有什么任务？",
            )
        )
    )

    assert result.capture_id.startswith("cap2_")
    assert store.list_agent_runs()


def test_redaction_masks_sender_open_id_and_truncates_raw_text():
    redacted = redact_mapping(
        {
            "sender_id": "ou_real_sender",
            "open_id": "ou_real_open",
            "open_ids": ["ou_real_open_plural"],
            "user_ids": ["user_real_plural"],
            "union_ids": ["union_real_plural"],
            "attendee_open_ids": ["ou_real_a", "ou_real_b"],
            "reply_text": "y" * 220,
            "attachment": {"local_path": r"C:\secret\attachments\private.png"},
            "raw_text": "x" * 220,
        }
    )
    dumped = json.dumps(redacted, ensure_ascii=False)

    assert "ou_real_sender" not in dumped
    assert "ou_real_open" not in dumped
    assert "ou_real_open_plural" not in dumped
    assert "user_real_plural" not in dumped
    assert "union_real_plural" not in dumped
    assert "ou_real_a" not in dumped
    assert r"C:\secret\attachments\private.png" not in dumped
    assert redacted["sender_id"]["hash"].startswith("sha256:")
    assert redacted["open_id"]["hash"].startswith("sha256:")
    assert redacted["open_ids"][0]["hash"].startswith("sha256:")
    assert redacted["user_ids"][0]["hash"].startswith("sha256:")
    assert redacted["union_ids"][0]["hash"].startswith("sha256:")
    assert redacted["attendee_open_ids"][0]["hash"].startswith("sha256:")
    assert redacted["raw_text"]["truncated"] is True
    assert len(redacted["raw_text"]["text"]) == 160
    assert redacted["reply_text"]["truncated"] is True
    assert redacted["attachment"]["local_path"]["basename"] == "private.png"


def test_large_or_full_artifacts_are_summary_only(tmp_path):
    repo = Repository(str(tmp_path / "lifeos.sqlite3"))
    repo.migrate()
    store = SQLiteTraceStore(repo)
    store.migrate()
    emitter = SQLiteTraceEmitter(store, max_artifact_bytes=800, capture_full_payload=False)
    trace = emitter.start_trace(workflow_type="test", sender_id="ou_secret")

    emitter.artifact(
        trace.trace_id,
        kind="provider_input",
        label="oversized",
        redaction="full_local",
        payload_json={"prompt": "secret prompt" * 200, "open_id": "ou_secret"},
    )

    detail = store.get_trace(trace.trace_id)
    assert detail is not None
    artifact = detail.artifacts[0]
    dumped = json.dumps(artifact.model_dump(mode="json"), ensure_ascii=False)
    assert artifact.redaction == "summary_only"
    assert artifact.size_bytes is not None and artifact.size_bytes <= 800
    assert "ou_secret" not in dumped
    assert "secret prompt" not in dumped


def test_bad_observability_payload_does_not_fail_process_capture(monkeypatch, tmp_path):
    monkeypatch.setenv("TIMEZONE", TZ_NAME)
    reset_dependencies()
    repo = Repository(str(tmp_path / "lifeos.sqlite3"))
    repo.migrate()
    store = StateStore(repo)
    store.migrate()
    trace_store = SQLiteTraceStore(repo)
    trace_store.migrate()
    orchestrator = CoreAgentOrchestrator(
        store,
        MockAgentProvider(get_settings().tzinfo),
        MockFeishuNativeAdapter(),
        get_settings().tzinfo,
        trace_emitter=SQLiteTraceEmitter(trace_store),
    )

    def bad_context_summary(_compiled_context, _request):
        return {
            "attrs": {"raw_text": BadString(), "open_ids": [BadString()]},
            "artifact": {"prompt": BadString(), "custom_object": BadString()},
        }

    orchestrator._context_observability_summary = bad_context_summary

    result = asyncio.run(
        orchestrator.process_capture(
            CaptureIn(
                source="test",
                source_message_id="bad_payload_obs",
                sender_id="ou_test",
                chat_id="chat_test",
                raw_text="今天还有什么任务？",
            )
        )
    )

    assert result.capture_id.startswith("cap2_")
    assert store.list_agent_runs()
