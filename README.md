# Feishu Life OS Technical Review Package

Generated for architecture and source review. This repository contains the technical documentation package plus a sanitized source snapshot.

Source workspace:

```text
E:\learning\...\feishu-life-os
```

## Review Entry Points

1. [Review guide](docs/00_REVIEW_GUIDE.md)
2. [Architecture overview](docs/01_ARCHITECTURE.md)
3. [Agent runtime](docs/02_AGENT_RUNTIME.md)
4. [Data model](docs/03_DATA_MODEL.md)
5. [Feishu and reminders](docs/04_FEISHU_AND_REMINDERS.md)
6. [Operations](docs/05_OPERATIONS.md)
7. [Testing and build record](docs/06_TESTING_AND_BUILD_RECORD.md)
8. [Security, privacy, and risks](docs/07_SECURITY_AND_RISKS.md)
9. [Review questions](docs/08_REVIEW_QUESTIONS.md)
10. [Context compiler architecture proposal](docs/09_CONTEXT_COMPILER_ARCHITECTURE.md)
11. [Visual observability architecture](docs/10_VISUAL_OBSERVABILITY_ARCHITECTURE.md)
12. [Accelerated visual observability 90% sprint](docs/11_ACCELERATED_VISUAL_OBSERVABILITY_90_SPRINT.md)
13. [Sanitized source export manifest](SOURCE_EXPORT_MANIFEST.md)

## Source Snapshot

The sanitized project source is under:

```text
source/feishu-life-os/
```

It includes the FastAPI app, core agent runtime, planning layer, Context Compiler, Visual Observability implementation, no-build static dashboard, adapters, routers, workers, scripts, tests, validation summaries, and project metadata needed for review.

It intentionally excludes real environment files, local databases, attachments, screenshots, logs, caches, virtual environments, generated archives, and private runtime data.

## Current Review Focus

- The v2 runtime has a planning layer between provider output and tool execution.
- The local model may produce an `AssistantProposal` for ambiguous or long-term requests.
- `PlannerService` persists and refines proposal state through existing `PlanDraft` storage.
- `RiskPolicy` and confirmation cards remain the write boundary.
- `ToolRouter` is kept to confirmed concrete operations and rejects planning-only direct tools.
- `ContextCompiler` is implemented as a dual-track v1/v2 context layer with provider-readable capsules.
- Context capsule rendering applies provider policy: confirmation capsules are summary-only, plan drafts expose only compact draft facts, and schedule busy/free facts are gated to availability or scheduling contexts.
- The accelerated observability sprint targets a local high-density dashboard, context lens, timeline/graph APIs, replay, state diffs, provider/policy/planner/tool visibility, and coarse Feishu/reminder instrumentation.
- `Visual Observability` includes a best-effort SQLite trace layer, guarded read-only APIs, no-build static UI, Context Lens artifacts, timeline/graph/artifact endpoints, hardened redaction, and emitter entrypoints that must not fail the main request.

## Latest Local Validation

Executed in the source workspace before export:

```text
python -m pytest tests/test_observability.py -q
python -m pytest -q
python -m ruff check .
```

Results:

```text
tests/test_observability.py: 8 passed
tests/test_context_compiler.py: 9 passed
tests/test_core_agent_v2.py: 92 passed
full pytest suite: 166 passed
ruff check .: passed
```
