# Testing And Build Record

## Verification Environment

| Item | Value |
| --- | --- |
| Date | 2026-06-05 |
| OS/Shell | Windows / PowerShell |
| Python | 3.13.9 |
| App | FastAPI `app.main:app` |
| Primary local DB path | `.data/lifeos.sqlite3` |
| Sync mode in local source | `bitable` |

## Latest Verification Commands

Executed in the source workspace before this sanitized export:

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_decision_policy.py -q
.\.venv\Scripts\python.exe -m pytest tests\test_model_first_runtime.py -q
.\.venv\Scripts\python.exe -m pytest tests\test_core_agent_v2.py -q
.\.venv\Scripts\python.exe -m pytest tests\test_observability.py -q
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check app tests
```

## Latest Results

```text
tests/test_decision_policy.py: 16 passed
tests/test_model_first_runtime.py: 10 passed
tests/test_core_agent_v2.py: 94 passed
tests/test_observability.py: 9 passed
full pytest suite: 195 passed
ruff check app tests: All checks passed!
```

## Current Automated Coverage

- `tests/test_core_agent_v2.py`: v2 agent runtime, confirmation cards, Feishu callback flow, course timetable/habit/long-term plan behavior, fixed schedules, availability queries, and model routing guardrails.
- `tests/test_decision_policy.py`: `AssistantDecision` validation, proposal patch constraints, pending confirmation requirements, and write-boundary checks.
- `tests/test_model_first_runtime.py`: feature-flagged model-first branch, decision-only replies, proposal explanation without mutation, explicit proposal patch application, confirmation boundary execution, invalid query/write rejection, and model-first observability attrs.
- `tests/test_context_compiler.py`: dual-track v1/v2 context compilation, capsule rendering policy, schedule relevance gating, and budget trimming.
- `tests/test_observability.py`: disabled no-op behavior, enabled SQLite trace capture, timeline/graph/artifact/UI routes, write-failure isolation, route protection, redaction, full-payload safeguards, and bad-payload hardening.
- `tests/test_reminder_worker.py`: due reminders, pre-strong reminders, strong reminders, Pushover integration, daily summary, card callbacks, repeat/cancel behavior, and fixed schedule reminder behavior.
- `tests/test_feishu_events_and_codex.py`: legacy Feishu event handling, URL verification, sync behavior, permissions, and error paths.
- `tests/test_public_tunnel_protection.py`: public tunnel protection for docs/admin surfaces.
- `tests/test_time_parser.py`: Chinese relative time parsing.
- `tests/test_repository_and_api.py`: legacy repository/API behavior.
- `tests/test_extraction_service.py`: legacy rule-based extraction.
- `tests/test_codex_review_worker.py`: review worker behavior.

## Recent Build Notes

- Planning layer introduced `AssistantProposal` and `PlannerService`; ambiguous long-term goals now produce proposals instead of direct writes.
- Context Compiler introduced compact v2 capsules while preserving root `context_schema_version=1`.
- Context capsule render policy now hides large raw facts by default and gates schedule busy/free facts to relevant availability/scheduling requests.
- Visual Observability adds SQLite trace storage, a no-op default emitter, optional `OBSERVABILITY_ENABLED` tracing, hardened redaction, guarded read-only trace/timeline/graph/artifact/UI APIs, a no-build static dashboard, and best-effort CoreAgentOrchestrator spans/artifacts.
- Model-first runtime Task 2+3 adds `CORE_AGENT_RUNTIME_MODE=model_first`, native `run_decision()` support, `ModelDecisionProvider`, `DecisionPolicy`, and `PlannerRuntime`. Default remains `legacy`.
- In model-first mode, `PlannerRuntime` applies only `AssistantDecision` / explicit `ProposalPatch` fields and does not parse `raw_text`.

## Known Test Gaps

- No real Feishu sandbox end-to-end automation.
- No multi-process reminder worker concurrency soak test.
- No standalone OCR/table-parser fixture suite.
- No long-running scheduler soak test.
- No formal CI in the source workspace because the source workspace itself is not a git repository.
