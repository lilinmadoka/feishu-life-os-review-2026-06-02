# Feishu Life OS / 多端通用助理

这是一个“飞书可接入”的个人事务操作系统原型，目标是把学校事务、家教安排、学习任务、项目开发、聊天消息、邮件通知、学习平台通知、临时想法和口头/文字约定统一收进一个低摩擦的收件箱，再自动拆成任务、日程、等待事项、提醒和每日复盘。

当前版本已经完成：

- 独立 FastAPI 后端，可本地运行。
- SQLite 数据层与迁移脚本。
- 捕获入口：文本/API/飞书事件占位。
- 中文规则抽取器：可识别“明天/后天/今晚/周五/下周一/5月30日/15:30/下午3点”等常见截止时间。
- 自动分类：学校、家教、学习、项目、沟通、个人、其他。
- 自动优先级：基于截止时间、紧急词、任务类型生成 P0-P3。
- 去重：防止同一约定从聊天、截图、通知里重复进入待办。
- 每日复盘：生成今日/逾期/等待/未来 7 天摘要。
- 飞书适配层：tenant_access_token、自定义机器人 webhook、多维表格、任务、日历 API 的调用封装，默认 dry-run，等待 Codex 填入真实飞书参数并校验 payload。
- 完整交接文档：产品规格、飞书接入、数据模型、API 合同、Codex 任务清单、安全与运维。

## 快速启动

```bash
cd feishu-life-os
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
make dev
cp .env.example .env
make demo
make run
```

打开：

- API 文档：`http://localhost:8000/docs`
- 健康检查：`GET /health`
- 捕获入口：`POST /api/captures`
- 今日复盘：`GET /api/reviews/daily`

## 最小使用示例

```bash
curl -X POST http://localhost:8000/api/captures \
  -H 'Content-Type: application/json' \
  -d '{"raw_text":"明天下午3点给学生小王补课，记得今晚把资料发给家长", "source_type":"manual"}'
```

返回里会包含原始捕获项，以及从文本中抽取出的行动项。后续 Codex 接入飞书后，这些行动项可以同步到多维表格、飞书任务、飞书日历或私聊机器人提醒。

## 推荐系统结构

飞书不是只当“提醒工具”，而是承担 4 个角色：

1. **入口**：手机/电脑随手发给机器人；截图、转发消息、口头约定都先进入收件箱。
2. **数据库**：多维表格保存结构化任务、来源证据、状态、截止时间、项目与领域。
3. **行动层**：飞书任务/日历承接需要执行或有固定时间的事项。
4. **反馈层**：自定义机器人每天早晚推送“今天要做什么、哪些快炸了、哪些在等别人”。

## 目录

```text
app/                    FastAPI 应用与核心逻辑
  adapters/             飞书与外部系统适配层
  routers/              API 路由
  services/             抽取、计划、复盘、同步等服务
docs/                   产品、接入、数据、安全、交接文档
scripts/                飞书多维表格字段 schema、示例 payload
tests/                  单元测试
```

## 下一步给 Codex 的重点

从 `docs/CODEX_HANDOFF.md` 开始。最重要的接入顺序是：

1. 在飞书开放平台创建自建应用，配置机器人、事件订阅、权限和回调地址。
2. 按 `scripts/bitable_schema.json` 建好多维表格，并把 app_token/table_id 填入 `.env`。
3. 将 `FEISHU_SYNC_MODE` 从 `dry_run` 切到 `bitable`，先只同步多维表格。
4. 再接飞书任务和日历，避免一开始就制造大量真实提醒。
5. 最后接消息事件、截图/OCR、邮件转发和学习平台通知。

## 重要原则

这个系统默认“先收集，不打扰；先确认，再强提醒”。任何自动抽取都保留原始证据，低置信度事项进入 `needs_review`，避免误提醒、误建日程或误删信息。
