"""
Bearer Token 鉴权 + 租户上下文（多租户版）
- token → 查 tenant_config（DB） → 绑定 tenant_id / corpid / schema_name
- 不信任客户端传 tenant_id（服务端 DB 解析）
- contextvars 透传 [tenant_id, corpid, schema_name] 到 MCP tool
"""
from __future__ import annotations

import contextvars
import logging
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Mapping, Optional, Protocol

from starlette.authentication import AuthenticationError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .mcp_audit import (
    AuthWriteLimiter,
    client_ip_from_scope,
    request_id_from_scope,
    safe_summary,
    write_event,
)
from .mcp_log_models import McpLogEvent
from .tenant import get_tenant_by_token, reload_tenants

logger = logging.getLogger(__name__)
_auth_write_limiter = AuthWriteLimiter()


@dataclass(frozen=True)
class ConnectionCtx:
    """Safe request scope resolved from a connection-bound bearer token.

    Credential values intentionally do not belong here.  Connector execution
    loads them only after authentication through the connection resolver.
    """

    tenant_id: str
    connection_id: str
    connector_key: str
    data_mode: Literal["direct", "stored", "hybrid"]
    public_config: Mapping[str, Any]
    config_version: int = 0

    def __post_init__(self) -> None:
        for value in (self.tenant_id, self.connection_id, self.connector_key):
            if not isinstance(value, str) or not value:
                raise ValueError("connection context identifiers are required")
        if self.data_mode not in {"direct", "stored", "hybrid"}:
            raise ValueError("invalid connection data mode")
        if not isinstance(self.public_config, Mapping):
            raise TypeError("public_config must be a mapping")
        if isinstance(self.config_version, bool) or not isinstance(self.config_version, int):
            raise TypeError("config_version must be an integer")
        object.__setattr__(self, "public_config", MappingProxyType(dict(self.public_config)))


@dataclass
class TenantCtx:
    tenant_id: str
    corpid: str
    secret: str
    schema_name: str
    contact_secret: str
    checkin_userids: list[str]
    enabled_modules: set[str]
    data_mode: str


RequestCtx = ConnectionCtx | TenantCtx


class ConnectionContextResolver(Protocol):
    def resolve(self, connection_id: str, bearer_token: str) -> ConnectionCtx | None: ...

    def resolve_legacy(self, bearer_token: str) -> ConnectionCtx | None: ...


_ctx: contextvars.ContextVar[Optional[RequestCtx]] = contextvars.ContextVar(
    "tenant_ctx", default=None
)


def current_ctx() -> RequestCtx:
    """Return the authenticated connection scope (or legacy compatibility scope)."""
    ctx = _ctx.get()
    if not ctx:
        raise AuthenticationError("未识别到租户上下文（鉴权缺失）")
    return ctx


def require_tenant() -> str:
    """兼容旧调用：返回 tenant_id"""
    return current_ctx().tenant_id


def _record_auth(
    request: Request,
    event_name: str,
    tenant_id: str = "",
    connection_id: str = "",
) -> None:
    try:
        client_ip = client_ip_from_scope(request.scope)
        allowed, should_warn = _auth_write_limiter.allow_with_notice(
            client_ip,
            event_name,
        )
        if not allowed:
            if should_warn:
                logger.warning("MCP auth audit rate limit reached event=%s", event_name)
            return
        request_id = request_id_from_scope(request.scope)
        write_event(
            McpLogEvent(
                tenant_id=tenant_id,
                category="auth",
                event_name=event_name,
                # A connection ID is written only after it was resolved from
                # server-side state; never copy a failed path parameter here.
                target=safe_summary(connection_id, 256) if connection_id else "",
                result_status="ok" if event_name == "auth_ok" else "denied",
                error_code="" if event_name == "auth_ok" else "401",
                request_id=request_id,
                client_ip=client_ip,
                http_method=safe_summary(request.method, 16),
                http_status=0 if event_name == "auth_ok" else 401,
            )
        )
    except Exception as exc:
        try:
            logger.warning("MCP auth audit failed type=%s", type(exc).__name__)
        except Exception:
            pass


class BearerTokenMiddleware(BaseHTTPMiddleware):
    WHITE_LIST = {"/health", "/healthz", "/"}

    def __init__(
        self,
        app,
        *,
        resolver: ConnectionContextResolver | None = None,
        legacy: bool = False,
    ) -> None:
        super().__init__(app)
        self._resolver = resolver
        self._legacy = legacy

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in self.WHITE_LIST:
            return await call_next(request)

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            _record_auth(request, "auth_missing")
            return JSONResponse({"errcode": 401, "errmsg": "缺少 Bearer Token"}, status_code=401)

        token = auth[len("Bearer "):].strip()
        if self._resolver is None:
            ctx = self._resolve_legacy_tenant(token)
        else:
            ctx = self._resolve_connection(request, token)
        if not ctx:
            _record_auth(request, "auth_invalid")
            message = (
                "Token 无效或未绑定租户"
                if self._resolver is None
                else "Token 无效或未绑定连接"
            )
            return JSONResponse({"errcode": 401, "errmsg": message}, status_code=401)
        token_ctx = _ctx.set(ctx)
        try:
            _record_auth(
                request,
                "auth_ok",
                ctx.tenant_id,
                ctx.connection_id if isinstance(ctx, ConnectionCtx) else "",
            )
            return await call_next(request)
        finally:
            _ctx.reset(token_ctx)

    @staticmethod
    def _resolve_legacy_tenant(token: str) -> TenantCtx | None:
        tctx = get_tenant_by_token(token)
        if not tctx:
            # 缓存可能未刷新，重试一次
            reload_tenants()
            tctx = get_tenant_by_token(token)
        if not tctx:
            return None
        return TenantCtx(
            tenant_id=tctx.tenant_id,
            corpid=tctx.corpid,
            secret=tctx.secret,
            schema_name=tctx.schema_name,
            contact_secret=tctx.contact_secret,
            checkin_userids=tctx.checkin_userids,
            enabled_modules=tctx.enabled_modules,
            data_mode=tctx.data_mode,
        )

    def _resolve_connection(self, request: Request, token: str) -> ConnectionCtx | None:
        try:
            if self._legacy:
                return self._resolver.resolve_legacy(token)
            connection_id = request.path_params.get("connection_id")
            if not isinstance(connection_id, str) or not connection_id:
                return None
            return self._resolver.resolve(connection_id, token)
        except Exception as exc:
            logger.warning("MCP connection auth failed type=%s", type(exc).__name__)
            return None
