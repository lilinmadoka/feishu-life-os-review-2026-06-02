# 本机 Cloudflare Tunnel 网关说明

本机网关用于替代 Railway：FastAPI 运行在本机 `127.0.0.1:8000`，Cloudflare Tunnel 提供公网 HTTPS 地址给飞书事件回调。

## 组成

```text
FastAPI            -> http://127.0.0.1:8000
Cloudflare Tunnel  -> https://*.trycloudflare.com
Codex CLI          -> 收到飞书私聊消息时按需调用
Reminder Worker    -> 扫描 remind_at，到点通过飞书私聊提醒
```

Agent-first 模式不再启动常驻 Codex review worker。旧的 `start_codex_worker.ps1` 只会提示当前模式使用按需调用，不会启动后台审核进程。
提醒 worker 是轻量常驻进程，默认每 60 秒扫描一次本地 SQLite。

## 安装 cloudflared

```powershell
winget install Cloudflare.cloudflared
cloudflared --version
```

## 启动网关

```powershell
cd "E:\learning\基于飞书做的助理系统\feishu-life-os"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_local_gateway.ps1
```

脚本会输出类似：

```text
Feishu callback URL:
  https://xxxx.trycloudflare.com/api/v2/feishu/events
```

把这个地址填到飞书开放平台：

```text
事件与回调 -> 事件配置 -> 将事件发送至开发者服务器 -> 请求地址
```

`trycloudflare.com` 是临时地址，每次重启隧道都可能变化。变化后需要重新填写飞书后台的回调地址。

## 公网保护

默认启用：

```text
PUBLIC_TUNNEL_PROTECTION=true
```

公网 Cloudflare 请求只允许：

```text
GET /health
POST /api/v2/feishu/events
POST /api/feishu/events
带 X-Admin-Token 的管理请求
```

公网不能直接访问 `/docs`、`/api/captures`、`/api/actions`、`/api/reviews`。本机访问 `http://127.0.0.1:8000/docs` 不受影响。

## 查看状态

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\status_local_gateway.ps1
```

会显示 FastAPI、Cloudflare Tunnel、提醒 worker、8000 端口、`/health` 和当前检测到的飞书回调地址。`codex_worker` 正常应显示 stopped，因为 Agent-first 模式不需要常驻 Codex review worker。

## 停止网关

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\stop_local_gateway.ps1
```

停止后飞书事件不会再进入本机系统。本地 SQLite 数据和已经写入飞书多维表的数据不会丢失。

## 游戏/低占用模式

Agent-first 模式没有独立 Codex 后台进程。提醒 worker 很轻；如果需要完全低占用，直接停止整套网关：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\stop_local_gateway.ps1
```

重新使用时再启动：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_local_gateway.ps1
```

只暂停提醒 worker：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\stop_reminder_worker.ps1
```

单独恢复提醒 worker：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_reminder_worker.ps1
```

## Windows 计划任务

注册快捷管理入口：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\register_gateway_task.ps1
```

启动任务：

```powershell
Start-ScheduledTask -TaskName FeishuLifeOSGateway
```

删除任务：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\unregister_gateway_task.ps1
```

计划任务默认没有触发器，不会自动开机启动。

## 未来升级固定地址

当前没有接入 Cloudflare 的域名，所以只能使用临时 `trycloudflare.com` 地址。以后有域名后可以把 `.env` 改成：

```text
TUNNEL_MODE=named
CLOUDFLARE_TUNNEL_HOSTNAME=feishu.your-domain.com
```

固定隧道还需要在 Cloudflare 账号中创建 named tunnel 和 DNS 记录。当前脚本已预留配置位，但不会在没有域名时伪装成固定地址。
