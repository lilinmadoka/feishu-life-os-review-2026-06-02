# Sanitized Source Export Manifest

Export date: 2026-06-03

Local source root:

```text
E:\learning\基于飞书做的助理系统\feishu-life-os
```

Repository destination:

```text
source/feishu-life-os/
```

## Included

- `app/`: FastAPI application, adapters, routers, services, workers, and core agent runtime.
- `app/core/planner.py`: new `PlannerService` implementation.
- `app/core/schemas.py`: includes `AssistantProposal` schema.
- `app/core/orchestrator.py`: provider -> planner -> risk/confirmation/tool execution wiring.
- `app/core/policy.py`: proposal write-safety checks.
- `app/core/tools.py`: concrete tool execution boundary and planning-only tool rejection.
- `app/core/providers.py`: proposal-first behavior for complex planning requests.
- `app/core/context_builder.py`: proposal context summaries.
- `app/core/context/`: Context Compiler, v2 context schemas, budget trimming, and first compressor set.
- `app/core/agent_response_schema.json`: structured provider response schema with optional proposal.
- `tests/`: unit and regression tests, including planning-layer coverage.
- `scripts/`: development, validation, local gateway, and dry-run helper scripts.
- `docs/`: project documentation from the source workspace.
- `validation/`: non-private validation summaries.
- `README.md`, `pyproject.toml`, `Makefile`, `railway.json`, `.gitignore`, `.env.example`.

## Excluded

- `.env` and `.env.*` except `.env.example`.
- `.data/`, SQLite databases, local attachment storage, screenshots, and image uploads.
- `.venv/`, Python bytecode, pytest/ruff caches, and other runtime caches.
- `handoff_package/`, generated zip archives, local logs, and prior review exports.
- `pushover.txt` and other local-only secret or credential files.

## Sanitization Notes

- The real `.env` was not copied.
- The source `.env.example` contains placeholder values only.
- A deprecated Railway deployment document had Feishu-style example token/table IDs replaced with placeholders before export.
- A token scan was run for common OpenAI/GitHub/Slack/Bearer/Feishu patterns after export.

## Planning Layer Changes In This Snapshot

- Added `AssistantProposal` for planner-first responses.
- Persisted proposals through existing `PlanDraft.payload["assistant_proposal"]`.
- Added `PlannerService` between provider responses and risk/tool execution.
- Kept write operations behind existing confirmation cards and `RiskPolicy`.
- Moved planning responsibility out of `ToolRouter`; direct planning-only tool execution is rejected.
- Added tests for vague long-term goals, proposal refinement, confirmation-to-tool conversion, and router rejection of planning-only calls.

## Context Compiler Changes In This Snapshot

- Added `ContextCapsule`, `AgentContextPackV2`, and `CompiledContext`.
- Added `ContextCompiler` as a dual-track wrapper around existing `build_agent_context()`.
- Added pending confirmation, active plan draft, and schedule availability compressors.
- Wired `CoreAgentOrchestrator` to include `context_v2` while keeping root `context_schema_version=1`.
- Wired provider intent/entity context extraction to consume compact `context_capsules`.
- Added tests for v1/v2 request compatibility, safe confirmation summaries, active plan draft summaries, schedule busy/free facts, and v2-first budget trimming.

## Validation Record

Latest local checks before this export:

```text
.\.venv\Scripts\python.exe -m pytest tests/test_core_agent_v2.py -q
92 passed

.\.venv\Scripts\python.exe -m pytest tests/test_context_compiler.py -q
5 passed

.\.venv\Scripts\python.exe -m pytest -q
154 passed

.\.venv\Scripts\python.exe -m ruff check app tests
All checks passed!
```
