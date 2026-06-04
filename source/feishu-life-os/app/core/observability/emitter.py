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


def _safe_short_text(value: Any, *, limit: int = 300) -> str:
    try:
        return str(value or "")[:limit]
    except Exception:  # noqa: BLE001 - observability must never fail on bad values.
        return f"<unprintable:{type(value).__name__}>"


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
        try:
            self.attrs.update(redact_mapping(attrs))
        except Exception as exc:  # noqa: BLE001 - span attributes are best-effort.
            self.attrs["redaction_failed"] = True
            self.attrs["redaction_error_class"] = exc.__class__.__name__

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        try:
            ended_at = _now()
            status = "failed" if exc is not None else ("ok" if self.initial_status == "running" else self.initial_status)
            attrs = dict(self.attrs)
            if exc is not None:
                attrs["error_class"] = exc.__class__.__name__
                attrs["error"] = _safe_short_text(exc)
            self.emitter._safe(
                self.emitter.store.update_span,
                self.span_id,
                status=status,
                ended_at=ended_at,
                duration_ms=_duration_ms(self.started_at, ended_at),
                attrs=attrs,
            )
        except Exception:  # noqa: BLE001 - observability must not affect the wrapped block.
            LOGGER.debug("observability span close failed", exc_info=True)
        finally:
            if self._span_token is not None:
                try:
                    _CURRENT_SPAN_ID.reset(self._span_token)
                except Exception:  # noqa: BLE001 - context cleanup is best-effort.
                    LOGGER.debug("observability span context reset failed", exc_info=True)
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

    def __init__(
        self,
        store: SQLiteTraceStore,
        *,
        max_artifact_bytes: int = 12_000,
        capture_full_payload: bool = False,
    ):
        self.store = store
        self.max_artifact_bytes = max(512, max_artifact_bytes)
        self.capture_full_payload = capture_full_payload

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
        try:
            trace = TraceRecord(
                trace_id=new_id("trace"),
                workflow_type=_safe_short_text(workflow_type, limit=80) or "agent_message",
                root_entity_type=root_entity_type,
                root_entity_id=root_entity_id,
                capture_id=capture_id,
                agent_run_id=agent_run_id,
                sender_hash=self._hash_identifier_safe(sender_id),
                status="running",
                started_at=_now(),
                attrs=self._redact_mapping_safe(attrs or {}),
            )
            self._safe(self.store.create_trace, trace)
            _CURRENT_TRACE_ID.set(trace.trace_id)
            return trace
        except Exception as exc:  # noqa: BLE001 - tracing must not fail the main request.
            LOGGER.debug("observability trace start failed", exc_info=True)
            return TraceRecord(
                trace_id="trace_unavailable",
                workflow_type=_safe_short_text(workflow_type, limit=80) or "agent_message",
                status="running",
                started_at=_now(),
                attrs={"observability_failed": True, "error_class": exc.__class__.__name__},
            )

    def update_trace(self, trace_id: str, **kwargs: Any) -> None:
        try:
            attrs = kwargs.pop("attrs", None)
            self._safe(
                self.store.update_trace,
                trace_id,
                attrs=self._redact_mapping_safe(attrs) if attrs is not None else None,
                **kwargs,
            )
        except Exception:  # noqa: BLE001 - observability must not affect main flow.
            LOGGER.debug("observability trace update failed", exc_info=True)

    def end_trace(self, trace_id: str, *, status: str, summary: str = "", attrs: dict[str, Any] | None = None) -> None:
        try:
            ended_at = _now()
            self._safe(
                self.store.update_trace,
                trace_id,
                status=_safe_short_text(status, limit=40) or "unknown",
                ended_at=ended_at,
                duration_ms=self._trace_duration_ms(trace_id, ended_at),
                summary=_safe_short_text(summary),
                attrs=self._redact_mapping_safe(attrs or {}),
            )
            if _CURRENT_TRACE_ID.get() == trace_id:
                _CURRENT_TRACE_ID.set(None)
        except Exception:  # noqa: BLE001 - observability must not affect main flow.
            LOGGER.debug("observability trace end failed", exc_info=True)

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
        try:
            active_trace_id = trace_id or _CURRENT_TRACE_ID.get()
            if not active_trace_id:
                return NullSpan()
            return TraceSpanContext(
                self,
                trace_id=active_trace_id,
                name=_safe_short_text(name, limit=120) or "unknown",
                component=_safe_short_text(component, limit=80) or "unknown",
                lane=_safe_short_text(lane, limit=40) or "unknown",
                status=_safe_short_text(status, limit=40) or "running",
                attrs=attrs,
                parent_span_id=parent_span_id,
            )
        except Exception:  # noqa: BLE001 - span creation is best-effort.
            LOGGER.debug("observability span creation failed", exc_info=True)
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
        try:
            active_trace_id = trace_id or _CURRENT_TRACE_ID.get()
            if not active_trace_id:
                return
            event = TraceEvent(
                event_id=new_id("evt"),
                trace_id=active_trace_id,
                span_id=span_id or _CURRENT_SPAN_ID.get(),
                level=_safe_short_text(level, limit=40) or "info",
                name=_safe_short_text(name, limit=120) or "event",
                message=_safe_short_text(message),
                attrs=self._redact_mapping_safe(attrs or {}),
                created_at=_now(),
            )
            self._safe(self.store.create_event, event)
        except Exception:  # noqa: BLE001 - event recording is best-effort.
            LOGGER.debug("observability event failed", exc_info=True)

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
        try:
            active_trace_id = trace_id or _CURRENT_TRACE_ID.get()
            if not active_trace_id:
                return
            payload = self._redact_mapping_safe(payload_json)
            payload, encoded = self._bounded_artifact_payload(payload, redaction)
            artifact_redaction = redaction if self.capture_full_payload else "summary_only" if redaction == "full_local" else redaction
            if artifact_redaction == "full_local" and not self.capture_full_payload:
                artifact_redaction = "summary_only"
            artifact = TraceArtifact(
                artifact_id=new_id("art"),
                trace_id=active_trace_id,
                span_id=span_id or _CURRENT_SPAN_ID.get(),
                kind=_safe_short_text(kind, limit=80) or "artifact",
                label=_safe_short_text(label, limit=120),
                redaction=_safe_short_text(artifact_redaction, limit=40) or "redacted",
                payload_json=payload,
                payload_hash=f"sha256:{hashlib.sha256(encoded).hexdigest()[:16]}",
                size_bytes=len(encoded),
                created_at=_now(),
            )
            self._safe(self.store.create_artifact, artifact)
        except Exception:  # noqa: BLE001 - artifact recording is best-effort.
            LOGGER.debug("observability artifact failed", exc_info=True)

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
        try:
            active_trace_id = trace_id or _CURRENT_TRACE_ID.get()
            if not active_trace_id:
                return
            diff = StateDiff(
                diff_id=new_id("diff"),
                trace_id=active_trace_id,
                span_id=span_id or _CURRENT_SPAN_ID.get(),
                entity_type=_safe_short_text(entity_type, limit=80) or "entity",
                entity_id=_safe_short_text(entity_id, limit=120) or "unknown",
                operation=_safe_short_text(operation, limit=80) or "unknown",
                before_summary=self._redact_mapping_safe(before_summary or {}),
                after_summary=self._redact_mapping_safe(after_summary or {}),
                created_at=_now(),
            )
            self._safe(self.store.create_state_diff, diff)
        except Exception:  # noqa: BLE001 - state diff recording is best-effort.
            LOGGER.debug("observability state diff failed", exc_info=True)

    def _trace_duration_ms(self, trace_id: str, ended_at: datetime) -> int | None:
        try:
            detail = self.store.get_trace(trace_id)
            if detail:
                return _duration_ms(detail.trace.started_at, ended_at)
        except Exception:  # noqa: BLE001 - observability must not affect main flow.
            LOGGER.debug("failed to compute trace duration", exc_info=True)
        return None

    def _bounded_artifact_payload(self, payload: dict[str, Any], redaction: str) -> tuple[dict[str, Any], bytes]:
        if redaction == "full_local" and not self.capture_full_payload:
            payload = {
                "summary_only": True,
                "keys": sorted(payload.keys()),
                "payload_hash": self._payload_hash(payload),
            }
        payload, encoded = self._encode_payload_safe(payload)
        if len(encoded) <= self.max_artifact_bytes:
            return payload, encoded
        bounded = {
            "summary_only": True,
            "truncated": True,
            "original_size_bytes": len(encoded),
            "payload_hash": f"sha256:{hashlib.sha256(encoded).hexdigest()[:16]}",
            "keys": sorted(payload.keys()),
        }
        bounded, encoded = self._encode_payload_safe(bounded)
        return bounded, encoded

    def _payload_hash(self, payload: dict[str, Any]) -> str:
        _, encoded = self._encode_payload_safe(payload)
        return f"sha256:{hashlib.sha256(encoded).hexdigest()[:16]}"

    def _hash_identifier_safe(self, value: Any) -> str | None:
        try:
            return hash_identifier(value)
        except Exception:  # noqa: BLE001 - hashing is best-effort.
            LOGGER.debug("observability identifier hashing failed", exc_info=True)
            return None

    def _redact_mapping_safe(self, value: dict[str, Any] | None) -> dict[str, Any]:
        try:
            return redact_mapping(value)
        except Exception as exc:  # noqa: BLE001 - redaction failures must not escape.
            LOGGER.debug("observability redaction failed", exc_info=True)
            return {"redaction_failed": True, "error_class": exc.__class__.__name__}

    def _encode_payload_safe(self, payload: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
        try:
            return payload, json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        except Exception as exc:  # noqa: BLE001 - serialization failures become a summary payload.
            LOGGER.debug("observability payload serialization failed", exc_info=True)
            fallback = {
                "summary_only": True,
                "serialization_failed": True,
                "error_class": exc.__class__.__name__,
                "keys": sorted(_safe_short_text(key, limit=120) for key in payload),
            }
            return fallback, json.dumps(fallback, ensure_ascii=False).encode("utf-8")

    def _safe(self, func: Any, *args: Any, **kwargs: Any) -> None:
        started = time.perf_counter()
        try:
            func(*args, **kwargs)
        except Exception:  # noqa: BLE001 - observability is best-effort.
            LOGGER.debug("observability write failed", exc_info=True)
        finally:
            if time.perf_counter() - started > 1:
                LOGGER.debug("slow observability write: %s", getattr(func, "__name__", repr(func)))
