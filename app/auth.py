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
from typing import Optional

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


_ctx: contextvars.ContextVar[Optional[TenantCtx]] = contextvars.ContextVar(
    "tenant_ctx", default=None
)


def current_ctx() -> TenantCtx:
    """在 MCP tool / DAO 内取当前租户上下文；未鉴权直接抛错"""
    ctx = _ctx.get()
    if not ctx:
        raise AuthenticationError("未识别到租户上下文（鉴权缺失）")
    return ctx


def require_tenant() -> str:
    """兼容旧调用：返回 tenant_id"""
    return current_ctx().tenant_id


def _record_auth(request: Request, event_name: str, tenant_id: str = "") -> None:
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

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in self.WHITE_LIST:
            return await call_next(request)

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            _record_auth(request, "auth_missing")
            return JSONResponse({"errcode": 401, "errmsg": "缺少 Bearer Token"}, status_code=401)

        token = auth[len("Bearer "):].strip()
        tctx = get_tenant_by_token(token)
        if not tctx:
            # 缓存可能未刷新，重试一次
            reload_tenants()
            tctx = get_tenant_by_token(token)
        if not tctx:
            _record_auth(request, "auth_invalid")
            return JSONResponse({"errcode": 401, "errmsg": "Token 无效或未绑定租户"}, status_code=401)

        ctx = TenantCtx(
            tenant_id=tctx.tenant_id,
            corpid=tctx.corpid,
            secret=tctx.secret,
            schema_name=tctx.schema_name,
            contact_secret=tctx.contact_secret,
            checkin_userids=tctx.checkin_userids,
            enabled_modules=tctx.enabled_modules,
            data_mode=tctx.data_mode,
        )
        token_ctx = _ctx.set(ctx)
        try:
            _record_auth(request, "auth_ok", ctx.tenant_id)
            return await call_next(request)
        finally:
            _ctx.reset(token_ctx)
