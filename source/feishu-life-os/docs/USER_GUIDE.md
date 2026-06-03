# 使用说明

当前项目已经改为 Agent-first 飞书私人助理。飞书机器人私聊是主入口；多维表只作为后台视图和审计层。

## 当前状态

已接通：

- 本地 FastAPI 服务
- 本地 SQLite 数据库
- Cloudflare Tunnel 公网回调
- 飞书私聊消息事件入口
- 本机 Codex CLI provider
- 飞书机器人回复
- 本机提醒 worker
- 飞书任务/日历同步工具
- 多维表后台同步能力
- 公网访问保护

保留但降级：

- `POST /api/captures` 手动捕获入口
- 规则抽取服务
- 旧 Codex review job API

这些不再是主产品体验。主链路是：

```text
飞书私聊 -> /api/v2/feishu/events -> AgentOrchestrator -> codex_cli/mock_provider -> 工具执行 -> 飞书回复
```

## 启动

```powershell
cd "E:\learning\基于飞书做的助理系统\feishu-life-os"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_local_gateway.ps1
```

健康检查：

```text
http://127.0.0.1:8000/health
```

API 文档仅建议本机打开：

```text
http://127.0.0.1:8000/docs
```

## 飞书后台配置

启动脚本会输出：

```text
https://xxxx.trycloudflare.com/api/v2/feishu/events
```

填到飞书开放平台：

```text
事件与回调 -> 事件配置 -> 将事件发送至开发者服务器 -> 请求地址
```

然后在“添加事件”里订阅：

```text
接收消息 im.message.receive_v1
```

机器人只处理私聊消息，群聊消息会忽略。

## Agent 配置

`.env` 中应有：

```env
AGENT_PROVIDER=codex_cli
CODEX_CLI_PATH=C:\Users\Administrator\AppData\Roaming\npm\codex.ps1
AGENT_CODEX_TIMEOUT_SECONDS=300
```

如果 Codex CLI 不可用，机器人会回复：

```text
智能处理器未启动/不可用，已记录消息但不会自动处理。
```

系统不会静默退回规则抽取，也不会把消息直接写进多维表。

## 验收方式

在飞书私聊机器人中测试：

```text
今天还有什么任务？
```

预期：机器人回复今天任务列表，不创建新任务，不写成多维表任务输入。

```text
明天下午3点给小王补课，今晚把资料发家长
```

预期：机器人回复识别到 2 个事项，并创建结构化任务。

```text
把小王补课改到晚上7点
```

预期：单一明确匹配时修改时间；多候选时列候选让你确认。

发送截图：

预期：保存附件信息，回复截图处理状态；当前不会整条塞进多维表。

## 提醒怎么生效

当你说“开始前 30 分钟提醒我”时，Agent 会把提醒时间写到任务的 `remind_at` 字段。启动网关时会同时启动提醒 worker；它默认每 60 秒扫描一次，到点后用飞书机器人私聊提醒你，并写入 `reminder_sent_at`，避免重复发送。

注意：

- 电脑关机、睡眠、网关停止、提醒 worker 停止时，不能主动提醒。
- 如果错过提醒时间后再启动，只要还没有标记为已发送，worker 会补发一次。
- 临时 Cloudflare 地址变化不影响已保存的提醒时间，但会影响飞书新消息进入系统；重启隧道后仍要更新飞书回调地址。

## 飞书原生能力

Agent 现在可以调用这些飞书原生同步工具：

- `sync_feishu_task`：把普通待办同步到飞书任务。
- `sync_feishu_calendar`：把有开始/结束时间的事件同步到飞书日历。
- `sync_bitable`：把事项同步到多维表，作为后台视图和审计。

日历/任务同步需要飞书开放平台对应权限。如果权限或字段 schema 不匹配，系统会记录 `sync_events` 错误，并在机器人回复里说明同步失败。

## 停止

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\stop_local_gateway.ps1
```

需要打游戏或低占用时可以停止整套网关。只暂停提醒：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\stop_reminder_worker.ps1
```

恢复提醒：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_reminder_worker.ps1
```

## 数据位置

本地数据库：

```text
E:\learning\基于飞书做的助理系统\feishu-life-os\.data\lifeos.sqlite3
```

主要表：

- `captures`：原始飞书消息和手动输入
- `actions`：结构化任务
- `agent_runs`：Agent 请求、响应、工具调用审计
- `sync_events`：多维表等同步事件

## 更多说明

Agent-first 设计见：

```text
docs/AGENT_FIRST.md
```

本机网关见：

```text
docs/LOCAL_GATEWAY.md
```
