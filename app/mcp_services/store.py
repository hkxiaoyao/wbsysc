from __future__ import annotations

from dataclasses import replace
from datetime import datetime
import json
import logging
import secrets
import threading
import uuid
from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy import text

from app.connections.models import ToolPolicy
from app.connectors.contracts import (
    ConnectorSpec,
    ToolDisabledError,
    ToolSpec,
    WritePolicyError,
)
from app.connectors.registry import ConnectorRegistry
from app.connectors.runtime import InvalidToolPolicyError, PolicyGuard

from .crypto import decrypt_token, encrypt_token, token_hmac
from .models import (
    IssuedServiceToken,
    McpService,
    ServiceTokenMetadata,
    ServiceToolBinding,
    normalize_utc_datetime,
)


logger = logging.getLogger(__name__)

_DEFAULT_SERVICE_WATERMARK_KEY = "default_mcp_service_backfill_v1"
_DEFAULT_SERVICE_WATERMARK_STATUS = "completed"

ServiceCacheInvalidator = Callable[[str], None]
_service_cache_invalidators: list[ServiceCacheInvalidator] = []
_service_cache_invalidator_lock = threading.Lock()


class ServiceOwnershipError(PermissionError):
    """The requested service or connection is outside the tenant boundary."""


class ServiceVersionConflictError(RuntimeError):
    """A binding snapshot write targeted an obsolete service version."""


class ServiceReferenceConflictError(RuntimeError):
    """A destructive mutation targeted a connection used by MCP services."""

    def __init__(self, services: Sequence[McpService]) -> None:
        self.services = tuple(services)
        super().__init__("connection is referenced by MCP services")


class TokenUnavailableError(LookupError):
    """The service token cannot be revealed through the requested path."""


_SERVICE_DDLS = (
    """
    CREATE TABLE IF NOT EXISTS mcp_service (
      service_id VARCHAR(64) NOT NULL,
      tenant_id VARCHAR(64) NOT NULL,
      display_name VARCHAR(128) NOT NULL,
      service_key VARCHAR(64) NOT NULL,
      status VARCHAR(16) NOT NULL DEFAULT 'draft',
      config_version INT NOT NULL DEFAULT 1,
      created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
      PRIMARY KEY (service_id),
      UNIQUE KEY uk_mcp_service_tenant_key (tenant_id, service_key),
      KEY idx_mcp_service_tenant_status (tenant_id, status)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS mcp_service_tool_binding (
      binding_id VARCHAR(64) NOT NULL,
      service_id VARCHAR(64) NOT NULL,
      connection_id VARCHAR(64) NOT NULL,
      source_tool_key VARCHAR(128) NOT NULL,
      tool_alias VARCHAR(128) NOT NULL,
      binding_status VARCHAR(16) NOT NULL DEFAULT 'active',
      policy_json TEXT NOT NULL,
      created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
      PRIMARY KEY (binding_id),
      UNIQUE KEY uk_service_tool_alias (service_id, tool_alias),
      UNIQUE KEY uk_service_source_tool (service_id, connection_id, source_tool_key),
      KEY idx_service_binding_connection (connection_id, service_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
    """
    CREATE TABLE IF NOT EXISTS mcp_service_token (
      token_id VARCHAR(64) NOT NULL,
      service_id VARCHAR(64) NOT NULL,
      token_hmac CHAR(64) NOT NULL,
      encrypted_token VARBINARY(4096) NULL,
      token_prefix VARCHAR(32) NOT NULL,
      token_label VARCHAR(128) NOT NULL DEFAULT '',
      expires_at DATETIME NULL,
      revoked_at DATETIME NULL,
      last_used_at DATETIME NULL,
      created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
      PRIMARY KEY (token_id),
      UNIQUE KEY uk_mcp_service_token_hmac (token_hmac),
      KEY idx_mcp_service_token_service (service_id, revoked_at, expires_at)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """,
)


def _engine():
    from app.db import get_engine

    return get_engine()


def _service_from_row(row: Any) -> McpService:
    values = dict(row._mapping) if hasattr(row, "_mapping") else dict(row)
    return McpService(
        service_id=values["service_id"],
        tenant_id=values["tenant_id"],
        display_name=values["display_name"],
        service_key=values["service_key"],
        status=values["status"],
        config_version=int(values["config_version"]),
    )


def _binding_from_row(row: Any) -> ServiceToolBinding:
    values = dict(row._mapping) if hasattr(row, "_mapping") else dict(row)
    policy = json.loads(values["policy_json"] or "{}")
    if not isinstance(policy, dict):
        raise ValueError("invalid persisted binding policy")
    return ServiceToolBinding(
        binding_id=values["binding_id"],
        service_id=values["service_id"],
        connection_id=values["connection_id"],
        source_tool_key=values["source_tool_key"],
        tool_alias=values["tool_alias"],
        binding_status=values["binding_status"],
        policy=policy,
    )


def ensure_mcp_service_tables() -> None:
    with _engine().begin() as conn:
        for ddl in _SERVICE_DDLS:
            conn.execute(text(ddl))


def default_service_id(connection_id: str) -> str:
    if not isinstance(connection_id, str) or not connection_id:
        raise ValueError("connection_id is required")
    return str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"wbsysc:default-service:{connection_id}")
    )


def _default_binding_id(service_id: str, tool_key: str) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"wbsysc:default-service-binding:{service_id}:{tool_key}",
        )
    )


def _default_service_key(connection_id: str) -> str:
    return f"default_{uuid.UUID(default_service_id(connection_id)).hex}"


def _pending_default_connections() -> list[dict[str, Any]]:
    with _engine().connect() as conn:
        rows = conn.execute(
            text("""
                SELECT c.connection_id, c.tenant_id, c.connection_alias,
                       c.connector_key, c.display_name, c.status, w.state_json
                FROM connection_instance c
                LEFT JOIN connection_sync_state w
                  ON w.connection_id=c.connection_id
                 AND w.state_key=:state_key
                ORDER BY c.created_at, c.connection_id
            """),
            {"state_key": _DEFAULT_SERVICE_WATERMARK_KEY},
        ).mappings().all()
    return [
        dict(row)
        for row in rows
        if not _default_backfill_completed(dict(row).get("state_json"))
    ]


def _default_backfill_completed(value: Any) -> bool:
    if not value:
        return False
    try:
        state = json.loads(value)
    except (TypeError, ValueError):
        return False
    return (
        isinstance(state, dict)
        and state.get("status") == _DEFAULT_SERVICE_WATERMARK_STATUS
    )


def _enabled_tools(
    conn: Any,
    connection: dict[str, Any],
    spec: ConnectorSpec,
) -> tuple[ToolSpec, ...]:
    rows = conn.execute(
        text("""
            SELECT tool_name, enabled, policy_json
            FROM connection_tool_policy
            WHERE connection_id=:connection_id
        """),
        {"connection_id": connection["connection_id"]},
    ).mappings().all()
    policies: dict[str, ToolPolicy] = {}
    for row in rows:
        try:
            values = json.loads(row["policy_json"] or "{}")
        except (TypeError, ValueError):
            values = None
        if not isinstance(values, dict):
            values = {"allow_write": False}
            enabled = False
        else:
            enabled = bool(row["enabled"])
        policies[row["tool_name"]] = ToolPolicy(
            connection_id=connection["connection_id"],
            tool_name=row["tool_name"],
            enabled=enabled,
            policy=values,
        )

    guard = PolicyGuard()
    enabled_tools = []
    for tool in spec.tools:
        policy = policies.get(tool.tool_key)
        if policy is None and tool.mcp_name != tool.tool_key:
            policy = policies.get(tool.mcp_name)
        try:
            guard.assert_allowed(tool, policy)
        except (ToolDisabledError, WritePolicyError, InvalidToolPolicyError):
            continue
        enabled_tools.append(tool)
    return tuple(enabled_tools)


def _backfill_default_service(
    connection: dict[str, Any], spec: ConnectorSpec
) -> None:
    connection_id = connection["connection_id"]
    service_id = default_service_id(connection_id)
    tenant_id = connection["tenant_id"]
    service_key = _default_service_key(connection_id)
    display_name = connection["display_name"] or connection_id
    status = "active" if connection["status"] == "active" else "disabled"
    with _engine().begin() as conn:
        existing_row = conn.execute(
            text("""
                SELECT service_id, tenant_id, display_name, service_key, status,
                       config_version
                FROM mcp_service
                WHERE service_id=:service_id
                LIMIT 1 FOR UPDATE
            """),
            {"service_id": service_id},
        ).fetchone()
        key_owner_row = conn.execute(
            text("""
                SELECT service_id, tenant_id, display_name, service_key, status,
                       config_version
                FROM mcp_service
                WHERE tenant_id=:tenant_id AND service_key=:service_key
                LIMIT 1 FOR UPDATE
            """),
            {"tenant_id": tenant_id, "service_key": service_key},
        ).fetchone()
        existing = (
            _service_from_row(existing_row) if existing_row is not None else None
        )
        key_owner = (
            _service_from_row(key_owner_row) if key_owner_row is not None else None
        )
        if key_owner is not None and key_owner.service_id != service_id:
            raise ServiceOwnershipError("default service key has another owner")
        if existing is not None and (
            existing.tenant_id != tenant_id or existing.service_key != service_key
        ):
            raise ServiceOwnershipError("default service id has another owner")

        params = {
            "service_id": service_id,
            "tenant_id": tenant_id,
            "display_name": display_name,
            "service_key": service_key,
            "status": status,
            "connection_id": connection_id,
        }
        if existing is None:
            conn.execute(
                text("""
                    INSERT INTO mcp_service
                        (service_id, tenant_id, display_name, service_key, status,
                         config_version)
                    VALUES (:service_id, :tenant_id, :display_name, :service_key,
                            :status, 1)
                """),
                params,
            )
        elif existing.display_name != display_name or existing.status != status:
            conn.execute(
                text("""
                    UPDATE mcp_service SET
                        display_name=:display_name,
                        status=:status,
                        config_version=config_version+1
                    WHERE service_id=:service_id
                """),
                params,
            )

        conn.execute(
            text("""
                DELETE FROM mcp_service_tool_binding
                WHERE service_id = :service_id
            """),
            {"service_id": service_id},
        )
        for tool in _enabled_tools(conn, connection, spec):
            conn.execute(
                text("""
                    INSERT INTO mcp_service_tool_binding
                        (binding_id, service_id, connection_id, source_tool_key,
                         tool_alias, binding_status, policy_json)
                    VALUES (:binding_id, :service_id, :connection_id,
                            :source_tool_key, :tool_alias, 'active', '{}')
                    ON DUPLICATE KEY UPDATE
                        binding_id=mcp_service_tool_binding.binding_id
                """),
                {
                    "binding_id": _default_binding_id(service_id, tool.tool_key),
                    "service_id": service_id,
                    "connection_id": connection_id,
                    "source_tool_key": tool.tool_key,
                    "tool_alias": tool.mcp_name,
                },
            )


def _write_default_service_watermark(connection_id: str) -> None:
    with _engine().begin() as conn:
        conn.execute(
            text("""
                INSERT INTO connection_sync_state
                    (connection_id, state_key, state_json, last_success_at,
                     last_error)
                VALUES (:connection_id, :state_key, :state_json,
                        UTC_TIMESTAMP(), '')
                ON DUPLICATE KEY UPDATE
                    state_json=VALUES(state_json),
                    last_success_at=VALUES(last_success_at),
                    last_error=''
            """),
            {
                "connection_id": connection_id,
                "state_key": _DEFAULT_SERVICE_WATERMARK_KEY,
                "state_json": json.dumps(
                    {"status": _DEFAULT_SERVICE_WATERMARK_STATUS},
                    separators=(",", ":"),
                ),
            },
        )


def migrate_default_services(registry: ConnectorRegistry, enabled: bool) -> int:
    """Backfill one deterministic, token-free service per supported connection."""
    if not isinstance(enabled, bool):
        raise TypeError("enabled must be a bool")
    if not enabled:
        return 0
    candidates = []
    for connection in _pending_default_connections():
        spec = registry.validated_spec(connection["connector_key"])
        if spec is not None:
            candidates.append((connection, spec))

    # Watermarks are deliberately deferred until every selected service
    # transaction has committed, so a partial batch is retryable.
    for connection, spec in candidates:
        _backfill_default_service(connection, spec)
    for connection, _spec in candidates:
        _write_default_service_watermark(connection["connection_id"])
    return len(candidates)


def register_service_cache_invalidator(invalidator: ServiceCacheInvalidator) -> None:
    if not callable(invalidator):
        raise TypeError("invalidator must be callable")
    with _service_cache_invalidator_lock:
        if not any(existing == invalidator for existing in _service_cache_invalidators):
            _service_cache_invalidators.append(invalidator)


def unregister_service_cache_invalidator(invalidator: ServiceCacheInvalidator) -> None:
    with _service_cache_invalidator_lock:
        _service_cache_invalidators[:] = [
            existing
            for existing in _service_cache_invalidators
            if existing != invalidator
        ]


def _registered_service_cache_invalidators() -> tuple[ServiceCacheInvalidator, ...]:
    with _service_cache_invalidator_lock:
        return tuple(_service_cache_invalidators)


def _invalidate_service_ids(
    service_ids: Sequence[str], invalidators: Sequence[ServiceCacheInvalidator]
) -> bool:
    succeeded = True
    for service_id in service_ids:
        for invalidator in invalidators:
            try:
                invalidator(service_id)
            except Exception as exc:
                succeeded = False
                logger.warning(
                    "MCP service cache invalidation hook failed type=%s",
                    type(exc).__name__,
                )
    return succeeded


def invalidate_service_cache(service_id: str) -> None:
    """Retire every registered cache entry for one exact service ID."""
    if not service_id:
        raise ValueError("service_id is required")
    _invalidate_service_ids((service_id,), _registered_service_cache_invalidators())


def _lock_live_tenant(conn: Any, tenant_id: str) -> None:
    enabled = conn.execute(
        text("""
            SELECT enabled FROM tenant_config
            WHERE tenant_id=:tenant_id
            LIMIT 1 FOR UPDATE
        """),
        {"tenant_id": tenant_id},
    ).scalar()
    if enabled != 1:
        raise ServiceOwnershipError("tenant is not active")


def _list_service_ids_for_tenant(tenant_id: str) -> list[str]:
    with _engine().connect() as conn:
        rows = conn.execute(
            text("""
                SELECT service_id FROM mcp_service
                WHERE tenant_id=:tenant_id
                ORDER BY service_id
            """),
            {"tenant_id": tenant_id},
        ).mappings().all()
    return [row["service_id"] for row in rows]


def list_service_ids_for_connection(connection_id: str) -> list[str]:
    if not connection_id:
        raise ValueError("connection_id is required")
    with _engine().connect() as conn:
        rows = conn.execute(
            text("""
                SELECT DISTINCT service_id
                FROM mcp_service_tool_binding
                WHERE connection_id=:connection_id
                ORDER BY service_id
            """),
            {"connection_id": connection_id},
        ).mappings().all()
    return [row["service_id"] for row in rows]


def _list_service_references(
    conn: Any,
    connection_id: str,
    tenant_id: str,
    *,
    for_update: bool = False,
) -> list[McpService]:
    lock_clause = " FOR UPDATE" if for_update else ""
    rows = conn.execute(
        text(f"""
            SELECT service.service_id, service.tenant_id, service.display_name,
                   service.service_key, service.status, service.config_version
            FROM mcp_service_tool_binding AS binding
            JOIN mcp_service AS service
              ON service.service_id=binding.service_id
            JOIN connection_instance AS connection_row
              ON connection_row.connection_id=binding.connection_id
            WHERE binding.connection_id=:connection_id
              AND connection_row.tenant_id=:tenant_id
              AND service.tenant_id=:tenant_id
            ORDER BY service.service_id{lock_clause}
        """),
        {"connection_id": connection_id, "tenant_id": tenant_id},
    ).mappings().all()
    references: list[McpService] = []
    seen: set[str] = set()
    for row in rows:
        item = _service_from_row(row)
        if item.service_id not in seen:
            seen.add(item.service_id)
            references.append(item)
    return references


def list_service_references(
    connection_id: str, tenant_id: str
) -> list[McpService]:
    """List tenant-owned services with any binding to one owned connection."""
    if not connection_id or not tenant_id:
        raise ValueError("connection_id and tenant_id are required")
    with _engine().connect() as conn:
        return _list_service_references(conn, connection_id, tenant_id)


def _connection_has_binding(
    conn: Any,
    connection_id: str,
    tenant_id: str,
    *,
    for_update: bool,
) -> bool:
    lock_clause = " FOR UPDATE" if for_update else ""
    value = conn.execute(
        text(f"""
            SELECT 1 FROM mcp_service_tool_binding AS binding
            JOIN connection_instance AS connection_row
              ON connection_row.connection_id=binding.connection_id
            WHERE binding.connection_id=:connection_id
              AND connection_row.tenant_id=:tenant_id
            LIMIT 1{lock_clause}
        """),
        {"connection_id": connection_id, "tenant_id": tenant_id},
    ).scalar()
    return value is not None


def assert_connection_deletable(
    connection_id: str,
    tenant_id: str,
    *,
    _connection: Any | None = None,
) -> None:
    """Reject deletion while an exact tenant-owned service binding exists."""
    if not connection_id or not tenant_id:
        raise ValueError("connection_id and tenant_id are required")
    if _connection is None:
        with _engine().connect() as conn:
            references = _list_service_references(conn, connection_id, tenant_id)
            has_binding = _connection_has_binding(
                conn,
                connection_id,
                tenant_id,
                for_update=False,
            )
    else:
        references = _list_service_references(
            _connection,
            connection_id,
            tenant_id,
            for_update=True,
        )
        has_binding = _connection_has_binding(
            _connection,
            connection_id,
            tenant_id,
            for_update=True,
        )
    if has_binding:
        raise ServiceReferenceConflictError(references)


def invalidate_services_for_connection(connection_id: str) -> None:
    """Invalidate referenced services, falling back to the owning tenant."""
    invalidators = _registered_service_cache_invalidators()
    if not invalidators:
        return
    tenant_id = None
    try:
        with _engine().connect() as conn:
            tenant_id = conn.execute(
                text("""
                    SELECT tenant_id FROM connection_instance
                    WHERE connection_id=:connection_id
                    LIMIT 1
                """),
                {"connection_id": connection_id},
            ).scalar()
        service_ids = list_service_ids_for_connection(connection_id)
        if _invalidate_service_ids(service_ids, invalidators):
            return
    except Exception as exc:
        logger.warning(
            "Exact MCP service cache invalidation failed type=%s",
            type(exc).__name__,
        )
    if not isinstance(tenant_id, str) or not tenant_id:
        return
    try:
        _invalidate_service_ids(
            _list_service_ids_for_tenant(tenant_id), invalidators
        )
    except Exception as exc:
        logger.warning(
            "Tenant MCP service cache invalidation failed type=%s",
            type(exc).__name__,
        )


def create_service(service: McpService) -> McpService:
    if not isinstance(service, McpService):
        raise TypeError("service must be an McpService")
    with _engine().begin() as conn:
        _lock_live_tenant(conn, service.tenant_id)
        conn.execute(
            text("""
                INSERT INTO mcp_service
                    (service_id, tenant_id, display_name, service_key, status,
                     config_version)
                VALUES (:service_id, :tenant_id, :display_name, :service_key,
                        :status, :config_version)
            """),
            {
                "service_id": service.service_id,
                "tenant_id": service.tenant_id,
                "display_name": service.display_name,
                "service_key": service.service_key,
                "status": service.status,
                "config_version": service.config_version,
            },
        )
    return service


def get_service(service_id: str, tenant_id: str) -> McpService | None:
    if not service_id or not tenant_id:
        raise ValueError("service_id and tenant_id are required")
    with _engine().connect() as conn:
        row = conn.execute(
            text("""
                SELECT service_id, tenant_id, display_name, service_key, status,
                       config_version
                FROM mcp_service
                WHERE service_id=:service_id AND tenant_id=:tenant_id
                LIMIT 1
            """),
            {"service_id": service_id, "tenant_id": tenant_id},
        ).fetchone()
    return _service_from_row(row) if row is not None else None


def list_services(tenant_id: str) -> list[McpService]:
    if not tenant_id:
        raise ValueError("tenant_id is required")
    with _engine().connect() as conn:
        rows = conn.execute(
            text("""
                SELECT service_id, tenant_id, display_name, service_key, status,
                       config_version
                FROM mcp_service
                WHERE tenant_id=:tenant_id
                ORDER BY created_at, service_id
            """),
            {"tenant_id": tenant_id},
        ).mappings().all()
    return [_service_from_row(row) for row in rows]


_SERVICE_STATUS_TRANSITIONS = {
    "draft": frozenset({"active", "disabled"}),
    "active": frozenset({"disabled"}),
    "disabled": frozenset({"draft", "active"}),
}


def update_service_status(
    service_id: str,
    tenant_id: str,
    status: str,
    *,
    expected_config_version: int,
) -> McpService:
    if not service_id or not tenant_id:
        raise ValueError("service_id and tenant_id are required")
    if status not in _SERVICE_STATUS_TRANSITIONS:
        raise ValueError("invalid service status")
    if (
        isinstance(expected_config_version, bool)
        or not isinstance(expected_config_version, int)
        or expected_config_version < 1
    ):
        raise ValueError("expected_config_version must be positive")
    with _engine().begin() as conn:
        if status == "active":
            _lock_live_tenant(conn, tenant_id)
        row = conn.execute(
            text("""
                SELECT service_id, tenant_id, display_name, service_key, status,
                       config_version
                FROM mcp_service
                WHERE service_id=:service_id AND tenant_id=:tenant_id
                LIMIT 1 FOR UPDATE
            """),
            {"service_id": service_id, "tenant_id": tenant_id},
        ).fetchone()
        if row is None:
            raise ServiceOwnershipError("service is not owned by tenant")
        current = _service_from_row(row)
        if current.config_version != expected_config_version:
            raise ServiceVersionConflictError
        if status not in _SERVICE_STATUS_TRANSITIONS[current.status]:
            raise ValueError("invalid service status transition")
        result = conn.execute(
            text("""
                UPDATE mcp_service
                SET status=:status, config_version=config_version+1
                WHERE service_id=:service_id AND tenant_id=:tenant_id
                  AND config_version=:expected_config_version
            """),
            {
                "service_id": service_id,
                "tenant_id": tenant_id,
                "status": status,
                "expected_config_version": expected_config_version,
            },
        )
        if result.rowcount != 1:
            raise ServiceVersionConflictError
    return replace(current, status=status, config_version=current.config_version + 1)


def issue_token(
    service_id: str,
    tenant_id: str,
    *,
    label: str = "",
    expires_at: datetime | None = None,
) -> IssuedServiceToken:
    if not service_id or not tenant_id:
        raise ValueError("service_id and tenant_id are required")
    if not isinstance(label, str) or len(label) > 128:
        raise ValueError("invalid token label")
    normalized_expiry = normalize_utc_datetime(expires_at)
    raw_value = f"mcp_svc_{secrets.token_urlsafe(32)}"
    digest = token_hmac(raw_value)
    issued = IssuedServiceToken(
        token_id=str(uuid.uuid4()),
        raw_value=raw_value,
        prefix=digest[:12],
    )
    ciphertext = encrypt_token(raw_value)
    with _engine().begin() as conn:
        _lock_live_tenant(conn, tenant_id)
        service_row = conn.execute(
            text("""
                SELECT service_id, tenant_id, display_name, service_key, status,
                       config_version, UTC_TIMESTAMP() AS db_now
                FROM mcp_service
                WHERE service_id=:service_id AND tenant_id=:tenant_id
                LIMIT 1 FOR UPDATE
            """),
            {"service_id": service_id, "tenant_id": tenant_id},
        ).fetchone()
        if service_row is None:
            raise ServiceOwnershipError("service is not owned by tenant")
        service = _service_from_row(service_row)
        if service.status != "active":
            raise ValueError("service must be active to issue a token")
        values = (
            dict(service_row._mapping)
            if hasattr(service_row, "_mapping")
            else dict(service_row)
        )
        db_now = normalize_utc_datetime(values["db_now"])
        if normalized_expiry is not None and normalized_expiry <= db_now:
            raise ValueError("expires_at must be in the future")
        conn.execute(
            text("""
                INSERT INTO mcp_service_token
                    (token_id, service_id, token_hmac, encrypted_token,
                     token_prefix, token_label, expires_at)
                VALUES (:token_id, :service_id, :token_hmac, :encrypted_token,
                        :token_prefix, :token_label, :expires_at)
            """),
            {
                "token_id": issued.token_id,
                "service_id": service_id,
                "token_hmac": digest,
                "encrypted_token": ciphertext,
                "token_prefix": issued.prefix,
                "token_label": label,
                "expires_at": normalized_expiry,
            },
        )
    return issued


def resolve_token(raw_token: str, service_id: str) -> McpService | None:
    if not service_id:
        raise ValueError("service_id is required")
    digest = token_hmac(raw_token)
    service = None
    with _engine().begin() as conn:
        candidate = conn.execute(
            text("""
                SELECT s.tenant_id
                FROM mcp_service_token t
                JOIN mcp_service s ON s.service_id=t.service_id
                JOIN tenant_config tenant_row ON tenant_row.tenant_id=s.tenant_id
                WHERE t.service_id=:service_id AND t.token_hmac=:token_hmac
                  AND t.revoked_at IS NULL
                  AND (t.expires_at IS NULL OR t.expires_at > UTC_TIMESTAMP())
                  AND s.status='active'
                  AND tenant_row.enabled=1
                LIMIT 1
            """),
            {"service_id": service_id, "token_hmac": digest},
        ).fetchone()
        if candidate is None:
            return None
        candidate_values = (
            dict(candidate._mapping)
            if hasattr(candidate, "_mapping")
            else dict(candidate)
        )
        tenant_id = candidate_values.get("tenant_id")
        if not isinstance(tenant_id, str) or not tenant_id:
            return None
        try:
            _lock_live_tenant(conn, tenant_id)
        except ServiceOwnershipError:
            return None
        service_row = conn.execute(
            text("""
                SELECT service_id, tenant_id, display_name, service_key, status,
                       config_version
                FROM mcp_service
                WHERE service_id=:service_id AND tenant_id=:tenant_id
                  AND status='active'
                LIMIT 1 FOR UPDATE
            """),
            {"service_id": service_id, "tenant_id": tenant_id},
        ).fetchone()
        if service_row is None:
            return None
        token_row = conn.execute(
            text("""
                SELECT token_id FROM mcp_service_token
                WHERE service_id=:service_id AND token_hmac=:token_hmac
                  AND revoked_at IS NULL
                  AND (expires_at IS NULL OR expires_at > UTC_TIMESTAMP())
                LIMIT 1 FOR UPDATE
            """),
            {"service_id": service_id, "token_hmac": digest},
        ).fetchone()
        if token_row is None:
            return None
        token_values = (
            dict(token_row._mapping) if hasattr(token_row, "_mapping") else dict(token_row)
        )
        conn.execute(
            text("""
                UPDATE mcp_service_token
                SET last_used_at=UTC_TIMESTAMP()
                WHERE token_id=:token_id AND service_id=:service_id
                  AND token_hmac=:token_hmac
                  AND revoked_at IS NULL
                  AND (expires_at IS NULL OR expires_at > UTC_TIMESTAMP())
            """),
            {
                "token_id": token_values["token_id"],
                "service_id": service_id,
                "token_hmac": digest,
            },
        )
        service = _service_from_row(service_row)
    return service


def reveal_token(service_id: str, tenant_id: str, token_id: str) -> str:
    if not service_id or not tenant_id or not token_id:
        raise ValueError("service_id, tenant_id, and token_id are required")
    with _engine().connect() as conn:
        row = conn.execute(
            text("""
                SELECT t.encrypted_token
                FROM mcp_service_token t
                JOIN mcp_service s ON s.service_id=t.service_id
                WHERE t.token_id=:token_id AND t.service_id=:service_id
                  AND s.tenant_id=:tenant_id
                  AND t.revoked_at IS NULL
                  AND (t.expires_at IS NULL OR t.expires_at > UTC_TIMESTAMP())
                  AND t.encrypted_token IS NOT NULL
                LIMIT 1
            """),
            {
                "token_id": token_id,
                "service_id": service_id,
                "tenant_id": tenant_id,
            },
        ).fetchone()
    if row is None:
        raise TokenUnavailableError("service token is unavailable")
    values = dict(row._mapping) if hasattr(row, "_mapping") else dict(row)
    return decrypt_token(values["encrypted_token"])


def list_tokens(service_id: str, tenant_id: str) -> list[ServiceTokenMetadata]:
    if not service_id or not tenant_id:
        raise ValueError("service_id and tenant_id are required")
    if get_service(service_id, tenant_id) is None:
        raise ServiceOwnershipError("service is not owned by tenant")
    with _engine().connect() as conn:
        rows = conn.execute(
            text("""
                SELECT t.token_id, t.token_prefix AS prefix,
                       t.token_label AS label, t.expires_at, t.revoked_at,
                       t.last_used_at, t.created_at
                FROM mcp_service_token t
                JOIN mcp_service s ON s.service_id=t.service_id
                WHERE t.service_id=:service_id AND s.tenant_id=:tenant_id
                ORDER BY t.created_at, t.token_id
            """),
            {"service_id": service_id, "tenant_id": tenant_id},
        ).mappings().all()
    return [ServiceTokenMetadata(**dict(row)) for row in rows]


def revoke_token(service_id: str, tenant_id: str, token_id: str) -> bool:
    if not service_id or not tenant_id or not token_id:
        raise ValueError("service_id, tenant_id, and token_id are required")
    with _engine().begin() as conn:
        result = conn.execute(
            text("""
                UPDATE mcp_service_token
                SET revoked_at=UTC_TIMESTAMP(), encrypted_token=NULL
                WHERE token_id=:token_id AND service_id=:service_id
                  AND EXISTS (
                    SELECT 1 FROM mcp_service
                    WHERE service_id=:service_id AND tenant_id=:tenant_id
                  )
            """),
            {
                "token_id": token_id,
                "service_id": service_id,
                "tenant_id": tenant_id,
            },
        )
    return result.rowcount == 1


def _validate_snapshot(
    service_id: str, bindings: Sequence[ServiceToolBinding]
) -> tuple[ServiceToolBinding, ...]:
    if isinstance(bindings, (str, bytes)) or not isinstance(bindings, Sequence):
        raise TypeError("bindings must be a sequence")
    snapshot = tuple(bindings)
    binding_ids: set[str] = set()
    aliases: set[str] = set()
    sources: set[tuple[str, str]] = set()
    for binding in snapshot:
        if not isinstance(binding, ServiceToolBinding):
            raise TypeError("bindings must contain ServiceToolBinding values")
        if binding.service_id != service_id:
            raise ValueError("binding service_id does not match service")
        binding_identity = binding.binding_id.casefold()
        alias_identity = binding.tool_alias.casefold()
        source_identity = (
            binding.connection_id.casefold(),
            binding.source_tool_key.casefold(),
        )
        if binding_identity in binding_ids:
            raise ValueError("duplicate binding_id")
        if alias_identity in aliases:
            raise ValueError("duplicate tool_alias")
        if source_identity in sources:
            raise ValueError("duplicate connection source_tool_key")
        binding_ids.add(binding_identity)
        aliases.add(alias_identity)
        sources.add(source_identity)
    return snapshot


def _lock_connections(conn: Any, connection_ids: set[str], tenant_id: str) -> None:
    if not connection_ids:
        return
    params: dict[str, str] = {}
    placeholders = []
    for index, connection_id in enumerate(sorted(connection_ids)):
        key = f"connection_id_{index}"
        params[key] = connection_id
        placeholders.append(f":{key}")
    rows = conn.execute(
        text(f"""
            SELECT connection_id, tenant_id
            FROM connection_instance
            WHERE connection_id IN ({', '.join(placeholders)})
            FOR UPDATE
        """),
        params,
    ).mappings().all()
    owned = {
        row["connection_id"]
        for row in rows
        if row["tenant_id"] == tenant_id
    }
    if owned != connection_ids:
        raise ServiceOwnershipError("binding connection is not owned by tenant")


def _assert_management_binding_transitions(
    conn: Any, service_id: str, snapshot: tuple[ServiceToolBinding, ...]
) -> None:
    rows = conn.execute(
        text("""
            SELECT binding_id, binding_status
            FROM mcp_service_tool_binding
            WHERE service_id=:service_id
            FOR UPDATE
        """),
        {"service_id": service_id},
    ).mappings().all()
    previous = {row["binding_id"]: row["binding_status"] for row in rows}
    for binding in snapshot:
        prior_status = previous.get(binding.binding_id)
        if binding.binding_status == "broken" and prior_status != "broken":
            raise ValueError("only domain validation may mark a binding broken")
        if prior_status == "broken" and binding.binding_status == "disabled":
            raise ValueError("a broken binding must be explicitly restored to active")


def replace_bindings(
    service_id: str,
    tenant_id: str,
    bindings: Sequence[ServiceToolBinding],
    expected_config_version: int,
) -> McpService:
    if not service_id or not tenant_id:
        raise ValueError("service_id and tenant_id are required")
    if (
        isinstance(expected_config_version, bool)
        or not isinstance(expected_config_version, int)
        or expected_config_version < 1
    ):
        raise ValueError("expected_config_version must be positive")
    snapshot = _validate_snapshot(service_id, bindings)
    with _engine().begin() as conn:
        _lock_connections(
            conn,
            {binding.connection_id for binding in snapshot},
            tenant_id,
        )
        row = conn.execute(
            text("""
                SELECT service_id, tenant_id, display_name, service_key, status,
                       config_version
                FROM mcp_service
                WHERE service_id=:service_id
                LIMIT 1 FOR UPDATE
            """),
            {"service_id": service_id},
        ).fetchone()
        if row is None:
            raise ServiceOwnershipError("service is not owned by tenant")
        current = _service_from_row(row)
        if current.tenant_id != tenant_id:
            raise ServiceOwnershipError("service is not owned by tenant")
        if current.config_version != expected_config_version:
            raise ServiceVersionConflictError
        if current.status == "disabled":
            raise ValueError("disabled service bindings cannot be changed")
        _assert_management_binding_transitions(conn, service_id, snapshot)
        conn.execute(
            text("DELETE FROM mcp_service_tool_binding WHERE service_id=:service_id"),
            {"service_id": service_id},
        )
        for binding in snapshot:
            conn.execute(
                text("""
                    INSERT INTO mcp_service_tool_binding
                        (binding_id, service_id, connection_id, source_tool_key,
                         tool_alias, binding_status, policy_json)
                    VALUES (:binding_id, :service_id, :connection_id,
                            :source_tool_key, :tool_alias, :binding_status,
                            :policy_json)
                """),
                {
                    "binding_id": binding.binding_id,
                    "service_id": binding.service_id,
                    "connection_id": binding.connection_id,
                    "source_tool_key": binding.source_tool_key,
                    "tool_alias": binding.tool_alias,
                    "binding_status": binding.binding_status,
                    "policy_json": json.dumps(
                        dict(binding.policy),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                },
            )
        result = conn.execute(
            text("""
                UPDATE mcp_service SET config_version=config_version+1
                WHERE service_id=:service_id
                  AND config_version=:expected_config_version
            """),
            {
                "service_id": service_id,
                "expected_config_version": expected_config_version,
            },
        )
        if result.rowcount != 1:
            raise ServiceVersionConflictError
    return replace(current, config_version=current.config_version + 1)


def list_bindings(service_id: str, tenant_id: str) -> list[ServiceToolBinding]:
    if not service_id or not tenant_id:
        raise ValueError("service_id and tenant_id are required")
    with _engine().connect() as conn:
        service_row = conn.execute(
            text("""
                SELECT service_id, tenant_id, display_name, service_key, status,
                       config_version
                FROM mcp_service
                WHERE service_id=:service_id AND tenant_id=:tenant_id
                LIMIT 1
            """),
            {"service_id": service_id, "tenant_id": tenant_id},
        ).fetchone()
        if service_row is None:
            raise ServiceOwnershipError("service is not owned by tenant")
        rows = conn.execute(
            text("""
                SELECT binding_id, service_id, connection_id, source_tool_key,
                       tool_alias, binding_status, policy_json
                FROM mcp_service_tool_binding
                WHERE service_id=:service_id
                ORDER BY created_at, binding_id
            """),
            {"service_id": service_id},
        ).mappings().all()
    return [_binding_from_row(row) for row in rows]
