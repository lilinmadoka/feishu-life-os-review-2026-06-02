# Manual Feishu MVP Checklist

Use this checklist after starting the local gateway.

## 1. Start Local Gateway

```powershell
cd "E:\learning\基于飞书做的助理系统\feishu-life-os"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_local_gateway.ps1
```

Copy the printed Agent-first callback URL:

```text
https://<trycloudflare-host>/api/v2/feishu/events
```

## 2. Feishu Developer Console

In **事件与回调**:

- **事件配置**: choose "将事件发送至开发者服务器".
- Request URL: `https://<trycloudflare-host>/api/v2/feishu/events`.
- Add event: `im.message.receive_v1`.
- **回调配置**: add callback URL `https://<trycloudflare-host>/api/v2/feishu/card`.
- Do not enable encryption yet.
- Verification Token must match `.env` `FEISHU_EVENT_VERIFICATION_TOKEN`.

Required app capabilities/permissions:

- Receive messages.
- Send messages.
- Send interactive cards.
- Calendar event create/write.
- Task create/write.
- Bitable write is optional for audit view.

## 3. Conversation Tests

Send these to the bot:

1. `今天还有什么任务？`
   - Expected: task/calendar list or empty list.
   - Must not create a task or confirmation.

2. `明天下午3点给小王补课，今晚把资料发家长`
   - Expected: interactive card with one calendar candidate and one task candidate.
   - SQLite must not create official task/calendar until confirmation.

3. Click card **确认**
   - Expected: bot replies with created items.
   - SQLite has one `action_items` row and one `calendar_events` row.
   - `tool_runs` contains `resolve_confirmation.apply`.
   - Calendar sync is `synced` if Feishu permission works, otherwise `failed` with staged payload.

4. Click the same **确认** again
   - Expected: no duplicate task/calendar; bot says it was already handled.

5. Send another candidate message and click **取消**
   - Expected: no new task/calendar.

6. `明天有什么安排？`
   - Expected: bot lists tomorrow's task/calendar.

7. `本周有哪些任务？`
   - Expected: bot lists the next 7 days.

8. `小王相关的任务有哪些？`
   - Expected: bot filters related tasks/calendar.

9. `把小王补课改到晚上7点`
   - Expected: bot sends confirmation card; only changes time after confirmation.

10. `完成整理资料任务`
    - Expected: if unique, task becomes done; if ambiguous, bot asks you to choose.

11. `我每周一三五晚上7点到9点固定上课，周二下午2点到5点实验课`
    - Expected: ScheduleBlock confirmation card, not ordinary tasks.

12. `每晚12点到早上8点睡觉`
    - Expected: ScheduleBlock confirmation card.

## 4. Database Checks

```powershell
.\.venv\Scripts\python.exe -c "import sqlite3; conn=sqlite3.connect('.data/lifeos.sqlite3'); tables=['core_captures','action_items','calendar_events','schedule_blocks','confirmations','core_agent_runs','tool_runs']; [print(t, conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]) for t in tables]"
```

## 5. Stop Gateway

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\stop_local_gateway.ps1
```
