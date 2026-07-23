from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Any, Literal


_CONNECTOR_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,127}$")

def validate_connection_alias(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 64
        or _CONNECTOR_IDENTIFIER_PATTERN.fullmatch(value) is None
    ):
        raise ValueError("invalid connection_alias")
    return value


@dataclass(frozen=True)
class ConnectionRecord:
    connection_id: str
    tenant_id: str
    connector_key: str
    display_name: str
    status: Literal["draft", "active", "disabled", "error"]
    data_mode: Literal["direct", "stored", "hybrid"]
    public_config: dict[str, Any]
    config_version: int
    connection_alias: str = ""

    def __post_init__(self) -> None:
        if self.connection_alias:
            validate_connection_alias(self.connection_alias)


@dataclass(frozen=True)
class IssuedToken:
    token_id: str
    raw_value: str
    prefix: str


@dataclass(frozen=True)
class CredentialRecord:
    connection_id: str
    credential_key: str
    encrypted_value: bytes
    metadata: dict[str, Any]


@dataclass(frozen=True)
class ToolPolicy:
    connection_id: str
    tool_name: str
    enabled: bool
    policy: dict[str, Any]


@dataclass(frozen=True)
class ConnectionToken:
    token_id: str
    connection_id: str
    token_hmac: str
    prefix: str
    label: str = ""
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class SyncState:
    connection_id: str
    state_key: str
    state: dict[str, Any]
    last_success_at: datetime | None = None
    last_error: str = ""
