"""Read-only tenant console adapters with server-owned tenant scope."""
from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request

from .connections import store as connection_store
from .mcp_log_models import LogFilters
from . import mcp_log_store
from .mcp_logs_admin import safe_log_list, safe_log_stats
from .tenant_auth.dependencies import require_tenant_principal


router = APIRouter(prefix="/tenant", tags=["tenant-console"])
logger = logging.getLogger("wecom-gateway")

TenantLogStatus = Literal["ok", "partial", "error", "denied"]
_OVERVIEW_QUERY_KEYS = frozenset()
_CONNECTION_QUERY_KEYS = frozenset()
_LOG_QUERY_KEYS = frozenset(
    {
        "connection_id",
        "source_tool_key",
        "status",
        "page",
        "page_size",
    }
)


async def _validate_read_request(
    request: Request, allowed_query_keys: frozenset[str]
) -> None:
    if await request.body():
        raise HTTPException(422, "tenant read routes do not accept a body")

    seen: set[str] = set()
    for key, _value in request.query_params.multi_items():
        if key not in allowed_query_keys or key in seen:
            raise HTTPException(422, "ambiguous tenant console query")
        seen.add(key)


def _store_call(operation, *args):
    try:
        return operation(*args)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Tenant console storage operation failed type=%s", type(exc).__name__)
        raise HTTPException(500, "租户控制台暂不可用") from exc


def _connection_item(record: Any) -> dict[str, Any]:
    public_config = getattr(record, "public_config", {})
    if not isinstance(public_config, dict):
        raise TypeError("connection public_config must be an object")
    return {
        "connection_id": record.connection_id,
        "tenant_id": record.tenant_id,
        "connector_key": record.connector_key,
        "connection_alias": getattr(record, "connection_alias", ""),
        "display_name": record.display_name,
        "status": record.status,
        "data_mode": record.data_mode,
        "public_config": dict(public_config),
        "config_version": record.config_version,
    }


@router.get("/connections", dependencies=[])
async def tenant_connections(request: Request):
    principal = require_tenant_principal(request)
    await _validate_read_request(request, _CONNECTION_QUERY_KEYS)
    records = _store_call(connection_store.list_connections, principal.tenant_id)
    try:
        return {"items": [_connection_item(record) for record in records]}
    except Exception as exc:
        logger.error("Tenant connection projection failed type=%s", type(exc).__name__)
        raise HTTPException(500, "租户控制台暂不可用") from exc


@router.get("/mcp-logs", dependencies=[])
async def tenant_logs(
    request: Request,
    connection_id: Annotated[str | None, Query(max_length=64)] = None,
    source_tool_key: Annotated[str | None, Query(max_length=128)] = None,
    status: Annotated[TenantLogStatus | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    principal = require_tenant_principal(request)
    await _validate_read_request(request, _LOG_QUERY_KEYS)
    try:
        filters = LogFilters(
            tenant_id=principal.tenant_id,
            connection_id=connection_id,
            tool_key=source_tool_key,
            status=status or "",
        )
    except (TypeError, ValueError) as exc:
        raise HTTPException(422, "无效的日志筛选条件") from exc
    result = _store_call(mcp_log_store.list_logs, filters, page, page_size)
    try:
        return safe_log_list(result, page, page_size)
    except Exception as exc:
        logger.error("Tenant log projection failed type=%s", type(exc).__name__)
        raise HTTPException(500, "租户控制台暂不可用") from exc


@router.get("/overview", dependencies=[])
async def tenant_overview(request: Request):
    principal = require_tenant_principal(request)
    await _validate_read_request(request, _OVERVIEW_QUERY_KEYS)
    tenant_id = principal.tenant_id
    connections = _store_call(connection_store.list_connections, tenant_id)
    log_stats = _store_call(
        mcp_log_store.get_log_stats,
        LogFilters(tenant_id=tenant_id),
    )
    try:
        return {
            "tenant_id": tenant_id,
            "connections": {
                "total": len(connections),
                "active": sum(item.status == "active" for item in connections),
            },
            "mcp": {
                "total": len(connections),
                "active": sum(item.status == "active" for item in connections),
            },
            "logs": safe_log_stats(log_stats),
        }
    except Exception as exc:
        logger.error("Tenant overview projection failed type=%s", type(exc).__name__)
        raise HTTPException(500, "租户控制台暂不可用") from exc
