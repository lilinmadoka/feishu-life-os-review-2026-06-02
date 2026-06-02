# 测试与构建记录

## 验证环境

| 项 | 值 |
| --- | --- |
| 日期 | 2026-06-02 |
| OS/Shell | Windows / PowerShell |
| Python | 3.13.9 |
| App | FastAPI `app.main:app` |
| Worker | `app.workers.reminder_worker` |
| 数据库 | `.data/lifeos.sqlite3` |
| 当前 sync mode | `bitable` |

## 最新验证命令

```powershell
.\.venv\Scripts\python.exe -V
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check app tests
Invoke-RestMethod -Uri http://127.0.0.1:8000/health -TimeoutSec 3
```

## 最新结果

```text
Python 3.13.9
144 passed in 34.01s
All checks passed!
{"ok":true,"env":"local","sync_mode":"bitable"}
```

## 覆盖范围摘要

测试文件：

- `tests/test_core_agent_v2.py`: v2 agent、确认卡、飞书回调、课程表/习惯/长期计划、固定安排、可用时间查询、模型路由防错。
- `tests/test_reminder_worker.py`: due reminder、预强提醒、强提醒、Pushover、每日汇总、卡片回调、重排/取消、固定安排提醒。
- `tests/test_feishu_events_and_codex.py`: legacy 飞书事件、URL verification、同步、权限、错误路径。
- `tests/test_public_tunnel_protection.py`: 公网 tunnel 下 docs/admin 保护。
- `tests/test_time_parser.py`: 中文相对时间。
- `tests/test_repository_and_api.py`: legacy repository/API。
- `tests/test_extraction_service.py`: legacy 规则抽取。
- `tests/test_codex_review_worker.py`: review worker。

近期新增/修复测试：

- 模糊习惯目标只生成草案，不创建任务/日历。
- 习惯补充信息后生成日程确认卡，确认后才写入。
- 课程表图片请求识别为 `course_timetable`，不走 `schedule_time_budget_plan`。
- 第 13 周 + 消息日期推断第 1 周周一。
- 周次范围如 `1-4周, 6-10周, 13-14周` 生成未来日期事件。
- 过期确认不在晨间汇总中显示为内部 `schedule_blocks`。
- 固定每周安排可关闭提醒，但不取消安排。

## 构建记录：长期计划/课程表能力

目标：

- 引入通用 `PlanDraft`，统一处理习惯养成、课程表导入、长期日程。
- 不靠硬编码课表模板，先让 AI 抽结构，再经草案和确认。
- 课程表保存结构化事实源，再生成日历事件。
- 确认前不写正式任务或日历。

主要变更：

- `app/core/schemas.py`: 新增 `PlanDraftStatus`, `PlanDraftKind`, `PlanDraft` 和相关 tool names。
- `app/core/store.py`: 新增 `plan_drafts` table；`calendar_events` 增加 `plan_draft_id`, `plan_item_id`。
- `app/core/tools.py`: 新增 plan refinement、habit schedule、course timetable normalization/scheduling。
- `app/core/providers.py`: 课程表图片和纠错语义优先进入 `course_timetable` 草案。
- `app/core/context_builder.py`: 上下文加入 active plan drafts。
- `app/core/policy.py`: 新增计划工具安全策略。

## 构建记录：晨间汇总和固定安排提醒

目标：

- 晨间汇总不显示过期/内部待确认项。
- 固定每周安排可以保留但关闭提醒。

主要变更：

- `app/workers/reminder_worker.py`: 过滤过期待确认并标记 expired；确认显示友好标题。
- `app/core/schemas.py`: `ScheduleBlock.reminder_enabled`。
- `app/core/store.py`: `schedule_blocks.reminder_enabled` 迁移和 CRUD。
- `app/core/tools.py`: 新增 `disable_schedule_block_reminders`。
- `app/core/providers.py`: “固定安排不用提醒”路由到关闭提醒，不误走取消。
- `app/workers/reminder_worker.py`: disabled schedule blocks 不发预强提醒和强提醒。

真实运行库处理：

- 2026-06-02 已将本地 12 个 active fixed schedule blocks 的 `reminder_enabled` 设置为 false。
- 安排仍保留 active，未删除飞书日历 event id。

## 已知测试缺口

- 没有真实飞书沙盒端到端自动测试。
- 没有多进程 worker 并发发送防重测试。
- 没有 OCR/table parser 的独立 fixture 测试。
- 没有长期运行的 scheduler soak test。
- 没有针对所有 Feishu API error code 的分类测试。
- 没有正式 CI，因为源工作区当前不是 git repo。

