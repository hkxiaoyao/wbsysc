"""Atomic, fail-closed retirement of a tenant's live authorization state."""
from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import Any, Callable

from sqlalchemy import text

from .connections import store as connection_store
from .db import get_engine
from .mcp_log_models import McpLogEvent
from .mcp_log_store import insert_event
from .mcp_services import store as service_store
from .tenant_auth import store as tenant_auth_store


logger = logging.getLogger(__name__)
_SAFE_REQUEST_METADATA = re.compile(r"[A-Za-z0-9_.:-]*\Z")
_SAFE_METHOD = re.compile(r"[A-Z]*\Z")


class TenantNotFoundError(LookupError):
    """The exact tenant authorization root did not exist at lock time."""


@dataclass(frozen=True)
class TenantRetirement:
    tenant_id: str
    connection_versions: tuple[tuple[str, int], ...]
    service_ids: tuple[str, ...]
    service_token_count: int
    connection_token_count: int


def _mapping(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    mapping = getattr(row, "_mapping", None)
    if mapping is not None:
        return dict(mapping)
    return {"tenant_id": row[0], "schema_name": row[1]}


def _rows(result: Any) -> list[Any]:
    mappings = getattr(result, "mappings", None)
    if callable(mappings):
        mapped = mappings()
        all_rows = getattr(mapped, "all", None)
        if callable(all_rows):
            return list(all_rows())
    fetchall = getattr(result, "fetchall", None)
    return list(fetchall()) if callable(fetchall) else []


def _safe_metadata(value: Any, maximum: int, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str):
        return ""
    bounded = value[:maximum]
    return bounded if pattern.fullmatch(bounded) is not None else ""


def retire_tenant(
    tenant_id: str,
    *,
    request_id: str = "",
    client_ip: str = "",
    http_method: str = "DELETE",
) -> TenantRetirement:
    """Commit all credential retirement or leave every lifecycle row unchanged."""
    if not isinstance(tenant_id, str) or not tenant_id:
        raise ValueError("tenant_id is required")
    audit_request_id = _safe_metadata(
        request_id, 64, _SAFE_REQUEST_METADATA
    )
    audit_client_ip = _safe_metadata(client_ip, 64, _SAFE_REQUEST_METADATA)
    audit_method = _safe_metadata(http_method, 16, _SAFE_METHOD) or "DELETE"

    with get_engine().begin() as conn:
        tenant_row = conn.execute(
            text("""
                SELECT tenant_id, schema_name FROM tenant_config
                WHERE tenant_id=:tenant_id
                LIMIT 1 FOR UPDATE
            """),
            {"tenant_id": tenant_id},
        ).fetchone()
        if tenant_row is None:
            raise TenantNotFoundError

        connection_rows = _rows(conn.execute(
            text("""
                SELECT connection_id, config_version FROM connection_instance
                WHERE tenant_id=:tenant_id
                ORDER BY connection_id
                FOR UPDATE
            """),
            {"tenant_id": tenant_id},
        ))
        service_rows = _rows(conn.execute(
            text("""
                SELECT service_id, config_version FROM mcp_service
                WHERE tenant_id=:tenant_id
                ORDER BY service_id
                FOR UPDATE
            """),
            {"tenant_id": tenant_id},
        ))
        connections = tuple(
            (str(_mapping(row)["connection_id"]), int(_mapping(row)["config_version"]))
            for row in connection_rows
        )
        services = tuple(str(_mapping(row)["service_id"]) for row in service_rows)

        conn.execute(
            text("""
                UPDATE mcp_service
                SET status='disabled', config_version=config_version+1
                WHERE tenant_id=:tenant_id AND status<>'disabled'
            """),
            {"tenant_id": tenant_id},
        )
        service_tokens = conn.execute(
            text("""
                UPDATE mcp_service_token AS token_row
                JOIN mcp_service AS service_row
                  ON service_row.service_id=token_row.service_id
                SET token_row.revoked_at=COALESCE(
                      token_row.revoked_at, UTC_TIMESTAMP()),
                    token_row.encrypted_token=NULL
                WHERE service_row.tenant_id=:tenant_id
                  AND (token_row.revoked_at IS NULL
                       OR token_row.encrypted_token IS NOT NULL)
            """),
            {"tenant_id": tenant_id},
        )
        conn.execute(
            text("""
                UPDATE connection_instance
                SET status='disabled', config_version=config_version+1
                WHERE tenant_id=:tenant_id AND status<>'disabled'
            """),
            {"tenant_id": tenant_id},
        )
        connection_tokens = conn.execute(
            text("""
                UPDATE connection_token AS token_row
                JOIN connection_instance AS connection_row
                  ON connection_row.connection_id=token_row.connection_id
                SET token_row.revoked_at=COALESCE(
                      token_row.revoked_at, UTC_TIMESTAMP())
                WHERE connection_row.tenant_id=:tenant_id
                  AND token_row.revoked_at IS NULL
            """),
            {"tenant_id": tenant_id},
        )
        tenant_auth_store.delete_account(tenant_id, conn=conn)
        conn.execute(
            text("DELETE FROM domain_verify_file WHERE tenant_id=:tenant_id"),
            {"tenant_id": tenant_id},
        )

        service_token_count = int(getattr(service_tokens, "rowcount", 0) or 0)
        connection_token_count = int(
            getattr(connection_tokens, "rowcount", 0) or 0
        )
        insert_event(
            McpLogEvent(
                tenant_id=tenant_id,
                category="protocol",
                event_name="tenant_deleted",
                params_summary=(
                    f"services={len(services)},connections={len(connections)},"
                    f"service_tokens={service_token_count},"
                    f"connection_tokens={connection_token_count}"
                ),
                result_status="ok",
                request_id=audit_request_id,
                client_ip=audit_client_ip,
                http_method=audit_method,
                http_status=200,
            ),
            conn=conn,
        )
        deleted = conn.execute(
            text("DELETE FROM tenant_config WHERE tenant_id=:tenant_id"),
            {"tenant_id": tenant_id},
        )
        if getattr(deleted, "rowcount", 0) != 1:
            raise RuntimeError("tenant retirement failed closed")

    return TenantRetirement(
        tenant_id=tenant_id,
        connection_versions=connections,
        service_ids=services,
        service_token_count=service_token_count,
        connection_token_count=connection_token_count,
    )


def invalidate_retired_tenant(
    retirement: TenantRetirement,
    *,
    reload_tenants: Callable[[], None],
) -> None:
    """Best-effort cleanup after the authoritative transaction has committed."""
    for connection_id, config_version in retirement.connection_versions:
        try:
            connection_store.invalidate_connection_cache(
                connection_id, config_version
            )
        except Exception as exc:
            logger.warning(
                "Tenant connection cache invalidation failed type=%s",
                type(exc).__name__,
            )
    for service_id in retirement.service_ids:
        try:
            service_store.invalidate_service_cache(service_id)
        except Exception as exc:
            logger.warning(
                "Tenant service cache invalidation failed type=%s",
                type(exc).__name__,
            )
    try:
        reload_tenants()
    except Exception as exc:
        logger.warning("Tenant cache reload failed type=%s", type(exc).__name__)
