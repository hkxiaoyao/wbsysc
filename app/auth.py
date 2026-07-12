"""
Bearer Token 鉴权 + 租户上下文（多租户版）
- token → 查 tenant_config（DB） → 绑定 tenant_id / corpid / schema_name
- 不信任客户端传 tenant_id（服务端 DB 解析）
- contextvars 透传 [tenant_id, corpid, schema_name] 到 MCP tool
"""
from __future__ import annotations

import contextvars
from dataclasses import dataclass
from typing import Optional

from starlette.authentication import AuthenticationError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .tenant import get_tenant_by_token, reload_tenants


@dataclass
class TenantCtx:
    tenant_id: str
    corpid: str
    schema_name: str


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


class BearerTokenMiddleware(BaseHTTPMiddleware):
    WHITE_LIST = {"/health", "/healthz", "/"}

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in self.WHITE_LIST:
            return await call_next(request)

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse({"errcode": 401, "errmsg": "缺少 Bearer Token"}, status_code=401)

        token = auth[len("Bearer "):].strip()
        tctx = get_tenant_by_token(token)
        if not tctx:
            # 缓存可能未刷新，重试一次
            reload_tenants()
            tctx = get_tenant_by_token(token)
        if not tctx:
            return JSONResponse({"errcode": 401, "errmsg": "Token 无效或未绑定租户"}, status_code=401)

        ctx = TenantCtx(tenant_id=tctx.tenant_id, corpid=tctx.corpid, schema_name=tctx.schema_name)
        token_ctx = _ctx.set(ctx)
        try:
            return await call_next(request)
        finally:
            _ctx.reset(token_ctx)