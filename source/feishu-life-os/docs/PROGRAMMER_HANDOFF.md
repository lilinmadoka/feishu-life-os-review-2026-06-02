# Programmer Handoff

Updated: 2026-05-29

## Project Summary

This repository is a local-first Feishu personal assistant prototype. Feishu is the user-facing channel. The FastAPI service receives captures/messages, stores evidence in SQLite, asks an Agent provider to return structured tool calls, and then executes those tool calls through a controlled ToolRouter.

The current focus is the v2 "Agent-first" runtime:

```text
Feishu message/card callback
-> app.routers.core_agent / app.routers.feishu
-> CoreAgentOrchestrator
-> CoreAgentProvider
-> RiskPolicy
-> ToolRouter
-> SQLite StateStore + Feishu native adapter
```

## Current Runtime State

- Active core provider in `.env`: `CORE_AGENT_PROVIDER=lm_studio_provider`
- LM Studio base URL: `http://127.0.0.1:1234/v1`
- Current local model identifier: `gemma-4-e4b-it`
- Feishu sync mode: `bitable`
- Local FastAPI port: `127.0.0.1:8000`
- Cloudflare quick tunnel is used for Feishu callbacks.
- The actual `.env` contains private Feishu app credentials and table tokens and is intentionally excluded from the handoff package.

## What Works

- FastAPI app starts locally.
- `/health` responds locally.
- `/api/v2/agent/messages` works for local Agent-style testing.
- `/api/v2/feishu/events` handles Feishu message events.
- `/api/v2/feishu/card` handles confirmation/cancel card callbacks.
- SQLite migrations run through repository/store startup.
- ToolRouter records ToolRuns and AgentRuns.
- RiskPolicy prevents query intents from mutating state.
- Create/update/cancel/schedule-block operations require confirmation.
- LM Studio provider can call a local model and return structured intent JSON. The runtime keeps `LM_STUDIO_USE_NATIVE_CHAT=false` and uses the stable OpenAI-compatible `/v1/chat/completions` endpoint. The prompt is intentionally compact so the default 4k local instance is enough for normal routing.
- Local smoke test for `明天我都啥时间有空？` returned `query_availability` and produced free-time output.

## Important Caveats

- This folder is not currently a git repository. Treat the package as a source snapshot.
- The local `.env` has real secrets and is not included in the package. Use `.env.example` plus the redacted configuration notes.
- `.data/lifeos.sqlite3` is not included because it may contain personal captures/messages. The package includes schema only.
- LM Studio `response_format=json_schema` caused context-length errors with this local model/server combination, so the current default is `LM_STUDIO_RESPONSE_FORMAT=none`. The provider still validates returned JSON locally with Pydantic. Local `.env` currently uses `LM_STUDIO_MODEL=gemma-4-e4b-it`, sets `LM_STUDIO_CONTEXT_LENGTH=0` to avoid loading a large KV cache, and sets `LM_STUDIO_MAX_TOKENS=512`.
- The legacy `app.agents.*` flow still exists. The current real Feishu runtime should be evaluated through `app.core.*`.
- Some older Chinese documentation may display garbled text in non-UTF-8 terminals. Read files as UTF-8 in an editor.

## Main Entry Points

- App factory: `app/main.py`
- Dependency wiring: `app/dependencies.py`
- Settings: `app/config.py`
- v2 routers: `app/routers/core_agent.py`, `app/routers/feishu.py`
- Core orchestrator: `app/core/orchestrator.py`
- Core provider implementations: `app/core/providers.py`
- Safety policy: `app/core/policy.py`
- Tool execution: `app/core/tools.py`
- State store: `app/core/store.py`
- SQLite repository/migrations: `app/database.py`
- Feishu API adapter: `app/adapters/feishu_client.py`
- Native Feishu v2 adapter: `app/core/feishu_native.py`

## Run Locally

```powershell
cd "E:\learning\基于飞书做的助理系统\feishu-life-os"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check app tests
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_local_gateway.ps1
```

LM Studio:

```powershell
lms server start
lms load gemma-4-e4b-it --identifier gemma-4-e4b-it -y
```

Local smoke test:

```powershell
Invoke-RestMethod `
  -Uri "http://127.0.0.1:8000/api/v2/agent/messages" `
  -Method Post `
  -ContentType "application/json" `
  -Body '{"raw_text":"明天我都啥时间有空？","source_message_id":"local_test_001"}'
```

## Validation Snapshot

Last checked in this handoff pass:

- `python -m pytest`: 60 passed
- `python -m ruff check app tests`: all checks passed
- Local API smoke test: passed with `query_availability`

## Suggested Review Order

1. Read this file, then `docs/ARCHITECTURE.md`, `docs/AGENT_PROTOCOL.md`, and `docs/PROJECT_STATE.md`.
2. Inspect `app/core/orchestrator.py`, `app/core/providers.py`, `app/core/tools.py`, `app/core/policy.py`.
3. Run tests and a local smoke test.
4. Review Feishu callback and card behavior with real Feishu credentials.
5. Decide whether LM Studio model quality is sufficient or replace with a stronger OpenAI-compatible provider.
