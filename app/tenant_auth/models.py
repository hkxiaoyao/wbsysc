from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class TenantAccount:
    tenant_id: str
    status: Literal["active", "disabled", "locked"]
    failed_attempts: int = 0
    locked_until: datetime | None = None


@dataclass(frozen=True)
class TenantPrincipal:
    principal_type: Literal["tenant"]
    tenant_id: str


@dataclass(frozen=True)
class IssuedTenantSession:
    session_id: str
    tenant_id: str
    raw_value: str = field(repr=False)
    expires_at: datetime
