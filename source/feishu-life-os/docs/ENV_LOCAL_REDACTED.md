# Local Environment Redacted Notes

Updated: 2026-05-29

The real `.env` on the local machine contains Feishu app credentials and Bitable table identifiers. It is intentionally excluded from the handoff package.

Use `.env.example` as the base. The important local runtime values currently are:

```text
APP_ENV=local
DATABASE_PATH=.data/lifeos.sqlite3
TIMEZONE=Asia/Singapore
PUBLIC_API_BASE=http://127.0.0.1:8000
PUBLIC_TUNNEL_PROTECTION=true
TUNNEL_MODE=quick

FEISHU_SYNC_MODE=bitable
FEISHU_OPEN_API_BASE=https://open.feishu.cn/open-apis
FEISHU_CALENDAR_ID=primary

AGENT_PROVIDER=codex_cli
CORE_AGENT_PROVIDER=lm_studio_provider
AGENT_CODEX_TIMEOUT_SECONDS=300

LM_STUDIO_BASE_URL=http://127.0.0.1:1234/v1
LM_STUDIO_MODEL=gemma-4-e4b-it
LM_STUDIO_API_KEY=
LM_STUDIO_TIMEOUT_SECONDS=120
LM_STUDIO_RESPONSE_FORMAT=none
LM_STUDIO_MAX_TOKENS=512
LM_STUDIO_CONTEXT_LENGTH=0
LM_STUDIO_USE_NATIVE_CHAT=false
```

Secrets and local IDs that must be supplied separately:

```text
ADMIN_API_TOKEN=<redacted>
FEISHU_APP_ID=<redacted>
FEISHU_APP_SECRET=<redacted>
FEISHU_EVENT_VERIFICATION_TOKEN=<redacted or empty>
FEISHU_BITABLE_APP_TOKEN=<redacted>
FEISHU_BITABLE_CAPTURE_TABLE_ID=<redacted>
FEISHU_BITABLE_ACTION_TABLE_ID=<redacted>
FEISHU_BITABLE_REVIEW_TABLE_ID=<redacted>
FEISHU_DEFAULT_ASSIGNEE_OPEN_ID=<redacted or empty>
CODEX_CLI_PATH=<local absolute path>
```

Do not commit or forward the real `.env` unless the owner explicitly chooses to share those credentials.
