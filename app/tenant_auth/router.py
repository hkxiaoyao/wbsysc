from __future__ import annotations

from collections import OrderedDict, deque
import threading
import time
from typing import Deque
import re

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, SecretStr, field_validator

from app.config import get_settings
from app.mcp_audit import client_ip_from_scope

from . import store
from .dependencies import (
    TENANT_SESSION_COOKIE,
    require_same_origin,
    require_tenant_principal,
)
from .models import TenantPrincipal


router = APIRouter(prefix="/tenant", tags=["tenant"])
_TENANT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SESSION_TTL_MINUTES = 480


class TenantLoginLimiter:
    """Best-effort per-process limiter; persistent account lockout is authoritative."""

    def __init__(self, *, pair_limit: int = 5, ip_limit: int = 30,
                 window_seconds: float = 900.0, max_buckets: int = 4096) -> None:
        self.pair_limit = pair_limit
        self.ip_limit = ip_limit
        self.window_seconds = window_seconds
        self.max_buckets = max_buckets
        self._pairs: OrderedDict[tuple[str, str], Deque[float]] = OrderedDict()
        self._ips: OrderedDict[str, Deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def _bucket(self, buckets, key, now):
        values = buckets.pop(key, deque())
        cutoff = now - self.window_seconds
        while values and values[0] <= cutoff:
            values.popleft()
        buckets[key] = values
        while len(buckets) > self.max_buckets:
            buckets.popitem(last=False)
        return values

    def limited(self, tenant_id: str, client_ip: str, *, now: float | None = None) -> bool:
        timestamp = time.monotonic() if now is None else now
        pair_key = (tenant_id.strip().lower(), client_ip)
        with self._lock:
            pair = self._bucket(self._pairs, pair_key, timestamp)
            if not client_ip:
                return len(pair) >= self.pair_limit
            ip = self._bucket(self._ips, client_ip, timestamp)
            return len(pair) >= self.pair_limit or len(ip) >= self.ip_limit

    def record_failure(self, tenant_id: str, client_ip: str,
                       *, now: float | None = None) -> None:
        timestamp = time.monotonic() if now is None else now
        pair_key = (tenant_id.strip().lower(), client_ip)
        with self._lock:
            self._bucket(self._pairs, pair_key, timestamp).append(timestamp)
            if client_ip:
                self._bucket(self._ips, client_ip, timestamp).append(timestamp)


_login_limiter = TenantLoginLimiter()


def reset_login_limiter() -> None:
    global _login_limiter
    _login_limiter = TenantLoginLimiter()


class TenantLoginRequest(BaseModel):
    tenant_id: str
    password: SecretStr

    @field_validator("tenant_id")
    @classmethod
    def validate_tenant_id(cls, value: str) -> str:
        if not _TENANT_ID_PATTERN.fullmatch(value):
            raise ValueError("invalid tenant ID")
        return value


class TenantPasswordChangeRequest(BaseModel):
    current_password: SecretStr
    new_password: SecretStr


@router.post("/login")
def login(body: TenantLoginRequest, request: Request, response: Response):
    require_same_origin(request)
    client_ip = client_ip_from_scope(request.scope)
    if _login_limiter.limited(body.tenant_id, client_ip):
        raise HTTPException(429, "请求过于频繁")
    account = store.authenticate(body.tenant_id, body.password.get_secret_value())
    if account is None:
        _login_limiter.record_failure(body.tenant_id, client_ip)
        raise HTTPException(401, "认证失败")
    issued = store.issue_session(
        account.tenant_id,
        ttl_seconds=_SESSION_TTL_MINUTES * 60,
    )
    settings = get_settings()
    response.set_cookie(
        TENANT_SESSION_COOKIE,
        issued.raw_value,
        httponly=True,
        secure=settings.app_env.lower() == "prod",
        samesite="lax",
        max_age=_SESSION_TTL_MINUTES * 60,
        path="/tenant",
    )
    return {"ok": True, "tenant_id": account.tenant_id}


@router.get("/session")
def session(principal: TenantPrincipal = Depends(require_tenant_principal)):
    return {"authed": True, "tenant_id": principal.tenant_id}


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    principal: TenantPrincipal = Depends(require_tenant_principal),
):
    del principal
    require_same_origin(request)
    raw_value = request.cookies.get(TENANT_SESSION_COOKIE, "")
    if raw_value:
        store.revoke_session(raw_value)
    response.delete_cookie(TENANT_SESSION_COOKIE, path="/tenant")
    return {"ok": True}


@router.post("/password/change")
def change_password(
    body: TenantPasswordChangeRequest,
    request: Request,
    response: Response,
    principal: TenantPrincipal = Depends(require_tenant_principal),
):
    require_same_origin(request)
    current = body.current_password.get_secret_value()
    if store.authenticate(principal.tenant_id, current) is None:
        raise HTTPException(401, "认证失败")
    try:
        store.upsert_account(principal.tenant_id, body.new_password.get_secret_value())
    except ValueError:
        raise HTTPException(422, "新密码不符合要求") from None
    response.delete_cookie(TENANT_SESSION_COOKIE, path="/tenant")
    return {"ok": True}
