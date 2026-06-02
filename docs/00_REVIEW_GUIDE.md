# 审查导读

## 项目定位

Feishu Life OS 是一个以飞书为用户界面的个人事务操作系统原型。目标不是简单提醒机器人，而是把聊天、截图、口头约定、课程表、长期计划、任务、日程和每日复盘统一进一个本地事实源，再把需要用户确认或执行的内容同步到飞书。

当前重点用户场景：

- 随手把消息发给飞书机器人，系统识别任务、日程、长期计划或查询意图。
- 对新增、修改、取消等写操作先生成确认卡，用户确认后再写入 SQLite 和飞书。
- 对长期习惯、课程表图片、模糊长期安排先进入草案层，不直接建日历。
- 对固定每周安排保留在日历/查询中，但支持关闭强提醒。
- 每天晨间发今日任务汇总，用户确认后不再触发后续强提醒。

## 当前代码状态

主实现分两代：

- v2 主路径：`app/core/*`、`app/routers/core_agent.py`、`app/workers/reminder_worker.py`。
- legacy 路径：`app/agents/*`、`app/services/*`、`actions/captures` 表和相关 API。仍可运行，但不是当前新增能力的主要承载层。

建议审查时优先阅读 v2 主路径，再决定 legacy 是否保留、迁移或删除。

## 架构审查重点

1. Agent tool 协议是否足够通用，是否能继续承载课程表、习惯、长期计划等复杂对话。
2. `PlanDraft` 状态机是否适合做通用“草案到日程确认”的中间层。
3. SQLite schema 和手写迁移是否已到需要 Alembic 或其他迁移系统的阶段。
4. 飞书卡片回调、强提醒、Pushover emergency 和日历同步的幂等性是否可靠。
5. 当前 runtime 是本地服务加轮询 worker，是否需要队列、任务调度器或独立事件流。
6. 多模态和 OCR 策略是否应该从 prompt-only 抽象成可插拔解析服务。
7. 安全边界：公开 tunnel、飞书 token、授权 open_id、附件落盘、运行日志。

## 审查建议

建议把阅读分成三轮：

第一轮看业务流是否成立：

- 从飞书消息到 `CaptureIn`
- 从上下文构造到 provider intent
- 从 tool call 到确认卡
- 从确认卡到数据库和飞书同步
- 从 worker 到提醒卡/强提醒

第二轮看技术债：

- 双 agent stack
- prompt 和业务规则混杂
- SQLite 手写 schema 迁移
- worker 的幂等和并发
- 模型错误恢复

第三轮定下一步架构目标：

- 是否拆出 planner/scheduler/parser 子域
- 是否引入事件总线和队列
- 是否建立正式代码仓库和 CI
- 是否将文档、schema、prompt、tool 协议纳入版本化发布流程

