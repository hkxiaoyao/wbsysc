from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import re
from types import MappingProxyType
from typing import Any, Literal, Mapping


_SERVICE_STATUSES = frozenset({"draft", "active", "disabled"})
_BINDING_STATUSES = frozenset({"active", "disabled", "broken"})
_CONNECTOR_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")
_RFC3339_SECONDS_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})$"
)


def normalize_utc_datetime(value: datetime | None) -> datetime | None:
    """Normalize an optional whole-second datetime to UTC-naive SQL form."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, datetime):
        raise TypeError("expires_at must be a datetime or None")
    if value.microsecond:
        raise ValueError("expires_at must use whole-second precision")
    if value.tzinfo is None:
        return value
    offset = value.utcoffset()
    if offset is None:
        raise ValueError("expires_at timezone is invalid")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def parse_rfc3339_utc(value: object) -> datetime:
    """Parse strict RFC 3339 seconds and return UTC-naive SQL form."""
    if not isinstance(value, str) or _RFC3339_SECONDS_PATTERN.fullmatch(value) is None:
        raise ValueError("expires_at must be RFC3339 with seconds and timezone")
    candidate = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        raise ValueError("expires_at must be a valid RFC3339 timestamp") from None
    normalized = normalize_utc_datetime(parsed)
    if normalized is None:  # pragma: no cover - parsed is always a datetime
        raise ValueError("expires_at must be a valid RFC3339 timestamp")
    return normalized


def _required_identifier(label: str, value: str, *, max_length: int = 128) -> None:
    if (
        not isinstance(value, str)
        or len(value) > max_length
        or _CONNECTOR_IDENTIFIER_PATTERN.fullmatch(value) is None
    ):
        raise ValueError(f"invalid {label}")


def _required_text(label: str, value: str, *, max_length: int) -> None:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise ValueError(f"invalid {label}")


@dataclass(frozen=True)
class McpService:
    service_id: str
    tenant_id: str
    display_name: str
    service_key: str
    status: Literal["draft", "active", "disabled"]
    config_version: int

    def __post_init__(self) -> None:
        _required_text("service_id", self.service_id, max_length=64)
        _required_text("tenant_id", self.tenant_id, max_length=64)
        _required_text("display_name", self.display_name, max_length=128)
        _required_identifier("service_key", self.service_key, max_length=64)
        if self.status not in _SERVICE_STATUSES:
            raise ValueError("invalid service status")
        if (
            isinstance(self.config_version, bool)
            or not isinstance(self.config_version, int)
            or self.config_version < 1
        ):
            raise ValueError("invalid service config_version")


@dataclass(frozen=True)
class IssuedServiceToken:
    token_id: str
    raw_value: str = field(repr=False)
    prefix: str

    def __post_init__(self) -> None:
        _required_text("token_id", self.token_id, max_length=64)
        _required_text("raw_value", self.raw_value, max_length=512)
        _required_text("prefix", self.prefix, max_length=32)


@dataclass(frozen=True)
class ServiceTokenMetadata:
    token_id: str
    prefix: str
    label: str
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None
    created_at: datetime

    def __post_init__(self) -> None:
        _required_text("token_id", self.token_id, max_length=64)
        _required_text("prefix", self.prefix, max_length=32)
        if not isinstance(self.label, str) or len(self.label) > 128:
            raise ValueError("invalid token label")


@dataclass(frozen=True)
class ServiceToolBinding:
    binding_id: str
    service_id: str
    connection_id: str
    source_tool_key: str
    tool_alias: str
    binding_status: Literal["active", "disabled", "broken"]
    policy: Mapping[str, Any]

    def __post_init__(self) -> None:
        _required_text("binding_id", self.binding_id, max_length=64)
        _required_text("service_id", self.service_id, max_length=64)
        _required_text("connection_id", self.connection_id, max_length=64)
        _required_identifier("source_tool_key", self.source_tool_key)
        _required_identifier("tool_alias", self.tool_alias)
        if self.binding_status not in _BINDING_STATUSES:
            raise ValueError("invalid binding status")
        if not isinstance(self.policy, Mapping):
            raise ValueError("invalid binding policy")
        try:
            detached = json.loads(
                json.dumps(dict(self.policy), ensure_ascii=False, allow_nan=False)
            )
        except (TypeError, ValueError):
            raise ValueError("invalid binding policy") from None
        if not isinstance(detached, dict):
            raise ValueError("invalid binding policy")
        object.__setattr__(self, "policy", MappingProxyType(detached))
