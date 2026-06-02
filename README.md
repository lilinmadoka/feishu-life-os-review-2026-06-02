# Feishu Life OS 技术审查包

生成时间：2026-06-02  
来源工作区：`E:\learning\基于飞书做的助理系统\feishu-life-os`  
用途：给资深开发者做架构阅读、风险审查和后续重构规划。

这个仓库是 docs-only 审查包，不包含 `.env`、SQLite 数据库、附件、日志、真实飞书 token、真实 open_id、图片截图或本地运行缓存。

## 推荐阅读顺序

1. [审查导读](docs/00_REVIEW_GUIDE.md)
2. [架构总览](docs/01_ARCHITECTURE.md)
3. [AI Agent 运行时](docs/02_AGENT_RUNTIME.md)
4. [数据模型](docs/03_DATA_MODEL.md)
5. [飞书与提醒系统](docs/04_FEISHU_AND_REMINDERS.md)
6. [运行与运维](docs/05_OPERATIONS.md)
7. [测试与构建记录](docs/06_TESTING_AND_BUILD_RECORD.md)
8. [安全、隐私与主要风险](docs/07_SECURITY_AND_RISKS.md)
9. [建议审查问题清单](docs/08_REVIEW_QUESTIONS.md)

## 当前结论摘要

- 项目是一个本地优先的飞书个人助理系统：飞书负责入口、确认卡、任务/日历同步和提醒反馈；SQLite 是事实源。
- 当前主路径是 `app/core/*` 的 v2 agent runtime；`app/agents/*` 和 legacy `actions/captures` 仍保留，属于兼容/历史路径。
- AI 不直接写库。模型输出轻量 intent/entities，后端映射为受控 tool calls，再经 `RiskPolicy`、确认卡和 `ToolRouter` 执行。
- 最近新增了通用 `PlanDraft` 层，用于习惯养成、课程表图片导入、长期日程草案等“不够明确但需要持续讨论”的场景。
- 当前运行验证：Python 3.13.9，`pytest` 144 个测试通过，`ruff check app tests` 通过，FastAPI `/health` 正常。

## 不在本包内的内容

- 未上传真实业务数据。
- 未上传源代码完整快照。文档中列出了关键源文件和责任边界，便于审查者回到工作区或后续代码仓库阅读。
- 未上传旧 `handoff_package` zip，避免把过期代码或潜在本地信息混入审查仓库。

