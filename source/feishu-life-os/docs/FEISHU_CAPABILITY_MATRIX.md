# Feishu Capability Matrix

| Capability | Status | Needed Permission/Config | Validation | Mock Validation |
|---|---|---|---|---|
| Receive private messages | Legacy implemented, v2 implemented | event subscription `im.message.receive_v1`, verification token, public HTTPS callback | send private message to bot | POST `/api/v2/feishu/events` sample payload |
| Receive group @ messages | v2 implemented with mention guard | same event permission | @ bot in group | mock group payload |
| Send plain text | implemented | app message send permission, `FEISHU_APP_ID`, `FEISHU_APP_SECRET` | bot replies in chat | `MockFeishuNativeAdapter.sent_texts` |
| Send rich text | not implemented | message send permission | future | stub only |
| Send interactive card | adapter implemented, real card schema needs final Feishu verification | message send permission | card appears in chat | `MockFeishuNativeAdapter.sent_cards` |
| Receive card callback | v2 implemented for confirm/cancel | card callback config | click card button | POST `/api/v2/feishu/card` |
| Download image | v2 implemented | message resource permissions | send image | saved locally and attached to LM Studio vision request |
| Download file | stub | file resource permissions | send file | attachment stored as ref |
| Voice resource processing | stub | audio resource permissions + ASR provider | send voice | attachment stored as ref |
| Feishu Task API | client call implemented, returns failed/staged on missing permission/token | task write permission, assignee id | create task | `sync_feishu_task` staged |
| Feishu Calendar API | client call implemented, returns failed/staged on missing permission/token | calendar write permission, `FEISHU_CALENDAR_ID` | create event | `sync_feishu_calendar` staged |
| Bitable API | legacy implemented | app token, table ids, Bitable permissions | record appears | `sync_bitable_audit` mock |
| HTTP callback mode | implemented | Cloudflare Tunnel URL | challenge passes | TestClient |
| Long connection mode | not implemented | Feishu SDK/client | future | none |

## Environment Variables

- `FEISHU_APP_ID`
- `FEISHU_APP_SECRET`
- `FEISHU_EVENT_VERIFICATION_TOKEN`
- `FEISHU_CALENDAR_ID`
- `FEISHU_BITABLE_APP_TOKEN`
- `FEISHU_BITABLE_CAPTURE_TABLE_ID`
- `FEISHU_BITABLE_ACTION_TABLE_ID`
- `FEISHU_BITABLE_REVIEW_TABLE_ID`
- `CORE_AGENT_PROVIDER`
- `CODEX_CLI_PATH`

## Current Gap

Task/calendar payloads now attempt real OpenAPI calls when credentials exist. Real Feishu permission and schema verification is still required; failures are captured as `failed` sync results with `staged_payload`.
