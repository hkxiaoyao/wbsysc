from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from .models import TenantPrincipal
from . import store


TENANT_SESSION_COOKIE = "wbg_tenant_session"


def require_tenant_principal(request: Request) -> TenantPrincipal:
    raw_value = request.cookies.get(TENANT_SESSION_COOKIE, "")
    principal = store.resolve_session(raw_value) if raw_value else None
    if principal is None:
        raise HTTPException(401, "未登录或会话过期")
    return principal


def require_same_origin(request: Request) -> None:
    supplied = request.headers.get("origin") or request.headers.get("referer")
    if not supplied:
        raise HTTPException(403, "请求来源无效")
    try:
        actual = urlsplit(supplied)
        expected = urlsplit(str(request.base_url))
    except ValueError:
        raise HTTPException(403, "请求来源无效") from None
    if (
        actual.scheme not in {"http", "https"}
        or actual.scheme.lower() != expected.scheme.lower()
        or actual.netloc.lower() != expected.netloc.lower()
    ):
        raise HTTPException(403, "请求来源无效")
