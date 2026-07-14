"""Immutable contracts for central MCP call-log storage."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


LogCategory = Literal["tool", "protocol", "auth"]
LogStatus = Literal["ok", "partial", "error", "denied"]
DeleteMode = Literal["ids", "filter", "before_date", "all"]

_CATEGORIES = frozenset(("tool", "protocol", "auth"))
_STATUSES = frozenset(("ok", "partial", "error", "denied"))


def _validate_string(name: str, value: str, maximum: int) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if len(value) > maximum:
        raise ValueError(f"{name} must be at most {maximum} characters")


def _validate_utc_naive(name: str, value: datetime | None) -> None:
    if value is None:
        return
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is not None and value.utcoffset() is not None:
        raise ValueError(f"{name} must be UTC-naive")


@dataclass(frozen=True)
class McpLogEvent:
    tenant_id: str = ""
    category: LogCategory = "protocol"
    event_name: str = "mcp_http_request"
    target: str = ""
    params_summary: str = ""
    result_status: LogStatus = "ok"
    error_code: str = ""
    error_summary: str = ""
    cost_ms: int = 0
    request_id: str = ""
    client_ip: str = ""
    http_method: str = ""
    http_status: int = 0
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.category not in _CATEGORIES:
            raise ValueError("category must be tool, protocol, or auth")
        if self.result_status not in _STATUSES:
            raise ValueError("result_status must be ok, partial, error, or denied")
        for name, maximum in (
            ("tenant_id", 64),
            ("event_name", 96),
            ("target", 256),
            ("params_summary", 512),
            ("error_code", 64),
            ("error_summary", 256),
            ("request_id", 64),
            ("client_ip", 64),
            ("http_method", 16),
        ):
            _validate_string(name, getattr(self, name), maximum)
        if isinstance(self.cost_ms, bool) or not isinstance(self.cost_ms, int):
            raise TypeError("cost_ms must be an integer")
        if self.cost_ms < 0:
            raise ValueError("cost_ms must be non-negative")
        if isinstance(self.http_status, bool) or not isinstance(self.http_status, int):
            raise TypeError("http_status must be an integer")
        if not 0 <= self.http_status <= 999:
            raise ValueError("http_status must be between 0 and 999")
        _validate_utc_naive("created_at", self.created_at)


@dataclass(frozen=True)
class LogFilters:
    tenant_id: str | None = None
    category: LogCategory | Literal[""] = ""
    event_name: str = ""
    status: LogStatus | Literal[""] = ""
    from_time: datetime | None = None
    to_time: datetime | None = None
    q: str = ""
    request_id: str = ""
    client_ip: str = ""
    cost_min: int | None = None
    cost_max: int | None = None

    def __post_init__(self) -> None:
        if self.category and self.category not in _CATEGORIES:
            raise ValueError("category must be tool, protocol, or auth")
        if self.status and self.status not in _STATUSES:
            raise ValueError("status must be ok, partial, error, or denied")
        if self.tenant_id is not None:
            _validate_string("tenant_id", self.tenant_id, 64)
        for name, maximum in (
            ("event_name", 96),
            ("q", 100),
            ("request_id", 64),
            ("client_ip", 64),
        ):
            _validate_string(name, getattr(self, name), maximum)
        _validate_utc_naive("from_time", self.from_time)
        _validate_utc_naive("to_time", self.to_time)
        if self.from_time and self.to_time and self.from_time > self.to_time:
            raise ValueError("from_time must not be later than to_time")
        for name in ("cost_min", "cost_max"):
            value = getattr(self, name)
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, int):
                    raise TypeError(f"{name} must be an integer")
                if value < 0:
                    raise ValueError(f"{name} must be non-negative")
        if (
            self.cost_min is not None
            and self.cost_max is not None
            and self.cost_min > self.cost_max
        ):
            raise ValueError("cost_min must not exceed cost_max")


@dataclass(frozen=True)
class DeleteSpec:
    mode: DeleteMode
    ids: tuple[int, ...] = ()
    filters: LogFilters = field(default_factory=LogFilters)
    before_date: datetime | None = None

    def __post_init__(self) -> None:
        if self.mode not in ("ids", "filter", "before_date", "all"):
            raise ValueError("mode must be ids, filter, before_date, or all")
        if not isinstance(self.filters, LogFilters):
            raise TypeError("filters must be LogFilters")
        normalized_ids = tuple(sorted(set(self.ids)))
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in normalized_ids):
            raise ValueError("ids must contain positive integers")
        object.__setattr__(self, "ids", normalized_ids)
        _validate_utc_naive("before_date", self.before_date)
        if self.mode == "ids" and not normalized_ids:
            raise ValueError("ids mode requires ids")
        if self.mode == "before_date" and self.before_date is None:
            raise ValueError("before_date mode requires before_date")
