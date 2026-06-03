# Next Tasks

## P0

- Replace the real runtime provider with a Chinese-capable Agent provider. Options:
  - Fix `codex_cli_provider` Chinese prompt delivery so `明天我都啥时间有空？` is not returned as `unknown`.
  - Add and use `openai_api_provider` for production Feishu messages.
  - Add a reliable local Chinese LLM provider. `lm_studio_provider` is now wired for local smoke testing.
- After provider replacement, rerun the real Feishu checklist in `validation/manual_runtime_behavior_checklist.md`.

## P1

- Verify real Feishu card callback payloads in the developer console after the current Cloudflare URL is pasted into 回调配置.
- Live-verify Feishu Calendar sync permissions and payload shape.
- Live-verify Feishu Task sync permissions and payload shape.
- Improve ScheduleBlock persistence format so recurrence details are not stored as lossy text only.
- Add richer confirmation card display for long weekly schedules.

## P2

- Add local multimodal provider for screenshots.
- Add attachment download and Evidence rows for images/files/audio.
- Add conflict-aware suggestions for where to place tasks.
- Add project/person alias learning.
- Add durable fixed Cloudflare Tunnel once a Cloudflare-managed domain is available.
