from __future__ import annotations

from collections import OrderedDict, deque
from datetime import datetime, timezone
import json
import logging
import threading
import time
from typing import Any, Literal
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app import admin
from app.mcp_audit import (
    client_ip_from_scope,
    request_id_from_scope,
    write_event,
)
from app.mcp_log_models import McpLogEvent
from app.tenant_auth.dependencies import require_same_origin, require_tenant_principal
from app.tenant_auth.models import TenantPrincipal

from .manager import ServiceManager
from .models import (
    McpService,
    ServiceTokenMetadata,
    ServiceToolBinding,
    parse_rfc3339_utc,
)
from . import store


logger = logging.getLogger(__name__)
tenant_router = APIRouter(prefix="/tenant", tags=["tenant-services"])
admin_router = APIRouter(prefix="/admin/tenants/{tenant_id}", tags=["admin-services"])
manager = ServiceManager()
_IDENTIFIER_PATTERN = r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$"
_NO_STORE_HEADERS = {"Cache-Control": "no-store"}


class _StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServiceCreate(_StrictBody):
    display_name: str = Field(min_length=1, max_length=128)
    service_key: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER_PATTERN)


class ServicePatch(_StrictBody):
    status: Literal["draft", "active", "disabled"]
    expected_config_version: int = Field(ge=1)


class BindingItem(_StrictBody):
    binding_id: str | None = Field(default=None, min_length=1, max_length=64)
    connection_id: str = Field(min_length=1, max_length=64)
    source_tool_key: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    tool_alias: str = Field(min_length=1, max_length=128, pattern=_IDENTIFIER_PATTERN)
    binding_status: Literal["active", "disabled"] = "active"
    policy: dict[str, Any] = Field(default_factory=dict)


class BindingReplace(_StrictBody):
    items: list[BindingItem]
    expected_config_version: int = Field(ge=1)


class TokenIssue(_StrictBody):
    label: str = Field(default="", max_length=128)
    expires_at: datetime | None = None

    @field_validator("expires_at", mode="before")
    @classmethod
    def validate_expires_at(cls, value: object) -> datetime | None:
        if value is None:
            return None
        return parse_rfc3339_utc(value)


class RevealLimiter:
    def __init__(
        self,
        *,
        limit: int = 10,
        window_seconds: float = 60.0,
        max_buckets: int = 4096,
    ) -> None:
        self.limit = max(1, int(limit))
        self.window_seconds = max(0.001, float(window_seconds))
        self.max_buckets = max(1, int(max_buckets))
        self._buckets: OrderedDict[tuple[str, str, str], deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def allow(self, key: tuple[str, str, str], *, now: float | None = None) -> bool:
        timestamp = time.monotonic() if now is None else float(now)
        cutoff = timestamp - self.window_seconds
        with self._lock:
            for stale_key in list(self._buckets):
                timestamps = self._buckets[stale_key]
                while timestamps and timestamps[0] <= cutoff:
                    timestamps.popleft()
                if timestamps:
                    break
                del self._buckets[stale_key]
            timestamps = self._buckets.pop(key, deque())
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()
            if len(timestamps) >= self.limit:
                self._buckets[key] = timestamps
                return False
            if len(self._buckets) >= self.max_buckets:
                self._buckets.popitem(last=False)
            timestamps.append(timestamp)
            self._buckets[key] = timestamps
            return True


_reveal_limiter = RevealLimiter()


def reset_reveal_limiter() -> None:
    global _reveal_limiter
    _reveal_limiter = RevealLimiter()


def _service(item: McpService) -> dict[str, Any]:
    return {
        "service_id": item.service_id,
        "tenant_id": item.tenant_id,
        "display_name": item.display_name,
        "service_key": item.service_key,
        "status": item.status,
        "config_version": item.config_version,
    }


def _binding(item: ServiceToolBinding) -> dict[str, Any]:
    return {
        "binding_id": item.binding_id,
        "service_id": item.service_id,
        "connection_id": item.connection_id,
        "source_tool_key": item.source_tool_key,
        "tool_alias": item.tool_alias,
        "binding_status": item.binding_status,
        "policy": dict(item.policy),
    }


def _token(item: ServiceTokenMetadata | Any) -> dict[str, Any]:
    def timestamp(value: datetime | None) -> str | None:
        if not isinstance(value, datetime):
            return value
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        else:
            value = value.astimezone(timezone.utc)
        return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    return {
        "token_id": item.token_id,
        "prefix": item.prefix,
        "label": item.label,
        "expires_at": timestamp(item.expires_at),
        "revoked_at": timestamp(item.revoked_at),
        "last_used_at": timestamp(item.last_used_at),
        "created_at": timestamp(item.created_at),
    }


def _domain_error(exc: Exception) -> HTTPException:
    if isinstance(exc, (store.ServiceOwnershipError, store.TokenUnavailableError)):
        return HTTPException(404, "resource not found")
    if isinstance(exc, store.ServiceVersionConflictError):
        return HTTPException(409, "service configuration changed")
    if isinstance(exc, (ValueError, TypeError)):
        return HTTPException(409, "service operation rejected")
    return HTTPException(500, "service operation failed")


def _call(operation, *args, **kwargs):
    try:
        return operation(*args, **kwargs)
    except (
        store.ServiceOwnershipError,
        store.TokenUnavailableError,
        store.ServiceVersionConflictError,
        ValueError,
        TypeError,
    ) as exc:
        raise _domain_error(exc) from None


def _same_origin(request: Request) -> None:
    require_same_origin(request)


def _admin_auth(request: Request) -> None:
    admin._require_auth(request)


def _binding_models(service_id: str, body: BindingReplace) -> list[ServiceToolBinding]:
    return [
        ServiceToolBinding(
            binding_id=item.binding_id or str(uuid.uuid4()),
            service_id=service_id,
            connection_id=item.connection_id,
            source_tool_key=item.source_tool_key,
            tool_alias=item.tool_alias,
            binding_status=item.binding_status,
            policy=item.policy,
        )
        for item in body.items
    ]


def _audit_reveal(
    request: Request,
    *,
    principal_type: Literal["tenant", "admin"],
    tenant_id: str,
    service_id: str,
    token_id: str,
    result: Literal["ok", "denied", "error"],
) -> bool:
    params = {"principal_type": principal_type}
    if result == "ok":
        params.update({"service_id": service_id, "token_id": token_id})

    try:
        accepted = write_event(
            McpLogEvent(
                tenant_id=tenant_id,
                category="auth",
                event_name="service_token_reveal",
                params_summary=json.dumps(
                    params,
                    ensure_ascii=True,
                    separators=(",", ":"),
                ),
                result_status=result,
                request_id=request_id_from_scope(request.scope),
                client_ip=client_ip_from_scope(request.scope),
            )
        )
    except Exception as exc:
        logger.warning("Service token reveal audit failed type=%s", type(exc).__name__)
        return False
    return accepted is True


def _no_store_exception(exc: HTTPException) -> HTTPException:
    exc.headers = {**(exc.headers or {}), **_NO_STORE_HEADERS}
    return exc


def _tenant_reveal_guard(
    request: Request,
    service_id: str,
    token_id: str,
) -> TenantPrincipal:
    principal: TenantPrincipal | None = None
    try:
        principal = require_tenant_principal(request)
        require_same_origin(request)
        return principal
    except HTTPException as exc:
        _audit_reveal(
            request,
            principal_type="tenant",
            tenant_id=principal.tenant_id if principal is not None else "",
            service_id=service_id,
            token_id=token_id,
            result="denied",
        )
        raise _no_store_exception(exc) from None
    except Exception as exc:
        logger.warning("Tenant service reveal guard failed type=%s", type(exc).__name__)
        _audit_reveal(
            request,
            principal_type="tenant",
            tenant_id=principal.tenant_id if principal is not None else "",
            service_id=service_id,
            token_id=token_id,
            result="error",
        )
        raise HTTPException(
            500, "service operation failed", headers=_NO_STORE_HEADERS
        ) from None


def _admin_reveal_guard(
    request: Request,
    tenant_id: str,
    service_id: str,
    token_id: str,
) -> None:
    try:
        admin._require_auth(request)
        require_same_origin(request)
    except HTTPException as exc:
        _audit_reveal(
            request,
            principal_type="admin",
            tenant_id=tenant_id,
            service_id=service_id,
            token_id=token_id,
            result="denied",
        )
        raise _no_store_exception(exc) from None
    except Exception as exc:
        logger.warning("Admin service reveal guard failed type=%s", type(exc).__name__)
        _audit_reveal(
            request,
            principal_type="admin",
            tenant_id=tenant_id,
            service_id=service_id,
            token_id=token_id,
            result="error",
        )
        raise HTTPException(
            500, "service operation failed", headers=_NO_STORE_HEADERS
        ) from None


def _reveal(
    request: Request,
    response: Response,
    *,
    principal_type: Literal["tenant", "admin"],
    principal_key: str,
    tenant_id: str,
    service_id: str,
    token_id: str,
) -> dict[str, str]:
    response.headers["Cache-Control"] = "no-store"
    if not _reveal_limiter.allow((principal_type, principal_key, token_id)):
        _audit_reveal(
            request,
            principal_type=principal_type,
            tenant_id=tenant_id,
            service_id=service_id,
            token_id=token_id,
            result="denied",
        )
        raise HTTPException(
            429,
            "request rate limit exceeded",
            headers=_NO_STORE_HEADERS,
        )
    try:
        raw_value = manager.reveal_token(tenant_id, service_id, token_id)
    except (
        store.ServiceOwnershipError,
        store.TokenUnavailableError,
        ValueError,
        TypeError,
    ) as exc:
        _audit_reveal(
            request,
            principal_type=principal_type,
            tenant_id=tenant_id,
            service_id=service_id,
            token_id=token_id,
            result="denied",
        )
        error = _domain_error(exc)
        error.headers = _NO_STORE_HEADERS
        raise error from None
    except Exception as exc:
        logger.warning("Service token reveal failed type=%s", type(exc).__name__)
        _audit_reveal(
            request,
            principal_type=principal_type,
            tenant_id=tenant_id,
            service_id=service_id,
            token_id=token_id,
            result="error",
        )
        raise HTTPException(
            500,
            "service operation failed",
            headers=_NO_STORE_HEADERS,
        ) from None
    accepted = _audit_reveal(
        request,
        principal_type=principal_type,
        tenant_id=tenant_id,
        service_id=service_id,
        token_id=token_id,
        result="ok",
    )
    if not accepted:
        raw_value = ""
        logger.warning("Service token reveal success audit was not accepted")
        raise HTTPException(
            500,
            "service operation failed",
            headers=_NO_STORE_HEADERS,
        )
    return {"token": raw_value}


@tenant_router.get("/services")
def list_tenant_services(
    principal: TenantPrincipal = Depends(require_tenant_principal),
):
    return {"items": [_service(item) for item in manager.list_services(principal.tenant_id)]}


@tenant_router.post("/services", status_code=201)
def create_tenant_service(
    body: ServiceCreate,
    request: Request,
    principal: TenantPrincipal = Depends(require_tenant_principal),
):
    _same_origin(request)
    item = _call(
        manager.create_service,
        principal.tenant_id,
        body.display_name,
        body.service_key,
    )
    return {"service": _service(item)}


@tenant_router.get("/services/{service_id}")
def get_tenant_service(
    service_id: str,
    principal: TenantPrincipal = Depends(require_tenant_principal),
):
    return {"service": _service(_call(manager.get_service, principal.tenant_id, service_id))}


@tenant_router.patch("/services/{service_id}")
def patch_tenant_service(
    service_id: str,
    body: ServicePatch,
    request: Request,
    principal: TenantPrincipal = Depends(require_tenant_principal),
):
    _same_origin(request)
    item = _call(
        manager.update_status,
        principal.tenant_id,
        service_id,
        body.status,
        body.expected_config_version,
    )
    return {"service": _service(item)}


@tenant_router.get("/services/{service_id}/tools")
def list_tenant_bindings(
    service_id: str,
    principal: TenantPrincipal = Depends(require_tenant_principal),
):
    return {
        "items": [
            _binding(item)
            for item in _call(manager.list_bindings, principal.tenant_id, service_id)
        ]
    }


@tenant_router.put("/services/{service_id}/tools")
def replace_tenant_bindings(
    service_id: str,
    body: BindingReplace,
    request: Request,
    principal: TenantPrincipal = Depends(require_tenant_principal),
):
    _same_origin(request)
    item = _call(
        manager.replace_bindings,
        principal.tenant_id,
        service_id,
        _binding_models(service_id, body),
        body.expected_config_version,
    )
    return {"service": _service(item)}


@tenant_router.get("/services/{service_id}/tokens")
def list_tenant_tokens(
    service_id: str,
    principal: TenantPrincipal = Depends(require_tenant_principal),
):
    return {
        "items": [
            _token(item)
            for item in _call(manager.list_tokens, principal.tenant_id, service_id)
        ]
    }


@tenant_router.post("/services/{service_id}/tokens", status_code=201)
def issue_tenant_token(
    service_id: str,
    body: TokenIssue,
    request: Request,
    response: Response,
    principal: TenantPrincipal = Depends(require_tenant_principal),
):
    _same_origin(request)
    issued = _call(
        manager.issue_token,
        principal.tenant_id,
        service_id,
        body.label,
        body.expires_at,
    )
    response.headers["Cache-Control"] = "no-store"
    return {"token_id": issued.token_id, "token": issued.raw_value, "prefix": issued.prefix}


@tenant_router.post("/services/{service_id}/tokens/{token_id}/reveal")
def reveal_tenant_token(
    service_id: str,
    token_id: str,
    request: Request,
    response: Response,
    principal: TenantPrincipal = Depends(_tenant_reveal_guard),
):
    return _reveal(
        request,
        response,
        principal_type="tenant",
        principal_key=principal.tenant_id,
        tenant_id=principal.tenant_id,
        service_id=service_id,
        token_id=token_id,
    )


@tenant_router.delete("/services/{service_id}/tokens/{token_id}")
def revoke_tenant_token(
    service_id: str,
    token_id: str,
    request: Request,
    principal: TenantPrincipal = Depends(require_tenant_principal),
):
    _same_origin(request)
    if not _call(manager.revoke_token, principal.tenant_id, service_id, token_id):
        raise HTTPException(404, "resource not found")
    return {"ok": True}


@admin_router.get("/services")
def list_admin_services(tenant_id: str, request: Request):
    _admin_auth(request)
    return {"items": [_service(item) for item in manager.list_services(tenant_id)]}


@admin_router.post("/services", status_code=201)
def create_admin_service(tenant_id: str, body: ServiceCreate, request: Request):
    _admin_auth(request)
    _same_origin(request)
    item = _call(manager.create_service, tenant_id, body.display_name, body.service_key)
    return {"service": _service(item)}


@admin_router.get("/services/{service_id}")
def get_admin_service(tenant_id: str, service_id: str, request: Request):
    _admin_auth(request)
    return {"service": _service(_call(manager.get_service, tenant_id, service_id))}


@admin_router.patch("/services/{service_id}")
def patch_admin_service(
    tenant_id: str, service_id: str, body: ServicePatch, request: Request
):
    _admin_auth(request)
    _same_origin(request)
    item = _call(
        manager.update_status,
        tenant_id,
        service_id,
        body.status,
        body.expected_config_version,
    )
    return {"service": _service(item)}


@admin_router.get("/services/{service_id}/tools")
def list_admin_bindings(tenant_id: str, service_id: str, request: Request):
    _admin_auth(request)
    return {
        "items": [
            _binding(item)
            for item in _call(manager.list_bindings, tenant_id, service_id)
        ]
    }


@admin_router.put("/services/{service_id}/tools")
def replace_admin_bindings(
    tenant_id: str,
    service_id: str,
    body: BindingReplace,
    request: Request,
):
    _admin_auth(request)
    _same_origin(request)
    item = _call(
        manager.replace_bindings,
        tenant_id,
        service_id,
        _binding_models(service_id, body),
        body.expected_config_version,
    )
    return {"service": _service(item)}


@admin_router.get("/services/{service_id}/tokens")
def list_admin_tokens(tenant_id: str, service_id: str, request: Request):
    _admin_auth(request)
    return {
        "items": [
            _token(item)
            for item in _call(manager.list_tokens, tenant_id, service_id)
        ]
    }


@admin_router.post("/services/{service_id}/tokens", status_code=201)
def issue_admin_token(
    tenant_id: str,
    service_id: str,
    body: TokenIssue,
    request: Request,
    response: Response,
):
    _admin_auth(request)
    _same_origin(request)
    issued = _call(
        manager.issue_token, tenant_id, service_id, body.label, body.expires_at
    )
    response.headers["Cache-Control"] = "no-store"
    return {"token_id": issued.token_id, "token": issued.raw_value, "prefix": issued.prefix}


@admin_router.post("/services/{service_id}/tokens/{token_id}/reveal")
def reveal_admin_token(
    tenant_id: str,
    service_id: str,
    token_id: str,
    request: Request,
    response: Response,
    _guard: None = Depends(_admin_reveal_guard),
):
    del _guard
    return _reveal(
        request,
        response,
        principal_type="admin",
        principal_key="admin",
        tenant_id=tenant_id,
        service_id=service_id,
        token_id=token_id,
    )


@admin_router.delete("/services/{service_id}/tokens/{token_id}")
def revoke_admin_token(
    tenant_id: str, service_id: str, token_id: str, request: Request
):
    _admin_auth(request)
    _same_origin(request)
    if not _call(manager.revoke_token, tenant_id, service_id, token_id):
        raise HTTPException(404, "resource not found")
    return {"ok": True}
