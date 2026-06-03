# Project Scan

Scanned on 2026-05-28.

## Current Directory Structure

- `app/`: FastAPI app, routers, legacy agent layer, adapters, services, workers.
- `app/core/`: new Agent-first v2 core added in this pass.
- `docs/`: product, setup, security, runbook, Agent-first notes.
- `scripts/`: local gateway scripts, seed/validation helpers, Bitable schema.
- `tests/`: unit and integration tests.
- `validation/`: generated e2e results.

## Existing Entrypoints

- `app.main:create_app`
- Legacy Feishu callback: `POST /api/feishu/events`
- New v2 Feishu callback: `POST /api/v2/feishu/events`
- New local v2 test endpoint: `POST /api/v2/agent/messages`
- Health: `GET /health`
- Local gateway: `scripts/start_local_gateway.ps1`

## Existing Config

- `.env.example` contains SQLite, Feishu app credentials, Bitable ids, sync mode, Codex CLI path, tunnel settings.
- `.env` is local only and is not documented as safe to commit.

## Existing Tests

- Legacy extraction/time parser/repository tests.
- Legacy Feishu event + Codex tests.
- Public tunnel protection tests.
- Reminder worker tests.
- New `tests/test_core_agent_v2.py` covers the v2 vertical loop.

## Reusable Parts

- FastAPI project layout.
- SQLite repository patterns.
- Feishu OpenAPI client token handling and text send.
- Existing Cloudflare Tunnel scripts and public protection middleware.
- Codex CLI invocation strategy.
- Bitable/Task/Calendar adapter stubs from `FeishuClient`.

## Directions To Isolate

- Legacy `RuleBasedExtractor` remains only for old API/fallback; it is not the v2 decision layer.
- Legacy `CodexReviewWorker` remains for historical review jobs only.
- Multi-dimensional table sync remains audit/background view, not primary interaction.
- Desktop automation directions are not part of this project.
