"""Authenticated administration API for central MCP call logs."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import math
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from .admin import SESSION_COOKIE, _require_auth
from .config import get_settings
from .mcp_log_models import DeleteSpec, LogFilters
from .mcp_log_store import (
    delete_matching,
    get_log_stats,
    get_retention_days,
    list_logs,
    preview_delete,
    set_retention_days,
)


router = APIRouter(prefix="/admin", tags=["mcp-logs"])
logger = logging.getLogger("wecom-gateway")

SAFE_LOG_FIELDS = frozenset(
    (
        "id",
        "tenant_id",
        "service_id",
        "tool_alias",
        "connection_id",
        "connector_key",
        "tool_key",
        "category",
        "event_name",
        "target",
        "params_summary",
        "result_status",
        "error_code",
        "error_summary",
        "cost_ms",
        "request_id",
        "client_ip",
        "http_method",
        "http_status",
        "created_at",
    )
)

_TOKEN_VERSION = 1
_TOKEN_TTL_SECONDS = 5 * 60
_DEV_FALLBACK_KEY = secrets.token_bytes(32)
_dev_warning_emitted = False

Category = Literal["tool", "protocol", "auth"]
Status = Literal["ok", "partial", "error", "denied"]
DeleteMode = Literal["ids", "filter", "before_date", "all"]
MAX_DELETE_IDS = 200
MAX_CONFIRM_TOKEN_LENGTH = 8192
MAX_LOG_ID = 2**63 - 1
MAX_SAFE_INTEGER = 2**53 - 1


def _normalize_delete_id(value: Any) -> int:
    if type(value) is int:
        if not 1 <= value <= MAX_SAFE_INTEGER:
            raise ValueError("integer log IDs must be JavaScript-safe positive integers")
        return value
    if type(value) is not str or not value or not value.isascii() or not value.isdecimal():
        raise ValueError("log IDs must be canonical decimal strings or safe integers")
    if value[0] == "0":
        raise ValueError("decimal log IDs must not contain leading zeroes")
    max_text = str(MAX_LOG_ID)
    if len(value) > len(max_text) or (len(value) == len(max_text) and value > max_text):
        raise ValueError("log ID exceeds signed BIGINT range")
    return int(value)


DeleteId = Annotated[
    int,
    BeforeValidator(_normalize_delete_id, json_schema_input_type=int | str),
]
StrictNonNegativeInt = Annotated[int, Field(strict=True, ge=0)]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _now() -> float:
    return time.time()


def _normalize_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None and value.utcoffset() is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _complete_window(
    from_time: datetime | None, to_time: datetime | None
) -> tuple[datetime, datetime]:
    normalized_from = _normalize_datetime(from_time)
    normalized_to = _normalize_datetime(to_time)
    if normalized_from is None and normalized_to is None:
        normalized_to = _utcnow()
        normalized_from = normalized_to - timedelta(hours=24)
    elif normalized_from is None:
        normalized_from = normalized_to - timedelta(hours=24)
    elif normalized_to is None:
        normalized_to = normalized_from + timedelta(hours=24)
    return normalized_from, normalized_to


def _new_filters(
    *,
    tenant_id: str | None = None,
    service_id: str | None = None,
    tool_alias: str | None = None,
    connection_id: str | None = None,
    connector_key: str | None = None,
    tool_key: str | None = None,
    category: Category | None = None,
    event_name: str | None = None,
    status: Status | None = None,
    from_time: datetime | None = None,
    to_time: datetime | None = None,
    q: str | None = None,
    request_id: str | None = None,
    client_ip: str | None = None,
    cost_min: int | None = None,
    cost_max: int | None = None,
    complete_window: bool = False,
) -> LogFilters:
    normalized_from = _normalize_datetime(from_time)
    normalized_to = _normalize_datetime(to_time)
    if complete_window:
        normalized_from, normalized_to = _complete_window(
            normalized_from, normalized_to
        )
    try:
        return LogFilters(
            tenant_id=tenant_id,
            service_id=service_id,
            tool_alias=tool_alias,
            connection_id=connection_id,
            connector_key=connector_key,
            tool_key=tool_key,
            category=category or "",
            event_name=event_name or "",
            status=status or "",
            from_time=normalized_from,
            to_time=normalized_to,
            q=q or "",
            request_id=request_id or "",
            client_ip=client_ip or "",
            cost_min=cost_min,
            cost_max=cost_max,
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, "无效的日志筛选条件") from exc


def _query_filters(
    tenant_id: Annotated[str | None, Query(max_length=64)] = None,
    service_id: Annotated[str | None, Query(max_length=64)] = None,
    tool_alias: Annotated[str | None, Query(max_length=128)] = None,
    connection_id: Annotated[str | None, Query(max_length=64)] = None,
    connector_key: Annotated[str | None, Query(max_length=64)] = None,
    tool_key: Annotated[str | None, Query(max_length=128)] = None,
    category: Annotated[Category | None, Query()] = None,
    event_name: Annotated[str | None, Query(max_length=96)] = None,
    status: Annotated[Status | None, Query()] = None,
    from_time: Annotated[datetime | None, Query(alias="from")] = None,
    to_time: Annotated[datetime | None, Query(alias="to")] = None,
    q: Annotated[str | None, Query(max_length=100)] = None,
    request_id: Annotated[str | None, Query(max_length=64)] = None,
    client_ip: Annotated[str | None, Query(max_length=64)] = None,
    cost_min: Annotated[int | None, Query(ge=0)] = None,
    cost_max: Annotated[int | None, Query(ge=0)] = None,
) -> LogFilters:
    return _new_filters(
        tenant_id=tenant_id,
        service_id=service_id,
        tool_alias=tool_alias,
        connection_id=connection_id,
        connector_key=connector_key,
        tool_key=tool_key,
        category=category,
        event_name=event_name,
        status=status,
        from_time=from_time,
        to_time=to_time,
        q=q,
        request_id=request_id,
        client_ip=client_ip,
        cost_min=cost_min,
        cost_max=cost_max,
        complete_window=True,
    )


class DeleteFilterBody(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    tenant_id: str | None = Field(default=None, max_length=64)
    service_id: str | None = Field(default=None, max_length=64)
    tool_alias: str | None = Field(default=None, max_length=128)
    connection_id: str | None = Field(default=None, max_length=64)
    connector_key: str | None = Field(default=None, max_length=64)
    tool_key: str | None = Field(default=None, max_length=128)
    category: Category | None = None
    event_name: str | None = Field(default=None, max_length=96)
    status: Status | None = None
    from_time: datetime | None = Field(default=None, alias="from")
    to_time: datetime | None = Field(default=None, alias="to")
    q: str | None = Field(default=None, max_length=100)
    request_id: str | None = Field(default=None, max_length=64)
    client_ip: str | None = Field(default=None, max_length=64)
    cost_min: StrictNonNegativeInt | None = None
    cost_max: StrictNonNegativeInt | None = None

    @model_validator(mode="after")
    def validate_ranges(self):
        if (self.from_time is None) != (self.to_time is None):
            raise ValueError("filter time bounds must be supplied together")
        normalized_from = _normalize_datetime(self.from_time)
        normalized_to = _normalize_datetime(self.to_time)
        if (
            normalized_from is not None
            and normalized_to is not None
            and normalized_from > normalized_to
        ):
            raise ValueError("filter time bounds are inverted")
        if (
            self.cost_min is not None
            and self.cost_max is not None
            and self.cost_min > self.cost_max
        ):
            raise ValueError("filter cost bounds are inverted")
        return self

    def has_constraint(self) -> bool:
        return any(
            value not in (None, "")
            for value in (
                self.tenant_id,
                self.service_id,
                self.tool_alias,
                self.connection_id,
                self.connector_key,
                self.tool_key,
                self.category,
                self.event_name,
                self.status,
                self.from_time,
                self.to_time,
                self.q,
                self.request_id,
                self.client_ip,
                self.cost_min,
                self.cost_max,
            )
        )

    def to_filters(self) -> LogFilters:
        return _new_filters(
            tenant_id=self.tenant_id,
            service_id=self.service_id,
            tool_alias=self.tool_alias,
            connection_id=self.connection_id,
            connector_key=self.connector_key,
            tool_key=self.tool_key,
            category=self.category,
            event_name=self.event_name,
            status=self.status,
            from_time=self.from_time,
            to_time=self.to_time,
            q=self.q,
            request_id=self.request_id,
            client_ip=self.client_ip,
            cost_min=self.cost_min,
            cost_max=self.cost_max,
        )


class DeleteRequestBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: DeleteMode
    ids: list[DeleteId] = Field(
        default_factory=list,
        max_length=MAX_DELETE_IDS,
    )
    filter: DeleteFilterBody | None = None
    before_date: datetime | None = None

    @model_validator(mode="after")
    def validate_mode_fields(self):
        if self.mode == "ids":
            if not self.ids or self.filter is not None or self.before_date is not None:
                raise ValueError("ids mode requires only ids")
        elif self.mode == "filter":
            if (
                self.filter is None
                or not self.filter.has_constraint()
                or self.ids
                or self.before_date is not None
            ):
                raise ValueError("filter mode requires only a non-empty filter")
        elif self.mode == "before_date":
            if self.before_date is None or self.ids or self.filter is not None:
                raise ValueError("before_date mode requires only before_date")
        elif self.ids or self.filter is not None or self.before_date is not None:
            raise ValueError("all mode does not accept selection fields")
        return self

    def to_spec(self) -> DeleteSpec:
        if self.mode == "ids":
            return DeleteSpec(mode="ids", ids=tuple(sorted(set(self.ids))))
        if self.mode == "filter":
            return DeleteSpec(mode="filter", filters=self.filter.to_filters())
        if self.mode == "before_date":
            return DeleteSpec(
                mode="before_date",
                before_date=_normalize_datetime(self.before_date),
            )
        return DeleteSpec(mode="all")


class DeletePreviewRequest(DeleteRequestBase):
    pass


class DeleteExecuteRequest(DeleteRequestBase):
    confirm_token: str = Field(min_length=1, max_length=MAX_CONFIRM_TOKEN_LENGTH)


class RetentionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retention_days: int = Field(strict=True, ge=0, le=3650)


def _session_token(request: Request) -> str:
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        authorization = request.headers.get("Authorization", "")
        if authorization.startswith("Bearer "):
            token = authorization[len("Bearer ") :].strip()
    if not token:
        raise HTTPException(401, "未登录或会话过期")
    return token


def _session_digest(request: Request) -> str:
    return hashlib.sha256(_session_token(request).encode("utf-8")).hexdigest()


def _signing_key() -> bytes:
    global _dev_warning_emitted
    settings = get_settings()
    credential_key = str(getattr(settings, "credential_key", "") or "").strip()
    admin_password = str(getattr(settings, "admin_password", "") or "")
    if credential_key and admin_password:
        material = (
            b"wbsysc:mcp-log-delete:v1\0"
            + credential_key.encode("utf-8")
            + b"\0"
            + admin_password.encode("utf-8")
        )
        return hashlib.sha256(material).digest()
    if str(getattr(settings, "app_env", "dev")).lower() == "prod":
        raise RuntimeError("production delete confirmation key material is missing")
    if not _dev_warning_emitted:
        logger.warning(
            "MCP log delete confirmations use a process-local development key"
        )
        _dev_warning_emitted = True
    return _DEV_FALLBACK_KEY


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _datetime_payload(value: datetime) -> str:
    return value.isoformat(timespec="microseconds") + "Z"


def _filters_payload(filters: LogFilters) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for name in (
        "tenant_id",
        "service_id",
        "tool_alias",
        "connection_id",
        "connector_key",
        "tool_key",
        "category",
        "event_name",
        "status",
        "q",
        "request_id",
        "client_ip",
        "cost_min",
        "cost_max",
    ):
        value = getattr(filters, name)
        if value not in (None, ""):
            payload[name] = value
    if filters.from_time is not None:
        payload["from"] = _datetime_payload(filters.from_time)
    if filters.to_time is not None:
        payload["to"] = _datetime_payload(filters.to_time)
    return payload


def _spec_payload(spec: DeleteSpec) -> dict[str, Any]:
    payload: dict[str, Any] = {"mode": spec.mode}
    if spec.mode == "ids":
        payload["ids"] = list(spec.ids)
    elif spec.mode == "filter":
        payload["filter"] = _filters_payload(spec.filters)
    elif spec.mode == "before_date":
        payload["before_date"] = _datetime_payload(spec.before_date)
    return payload


def _confirmation_token(payload: dict[str, Any]) -> str:
    encoded = _b64encode(_canonical_json(payload))
    signature = hmac.new(
        _signing_key(), encoded.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{encoded}.{_b64encode(signature)}"


def _decode_confirmation(token: str) -> dict[str, Any]:
    try:
        encoded, supplied_signature = token.split(".", 1)
        expected_signature = hmac.new(
            _signing_key(), encoded.encode("ascii"), hashlib.sha256
        ).digest()
        if not hmac.compare_digest(
            _b64decode(supplied_signature), expected_signature
        ):
            raise ValueError("signature mismatch")
        payload = json.loads(_b64decode(encoded))
        if not isinstance(payload, dict):
            raise ValueError("invalid payload")
    except Exception as exc:
        raise HTTPException(400, "无效的清理确认令牌") from exc
    return payload


def _validate_confirmation(
    token: str, spec: DeleteSpec, request: Request
) -> dict[str, Any]:
    payload = _decode_confirmation(token)
    try:
        valid = (
            type(payload["v"]) is int
            and payload["v"] == _TOKEN_VERSION
            and payload["spec"] == _spec_payload(spec)
            and hmac.compare_digest(payload["session"], _session_digest(request))
            and _is_nonnegative_int(payload["max_id"])
            and _is_nonnegative_int(payload["count"])
            and _is_finite_number(payload["exp"])
            and payload["exp"] >= _now()
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise HTTPException(400, "无效或已过期的清理确认令牌")
    return payload


def _store_call(operation, *args, transform=None):
    try:
        result = operation(*args)
        return transform(result) if transform is not None else result
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("MCP log admin storage operation failed type=%s", type(exc).__name__)
        raise HTTPException(500, "日志服务暂不可用") from exc


def _safe_log_item(item: Any) -> dict[str, Any]:
    values = dict(item)
    safe_item = {field: values.get(field) for field in SAFE_LOG_FIELDS}
    log_id = values.get("id")
    if type(log_id) is not int or not 1 <= log_id <= MAX_LOG_ID:
        raise TypeError("expected a signed BIGINT log ID")
    safe_item["id"] = str(log_id)
    return safe_item


def _is_nonnegative_int(value: Any) -> bool:
    return type(value) is int and value >= 0


def _require_nonnegative_int(value: Any) -> int:
    if not _is_nonnegative_int(value):
        raise TypeError("expected a nonnegative integer")
    return value


def _is_finite_number(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _require_nonnegative_number(value: Any) -> int | float:
    if not _is_finite_number(value) or value < 0:
        raise TypeError("expected a finite nonnegative number")
    return value


def _safe_counted_rows(rows: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(rows, (list, tuple)):
        raise TypeError("expected a row collection")
    safe_rows = []
    for row in rows:
        values = dict(row)
        safe_rows.append(
            {
                label: values.get(label),
                "count": _require_nonnegative_int(values.get("count", 0)),
            }
        )
    return safe_rows


def _safe_list_result(result: Any, page: int, page_size: int) -> dict[str, Any]:
    values = dict(result)
    items = values.get("items", [])
    if not isinstance(items, (list, tuple)):
        raise TypeError("expected a log item collection")
    return {
        "items": [_safe_log_item(item) for item in items],
        "total": _require_nonnegative_int(values.get("total", 0)),
        "page": page,
        "page_size": page_size,
    }


def safe_log_list(result: Any, page: int, page_size: int) -> dict[str, Any]:
    """Project a store list result through the shared public log schema."""
    return _safe_list_result(result, page, page_size)


def _safe_stats(stats: Any) -> dict[str, Any]:
    values = dict(stats)
    return {
        "total": _require_nonnegative_int(values.get("total", 0)),
        "success_rate": _require_nonnegative_number(values.get("success_rate", 0.0)),
        "error_count": _require_nonnegative_int(values.get("error_count", 0)),
        "avg_cost_ms": _require_nonnegative_number(values.get("avg_cost_ms", 0.0)),
        "p95_cost_ms": _require_nonnegative_int(values.get("p95_cost_ms", 0)),
        "trend": _safe_counted_rows(values.get("trend", []), "bucket"),
        "top_tools": _safe_counted_rows(values.get("top_tools", []), "event_name"),
        "status_distribution": _safe_counted_rows(
            values.get("status_distribution", []), "result_status"
        ),
    }


def safe_log_stats(stats: Any) -> dict[str, Any]:
    """Project aggregate log data through the shared public stats schema."""
    return _safe_stats(stats)


def _safe_preview_result(result: Any) -> dict[str, int]:
    values = dict(result)
    return {
        "matched_count": _require_nonnegative_int(values.get("matched_count")),
        "max_id": _require_nonnegative_int(values.get("max_id")),
    }


def _safe_retention_days(value: Any) -> int:
    days = _require_nonnegative_int(value)
    if days > 3650:
        raise TypeError("retention days are out of range")
    return days


@router.get("/mcp-logs")
def get_mcp_logs(
    request: Request,
    filters: Annotated[LogFilters, Depends(_query_filters)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    _require_auth(request)
    return _store_call(
        list_logs,
        filters,
        page,
        page_size,
        transform=lambda result: safe_log_list(result, page, page_size),
    )


@router.get("/mcp-logs/stats")
def get_mcp_log_statistics(
    request: Request,
    filters: Annotated[LogFilters, Depends(_query_filters)],
):
    _require_auth(request)
    return _store_call(get_log_stats, filters, transform=safe_log_stats)


@router.post("/mcp-logs/delete-preview")
def post_mcp_log_delete_preview(body: DeletePreviewRequest, request: Request):
    _require_auth(request)
    spec = body.to_spec()
    preview = _store_call(preview_delete, spec, transform=_safe_preview_result)
    expires_at = int(_now() + _TOKEN_TTL_SECONDS)
    payload = {
        "v": _TOKEN_VERSION,
        "session": _session_digest(request),
        "spec": _spec_payload(spec),
        "max_id": preview["max_id"],
        "count": preview["matched_count"],
        "exp": expires_at,
    }
    try:
        token = _confirmation_token(payload)
    except Exception as exc:
        logger.error("MCP log delete confirmation signing failed type=%s", type(exc).__name__)
        raise HTTPException(500, "日志服务暂不可用") from exc
    return {
        "matched_count": payload["count"],
        "max_id": payload["max_id"],
        "expires_at": expires_at,
        "confirm_token": token,
    }


@router.post("/mcp-logs/delete")
@router.delete("/mcp-logs")
def delete_mcp_logs(body: DeleteExecuteRequest, request: Request):
    _require_auth(request)
    spec = body.to_spec()
    payload = _validate_confirmation(body.confirm_token, spec, request)
    deleted = _store_call(
        delete_matching,
        spec,
        payload["max_id"],
        transform=_require_nonnegative_int,
    )
    return {"deleted": deleted}


@router.get("/mcp-log-settings")
def get_mcp_log_settings(request: Request):
    _require_auth(request)
    days = _store_call(get_retention_days, transform=_safe_retention_days)
    return {"retention_days": days}


@router.put("/mcp-log-settings")
def put_mcp_log_settings(body: RetentionRequest, request: Request):
    _require_auth(request)
    days = _store_call(
        set_retention_days,
        body.retention_days,
        transform=_safe_retention_days,
    )
    return {"retention_days": days}
