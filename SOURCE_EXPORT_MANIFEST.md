# Sanitized Source Export Manifest

Export date: 2026-06-05

Local source root:

```text
E:\learning\...\feishu-life-os
```

Repository destination:

```text
source/feishu-life-os/
```

## Included

- `app/`: FastAPI application, adapters, routers, services, workers, and core agent runtime.
- `app/core/planner.py`: `PlannerService` implementation.
- `app/core/schemas.py`: includes `AssistantProposal` schema.
- `app/core/orchestrator.py`: provider -> planner -> risk/confirmation/tool execution wiring, plus observability spans and summary artifacts.
- `app/core/policy.py`: proposal write-safety checks.
- `app/core/tools.py`: concrete tool execution boundary and planning-only tool rejection.
- `app/core/providers.py`: proposal-first behavior for complex planning requests.
- `app/core/context_builder.py`: proposal context summaries.
- `app/core/context/`: Context Compiler, v2 context schemas, budget trimming, compressors, and provider render policy.
- `app/core/observability/`: trace schemas, hardened redaction, SQLite store, and no-op/SQLite emitters.
- `app/routers/observability.py`: read-only trace, timeline, graph, artifact, and UI routes guarded by the existing admin-token convention.
- `app/static/observability/`: no-build static HTML/CSS/JS/SVG dashboard.
- `app/core/agent_response_schema.json`: structured provider response schema with optional proposal.
- `tests/`: unit and regression tests, including planning-layer, Context Compiler, and observability coverage.
- `scripts/`: development, validation, local gateway, and dry-run helper scripts.
- `docs/`: project documentation from the source workspace, including Context Compiler and Visual Observability architecture documents.
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
- Added a provider render/policy layer: confirmation capsules are summary-only, plan draft facts are capped, and schedule busy/free facts are exposed only for availability/scheduling contexts.
- Added relevance gating so schedule availability compression does not run for ordinary confirm or smalltalk messages.

## Visual Observability Changes In This Snapshot

- Added `docs/10_VISUAL_OBSERVABILITY_ARCHITECTURE.md` at the review package root.
- Added `source/feishu-life-os/docs/10_VISUAL_OBSERVABILITY_ARCHITECTURE.md` for Codex implementation guidance inside the source snapshot.
- Added trace, span, event, artifact, and state-diff SQLite tables through the existing store migration path.
- Added no-op default tracing plus an `OBSERVABILITY_ENABLED` SQLite emitter path.
- Added hardened redaction for sensitive sender/open_id/user_id/union_id-style identifiers, including bare plural ID fields, raw text truncation, and attachment path basename/hash summaries.
- Added best-effort CoreAgentOrchestrator spans and summary artifacts for context, provider output, planner outcome, tool results, and state diffs.
- Added read-only `/api/v2/observability/traces`, `/timeline`, `/graph`, `/artifacts`, and `/ui` routes.
- Added a no-build static dashboard under `app/static/observability/`; no npm build and no external CDN are required.
- Removed the UI admin-token bypass path; observability routes remain admin-token protected.
- Added tests for disabled no-op behavior, enabled trace capture, write-failure isolation, route protection, redaction, large/full artifact safeguards, and bad payload hardening.

## Architecture Review Memo Added On 2026-06-05

- Added `docs/12_MODEL_FIRST_ARCHITECTURE_GAP_ANALYSIS.md`.
- This memo records an observed model-first architecture gap without changing business code.
- The memo highlights that `Provider`, `PlannerService`, and legacy planning paths can still interpret user natural language after model output.
- The memo asks reviewers to evaluate how to enforce a single semantic authority while keeping confirmation cards, RiskPolicy, deterministic planning calculations, and ToolRouter execution boundaries intact.

No source snapshot refresh was performed for this memo-only update.

## Validation Record

Latest local checks before this export:

```text
.\.venv\Scripts\python.exe -m pytest tests/test_core_agent_v2.py -q
92 passed

.\.venv\Scripts\python.exe -m pytest tests/test_context_compiler.py -q
9 passed

.\.venv\Scripts\python.exe -m pytest tests/test_observability.py -q
8 passed

.\.venv\Scripts\python.exe -m pytest -q
166 passed

.\.venv\Scripts\python.exe -m ruff check .
All checks passed!
```
