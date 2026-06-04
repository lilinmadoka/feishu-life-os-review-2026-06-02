from functools import lru_cache

from app.adapters.feishu_client import FeishuClient
from app.agents.orchestrator import AgentOrchestrator
from app.agents.providers.base import AgentProvider
from app.agents.providers.codex_cli import CodexCliAgentProvider
from app.agents.providers.rules_fallback import RulesFallbackAgentProvider
from app.config import Settings, get_settings
from app.core.feishu_native import FeishuOpenApiNativeAdapter, MockFeishuNativeAdapter
from app.core.observability import (
    NullTraceEmitter,
    SQLiteTraceEmitter,
    SQLiteTraceStore,
    TraceEmitter,
)
from app.core.orchestrator import CoreAgentOrchestrator
from app.core.providers import (
    CodexCliProvider,
    LmStudioProvider,
    LocalMultimodalProvider,
    MockAgentProvider,
    OpenAIApiProvider,
)
from app.core.store import StateStore
from app.database import Repository
from app.services.capture_service import CaptureService
from app.services.extraction_service import RuleBasedExtractor
from app.services.review_service import ReviewService
from app.services.sync_service import SyncService


@lru_cache
def get_repo() -> Repository:
    settings = get_settings()
    repo = Repository(settings.database_path, database_url=settings.database_url)
    repo.migrate()
    return repo


@lru_cache
def get_extractor() -> RuleBasedExtractor:
    settings = get_settings()
    return RuleBasedExtractor(settings.tzinfo)


def get_capture_service() -> CaptureService:
    return CaptureService(get_repo(), get_extractor())


def get_review_service() -> ReviewService:
    settings = get_settings()
    return ReviewService(get_repo(), settings.tzinfo)


@lru_cache
def get_feishu_client() -> FeishuClient:
    return FeishuClient(get_settings())


def get_sync_service() -> SyncService:
    settings: Settings = get_settings()
    return SyncService(get_repo(), get_feishu_client(), settings.feishu_sync_mode)


@lru_cache
def get_agent_provider() -> AgentProvider:
    settings = get_settings()
    provider = settings.agent_provider.lower()
    if provider == "rules_fallback":
        return RulesFallbackAgentProvider()
    if provider == "codex_cli":
        return CodexCliAgentProvider(
            settings.codex_cli_path,
            timeout_seconds=settings.agent_codex_timeout_seconds,
        )
    raise RuntimeError(f"Unsupported AGENT_PROVIDER: {settings.agent_provider}")


def get_agent_orchestrator() -> AgentOrchestrator:
    settings = get_settings()
    return AgentOrchestrator(
        repo=get_repo(),
        provider=get_agent_provider(),
        review_service=get_review_service(),
        sync=get_sync_service(),
        feishu=get_feishu_client(),
        tz=settings.tzinfo,
    )


@lru_cache
def get_core_store() -> StateStore:
    store = StateStore(get_repo())
    store.migrate()
    return store


@lru_cache
def get_core_provider():
    settings = get_settings()
    provider = settings.core_agent_provider.lower()
    if provider == "mock_provider":
        return MockAgentProvider(settings.tzinfo)
    if provider == "codex_cli_provider":
        return CodexCliProvider(
            settings.codex_cli_path,
            timeout_seconds=settings.agent_codex_timeout_seconds,
            workdir=".",
        )
    if provider == "lm_studio_provider":
        return LmStudioProvider(
            base_url=settings.lm_studio_base_url,
            model=settings.lm_studio_model,
            api_key=settings.lm_studio_api_key,
            timeout_seconds=settings.lm_studio_timeout_seconds,
            response_format=settings.lm_studio_response_format,
            max_tokens=settings.lm_studio_max_tokens,
            context_length=settings.lm_studio_context_length,
            use_native_chat=settings.lm_studio_use_native_chat,
            max_image_bytes=settings.vision_attachment_max_bytes,
        )
    if provider == "openai_api_provider":
        return OpenAIApiProvider()
    if provider == "local_multimodal_provider":
        return LocalMultimodalProvider()
    raise RuntimeError(f"Unsupported CORE_AGENT_PROVIDER: {settings.core_agent_provider}")


@lru_cache
def get_core_feishu_adapter():
    settings = get_settings()
    if settings.feishu_app_id and settings.feishu_app_secret:
        return FeishuOpenApiNativeAdapter(get_feishu_client())
    return MockFeishuNativeAdapter()


@lru_cache
def get_observability_store() -> SQLiteTraceStore:
    store = SQLiteTraceStore(get_repo())
    store.migrate()
    return store


def get_trace_emitter() -> TraceEmitter:
    settings = get_settings()
    if not settings.observability_enabled:
        return NullTraceEmitter()
    return SQLiteTraceEmitter(get_observability_store())


def get_core_orchestrator() -> CoreAgentOrchestrator:
    settings = get_settings()
    return CoreAgentOrchestrator(
        store=get_core_store(),
        provider=get_core_provider(),
        feishu=get_core_feishu_adapter(),
        tz=settings.tzinfo,
        trace_emitter=get_trace_emitter(),
    )
