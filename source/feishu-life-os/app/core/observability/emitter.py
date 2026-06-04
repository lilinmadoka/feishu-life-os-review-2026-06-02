from __future__ import annotations

import hashlib
import json
import logging
import time
from contextvars import ContextVar
from datetime import UTC, datetime
from types import TracebackType
from typing import Any

from app.core.observability.redaction import hash_identifier, redact_mapping
from app.core.observability.schemas import (
    StateDiff,
    TraceArtifact,
    TraceEvent,
    TraceRecord,
    TraceSpan,
)
from app.core.observability.store import SQLiteTraceStore
from app.database import new_id

LOGGER = logging.getLogger("lifeos.observability")

_CURRENT_TRACE_ID: ContextVar[str | None] = ContextVar("observability_trace_id", default=None)
_CURRENT_SPAN_ID: ContextVar[str | None] = ContextVar("observability_span_id", default=None)


def _now() -> datetime:
    return datetime.now(UTC)


def _duration_ms(started_at: datetime, ended_at: datetime) -> int:
    return max(0, int((ended_at - started_at).total_seconds() * 1000))


class NullSpan:
    span_id: str | None = None

    def add_attrs(self, attrs: dict[str, Any]) -> None:
        return None

    def __enter__(self) -> NullSpan:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        return False


class TraceSpanContext:
    def __init__(
        self,
        emitter: SQLiteTraceEmitter,
        *,
        trace_id: str,
        name: str,
        component: str,
        lane: str,
        status: str = "running",
        attrs: dict[str, Any] | None = None,
        parent_span_id: str | None = None,
    ) -> None:
        self.emitter = emitter
        self.trace_id = trace_id
        self.name = name
        self.component = component
        self.lane = lane
        self.initial_status = status
        self.attrs = redact_mapping(attrs or {})
        self.parent_span_id = parent_span_id
        self.span_id = new_id("span")
        self.started_at = _now()
        self._span_token = None

    def __enter__(self) -> TraceSpanContext:
        span = TraceSpan(
            span_id=self.span_id,
            trace_id=self.trace_id,
            parent_span_id=self.parent_span_id or _CURRENT_SPAN_ID.get(),
            name=self.name,
            component=self.component,
            lane=self.lane,
            status=self.initial_status,
            started_at=self.started_at,
            attrs=self.attrs,
        )
        self.emitter._safe(self.emitter.store.create_span, span)
        self._span_token = _CURRENT_SPAN_ID.set(self.span_id)
        return self

    def add_attrs(self, attrs: dict[str, Any]) -> None:
        self.attrs.update(redact_mapping(attrs))

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        ended_at = _now()
        status = "failed" if exc is not None else ("ok" if self.initial_status == "running" else self.initial_status)
        attrs = dict(self.attrs)
        if exc is not None:
            attrs["error_class"] = exc.__class__.__name__
            attrs["error"] = str(exc)[:300]
        self.emitter._safe(
            self.emitter.store.update_span,
            self.span_id,
            status=status,
            ended_at=ended_at,
            duration_ms=_duration_ms(self.started_at, ended_at),
            attrs=attrs,
        )
        if self._span_token is not None:
            _CURRENT_SPAN_ID.reset(self._span_token)
        return False


class TraceEmitter:
    enabled = False

    def start_trace(
        self,
        *,
        workflow_type: str,
        root_entity_type: str | None = None,
        root_entity_id: str | None = None,
        capture_id: str | None = None,
        agent_run_id: str | None = None,
        sender_id: str | None = None,
        attrs: dict[str, Any] | None = None,
    ) -> TraceRecord:
        return TraceRecord(
            trace_id="trace_null",
            workflow_type=workflow_type,
            root_entity_type=root_entity_type,
            root_entity_id=root_entity_id,
            capture_id=capture_id,
            agent_run_id=agent_run_id,
            sender_hash=hash_identifier(sender_id),
            status="running",
            started_at=_now(),
            attrs=redact_mapping(attrs or {}),
        )

    def update_trace(self, trace_id: str, **kwargs: Any) -> None:
        return None

    def end_trace(self, trace_id: str, *, status: str, summary: str = "", attrs: dict[str, Any] | None = None) -> None:
        return None

    def span(
        self,
        trace_id: str | None,
        name: str,
        *,
        component: str,
        lane: str,
        status: str = "running",
        attrs: dict[str, Any] | None = None,
        parent_span_id: str | None = None,
    ) -> NullSpan:
        return NullSpan()

    def event(
        self,
        trace_id: str | None,
        *,
        name: str,
        level: str = "info",
        message: str = "",
        attrs: dict[str, Any] | None = None,
        span_id: str | None = None,
    ) -> None:
        return None

    def artifact(
        self,
        trace_id: str | None,
        *,
        kind: str,
        label: str,
        payload_json: dict[str, Any],
        redaction: str = "redacted",
        span_id: str | None = None,
    ) -> None:
        return None

    def state_diff(
        self,
        trace_id: str | None,
        *,
        entity_type: str,
        entity_id: str,
        operation: str,
        before_summary: dict[str, Any] | None = None,
        after_summary: dict[str, Any] | None = None,
        span_id: str | None = None,
    ) -> None:
        return None


class NullTraceEmitter(TraceEmitter):
    enabled = False


class SQLiteTraceEmitter(TraceEmitter):
    enabled = True

    def __init__(self, store: SQLiteTraceStore):
        self.store = store

    def start_trace(
        self,
        *,
        workflow_type: str,
        root_entity_type: str | None = None,
        root_entity_id: str | None = None,
        capture_id: str | None = None,
        agent_run_id: str | None = None,
        sender_id: str | None = None,
        attrs: dict[str, Any] | None = None,
    ) -> TraceRecord:
        trace = TraceRecord(
            trace_id=new_id("trace"),
            workflow_type=workflow_type,
            root_entity_type=root_entity_type,
            root_entity_id=root_entity_id,
            capture_id=capture_id,
            agent_run_id=agent_run_id,
            sender_hash=hash_identifier(sender_id),
            status="running",
            started_at=_now(),
            attrs=redact_mapping(attrs or {}),
        )
        self._safe(self.store.create_trace, trace)
        _CURRENT_TRACE_ID.set(trace.trace_id)
        return trace

    def update_trace(self, trace_id: str, **kwargs: Any) -> None:
        attrs = kwargs.pop("attrs", None)
        self._safe(self.store.update_trace, trace_id, attrs=redact_mapping(attrs) if attrs is not None else None, **kwargs)

    def end_trace(self, trace_id: str, *, status: str, summary: str = "", attrs: dict[str, Any] | None = None) -> None:
        ended_at = _now()
        self._safe(
            self.store.update_trace,
            trace_id,
            status=status,
            ended_at=ended_at,
            duration_ms=self._trace_duration_ms(trace_id, ended_at),
            summary=summary[:300],
            attrs=redact_mapping(attrs or {}),
        )
        if _CURRENT_TRACE_ID.get() == trace_id:
            _CURRENT_TRACE_ID.set(None)

    def span(
        self,
        trace_id: str | None,
        name: str,
        *,
        component: str,
        lane: str,
        status: str = "running",
        attrs: dict[str, Any] | None = None,
        parent_span_id: str | None = None,
    ) -> TraceSpanContext | NullSpan:
        active_trace_id = trace_id or _CURRENT_TRACE_ID.get()
        if not active_trace_id:
            return NullSpan()
        return TraceSpanContext(
            self,
            trace_id=active_trace_id,
            name=name,
            component=component,
            lane=lane,
            status=status,
            attrs=attrs,
            parent_span_id=parent_span_id,
        )

    def event(
        self,
        trace_id: str | None,
        *,
        name: str,
        level: str = "info",
        message: str = "",
        attrs: dict[str, Any] | None = None,
        span_id: str | None = None,
    ) -> None:
        active_trace_id = trace_id or _CURRENT_TRACE_ID.get()
        if not active_trace_id:
            return
        event = TraceEvent(
            event_id=new_id("evt"),
            trace_id=active_trace_id,
            span_id=span_id or _CURRENT_SPAN_ID.get(),
            level=level,
            name=name,
            message=message[:300],
            attrs=redact_mapping(attrs or {}),
            created_at=_now(),
        )
        self._safe(self.store.create_event, event)

    def artifact(
        self,
        trace_id: str | None,
        *,
        kind: str,
        label: str,
        payload_json: dict[str, Any],
        redaction: str = "redacted",
        span_id: str | None = None,
    ) -> None:
        active_trace_id = trace_id or _CURRENT_TRACE_ID.get()
        if not active_trace_id:
            return
        payload = redact_mapping(payload_json)
        encoded = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        artifact = TraceArtifact(
            artifact_id=new_id("art"),
            trace_id=active_trace_id,
            span_id=span_id or _CURRENT_SPAN_ID.get(),
            kind=kind,
            label=label[:120],
            redaction=redaction,
            payload_json=payload,
            payload_hash=f"sha256:{hashlib.sha256(encoded).hexdigest()[:16]}",
            size_bytes=len(encoded),
            created_at=_now(),
        )
        self._safe(self.store.create_artifact, artifact)

    def state_diff(
        self,
        trace_id: str | None,
        *,
        entity_type: str,
        entity_id: str,
        operation: str,
        before_summary: dict[str, Any] | None = None,
        after_summary: dict[str, Any] | None = None,
        span_id: str | None = None,
    ) -> None:
        active_trace_id = trace_id or _CURRENT_TRACE_ID.get()
        if not active_trace_id:
            return
        diff = StateDiff(
            diff_id=new_id("diff"),
            trace_id=active_trace_id,
            span_id=span_id or _CURRENT_SPAN_ID.get(),
            entity_type=entity_type,
            entity_id=entity_id,
            operation=operation,
            before_summary=redact_mapping(before_summary or {}),
            after_summary=redact_mapping(after_summary or {}),
            created_at=_now(),
        )
        self._safe(self.store.create_state_diff, diff)

    def _trace_duration_ms(self, trace_id: str, ended_at: datetime) -> int | None:
        try:
            detail = self.store.get_trace(trace_id)
            if detail:
                return _duration_ms(detail.trace.started_at, ended_at)
        except Exception:  # noqa: BLE001 - observability must not affect main flow.
            LOGGER.debug("failed to compute trace duration", exc_info=True)
        return None

    def _safe(self, func: Any, *args: Any, **kwargs: Any) -> None:
        started = time.perf_counter()
        try:
            func(*args, **kwargs)
        except Exception:  # noqa: BLE001 - observability is best-effort.
            LOGGER.debug("observability write failed", exc_info=True)
        finally:
            if time.perf_counter() - started > 1:
                LOGGER.debug("slow observability write: %s", getattr(func, "__name__", repr(func)))
