# Deployment

## Local Gateway

```powershell
cd "E:\learning\基于飞书做的助理系统\feishu-life-os"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_local_gateway.ps1
```

The script starts FastAPI, Cloudflare Tunnel, and the lightweight reminder worker. Agent-first Codex CLI calls are made on demand inside FastAPI, not by a background review worker.

## Feishu Callback

Use the callback printed by the gateway:

```text
https://<trycloudflare-host>/api/v2/feishu/events
```

Put this URL in **事件配置**. For interactive card buttons, put this URL in **回调配置**:

```text
https://<trycloudflare-host>/api/v2/feishu/card
```

Legacy callback remains available at `/api/feishu/events`, but the Agent-first MVP uses `/api/v2/feishu/events`.

## Environment

Required for real Feishu calls:

```env
FEISHU_APP_ID=
FEISHU_APP_SECRET=
FEISHU_EVENT_VERIFICATION_TOKEN=
FEISHU_CALENDAR_ID=primary
FEISHU_BITABLE_APP_TOKEN=
FEISHU_BITABLE_CAPTURE_TABLE_ID=
FEISHU_BITABLE_ACTION_TABLE_ID=
FEISHU_BITABLE_REVIEW_TABLE_ID=
```

Recommended v2 development:

```env
CORE_AGENT_PROVIDER=mock_provider
```

For Codex CLI provider:

```env
CORE_AGENT_PROVIDER=codex_cli_provider
CODEX_CLI_PATH=C:\Users\Administrator\AppData\Local\OpenAI\Codex\...\codex.exe
```

## Real API Gaps

The v2 Feishu native sync adapter currently mock-stages Task/Calendar/Bitable payloads unless final permission/schema validation is done. This is intentional so local e2e can run without blocking on external credentials.

For the 80% MVP, sync behavior is:

- Text/card replies call real Feishu OpenAPI when `FEISHU_APP_ID` and `FEISHU_APP_SECRET` are set.
- Task/calendar sync attempts real Feishu OpenAPI calls.
- Missing token, missing permission, or rejected payload returns `failed` with `staged_payload`.
- SQLite creation remains successful even if Feishu sync fails.
