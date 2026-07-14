"""Safe, centralized audit instrumentation for MCP traffic."""
from __future__ import annotations

import contextvars
import ipaddress
import json
import logging
import re
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

from .mcp_log_models import McpLogEvent
from .mcp_log_store import insert_event

logger = logging.getLogger(__name__)

_DB_CREDENTIAL_RE = re.compile(
    r"(?i)(\b(?:mysql(?:\+pymysql)?|postgres(?:ql)?|mariadb|mongodb|redis)://"
    r"[^\s:/@]+:)([^\s/@]+)(@)"
)
_AUTHORIZATION_RE = re.compile(
    r"(?i)(\bauthorization\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_SENSITIVE_KEY_RE = re.compile(
    r"(?ix)"
    r"([\"']?\b(?:"
    r"mcp[\s_-]*token|access[\s_-]*token|refresh[\s_-]*token|token|"
    r"client[\s_-]*secret|contact[\s_-]*secret|secret|"
    r"set[\s_-]*cookie|cookies?|password|passwd|pwd|database[\s_-]*url"
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

    text = _DB_CREDENTIAL_RE.sub(r"\1[REDACTED]\3", text)
    text = _AUTHORIZATION_RE.sub(r"\1[REDACTED]", text)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    text = _SENSITIVE_KEY_RE.sub(r"\1\"[REDACTED]\"", text)
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

    def __init__(self, limit: int = 60, window_seconds: float = 60.0) -> None:
        self.limit = max(1, int(limit))
        self.window_seconds = max(0.001, float(window_seconds))
        self._buckets: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, client_ip: str, event_name: str, *, now: float | None = None) -> bool:
        timestamp = time.monotonic() if now is None else float(now)
        key = (_normalized_ip(client_ip), safe_summary(event_name, 96))
        cutoff = timestamp - self.window_seconds
        with self._lock:
            for bucket_key, timestamps in tuple(self._buckets.items()):
                while timestamps and timestamps[0] <= cutoff:
                    timestamps.popleft()
                if not timestamps:
                    del self._buckets[bucket_key]
            timestamps = self._buckets.setdefault(key, deque())
            if len(timestamps) >= self.limit:
                return False
            timestamps.append(timestamp)
            return True


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
        self.writer = writer or write_event
        self.max_body_bytes = max(0, int(max_body_bytes))
        self.clock = clock

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method", "")).upper()
        event_name = "mcp_http_request"
        buffered_messages: list[dict[str, Any]] = []
        body_parts: list[bytes] = []
        body_size = 0
        body_oversized = False

        if method == "POST":
            while True:
                message = await receive()
                buffered_messages.append(message)
                if message.get("type") != "http.request":
                    break
                chunk = message.get("body", b"")
                body_size += len(chunk)
                if body_size <= self.max_body_bytes:
                    body_parts.append(chunk)
                else:
                    body_oversized = True
                    body_parts.clear()
                if not message.get("more_body", False):
                    break
            if not body_oversized:
                try:
                    payload = json.loads(b"".join(body_parts))
                    rpc_method = payload.get("method") if isinstance(payload, dict) else None
                    if isinstance(rpc_method, str) and rpc_method:
                        event_name = safe_summary(rpc_method, 96)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    pass
        elif method == "GET":
            event_name = "mcp_http_get"
        elif method == "DELETE":
            event_name = "mcp_http_delete"

        replay_index = 0

        async def replay_receive():
            nonlocal replay_index
            if replay_index < len(buffered_messages):
                message = buffered_messages[replay_index]
                replay_index += 1
                return message
            return await receive()

        http_status = 500

        async def audit_send(message):
            nonlocal http_status
            if message.get("type") == "http.response.start":
                http_status = int(message.get("status", 500))
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
            await self.app(scope, replay_receive, audit_send)
        finally:
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
            _request_metadata.reset(token)
            body_parts.clear()
            buffered_messages.clear()
