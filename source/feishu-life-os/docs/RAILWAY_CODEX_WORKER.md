# Railway + Codex Worker 部署说明

> Deprecated: 当前方向已改为本机 Cloudflare Tunnel + Agent-first。Codex CLI 由 FastAPI 在收到飞书私聊消息时按需调用，不再默认运行常驻 Codex review worker。新说明见 `docs/AGENT_FIRST.md` 和 `docs/LOCAL_GATEWAY.md`。

本文说明第二/第三阶段的部署方式：Railway 负责公网飞书入口，本机负责运行 Codex 审核 Worker。

## 1. Railway 服务

Railway 启动命令已写入 `railway.json`：

```text
python -m uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Railway 需要添加 Postgres，并配置环境变量：

```text
APP_ENV=production
DATABASE_URL=<Railway Postgres 自动提供>
TIMEZONE=Asia/Singapore
ADMIN_API_TOKEN=<自己生成的长随机字符串>

FEISHU_SYNC_MODE=bitable
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_OPEN_API_BASE=https://open.feishu.cn/open-apis
FEISHU_EVENT_VERIFICATION_TOKEN=<飞书事件订阅 verification token>

FEISHU_BITABLE_APP_TOKEN=appxxx
FEISHU_BITABLE_SCHEMA=personal_base
FEISHU_BITABLE_CAPTURE_TABLE_ID=tblxxx
FEISHU_BITABLE_ACTION_TABLE_ID=tblxxx
FEISHU_BITABLE_REVIEW_TABLE_ID=tblxxx
```

部署后检查：

```text
https://<railway-domain>/health
```

应返回：

```json
{"ok": true, "env": "production", "sync_mode": "bitable"}
```

## 2. 飞书事件订阅

飞书开放平台中配置事件回调地址：

```text
https://<railway-domain>/api/feishu/events
```

事件订阅只需要先开：

```text
im.message.receive_v1
```

当前代码只处理机器人私聊消息：

- `chat_type=p2p` 会进入系统。
- 群聊、非私聊、非 text 消息会被忽略。
- 相同 `message_id` 重复投递不会重复创建捕获项。

## 3. 本机 Codex Worker

本机 `.env` 需要：

```text
PUBLIC_API_BASE=https://<railway-domain>
ADMIN_API_TOKEN=<与 Railway 相同>
CODEX_CLI_PATH=C:\Users\Administrator\AppData\Roaming\npm\codex.ps1
CODEX_WORKER_POLL_SECONDS=10
```

启动 Worker：

```powershell
cd "E:\learning\基于飞书做的助理系统\feishu-life-os"
.\.venv\Scripts\python.exe -m app.workers.codex_review_worker
```

Worker 会轮询：

```text
GET /api/codex/jobs/next
```

拿到任务后调用本机 Codex CLI，并回写：

```text
POST /api/codex/jobs/{job_id}/complete
POST /api/codex/jobs/{job_id}/fail
```

这些接口都要求：

```text
X-Admin-Token: <ADMIN_API_TOKEN>
```

## 4. Codex 审核范围

v1 只记录审核结果，不自动修改行动项。

Codex 输出字段：

```json
{
  "decision": "ok | needs_user_review | system_issue",
  "summary": "string",
  "proposed_actions": ["string"],
  "problems_found": ["string"],
  "confidence": 0.0,
  "should_change_existing_actions": false
}
```

触发审核 job 的场景：

- 每条飞书私聊消息进入系统后创建 `extraction_review`。
- 飞书同步失败时创建 `sync_error`。

## 5. 本地验证

运行测试：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
```

当前覆盖：

- 飞书 URL verification。
- 私聊 text 消息入库。
- 群聊忽略。
- `message_id` 幂等。
- verification token 校验。
- 同步失败生成 Codex job。
- Worker 成功/失败/无 job 三种路径。
