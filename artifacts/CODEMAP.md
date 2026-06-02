# Codemap

This is a compact file responsibility map for reviewers.

## Runtime

- `app/main.py`: FastAPI app factory, middleware, routers, health.
- `app/config.py`: Pydantic settings and env var definitions.
- `app/dependencies.py`: dependency injection and provider selection.
- `app/security.py`: public tunnel protection middleware.

## v2 agent core

- `app/core/schemas.py`: domain and agent schemas.
- `app/core/store.py`: v2 SQLite tables and CRUD.
- `app/core/context_builder.py`: context pack for LLM.
- `app/core/providers.py`: provider implementations, prompt, intent/entity mapping.
- `app/core/policy.py`: risk policy and confirmation rules.
- `app/core/tools.py`: tool execution, confirmation lifecycle, plan/habit/course scheduling.
- `app/core/orchestrator.py`: capture processing runtime.
- `app/core/feishu_native.py`: v2 Feishu abstraction.
- `app/core/relative_time.py`: user-day rollover helpers.

## API routers

- `app/routers/core_agent.py`: v2 local and Feishu agent endpoints, Feishu card callbacks.
- `app/routers/feishu.py`: legacy Feishu endpoint.
- `app/routers/captures.py`: legacy capture API.
- `app/routers/actions.py`: legacy action API.
- `app/routers/reviews.py`: daily review API.
- `app/routers/codex.py`: Codex review jobs API.

## Integrations

- `app/adapters/feishu_client.py`: Feishu OpenAPI HTTP client and payload builders.
- `app/adapters/pushover_client.py`: Pushover emergency notifications.

## Workers

- `app/workers/reminder_worker.py`: reminders, strong reminders, daily review, card-action helpers.
- `app/workers/codex_review_worker.py`: Codex review job worker.

## Legacy stack

- `app/agents/*`: legacy agent orchestrator/provider/tool protocol.
- `app/services/*`: legacy capture, extraction, review, sync services.
- `app/models.py`, `app/database.py`: legacy model/repository plus shared database primitives.

## Tests

- `tests/test_core_agent_v2.py`: main v2 behavior suite.
- `tests/test_reminder_worker.py`: reminder worker suite.
- `tests/test_feishu_events_and_codex.py`: legacy Feishu and sync suite.
- `tests/test_public_tunnel_protection.py`: middleware protection.
- `tests/test_time_parser.py`: time parser.
- `tests/test_extraction_service.py`: rule extractor.
- `tests/test_repository_and_api.py`: legacy repository/API.
- `tests/test_codex_review_worker.py`: review worker.

