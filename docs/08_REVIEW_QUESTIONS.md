# 建议审查问题清单

## 产品与领域建模

- `ActionItem`, `CalendarEvent`, `ScheduleBlock`, `PlanDraft` 四类实体边界是否清晰？
- “固定安排”应该是独立表，还是日历事件的一种 recurrence？
- 课程表应该保存为独立 `course_timetables/course_sessions` 表，还是继续作为 `PlanDraft.payload`？
- 习惯养成最终是否应该变成任务、日程、固定安排，还是独立 habit domain？

## Agent 架构

- `PlanDraft` 是否足以覆盖所有长期模糊计划？
- ToolName 是否需要按 domain 拆分和版本化？
- 是否应该让模型只输出 intent/entities，完全由 deterministic planner 生成 tool calls？
- 二阶段 entity extraction 是否应该成为硬性流程？
- 低置信度澄清策略是否需要统一卡片/文本模板？

## 数据与迁移

- SQLite 是否继续作为长期事实源？
- 是否需要 PostgreSQL 以支持多设备/多用户/并发 worker？
- 何时引入 Alembic？
- legacy 表是否迁移或删除？
- JSON payload 字段如何做 schema validation 和演进？

## 飞书集成

- 飞书任务、日历、多维表格三者之间的事实源关系是否明确？
- 如果飞书日历事件被用户手动修改，本地如何感知？
- 卡片回调失败时用户体验是否可接受？
- 公开 tunnel 下哪些路径必须锁定？

## 提醒系统

- 强提醒使用视频会议是否符合长期体验？
- Pushover emergency 是否作为兜底还是主要通道？
- 每日汇总和固定安排提醒的策略是否应该用户可配置？
- worker 多实例是否会重复发提醒？
- snooze/reschedule 是否应该写回日历事件还是只写 reminder 状态？

## 代码结构

- `ToolRouter` 是否已经过大？
- `providers.py` 中规则兜底是否需要拆到独立模块？
- 测试 fixture 是否需要按 domain 拆分？
- 是否需要引入 service layer，例如 `PlanService`, `CalendarService`, `ReminderService`？

## 生产化

- 是否建立 GitHub 主代码仓库和 CI？
- 是否需要 Dockerfile 或 Windows Task Scheduler 文档？
- 是否需要 staging 飞书应用？
- 是否需要脱敏样本和 replay harness？
- 是否需要 structured logging 和 metrics？

