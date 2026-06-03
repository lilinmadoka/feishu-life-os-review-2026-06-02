# Review Request

## Goal

Review the runtime-behavior fix for the real Feishu bot. This pass does not claim true Agent-first completion; it fixes the immediate bad behaviors seen in Feishu and documents that the active provider is still deterministic.

## Key Runtime Finding

- Active `.env`: `CORE_AGENT_PROVIDER=mock_provider`.
- Recent DB AgentRuns also show `provider=mock_provider`.
- `codex_cli_provider` can launch, but a Chinese smoke test for `明天我都啥时间有空？` returned `unknown`, so it is not reliable enough for real Feishu use.
- `mock_provider` must not be represented as a true LLM Agent.

## Modified Areas

- `app/core/schemas.py`
- `app/core/policy.py`
- `app/core/providers.py`
- `app/core/orchestrator.py`
- `app/core/tools.py`
- `app/core/agent_response_schema.json`
- `tests/test_core_agent_v2.py`
- `.env`
- `docs/PROJECT_STATE.md`
- `docs/NEXT_TASKS.md`
- `docs/REVIEW_REQUEST.md`
- `validation/manual_runtime_behavior_checklist.md`

## Behavior To Review

- Duplicate Feishu message delivery should not create a second Capture, AgentRun, or reply.
- If `send_feishu_reply` tool already sent a reply, Orchestrator must not send a duplicate final reply.
- `query_availability` should handle:
  - `明天我都啥时间有空？`
  - `明天我什么时候有空？`
  - `周六我有什么安排？`
  - `周天能不能安排家教？`
- `query_today`, `query_tomorrow`, and `query_week` include expanded ScheduleBlock occurrences.
- Query messages must not create ActionItem/CalendarEvent/Confirmation.
- Runtime logs include provider name and fallback flag.

## How To Test

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
```

## Manual Feishu Test

Use:

```text
validation/manual_runtime_behavior_checklist.md
```

Focus on:

- No duplicate reply for one message.
- `明天我都啥时间有空？` returns occupied and free time.
- `明天有什么任务吗` mentions fixed arrangements if there are ScheduleBlocks.
- `周六我有什么安排？` returns the Saturday ScheduleBlock.

## Database Check

```powershell
.\.venv\Scripts\python.exe -c "import sqlite3; conn=sqlite3.connect('.data/lifeos.sqlite3'); print(conn.execute('select provider, model, status from core_agent_runs order by created_at desc limit 5').fetchall())"
```

## Review Focus

- Is the system being honest about `mock_provider` versus true Agent runtime?
- Is idempotency applied before any provider/tool execution that could send a reply?
- Does ScheduleBlock recurrence expansion cover the Chinese weekday formats used in real messages?
- Next P0: make the real provider Chinese-capable, either by fixing Codex CLI prompt encoding or replacing it with OpenAI API/local provider.
