"""Safe, centralized audit instrumentation for MCP traffic."""
from __future__ import annotations

import contextvars
import hashlib
import ipaddress
import json
import logging
import queue
import re
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from typing import Any

from .mcp_log_models import McpLogEvent
from .mcp_log_store import insert_event

logger = logging.getLogger(__name__)

_CREDENTIAL_URL_RE = re.compile(
    r"(?i)(\b[a-z][a-z0-9+.-]*://[^\s:/@]+:)([^\s/@]+)(@)"
)
_QUOTED_HEADER_VALUE_RE = re.compile(
    r"(?ix)"
    r"([\"']?\b(?:authorization|set[\s_-]*cookie|cookies?)\b[\"']?\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*')"
)
_HEADER_VALUE_RE = re.compile(
    r"(?im)(\b(?:authorization|set[\s_-]*cookie|cookies?)\b\s*[:=]\s*)"
    r"[^\r\n]+"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_SENSITIVE_KEY_RE = re.compile(
    r"(?ix)"
    r"([\"']?\b(?:"
    r"mcp[\s_-]*token|access[\s_-]*token|refresh[\s_-]*token|token|"
    r"corp[\s_-]*secret|api[\s_-]*key|dsn|"
    r"private[\s_-]*key|jwt|session(?:[\s_-]*id)?|"
    r"client[\s_-]*secret|contact[\s_-]*secret|secret|"
    r"db[\s_-]*password|database[\s_-]*password|password|passwd|pwd|"
    r"database[\s_-]*url"
    r")\b[\"']?\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;}\]]+)"
)

_request_metadata: contextvars.ContextVar[dict[str, str]] = contextvars.ContextVar(
    "mcp_request_metadata", default={}
)


def safe_summary(value: Any, limit: int) -> str:
    """Return a redacted, bounded representation suitable for audit storage."""
    if limit <= 0:
        return ""
    if isinstance(value, (dict, list, tuple)):
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = type(value).__name__
    else:
        try:
            text = str(value)
        except Exception:
            text = type(value).__name__

    text = _CREDENTIAL_URL_RE.sub(r"\1[REDACTED]\3", text)
    text = _QUOTED_HEADER_VALUE_RE.sub(r'\1"[REDACTED]"', text)
    text = _HEADER_VALUE_RE.sub(r"\1[REDACTED]", text)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _SENSITIVE_KEY_RE.sub(r'\1"[REDACTED]"', text)
    return text[:limit]


class AuditEventWriter:
    """Bounded single-worker queue isolating audit persistence from requests."""

    def __init__(
        self,
        *,
        insert: Callable[[McpLogEvent], None] | None = None,
        max_queue_size: int = 2048,
        warning_interval: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._insert = insert
        self._queue: queue.Queue[McpLogEvent] = queue.Queue(
            maxsize=max(1, int(max_queue_size))
        )
        self._warning_interval = max(0.0, float(warning_interval))
        self._clock = clock
        self._state_lock = threading.Lock()
        self._pending_condition = threading.Condition()
        self._pending = 0
        self._thread: threading.Thread | None = None
        self._stop_requested = threading.Event()
        self._accepting = True
        self._dropped_count = 0
        self._unreported_drops = 0
        self._unreported_drop_type = "Full"
        self._last_drop_warning: float | None = None

    @property
    def dropped_count(self) -> int:
        with self._state_lock:
            return self._dropped_count

    def start(self) -> bool:
        """Start the worker eagerly when the application lifespan begins."""
        with self._state_lock:
            if not self._accepting:
                return False
            self._ensure_worker_locked()
            return True

    def _ensure_worker_locked(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run,
            name="mcp-audit-writer",
            daemon=True,
        )
        self._thread.start()

    def _record_drop_locked(self, reason_type: str) -> None:
        self._dropped_count += 1
        self._unreported_drops += 1
        self._unreported_drop_type = reason_type

    def _emit_drop_warning(self, *, force: bool = False) -> None:
        with self._state_lock:
            if not self._unreported_drops:
                return
            now = self._clock()
            if (
                not force
                and self._last_drop_warning is not None
                and now - self._last_drop_warning < self._warning_interval
            ):
                return
            count = self._unreported_drops
            reason_type = self._unreported_drop_type
            self._unreported_drops = 0
            self._last_drop_warning = now
        try:
            logger.warning(
                "MCP audit queue full type=%s dropped_count=%s",
                reason_type,
                count,
            )
        except Exception:
            return

    def submit(self, event: McpLogEvent) -> bool:
        """Queue one event without blocking the caller."""
        with self._state_lock:
            if not self._accepting:
                self._record_drop_locked("WriterClosed")
                return False
            try:
                self._ensure_worker_locked()
            except Exception as exc:
                self._record_drop_locked(type(exc).__name__)
                return False

            with self._pending_condition:
                self._pending += 1
            try:
                self._queue.put_nowait(event)
                return True
            except queue.Full:
                with self._pending_condition:
                    self._pending -= 1
                    self._pending_condition.notify_all()
                self._record_drop_locked("Full")
                return False

    def _run(self) -> None:
        try:
            while True:
                self._emit_drop_warning()
                try:
                    event = self._queue.get(timeout=0.1)
                except queue.Empty:
                    if self._stop_requested.is_set():
                        return
                    continue

                try:
                    operation = insert_event if self._insert is None else self._insert
                    operation(event)
                except Exception as exc:
                    logger.warning(
                        "MCP audit storage failed type=%s",
                        type(exc).__name__,
                    )
                finally:
                    self._queue.task_done()
                    with self._pending_condition:
                        self._pending -= 1
                        self._pending_condition.notify_all()
                if self._stop_requested.is_set() and self._queue.empty():
                    return
        finally:
            self._emit_drop_warning(force=True)

    def flush(self, timeout: float = 2.0) -> bool:
        """Wait up to timeout seconds for queued and active writes to finish."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._pending_condition:
            while self._pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._pending_condition.wait(remaining)
        return True

    def _begin_shutdown(self) -> threading.Thread | None:
        """Atomically stop accepting new events without waiting for storage."""
        with self._state_lock:
            self._accepting = False
            self._stop_requested.set()
            return self._thread

    def shutdown(self, timeout: float = 2.0) -> bool:
        """Stop accepting events and perform a bounded best-effort flush."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        thread = self._begin_shutdown()

        flushed = self.flush(max(0.0, deadline - time.monotonic()))
        if thread is not None:
            thread.join(max(0.0, deadline - time.monotonic()))
        self._emit_drop_warning(force=True)
        return flushed and (thread is None or not thread.is_alive())


_audit_writer = AuditEventWriter()
_audit_writer_lifecycle_lock = threading.Lock()
_audit_writer_refcount = 0


def acquire_audit_writer() -> AuditEventWriter:
    """Acquire one application-lifespan reference and eagerly start its worker."""
    global _audit_writer, _audit_writer_refcount
    with _audit_writer_lifecycle_lock:
        if _audit_writer_refcount == 0:
            if not _audit_writer.start():
                _audit_writer = AuditEventWriter()
                _audit_writer.start()
        _audit_writer_refcount += 1
        return _audit_writer


def release_audit_writer(timeout: float = 2.0) -> bool:
    """Release one lifespan reference, stopping only after the final release."""
    global _audit_writer_refcount
    with _audit_writer_lifecycle_lock:
        if _audit_writer_refcount <= 0:
            return True
        _audit_writer_refcount -= 1
        if _audit_writer_refcount:
            return True
        writer = _audit_writer
        writer._begin_shutdown()
    return writer.shutdown(timeout)


def write_event(event: McpLogEvent) -> bool:
    """Queue an event without blocking or touching storage on the caller thread."""
    try:
        return _audit_writer.submit(event)
    except Exception:
        return False


def shutdown_audit_writer(timeout: float = 2.0) -> bool:
    """Force a bounded process-lifetime shutdown, regardless of active leases."""
    global _audit_writer_refcount
    with _audit_writer_lifecycle_lock:
        _audit_writer_refcount = 0
        writer = _audit_writer
        writer._begin_shutdown()
    return writer.shutdown(timeout)


def _normalized_ip(value: Any) -> str:
    try:
        address = ipaddress.ip_address(str(value).strip())
    except (TypeError, ValueError):
        return ""
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return str(address.ipv4_mapped)
    return address.compressed


def client_ip_from_scope(scope: dict[str, Any]) -> str:
    """Read and normalize the direct ASGI peer address."""
    client = scope.get("client")
    if not isinstance(client, (tuple, list)) or not client:
        return ""
    return _normalized_ip(client[0])


class AuthWriteLimiter:
    """Rolling-window limiter keyed exclusively by normalized IP and event."""

    def __init__(
        self,
        limit: int = 60,
        window_seconds: float = 60.0,
        max_buckets: int = 4096,
    ) -> None:
        self.limit = max(1, int(limit))
        self.window_seconds = max(0.001, float(window_seconds))
        self.max_buckets = max(1, int(max_buckets))
        self._buckets: OrderedDict[tuple[str, str], deque[float]] = OrderedDict()
        self._warned_buckets: set[tuple[str, str]] = set()
        self._lock = threading.Lock()

    @staticmethod
    def _prune_timestamps(timestamps: deque[float], cutoff: float) -> None:
        while timestamps and timestamps[0] <= cutoff:
            timestamps.popleft()

    def _prune_oldest_buckets(self, cutoff: float) -> None:
        while self._buckets:
            key = next(iter(self._buckets))
            timestamps = self._buckets[key]
            self._prune_timestamps(timestamps, cutoff)
            if timestamps:
                break
            self._buckets.popitem(last=False)
            self._warned_buckets.discard(key)

    def allow_with_notice(
        self,
        client_ip: str,
        event_name: str,
        *,
        now: float | None = None,
    ) -> tuple[bool, bool]:
        """Return whether to write and whether this rejection needs one warning."""
        timestamp = time.monotonic() if now is None else float(now)
        key = (_normalized_ip(client_ip), safe_summary(event_name, 96))
        cutoff = timestamp - self.window_seconds
        with self._lock:
            self._prune_oldest_buckets(cutoff)
            timestamps = self._buckets.pop(key, None)
            if timestamps is None:
                if len(self._buckets) >= self.max_buckets:
                    evicted_key, _ = self._buckets.popitem(last=False)
                    self._warned_buckets.discard(evicted_key)
                timestamps = deque()
            else:
                self._prune_timestamps(timestamps, cutoff)
                if not timestamps:
                    self._warned_buckets.discard(key)

            if len(timestamps) >= self.limit:
                should_warn = key not in self._warned_buckets
                self._warned_buckets.add(key)
                self._buckets[key] = timestamps
                return False, should_warn

            timestamps.append(timestamp)
            self._buckets[key] = timestamps
            return True, False

    def allow(self, client_ip: str, event_name: str, *, now: float | None = None) -> bool:
        allowed, _ = self.allow_with_notice(client_ip, event_name, now=now)
        return allowed


def current_request_metadata() -> dict[str, str]:
    """Return safe request metadata for tool-level events."""
    return dict(_request_metadata.get())


def _header(scope: dict[str, Any], name: bytes) -> str:
    for key, value in scope.get("headers", ()):
        if key.lower() == name:
            return safe_summary(value.decode("latin-1", "replace"), 64)
    return ""


def request_id_from_scope(scope: dict[str, Any]) -> str:
    """Prefer a request ID, hashing the MCP session fallback before storage."""
    request_id = _header(scope, b"x-request-id")
    if request_id:
        return request_id
    for key, value in scope.get("headers", ()):
        if key.lower() == b"mcp-session-id":
            if not value:
                return ""
            digest = hashlib.sha256(bytes(value)).hexdigest()[:32]
            return f"sha256:{digest}"
    return ""


def _tenant_id() -> str:
    try:
        from .auth import current_ctx

        return current_ctx().tenant_id
    except Exception:
        return ""


class McpProtocolAuditMiddleware:
    """Pure ASGI middleware recording bounded MCP protocol metadata."""

    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        *,
        writer: Callable[[McpLogEvent], None] | None = None,
        max_body_bytes: int = 64 * 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.app = app
        self.writer = write_event if writer is None else writer
        self.max_body_bytes = max(0, int(max_body_bytes))
        self.clock = clock

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", "")).upper()
        event_name = "mcp_http_request"
        body_parts: list[bytes] = []
        body_size = 0
        body_complete = False
        body_capture_enabled = method == "POST"

        if method == "GET":
            event_name = "mcp_http_get"
        elif method == "DELETE":
            event_name = "mcp_http_delete"

        async def audit_receive():
            nonlocal body_capture_enabled, body_complete, body_size
            message = await receive()
            if not body_capture_enabled or body_complete:
                return message
            try:
                if message.get("type") != "http.request":
                    body_capture_enabled = False
                    body_parts.clear()
                    return message
                chunk = message.get("body", b"")
                if not isinstance(chunk, bytes):
                    raise TypeError("ASGI request body must be bytes")
                body_size += len(chunk)
                if body_size <= self.max_body_bytes:
                    body_parts.append(chunk)
                else:
                    body_capture_enabled = False
                    body_parts.clear()
                if not message.get("more_body", False):
                    body_complete = True
            except Exception:
                body_capture_enabled = False
                body_parts.clear()
            return message

        http_status = 500

        async def audit_send(message):
            nonlocal http_status
            if message.get("type") == "http.response.start":
                try:
                    http_status = int(message.get("status", 500))
                except (TypeError, ValueError):
                    http_status = 500
            await send(message)

        request_id = request_id_from_scope(scope)
        metadata = {
            "request_id": request_id,
            "client_ip": client_ip_from_scope(scope),
            "http_method": safe_summary(method, 16),
        }
        started_at = self.clock()
        token = _request_metadata.set(metadata)
        try:
            await self.app(scope, audit_receive, audit_send)
        finally:
            try:
                if body_capture_enabled and body_complete:
                    try:
                        payload = json.loads(b"".join(body_parts))
                        rpc_method = (
                            payload.get("method") if isinstance(payload, dict) else None
                        )
                        del payload
                        if isinstance(rpc_method, str) and rpc_method:
                            event_name = safe_summary(rpc_method, 96)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        pass
                body_parts.clear()

                cost_ms = max(0, int((self.clock() - started_at) * 1000))
                status = "ok" if http_status < 400 else (
                    "denied" if http_status in (401, 403) else "error"
                )
                self.writer(
                    McpLogEvent(
                        tenant_id=_tenant_id(),
                        category="protocol",
                        event_name=event_name,
                        result_status=status,
                        error_code=str(http_status) if http_status >= 400 else "",
                        cost_ms=cost_ms,
                        request_id=request_id,
                        client_ip=metadata["client_ip"],
                        http_method=metadata["http_method"],
                        http_status=http_status,
                    )
                )
            except Exception as exc:
                logger.warning(
                    "MCP protocol audit failed: %s",
                    type(exc).__name__,
                )
            finally:
                try:
                    _request_metadata.reset(token)
                finally:
                    body_parts.clear()
