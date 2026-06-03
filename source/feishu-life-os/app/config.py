from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = Field(default="local", alias="APP_ENV")
    database_path: str = Field(default=".data/lifeos.sqlite3", alias="DATABASE_PATH")
    database_url: str | None = Field(default=None, alias="DATABASE_URL")
    timezone: str = Field(default="Asia/Singapore", alias="TIMEZONE")
    assistant_user_display_name: str = Field(default="我", alias="ASSISTANT_USER_DISPLAY_NAME")
    default_morning_review_hour: int = Field(default=7, alias="DEFAULT_MORNING_REVIEW_HOUR")
    default_morning_review_minute: int = Field(default=30, alias="DEFAULT_MORNING_REVIEW_MINUTE")
    default_evening_review_hour: int = Field(default=21, alias="DEFAULT_EVENING_REVIEW_HOUR")
    daily_review_followup_hours: int = Field(default=2, alias="DAILY_REVIEW_FOLLOWUP_HOURS")
    admin_api_token: str | None = Field(default=None, alias="ADMIN_API_TOKEN")
    public_api_base: str | None = Field(default=None, alias="PUBLIC_API_BASE")
    public_tunnel_protection: bool = Field(default=True, alias="PUBLIC_TUNNEL_PROTECTION")

    feishu_sync_mode: str = Field(default="dry_run", alias="FEISHU_SYNC_MODE")
    feishu_app_id: str | None = Field(default=None, alias="FEISHU_APP_ID")
    feishu_app_secret: str | None = Field(default=None, alias="FEISHU_APP_SECRET")
    feishu_open_api_base: str = Field(
        default="https://open.feishu.cn/open-apis", alias="FEISHU_OPEN_API_BASE"
    )
    feishu_bot_webhook: str | None = Field(default=None, alias="FEISHU_BOT_WEBHOOK")
    feishu_bot_secret: str | None = Field(default=None, alias="FEISHU_BOT_SECRET")
    feishu_event_verification_token: str | None = Field(
        default=None, alias="FEISHU_EVENT_VERIFICATION_TOKEN"
    )
    feishu_bitable_app_token: str | None = Field(default=None, alias="FEISHU_BITABLE_APP_TOKEN")
    feishu_bitable_schema: str = Field(default="project", alias="FEISHU_BITABLE_SCHEMA")
    feishu_bitable_capture_table_id: str | None = Field(
        default=None, alias="FEISHU_BITABLE_CAPTURE_TABLE_ID"
    )
    feishu_bitable_action_table_id: str | None = Field(
        default=None, alias="FEISHU_BITABLE_ACTION_TABLE_ID"
    )
    feishu_bitable_review_table_id: str | None = Field(
        default=None, alias="FEISHU_BITABLE_REVIEW_TABLE_ID"
    )
    feishu_default_assignee_open_id: str | None = Field(
        default=None, alias="FEISHU_DEFAULT_ASSIGNEE_OPEN_ID"
    )
    feishu_calendar_attendee_open_ids: str | None = Field(
        default=None, alias="FEISHU_CALENDAR_ATTENDEE_OPEN_IDS"
    )
    feishu_allowed_open_ids: str | None = Field(default=None, alias="FEISHU_ALLOWED_OPEN_IDS")
    feishu_calendar_id: str = Field(default="primary", alias="FEISHU_CALENDAR_ID")
    feishu_strong_reminder_mode: str = Field(default="video_meeting", alias="FEISHU_STRONG_REMINDER_MODE")
    feishu_video_meeting_owner_open_id: str | None = Field(
        default=None, alias="FEISHU_VIDEO_MEETING_OWNER_OPEN_ID"
    )
    feishu_video_meeting_ttl_minutes: int = Field(default=30, alias="FEISHU_VIDEO_MEETING_TTL_MINUTES")
    feishu_video_meeting_call_enabled: bool = Field(default=False, alias="FEISHU_VIDEO_MEETING_CALL_ENABLED")
    pushover_user_key: str | None = Field(default=None, alias="PUSHOVER_USER_KEY")
    pushover_app_token: str | None = Field(default=None, alias="PUSHOVER_APP_TOKEN")
    pushover_retry_seconds: int = Field(default=60, alias="PUSHOVER_RETRY_SECONDS")
    pushover_expire_seconds: int = Field(default=600, alias="PUSHOVER_EXPIRE_SECONDS")
    pushover_sound: str = Field(default="siren", alias="PUSHOVER_SOUND")
    codex_cli_path: str = Field(
        default=r"C:\Users\Administrator\AppData\Roaming\npm\codex.ps1",
        alias="CODEX_CLI_PATH",
    )
    codex_worker_poll_seconds: float = Field(default=10, alias="CODEX_WORKER_POLL_SECONDS")
    reminder_worker_poll_seconds: float = Field(default=60, alias="REMINDER_WORKER_POLL_SECONDS")
    agent_provider: str = Field(default="codex_cli", alias="AGENT_PROVIDER")
    agent_codex_timeout_seconds: int = Field(default=300, alias="AGENT_CODEX_TIMEOUT_SECONDS")
    agent_stack: str = Field(default="legacy", alias="AGENT_STACK")
    core_agent_provider: str = Field(default="mock_provider", alias="CORE_AGENT_PROVIDER")
    lm_studio_base_url: str = Field(default="http://127.0.0.1:1234/v1", alias="LM_STUDIO_BASE_URL")
    lm_studio_model: str | None = Field(default=None, alias="LM_STUDIO_MODEL")
    lm_studio_api_key: str | None = Field(default=None, alias="LM_STUDIO_API_KEY")
    lm_studio_timeout_seconds: int = Field(default=120, alias="LM_STUDIO_TIMEOUT_SECONDS")
    lm_studio_response_format: str = Field(default="none", alias="LM_STUDIO_RESPONSE_FORMAT")
    lm_studio_max_tokens: int | None = Field(default=512, alias="LM_STUDIO_MAX_TOKENS")
    lm_studio_context_length: int | None = Field(default=None, alias="LM_STUDIO_CONTEXT_LENGTH")
    lm_studio_use_native_chat: bool = Field(default=False, alias="LM_STUDIO_USE_NATIVE_CHAT")
    attachment_storage_dir: str = Field(default=".data/attachments", alias="ATTACHMENT_STORAGE_DIR")
    vision_attachment_max_bytes: int = Field(default=8 * 1024 * 1024, alias="VISION_ATTACHMENT_MAX_BYTES")

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@lru_cache
def get_settings() -> Settings:
    return Settings()
