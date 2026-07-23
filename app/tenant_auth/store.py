from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
import hashlib
import secrets
from typing import Any
import uuid

from sqlalchemy import text

from .models import IssuedTenantSession, TenantAccount, TenantPrincipal
from .passwords import hash_password, verify_password


_LOCK_THRESHOLD = 5
_LOCK_MINUTES = 15
_DUMMY_PASSWORD_HASH = hash_password("Timing-Guard-Value-123")

_ACCOUNT_DDL = """
CREATE TABLE IF NOT EXISTS `tenant_account` (
  `tenant_id` VARCHAR(64) NOT NULL,
  `password_hash` VARCHAR(255) NOT NULL,
  `status` VARCHAR(16) NOT NULL DEFAULT 'active',
  `failed_attempts` INT NOT NULL DEFAULT 0,
  `locked_until` DATETIME NULL,
  `password_changed_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `last_login_at` DATETIME NULL,
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`tenant_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

_SESSION_DDL = """
CREATE TABLE IF NOT EXISTS `tenant_session` (
  `session_id` VARCHAR(64) NOT NULL,
  `tenant_id` VARCHAR(64) NOT NULL,
  `session_digest` CHAR(64) NOT NULL,
  `expires_at` DATETIME NOT NULL,
  `revoked_at` DATETIME NULL,
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`session_id`),
  UNIQUE KEY `uk_tenant_session_digest` (`session_digest`),
  KEY `idx_tenant_session_tenant` (`tenant_id`, `revoked_at`, `expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def _engine():
    from app.db import get_engine

    return get_engine()


def _mapping(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    if isinstance(row, dict):
        return row
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        return dict(mapping)
    return dict(row)


def ensure_tenant_auth_tables() -> None:
    with _engine().begin() as conn:
        conn.execute(text(_ACCOUNT_DDL))
        conn.execute(text(_SESSION_DDL))


def _session_digest(raw_value: str) -> str:
    return hashlib.sha256(raw_value.encode("utf-8")).hexdigest()


def _revoke_tenant_sessions(conn: Any, tenant_id: str) -> None:
    conn.execute(
        text(
            "UPDATE tenant_session SET revoked_at=UTC_TIMESTAMP() "
            "WHERE tenant_id=:tenant_id AND revoked_at IS NULL"
        ),
        {"tenant_id": tenant_id},
    )


def authenticate(
    tenant_id: str,
    raw_password: str,
    *,
    now: datetime | None = None,
) -> TenantAccount | None:
    if not isinstance(tenant_id, str) or not tenant_id or not isinstance(raw_password, str):
        return None
    current = now or datetime.now(timezone.utc).replace(tzinfo=None)
    with _engine().connect() as conn:
        initial_row = conn.execute(
            text(
                "SELECT tenant_id, password_hash, status, failed_attempts, locked_until "
                "FROM tenant_account WHERE tenant_id=:tenant_id"
            ),
            {"tenant_id": tenant_id},
        ).mappings().first()
    initial = _mapping(initial_row)
    if not initial:
        verify_password(_DUMMY_PASSWORD_HASH, raw_password)
        return None

    initial_hash = str(initial.get("password_hash", ""))
    password_matches = verify_password(initial_hash, raw_password)
    with _engine().begin() as conn:
        row = conn.execute(
            text(
                "SELECT tenant_id, password_hash, status, failed_attempts, locked_until "
                "FROM tenant_account WHERE tenant_id=:tenant_id FOR UPDATE"
            ),
            {"tenant_id": tenant_id},
        ).mappings().first()
        values = _mapping(row)
        if not values or str(values.get("password_hash", "")) != initial_hash:
            return None

        status = values.get("status")
        locked_until = values.get("locked_until")
        if status == "locked" and isinstance(locked_until, datetime) and locked_until <= current:
            status = "active"
            locked_until = None
            conn.execute(
                text(
                    "UPDATE tenant_account SET status='active', failed_attempts=0, "
                    "locked_until=NULL WHERE tenant_id=:tenant_id"
                ),
                {"tenant_id": tenant_id},
            )
        if status != "active":
            return None

        if not password_matches:
            failed_attempts = max(int(values.get("failed_attempts") or 0), 0) + 1
            next_status = "locked" if failed_attempts >= _LOCK_THRESHOLD else "active"
            next_locked_until = (
                current + timedelta(minutes=_LOCK_MINUTES)
                if next_status == "locked"
                else None
            )
            conn.execute(
                text(
                    "UPDATE tenant_account SET failed_attempts=:failed_attempts, "
                    "status=:status, locked_until=:locked_until "
                    "WHERE tenant_id=:tenant_id"
                ),
                {
                    "tenant_id": tenant_id,
                    "failed_attempts": failed_attempts,
                    "status": next_status,
                    "locked_until": next_locked_until,
                },
            )
            return None

        conn.execute(
            text(
                "UPDATE tenant_account SET failed_attempts=0, locked_until=NULL, "
                "last_login_at=:last_login_at WHERE tenant_id=:tenant_id"
            ),
            {"tenant_id": tenant_id, "last_login_at": current},
        )
        return TenantAccount(tenant_id=tenant_id, status="active")


def upsert_account(
    tenant_id: str,
    raw_password: str,
    *,
    status: str = "active",
    conn: Any | None = None,
) -> None:
    if status not in {"active", "disabled"}:
        raise ValueError("invalid tenant account status")
    encoded = hash_password(raw_password)
    transaction = nullcontext(conn) if conn is not None else _engine().begin()
    with transaction as active_conn:
        active_conn.execute(
            text(
                "INSERT INTO tenant_account "
                "(tenant_id, password_hash, status, failed_attempts, locked_until) "
                "VALUES (:tenant_id, :password_hash, :status, 0, NULL) "
                "ON DUPLICATE KEY UPDATE password_hash=VALUES(password_hash), "
                "status=VALUES(status), failed_attempts=0, locked_until=NULL, "
                "password_changed_at=UTC_TIMESTAMP()"
            ),
            {"tenant_id": tenant_id, "password_hash": encoded, "status": status},
        )
        _revoke_tenant_sessions(active_conn, tenant_id)


def set_account_status(
    tenant_id: str,
    status: str,
    *,
    conn: Any | None = None,
) -> bool:
    if status not in {"active", "disabled"}:
        raise ValueError("invalid tenant account status")
    transaction = nullcontext(conn) if conn is not None else _engine().begin()
    with transaction as active_conn:
        result = active_conn.execute(
            text(
                "UPDATE tenant_account SET status=:status, failed_attempts=0, "
                "locked_until=NULL WHERE tenant_id=:tenant_id"
            ),
            {"tenant_id": tenant_id, "status": status},
        )
        if status == "disabled":
            _revoke_tenant_sessions(active_conn, tenant_id)
        return bool(getattr(result, "rowcount", 0))


def delete_account(tenant_id: str, *, conn: Any | None = None) -> None:
    transaction = nullcontext(conn) if conn is not None else _engine().begin()
    with transaction as active_conn:
        active_conn.execute(
            text("DELETE FROM tenant_session WHERE tenant_id=:tenant_id"),
            {"tenant_id": tenant_id},
        )
        active_conn.execute(
            text("DELETE FROM tenant_account WHERE tenant_id=:tenant_id"),
            {"tenant_id": tenant_id},
        )


def issue_session(
    tenant_id: str,
    ttl_seconds: int,
    *,
    now: datetime | None = None,
) -> IssuedTenantSession:
    if not isinstance(ttl_seconds, int) or isinstance(ttl_seconds, bool) or ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be a positive integer")
    current = now or datetime.now(timezone.utc).replace(tzinfo=None)
    expires_at = current + timedelta(seconds=ttl_seconds)
    raw_value = secrets.token_urlsafe(32)
    issued = IssuedTenantSession(
        session_id=str(uuid.uuid4()),
        tenant_id=tenant_id,
        raw_value=raw_value,
        expires_at=expires_at,
    )
    with _engine().begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tenant_session "
                "(session_id, tenant_id, session_digest, expires_at) "
                "VALUES (:session_id, :tenant_id, :session_digest, :expires_at)"
            ),
            {
                "session_id": issued.session_id,
                "tenant_id": tenant_id,
                "session_digest": _session_digest(raw_value),
                "expires_at": expires_at,
            },
        )
    return issued


def resolve_session(raw_value: str) -> TenantPrincipal | None:
    if not isinstance(raw_value, str) or not raw_value:
        return None
    with _engine().begin() as conn:
        row = conn.execute(
            text(
                "SELECT a.tenant_id FROM tenant_session s "
                "JOIN tenant_account a ON a.tenant_id=s.tenant_id "
                "JOIN tenant_config c ON c.tenant_id=s.tenant_id "
                "WHERE s.session_digest=:session_digest AND s.revoked_at IS NULL "
                "AND s.expires_at>UTC_TIMESTAMP() AND a.status='active' "
                "AND c.enabled=1 LIMIT 1"
            ),
            {"session_digest": _session_digest(raw_value)},
        ).mappings().first()
    values = _mapping(row)
    tenant_id = values.get("tenant_id")
    if not isinstance(tenant_id, str) or not tenant_id:
        return None
    return TenantPrincipal(principal_type="tenant", tenant_id=tenant_id)


def revoke_session(raw_value: str) -> bool:
    if not isinstance(raw_value, str) or not raw_value:
        return False
    with _engine().begin() as conn:
        result = conn.execute(
            text(
                "UPDATE tenant_session SET revoked_at=UTC_TIMESTAMP() "
                "WHERE session_digest=:session_digest AND revoked_at IS NULL"
            ),
            {"session_digest": _session_digest(raw_value)},
        )
        return bool(getattr(result, "rowcount", 0))


def revoke_tenant_sessions(tenant_id: str) -> None:
    with _engine().begin() as conn:
        _revoke_tenant_sessions(conn, tenant_id)
