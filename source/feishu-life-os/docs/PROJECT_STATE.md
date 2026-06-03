# Project State

Updated: 2026-06-01.

## Runtime Truth

Current real Feishu runtime is being switched to a local LM Studio LLM provider.

- Active local `.env`: `CORE_AGENT_PROVIDER=lm_studio_provider`.
- Recent persisted `core_agent_runs.provider`: `mock_provider`.
- Existing persisted runs can still show `mock_provider`; new runs should show `lm_studio_provider` after the local FastAPI process restarts and LM Studio Local Server is running.
- `mock_provider` remains a deterministic Agent-shaped fallback for tests and local validation.
- `codex_cli_provider` exists and `codex.exe --version` works locally, but the live smoke test for Chinese input returned `unknown` for `明天我都啥时间有空？`. The Codex CLI transcript also showed the Chinese request as unreadable placeholder text, so it is not reliable enough to put behind the real Feishu bot yet.
- `lm_studio_provider` can call LM Studio's OpenAI-compatible `/v1/chat/completions` endpoint, or LM Studio native `/api/v1/chat` when `LM_STUDIO_USE_NATIVE_CHAT=true`. Local runtime currently uses the OpenAI-compatible endpoint with `LM_STUDIO_MODEL=gemma-4-e4b-it` and `LM_STUDIO_CONTEXT_LENGTH=0` to avoid loading a large KV cache during normal use.
- `mock_provider` and any rules-style fallback must be treated as fallback/runtime scaffolding, not as proof that the system is Agent-first.

The previous “80% MVP achieved” wording is therefore retracted for the real Feishu runtime. The local infrastructure is useful, but the real bot is not yet a genuine LLM-driven private assistant.

## Fixed In This Pass

- Feishu message handling now records structured runtime logs for each processed message:
  - `capture_id`
  - `event_id`
  - `message_id`
  - `provider_name`
  - `intent`
  - `agent_run_id`
  - `used_fallback`
  - `tool_calls`
  - `reply_text`
- Duplicate Feishu delivery with the same `message_id` is ignored before provider execution, so it should not create a second Capture, AgentRun, or reply.
- Duplicate “unknown” replies were caused by both `send_feishu_reply` tool execution and Orchestrator final reply sending. The Orchestrator now detects direct reply tool execution and does not send a second final reply.
- Added `query_availability` / free-time handling for phrases such as:
  - `明天我都啥时间有空？`
  - `明天我什么时候有空？`
  - `明天有哪些空闲时间？`
  - `我明天下午有空吗？`
  - `周六我都有哪些时间被占了？`
  - `周天能不能安排家教？`
- Today/tomorrow/week queries now include expanded `ScheduleBlock` occurrences, not just tasks and one-off calendar events.
- ScheduleBlock expansion supports Chinese weekdays:
  - 周一、周二、周三、周四、周五、周六、周天、周日
- Feishu image attachments on the v2 event path are downloaded through the message resource API, saved under `ATTACHMENT_STORAGE_DIR`, kept in current/recent context, and sent to `lm_studio_provider` as OpenAI-compatible `image_url` content parts when available.

## Currently Runnable

- `POST /api/v2/feishu/events` for Feishu message events.
- `POST /api/v2/feishu/card` for card callback confirm/cancel.
- `POST /api/v2/agent/messages` for local agent-style testing.
- Query today/tomorrow/week.
- Query availability/free time from CalendarEvent + expanded ScheduleBlock.
- Candidate creation + confirmation cards.
- Confirmation idempotency.
- AgentRun/ToolRun/Confirmation audit records.
- Real Feishu text/card adapter calls with mock fallback when credentials or permissions are missing.

## Still Mock/Stub

- The active provider is `lm_studio_provider` once `.env` is loaded by a restarted process.
- `codex_cli_provider` is wrapped but not reliable for Chinese runtime yet.
- OpenAI API and the separate `local_multimodal_provider` are stubs; `lm_studio_provider` now carries image attachments through the OpenAI-compatible chat path.
- Feishu task/calendar sync attempts are adapter-backed, but payloads still need live permission/API verification.
- Bitable is an audit/background view only, not the fact source.
- Attachment/image understanding depends on a vision-capable LM Studio model and Feishu message-resource permissions.

## Needs Feishu Console Config

Use the current Cloudflare quick tunnel host from:

```powershell
.\scripts\status_local_gateway.ps1
```

Then configure:

- 事件配置 request URL: `https://<trycloudflare-host>/api/v2/feishu/events`
- 回调配置 callback URL: `https://<trycloudflare-host>/api/v2/feishu/card`

Because this is a quick tunnel, the host can change after restart.

## Manual Steps Codex Cannot Complete Alone

- Paste the current Cloudflare URL into Feishu developer console.
- Grant and verify Feishu message/card/calendar/task permissions.
- Run a live Feishu click test for interactive card callbacks.
- Decide whether to fix Codex CLI Chinese input, switch to OpenAI API, or use another Chinese-capable local provider for the real Agent layer.

## Current Most Important Problem

Make the real runtime provider genuinely intelligent for Chinese Feishu messages. The current deterministic provider now handles the broken P0 query behavior, but it is still not a real Agent.
