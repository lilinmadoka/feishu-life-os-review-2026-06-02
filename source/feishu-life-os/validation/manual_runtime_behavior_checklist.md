# Manual Runtime Behavior Checklist

当前目标：验证真实飞书运行行为，不再把 mock/rules 包装成真实 Agent。

运行前先确认：

```powershell
cd "E:\learning\基于飞书做的助理系统\feishu-life-os"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\status_local_gateway.ps1
Select-String -Path .env -Pattern '^CORE_AGENT_PROVIDER|^CODEX_CLI_PATH'
```

## 1. 今天有什么任务？

- Expected intent: `query_today`
- Write allowed: no
- Tables queried: `action_items`, `calendar_events`, `schedule_blocks`, `confirmations`
- Reply must mention: today tasks/calendar/fixed arrangements, even when empty
- Confirmation: no

## 2. 明天有什么任务？

- Expected intent: `query_tomorrow`
- Write allowed: no
- Tables queried: `action_items`, `calendar_events`, `schedule_blocks`, `confirmations`
- Reply must mention: tasks, calendar events, fixed schedule blocks
- If only fixed schedule exists, must not reply “没有任务或日程”
- Confirmation: no

## 3. 明天我都啥时间有空？

- Expected intent: `query_availability`
- Write allowed: no
- Tables queried: `calendar_events`, `schedule_blocks`, optional `action_items` deadlines
- Reply must mention: occupied time, free time, data source
- Confirmation: no

## 4. 周六我有什么安排？

- Expected intent: `query_availability`
- Write allowed: no
- Tables queried: `calendar_events`, `schedule_blocks`
- Reply must include any Saturday ScheduleBlock, such as `周六驾校`
- Confirmation: no

## 5. 周天能不能安排家教？

- Expected intent: `query_availability`
- Write allowed: no
- Tables queried: `calendar_events`, `schedule_blocks`
- Reply must say whether there is free time and list occupied blocks
- Confirmation: no

## 6. 明天下午3点给小王补课，今晚把资料发家长。

- Expected intent: `create_candidates`
- Write allowed: only Confirmation, not official task/calendar
- Tables queried/written: `core_captures`, `core_agent_runs`, `confirmations`, `tool_runs`
- Reply must be an interactive card
- Confirmation: yes

## 7. 确认。

- Expected intent: `update_existing` or card callback resolve
- Write allowed: yes, through ToolRouter only
- Tables written: `action_items`, `calendar_events`, `tool_runs`, `confirmations`
- Reply must mention created task/calendar in Chinese labels
- Confirmation: this is confirmation

## 8. 把小王补课改到晚上7点。

- Expected intent: `update_existing`
- Write allowed: Confirmation only until user confirms
- Tables queried/written: `calendar_events`, `confirmations`, `tool_runs`
- Reply must ask for confirmation
- Confirmation: yes

## 9. 取消。

- Expected intent: `update_existing`
- Write allowed: resolve/cancel Confirmation only
- Tables written: `confirmations`, `tool_runs`
- Reply must say no task/calendar was created or modified
- Confirmation: cancels pending confirmation

## 10. 再问一次明天我都啥时间有空。

- Expected intent: `query_availability`
- Write allowed: no
- Tables queried: `calendar_events`, `schedule_blocks`, optional `action_items` deadlines
- Reply must include occupied/free windows and reflect any confirmed changes
- Confirmation: no

## Runtime Log Check

After each message, inspect recent agent runs:

```powershell
.\.venv\Scripts\python.exe -c "import sqlite3,json; conn=sqlite3.connect('.data/lifeos.sqlite3'); conn.row_factory=sqlite3.Row; [print(dict(r)) for r in conn.execute('select id,provider,status,substr(output_json,1,300) output from core_agent_runs order by created_at desc limit 5')]"
```

Expected:

- `provider` must honestly show `mock_provider` or `codex_cli_provider`.
- If provider is `mock_provider`, this is not a real LLM Agent run.
- Duplicate Feishu retries must not create additional AgentRun rows for the same `message_id`.
