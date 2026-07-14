"""Safe, centralized audit instrumentation for MCP traffic."""
from __future__ import annotations

import contextvars
import ipaddress
import json
import logging
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


def write_event(event: McpLogEvent) -> None:
    """Persist an event without allowing audit storage to affect requests."""
    try:
        insert_event(event)
    except Exception as exc:
        logger.warning("MCP audit storage failed: %s", type(exc).__name__)


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

        request_id = _header(scope, b"x-request-id") or _header(
            scope, b"mcp-session-id"
        )
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
