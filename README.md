# Feishu Life OS Technical Review Package

Generated for architecture and source review. This repository contains the technical documentation package plus a sanitized source snapshot.

Source workspace:

```text
E:\learning\基于飞书做的助理系统\feishu-life-os
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
11. [Sanitized source export manifest](SOURCE_EXPORT_MANIFEST.md)

## Source Snapshot

The sanitized project source is under:

```text
source/feishu-life-os/
```

It includes the FastAPI app, core agent runtime, planning layer, adapters, routers, workers, scripts, tests, validation summaries, and project metadata needed for review.

It intentionally excludes real environment files, local databases, attachments, screenshots, logs, caches, virtual environments, generated archives, and private runtime data.

## Current Review Focus

- The v2 runtime now has a planning layer between provider output and tool execution.
- The local model may produce an `AssistantProposal` for ambiguous or long-term requests.
- `PlannerService` persists and refines proposal state through existing `PlanDraft` storage.
- `RiskPolicy` and confirmation cards remain the write boundary.
- `ToolRouter` is kept to confirmed concrete operations and rejects planning-only direct tools.
- `ContextCompiler` is now implemented as a dual-track v1/v2 context layer with provider-readable capsules.
- Context capsule rendering now applies provider policy: confirmation capsules are summary-only, plan drafts expose only compact draft facts, and schedule busy/free facts are gated to availability or scheduling contexts.

## Latest Local Validation

Executed in the source workspace before export:

```text
python -m pytest tests/test_core_agent_v2.py -q
python -m pytest tests/test_context_compiler.py -q
python -m pytest -q
python -m ruff check app tests
```

Results:

```text
tests/test_context_compiler.py: 9 passed
tests/test_core_agent_v2.py: 92 passed
full pytest suite: 158 passed
ruff check app tests: passed
```
