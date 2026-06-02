# 运行与运维

## 本地运行

项目是 Python/FastAPI 应用。

```powershell
cd E:\learning\基于飞书做的助理系统\feishu-life-os
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
.\.venv\Scripts\python.exe -m app.workers.reminder_worker
```

健康检查：

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8000/health
```

当前健康检查结果：

```json
{"ok":true,"env":"local","sync_mode":"bitable"}
```

## 当前进程状态

截至 2026-06-02 验证时，本地能看到：

- uvicorn `app.main:app` on `127.0.0.1:8000`
- `app.workers.reminder_worker`

PowerShell 进程列表可能显示每个服务两条 Python 记录，通常是 venv launcher 和 child process。审查时建议确认是否实际有重复 worker。

## 配置

主要配置在 `app/config.py`，来自 `.env`。

关键变量：

| 配置 | 作用 |
| --- | --- |
| `DATABASE_PATH` / `DATABASE_URL` | SQLite 或 PostgreSQL 连接 |
| `TIMEZONE` | 用户时区 |
| `FEISHU_SYNC_MODE` | `dry_run`/`bitable` 等同步模式 |
| `FEISHU_APP_ID`, `FEISHU_APP_SECRET` | 飞书自建应用凭证 |
| `FEISHU_EVENT_VERIFICATION_TOKEN` | 飞书事件 token |
| `FEISHU_ALLOWED_OPEN_IDS` | 授权用户白名单 |
| `CORE_AGENT_PROVIDER` | v2 provider 选择 |
| `LM_STUDIO_*` | 本地模型配置 |
| `ATTACHMENT_STORAGE_DIR` | 附件落盘目录 |
| `FEISHU_STRONG_REMINDER_MODE` | 强提醒模式 |
| `PUSHOVER_*` | Pushover emergency |

本审查包不包含任何真实配置值。

## 数据迁移

应用启动时调用：

- `Repository.migrate()`
- `StateStore.migrate()`

当前迁移方式：

- `CREATE TABLE IF NOT EXISTS`
- 对新增字段使用 `ALTER TABLE ... ADD COLUMN`，异常被 suppress。

短期可用，但建议改进：

- 引入迁移版本表。
- 引入 Alembic 或显式 migration scripts。
- 给高风险幂等字段建唯一索引。
- 将 legacy/v2 表迁移计划纳入 migration。

## 部署/公网入口

项目已支持本地 FastAPI 加公网 tunnel 的形态。审查时注意：

- 公开 tunnel 不应暴露 `/docs`、`/redoc`、管理 API。
- 飞书 callback URL 不要随意重启 tunnel，避免回调地址变化。
- 应明确回调域名、证书、token、白名单策略。

## 日常操作建议

启动顺序：

1. 启动 FastAPI。
2. 确认 `/health`。
3. 启动 reminder worker。
4. 确认飞书 callback 指向当前公开地址。
5. 发本地 `/api/v2/agent/messages` 测试 agent。
6. 发真实飞书消息测试端到端。

变更验证：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check app tests
```

## 观测能力

当前可观测点：

- `core_agent_runs`: provider input/output、latency、status、error。
- `tool_runs`: tool input/output/status/error。
- `sync_events`: legacy sync event。
- app logs。
- Feishu card reply/toast。

建议补充：

- 结构化 JSON logs。
- 每次消息的 trace id。
- reminder send/cancel/reschedule metrics。
- 飞书 API rate limit/error 分类。
- prompt/entity extraction 失败样本集。

