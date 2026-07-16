"""Central, parameterized storage operations for MCP call logs."""
from __future__ import annotations

import logging
import math
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import bindparam, text

from .mcp_log_models import DeleteSpec, LogFilters, McpLogEvent


logger = logging.getLogger(__name__)

_RETENTION_KEY = "mcp_log_retention_days"
_LEGACY_MIGRATION_KEY = "mcp_log_legacy_migration_v2"
_LEGACY_MIGRATION_VALUE = "completed"
_DEFAULT_RETENTION_DAYS = 90
_MAX_DELETE_BATCH = 5000

_LOG_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS `mcp_call_log` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `tenant_id` VARCHAR(64) NOT NULL DEFAULT '',
  `connection_id` VARCHAR(64) NULL,
  `connector_key` VARCHAR(64) NULL,
  `tool_key` VARCHAR(128) NULL,
  `category` VARCHAR(16) NOT NULL,
  `event_name` VARCHAR(96) NOT NULL,
  `target` VARCHAR(256) NOT NULL DEFAULT '',
  `params_summary` VARCHAR(512) NOT NULL DEFAULT '',
  `result_status` VARCHAR(16) NOT NULL,
  `error_code` VARCHAR(64) NOT NULL DEFAULT '',
  `error_summary` VARCHAR(256) NOT NULL DEFAULT '',
  `cost_ms` INT NOT NULL DEFAULT 0,
  `request_id` VARCHAR(64) NOT NULL DEFAULT '',
  `client_ip` VARCHAR(64) NOT NULL DEFAULT '',
  `http_method` VARCHAR(16) NOT NULL DEFAULT '',
  `http_status` SMALLINT NOT NULL DEFAULT 0,
  `legacy_schema` VARCHAR(64) NULL,
  `legacy_id` BIGINT NULL,
  `created_at` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
  PRIMARY KEY (`id`),
  KEY `idx_mcp_log_tenant_created` (`tenant_id`, `created_at`, `id`),
  KEY `idx_mcp_log_connection_created` (`tenant_id`, `connection_id`, `created_at`, `id`),
  KEY `idx_mcp_log_connector_created` (`connector_key`, `created_at`, `id`),
  KEY `idx_mcp_log_tool_created` (`tool_key`, `created_at`, `id`),
  KEY `idx_mcp_log_created` (`created_at`, `id`),
  KEY `idx_mcp_log_event` (`category`, `event_name`, `created_at`),
  KEY `idx_mcp_log_status` (`result_status`, `created_at`),
  KEY `idx_mcp_log_request` (`request_id`),
  KEY `idx_mcp_log_ip_created` (`client_ip`, `created_at`),
  KEY `idx_mcp_log_cost_created` (`cost_ms`, `created_at`),
  UNIQUE KEY `uk_mcp_log_legacy` (`legacy_schema`, `legacy_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

_SETTING_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS `gateway_setting` (
  `setting_key` VARCHAR(64) NOT NULL,
  `setting_value` VARCHAR(255) NOT NULL,
  `updated_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
    ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`setting_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

_SAFE_COLUMNS = """id, tenant_id, category, event_name, target, params_summary,
connection_id, connector_key, tool_key, result_status, error_code, error_summary,
cost_ms, request_id, client_ip, http_method, http_status, created_at"""
_TREND_BUCKET_EXPRESSION = "DATE_FORMAT(created_at, '%Y-%m-%d %H:00:00')"

_DIMENSION_COLUMN_DDLS = {
    "connection_id": "ADD COLUMN `connection_id` VARCHAR(64) NULL AFTER `tenant_id`",
    "connector_key": "ADD COLUMN `connector_key` VARCHAR(64) NULL AFTER `connection_id`",
    "tool_key": "ADD COLUMN `tool_key` VARCHAR(128) NULL AFTER `connector_key`",
}
_DIMENSION_INDEX_DDLS = {
    "idx_mcp_log_connection_created": (
        "ADD KEY `idx_mcp_log_connection_created` "
        "(`tenant_id`, `connection_id`, `created_at`, `id`)"
    ),
    "idx_mcp_log_connector_created": (
        "ADD KEY `idx_mcp_log_connector_created` "
        "(`connector_key`, `created_at`, `id`)"
    ),
    "idx_mcp_log_tool_created": (
        "ADD KEY `idx_mcp_log_tool_created` "
        "(`tool_key`, `created_at`, `id`)"
    ),
}


def _engine():
    # Local import prevents app.db -> this module startup migration cycles.
    from .db import get_engine

    return get_engine()


def ensure_central_log_tables() -> None:
    with _engine().begin() as conn:
        conn.execute(text(_LOG_TABLE_DDL))
        conn.execute(text(_SETTING_TABLE_DDL))
        existing_columns = _mysql_names(conn, "SHOW COLUMNS FROM `mcp_call_log`", "Field", 0)
        for name, ddl in _DIMENSION_COLUMN_DDLS.items():
            if name not in existing_columns:
                conn.execute(text(f"ALTER TABLE `mcp_call_log` {ddl}"))
        existing_indexes = _mysql_names(conn, "SHOW INDEX FROM `mcp_call_log`", "Key_name", 2)
        for name, ddl in _DIMENSION_INDEX_DDLS.items():
            if name not in existing_indexes:
                conn.execute(text(f"ALTER TABLE `mcp_call_log` {ddl}"))


def _mysql_names(conn: Any, statement: str, mapping_key: str, tuple_index: int) -> set[str]:
    """Read MySQL metadata without MySQL-8-only `IF NOT EXISTS` syntax."""
    rows = conn.execute(text(statement)).fetchall()
    names: set[str] = set()
    for row in rows:
        try:
            values = _mapping(row)
            value = values.get(mapping_key)
        except (TypeError, ValueError):
            try:
                value = row[tuple_index]
            except (IndexError, KeyError, TypeError):
                continue
        if isinstance(value, str) and value:
            names.add(value)
    return names


def _legacy_default_connection_id(tenant_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"wbsysc:legacy-wecom:{tenant_id}"))


def migrate_legacy_logs(days: int = 90) -> None:
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 3650:
        raise ValueError("days must be an integer between 1 and 3650")

    from .db import _valid_tenant_schema

    with _engine().connect() as conn:
        completed = conn.execute(
            text(
                "SELECT setting_value FROM gateway_setting "
                "WHERE setting_key=:setting_key"
            ),
            {"setting_key": _LEGACY_MIGRATION_KEY},
        ).scalar()
        if completed == _LEGACY_MIGRATION_VALUE:
            return
        tenants = conn.execute(
            text("SELECT tenant_id, schema_name, created_at FROM tenant_config")
        ).fetchall()

    if not tenants:
        return
    all_succeeded = True
    for tenant_id, schema_name, tenant_created_at in tenants:
        if not _valid_tenant_schema(schema_name):
            all_succeeded = False
            logger.warning("Skipping invalid legacy audit schema for tenant_id=%s", tenant_id)
            continue
        statement = text(f"""
            INSERT INTO mcp_call_log
              (tenant_id, connection_id, connector_key, tool_key, category,
               event_name, target, params_summary, result_status, error_code,
               error_summary, cost_ms, request_id, client_ip, http_method,
               http_status, legacy_schema, legacy_id, created_at)
            SELECT :tenant_id,
                   connection_map.mapped_connection_id,
                   CASE WHEN connection_map.mapped_connection_id IS NULL THEN NULL ELSE 'wecom' END,
                   CASE WHEN connection_map.mapped_connection_id IS NULL THEN NULL
                        ELSE CASE legacy.tool_name
                          WHEN 'wecom_list_reports' THEN 'reports.list'
                          WHEN 'wecom_get_report' THEN 'reports.get'
                          WHEN 'wecom_list_approvals' THEN 'approvals.list'
                          WHEN 'wecom_get_approval_detail' THEN 'approvals.get'
                          WHEN 'wecom_list_checkins' THEN 'checkins.list'
                          WHEN 'wecom_list_smart_table_records' THEN 'smart_tables.records.list'
                          ELSE NULL
                        END
                   END,
                   'tool', legacy.tool_name, legacy.target, legacy.params_summary,
                    CASE
                      WHEN legacy.result_status IN ('ok','partial','error','denied')
                        THEN legacy.result_status
                      ELSE 'error'
                    END,
                    '', '', GREATEST(legacy.cost_ms, 0), '', '', '', 0,
                    :legacy_schema, legacy.id, legacy.created_at
             FROM `{schema_name}`.`audit_log` AS legacy
             LEFT JOIN (
               SELECT connection_id AS mapped_connection_id
               FROM connection_instance
               WHERE connection_id=:default_connection_id
                 AND tenant_id=:tenant_id
                 AND connector_key='wecom'
               LIMIT 1
             ) AS connection_map ON 1=1
             WHERE legacy.created_at >= UTC_TIMESTAMP() - INTERVAL {days} DAY
               AND legacy.created_at >= :tenant_created_at
             ON DUPLICATE KEY UPDATE
               connection_id=COALESCE(connection_id, VALUES(connection_id)),
               connector_key=COALESCE(connector_key, VALUES(connector_key)),
               tool_key=COALESCE(tool_key, VALUES(tool_key))
        """)
        try:
            with _engine().begin() as conn:
                conn.execute(
                    statement,
                    {
                        "tenant_id": tenant_id,
                        "legacy_schema": schema_name,
                        "tenant_created_at": tenant_created_at,
                        "default_connection_id": _legacy_default_connection_id(tenant_id),
                    },
                )
        except Exception:
            all_succeeded = False
            logger.exception(
                "Legacy MCP audit migration failed tenant_id=%s schema=%s",
                tenant_id,
                schema_name,
            )

    if not all_succeeded:
        return
    with _engine().begin() as conn:
        conn.execute(
            text("""
                INSERT INTO gateway_setting (setting_key, setting_value)
                VALUES (:setting_key, :setting_value)
                ON DUPLICATE KEY UPDATE setting_value=VALUES(setting_value)
            """),
            {
                "setting_key": _LEGACY_MIGRATION_KEY,
                "setting_value": _LEGACY_MIGRATION_VALUE,
            },
        )


def insert_event(event: McpLogEvent) -> None:
    if not isinstance(event, McpLogEvent):
        raise TypeError("event must be McpLogEvent")
    statement = text("""
        INSERT INTO mcp_call_log
          (tenant_id, connection_id, connector_key, tool_key, category,
           event_name, target, params_summary, result_status, error_code,
           error_summary, cost_ms, request_id, client_ip, http_method,
           http_status, created_at)
        VALUES
          (:tenant_id, :connection_id, :connector_key, :tool_key, :category,
           :event_name, :target, :params_summary, :result_status, :error_code,
           :error_summary, :cost_ms, :request_id, :client_ip, :http_method,
           :http_status,
           COALESCE(:created_at, UTC_TIMESTAMP(6)))
    """)
    params = {
        "tenant_id": event.tenant_id,
        "connection_id": event.connection_id,
        "connector_key": event.connector_key,
        "tool_key": event.tool_key,
        "category": event.category,
        "event_name": event.event_name,
        "target": event.target,
        "params_summary": event.params_summary,
        "result_status": event.result_status,
        "error_code": event.error_code,
        "error_summary": event.error_summary,
        "cost_ms": event.cost_ms,
        "request_id": event.request_id,
        "client_ip": event.client_ip,
        "http_method": event.http_method,
        "http_status": event.http_status,
        "created_at": event.created_at,
    }
    with _engine().begin() as conn:
        conn.execute(statement, params)


def _escaped_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _filter_where(filters: LogFilters) -> tuple[list[str], dict[str, Any]]:
    if not isinstance(filters, LogFilters):
        raise TypeError("filters must be LogFilters")
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if filters.tenant_id is not None:
        clauses.append("tenant_id = :tenant_id")
        params["tenant_id"] = filters.tenant_id
    for attribute, column in (
        ("connection_id", "connection_id"),
        ("connector_key", "connector_key"),
        ("tool_key", "tool_key"),
    ):
        value = getattr(filters, attribute)
        if value is not None:
            clauses.append(f"{column} = :{attribute}")
            params[attribute] = value
    direct_fields = (
        ("category", "category"),
        ("event_name", "event_name"),
        ("status", "result_status"),
        ("request_id", "request_id"),
        ("client_ip", "client_ip"),
    )
    for attribute, column in direct_fields:
        value = getattr(filters, attribute)
        if value:
            clauses.append(f"{column} = :{attribute}")
            params[attribute] = value
    if filters.from_time is not None:
        clauses.append("created_at >= :from_time")
        params["from_time"] = filters.from_time
    if filters.to_time is not None:
        clauses.append("created_at <= :to_time")
        params["to_time"] = filters.to_time
    if filters.cost_min is not None:
        clauses.append("cost_ms >= :cost_min")
        params["cost_min"] = filters.cost_min
    if filters.cost_max is not None:
        clauses.append("cost_ms <= :cost_max")
        params["cost_max"] = filters.cost_max
    if filters.q:
        clauses.append("""(
            event_name LIKE :keyword ESCAPE '\\\\'
            OR target LIKE :keyword ESCAPE '\\\\'
            OR params_summary LIKE :keyword ESCAPE '\\\\'
            OR error_summary LIKE :keyword ESCAPE '\\\\'
        )""")
        params["keyword"] = f"%{_escaped_like(filters.q)}%"
    return clauses, params


def _where_sql(clauses: list[str]) -> str:
    return " WHERE " + " AND ".join(clauses) if clauses else ""


def _mapping(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return dict(row)
    if hasattr(row, "_mapping"):
        return dict(row._mapping)
    return dict(row)


def list_logs(filters: LogFilters, page: int, page_size: int) -> dict[str, Any]:
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ValueError("page must be a positive integer")
    if (
        isinstance(page_size, bool)
        or not isinstance(page_size, int)
        or not 1 <= page_size <= 100
    ):
        raise ValueError("page_size must be between 1 and 100")
    clauses, params = _filter_where(filters)
    where = _where_sql(clauses)
    with _engine().connect() as conn:
        total = int(
            conn.execute(text(f"SELECT COUNT(*) FROM mcp_call_log{where}"), params).scalar()
            or 0
        )
        item_params = dict(params, limit=page_size, offset=(page - 1) * page_size)
        rows = conn.execute(
            text(
                f"SELECT {_SAFE_COLUMNS} FROM mcp_call_log{where} "
                "ORDER BY created_at DESC, id DESC LIMIT :limit OFFSET :offset"
            ),
            item_params,
        ).mappings().all()
    return {
        "items": [_mapping(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _bounded_stats_filters(filters: LogFilters) -> LogFilters:
    from_time = filters.from_time
    to_time = filters.to_time
    if from_time is None and to_time is None:
        to_time = _utcnow()
        from_time = to_time - timedelta(hours=24)
    elif from_time is None:
        from_time = to_time - timedelta(hours=24)
    elif to_time is None:
        to_time = from_time + timedelta(hours=24)
    return replace(filters, from_time=from_time, to_time=to_time)


def get_log_stats(filters: LogFilters) -> dict[str, Any]:
    bounded_filters = _bounded_stats_filters(filters)
    clauses, params = _filter_where(bounded_filters)
    where = _where_sql(clauses)
    aggregate_sql = text(f"""
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN result_status='ok' THEN 1 ELSE 0 END) AS ok_count,
               SUM(CASE WHEN result_status IN ('error','denied') THEN 1 ELSE 0 END)
                 AS error_count,
               AVG(cost_ms) AS avg_cost_ms
        FROM mcp_call_log{where}
    """)
    with _engine().connect() as conn:
        aggregate_row = conn.execute(aggregate_sql, params).fetchone()
        aggregate = _mapping(aggregate_row) if aggregate_row is not None else {}
        total = int(aggregate.get("total") or 0)
        p95_cost_ms = 0
        if total:
            p95_offset = min(total - 1, max(0, math.floor((total - 1) * 0.95)))
            p95_cost_ms = int(
                conn.execute(
                    text(
                        f"SELECT cost_ms FROM mcp_call_log{where} "
                        "ORDER BY cost_ms ASC LIMIT :p95_offset, 1"
                    ),
                    dict(params, p95_offset=p95_offset),
                ).scalar()
                or 0
            )
        trend = conn.execute(
            text(
                f"SELECT {_TREND_BUCKET_EXPRESSION} AS bucket, "
                f"COUNT(*) AS count FROM mcp_call_log{where} "
                f"GROUP BY {_TREND_BUCKET_EXPRESSION} ORDER BY bucket"
            ),
            params,
        ).mappings().all()
        top_tools = []
        if bounded_filters.category not in ("protocol", "auth"):
            tool_clauses = list(clauses)
            tool_params = dict(params)
            if bounded_filters.category != "tool":
                tool_clauses.append("category = :top_tool_category")
                tool_params["top_tool_category"] = "tool"
            top_tools = conn.execute(
                text(
                    "SELECT event_name, COUNT(*) AS count FROM mcp_call_log"
                    f"{_where_sql(tool_clauses)} "
                    "GROUP BY event_name ORDER BY count DESC, event_name LIMIT 10"
                ),
                tool_params,
            ).mappings().all()
        status_distribution = conn.execute(
            text(
                f"SELECT result_status, COUNT(*) AS count FROM mcp_call_log{where} "
                "GROUP BY result_status ORDER BY result_status"
            ),
            params,
        ).mappings().all()
    ok_count = int(aggregate.get("ok_count") or 0)
    return {
        "total": total,
        "success_rate": round(ok_count * 100.0 / total, 2) if total else 0.0,
        "error_count": int(aggregate.get("error_count") or 0),
        "avg_cost_ms": round(float(aggregate.get("avg_cost_ms") or 0), 2),
        "p95_cost_ms": p95_cost_ms,
        "trend": [_mapping(row) for row in trend],
        "top_tools": [_mapping(row) for row in top_tools],
        "status_distribution": [_mapping(row) for row in status_distribution],
    }


def _delete_conditions(spec: DeleteSpec) -> tuple[list[str], dict[str, Any], bool]:
    if not isinstance(spec, DeleteSpec):
        raise TypeError("spec must be DeleteSpec")
    if spec.mode == "ids":
        return ["id IN :ids"], {"ids": list(spec.ids)}, True
    if spec.mode == "filter":
        clauses, params = _filter_where(spec.filters)
        if not clauses:
            raise ValueError("filter delete requires at least one filter")
        return clauses, params, False
    if spec.mode == "before_date":
        return ["created_at < :before_date"], {"before_date": spec.before_date}, False
    return [], {}, False


def _bind_ids(statement, expanding: bool):
    return statement.bindparams(bindparam("ids", expanding=True)) if expanding else statement


def preview_delete(spec: DeleteSpec) -> dict[str, int]:
    clauses, params, expanding = _delete_conditions(spec)
    statement = _bind_ids(
        text(
            "SELECT COUNT(*) AS matched_count, COALESCE(MAX(id), 0) AS max_id "
            f"FROM mcp_call_log{_where_sql(clauses)}"
        ),
        expanding,
    )
    with _engine().connect() as conn:
        row = conn.execute(statement, params).fetchone()
    if row is None:
        return {"matched_count": 0, "max_id": 0}
    values = _mapping(row) if isinstance(row, dict) or hasattr(row, "_mapping") else {
        "matched_count": row[0],
        "max_id": row[1],
    }
    return {
        "matched_count": int(values.get("matched_count") or 0),
        "max_id": int(values.get("max_id") or 0),
    }


def delete_matching(spec: DeleteSpec, max_id: int, batch_size: int = 5000) -> int:
    if isinstance(max_id, bool) or not isinstance(max_id, int) or max_id < 0:
        raise ValueError("max_id must be a non-negative integer")
    if (
        isinstance(batch_size, bool)
        or not isinstance(batch_size, int)
        or not 1 <= batch_size <= _MAX_DELETE_BATCH
    ):
        raise ValueError("batch_size must be between 1 and 5000")
    if max_id == 0:
        return 0

    base_clauses, base_params, expanding = _delete_conditions(spec)
    select_clauses = [*base_clauses, "id <= :max_id"]
    select_statement = _bind_ids(
        text(
            "SELECT id FROM mcp_call_log"
            f"{_where_sql(select_clauses)} ORDER BY id LIMIT :batch_size"
        ),
        expanding,
    )
    delete_statement = text(
        "DELETE FROM mcp_call_log WHERE id IN :ids"
    ).bindparams(bindparam("ids", expanding=True))
    total = 0
    while True:
        select_params = dict(base_params, max_id=max_id, batch_size=batch_size)
        with _engine().begin() as conn:
            rows = conn.execute(select_statement, select_params).fetchall()
            ids = [int(row[0]) for row in rows]
            if not ids:
                break
            result = conn.execute(delete_statement, {"ids": ids})
            total += int(result.rowcount or 0)
    return total


def get_retention_days() -> int:
    with _engine().connect() as conn:
        value = conn.execute(
            text("SELECT setting_value FROM gateway_setting WHERE setting_key=:setting_key"),
            {"setting_key": _RETENTION_KEY},
        ).scalar()
    if value is None:
        return _DEFAULT_RETENTION_DAYS
    try:
        days = int(value)
    except (TypeError, ValueError):
        logger.warning("Invalid MCP log retention setting; using default")
        return _DEFAULT_RETENTION_DAYS
    if not 0 <= days <= 3650:
        logger.warning("Out-of-range MCP log retention setting; using default")
        return _DEFAULT_RETENTION_DAYS
    return days


def set_retention_days(days: int) -> int:
    if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= 3650:
        raise ValueError("retention days must be an integer between 0 and 3650")
    with _engine().begin() as conn:
        conn.execute(
            text("""
                INSERT INTO gateway_setting (setting_key, setting_value)
                VALUES (:setting_key, :days)
                ON DUPLICATE KEY UPDATE setting_value=VALUES(setting_value)
            """),
            {"setting_key": _RETENTION_KEY, "days": str(days)},
        )
    return days


def cleanup_expired_logs(now: datetime | None = None) -> int:
    days = get_retention_days()
    if days == 0:
        return 0
    current = now or datetime.utcnow()
    if current.tzinfo is not None and current.utcoffset() is not None:
        raise ValueError("now must be UTC-naive")
    spec = DeleteSpec(mode="before_date", before_date=current - timedelta(days=days))
    preview = preview_delete(spec)
    if preview["matched_count"] == 0:
        return 0
    return delete_matching(spec, preview["max_id"], batch_size=_MAX_DELETE_BATCH)
