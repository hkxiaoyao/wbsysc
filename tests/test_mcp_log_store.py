from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from app import db
from app import mcp_log_store as store
from app.mcp_log_models import DeleteSpec, LogFilters, McpLogEvent


class Result:
    def __init__(self, rows=(), scalar_value=None, rowcount=0):
        self._rows = list(rows)
        self._scalar = scalar_value
        self.rowcount = rowcount

    def scalar(self):
        return self._scalar

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class RecordingConnection:
    def __init__(self, results=()):
        self.results = list(results)
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, statement, params=None):
        self.statements.append((str(statement), params or {}))
        if self.results:
            result = self.results.pop(0)
            if isinstance(result, BaseException):
                raise result
            return result
        return Result()


class RecordingEngine:
    def __init__(self, connection):
        self.connection = connection

    def begin(self):
        return self.connection

    def connect(self):
        return self.connection


def install_engine(monkeypatch, *results):
    connection = RecordingConnection(results)
    monkeypatch.setattr(db, "get_engine", lambda: RecordingEngine(connection))
    return connection


def test_models_are_immutable_and_filters_validate_ranges():
    event = McpLogEvent()
    with pytest.raises(FrozenInstanceError):
        event.event_name = "changed"

    with pytest.raises(ValueError, match="cost_min"):
        LogFilters(cost_min=500, cost_max=100)
    with pytest.raises(ValueError, match="from_time"):
        LogFilters(
            from_time=datetime(2026, 1, 2),
            to_time=datetime(2026, 1, 1),
        )
    with pytest.raises(ValueError, match="UTC-naive"):
        LogFilters(from_time=datetime.now(timezone.utc))


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"category": "other"}, "category"),
        ({"status": "unknown"}, "status"),
        ({"q": "x" * 101}, "q"),
        ({"cost_min": -1}, "cost_min"),
    ],
)
def test_log_filters_reject_invalid_values(kwargs, message):
    with pytest.raises(ValueError, match=message):
        LogFilters(**kwargs)


def test_delete_spec_requires_fields_for_selected_mode():
    with pytest.raises(ValueError, match="ids"):
        DeleteSpec(mode="ids")
    with pytest.raises(ValueError, match="before_date"):
        DeleteSpec(mode="before_date")
    with pytest.raises(ValueError, match="mode"):
        DeleteSpec(mode="raw_sql")


def test_ensure_central_log_tables_executes_mysql57_ddl(monkeypatch):
    connection = install_engine(monkeypatch)

    store.ensure_central_log_tables()

    sql = "\n".join(statement for statement, _ in connection.statements)
    assert "CREATE TABLE IF NOT EXISTS `mcp_call_log`" in sql
    assert "DATETIME(6)" in sql
    assert "UNIQUE KEY `uk_mcp_log_legacy` (`legacy_schema`, `legacy_id`)" in sql
    assert "CREATE TABLE IF NOT EXISTS `gateway_setting`" in sql
    assert "ADD COLUMN IF NOT EXISTS" not in sql


def test_insert_event_uses_bound_parameters(monkeypatch):
    connection = install_engine(monkeypatch)
    event = McpLogEvent(
        tenant_id="tenant-a",
        category="tool",
        event_name="wecom_list_reports",
        params_summary="safe summary",
        created_at=datetime(2026, 7, 14, 8, 0),
    )

    store.insert_event(event)

    sql, params = connection.statements[0]
    assert ":tenant_id" in sql and "tenant-a" not in sql
    assert params["tenant_id"] == "tenant-a"
    assert params["params_summary"] == "safe summary"
    assert params["created_at"] == datetime(2026, 7, 14, 8, 0)


def test_list_logs_escapes_like_metacharacters_and_paginates(monkeypatch):
    item = {"id": 7, "event_name": "tools/list"}
    connection = install_engine(
        monkeypatch,
        Result(scalar_value=1),
        Result(rows=[item]),
    )

    result = store.list_logs(LogFilters(q=r"50%_done\ok"), page=2, page_size=20)

    assert result == {"items": [item], "total": 1, "page": 2, "page_size": 20}
    item_sql, item_params = connection.statements[1]
    assert "ESCAPE '\\\\'" in item_sql
    assert item_params["keyword"] == r"%50\%\_done\\ok%"
    assert item_params["limit"] == 20
    assert item_params["offset"] == 20


def test_list_logs_can_filter_unknown_tenant_explicitly(monkeypatch):
    connection = install_engine(
        monkeypatch,
        Result(scalar_value=0),
        Result(rows=[]),
    )

    store.list_logs(LogFilters(tenant_id=""), page=1, page_size=20)

    sql, params = connection.statements[0]
    assert "tenant_id = :tenant_id" in sql
    assert params["tenant_id"] == ""


def test_stats_calculates_p95_with_one_sorted_offset_query(monkeypatch):
    connection = install_engine(
        monkeypatch,
        Result(rows=[{"total": 20, "ok_count": 15, "error_count": 3, "avg_cost_ms": 12.5}]),
        Result(scalar_value=95),
        Result(rows=[{"bucket": "2026-07-14 08:00:00", "count": 20}]),
        Result(rows=[{"event_name": "tools/list", "count": 10}]),
        Result(rows=[{"result_status": "ok", "count": 15}]),
    )

    stats = store.get_log_stats(LogFilters())

    assert stats["total"] == 20
    assert stats["success_rate"] == 75.0
    assert stats["p95_cost_ms"] == 95
    p95 = [entry for entry in connection.statements if "ORDER BY cost_ms" in entry[0]]
    assert len(p95) == 1
    assert p95[0][1]["p95_offset"] == 18


def test_stats_trend_groups_by_full_expression_for_mysql57(monkeypatch):
    connection = install_engine(
        monkeypatch,
        Result(rows=[{"total": 0, "ok_count": 0, "error_count": 0, "avg_cost_ms": 0}]),
        Result(rows=[]),
        Result(rows=[]),
        Result(rows=[]),
    )

    store.get_log_stats(LogFilters())

    trend_sql = next(sql for sql, _ in connection.statements if "DATE_FORMAT" in sql)
    bucket_expression = "DATE_FORMAT(created_at, '%Y-%m-%d %H:00:00')"
    assert f"GROUP BY {bucket_expression}" in trend_sql
    assert "GROUP BY bucket" not in trend_sql


def test_stats_default_window_is_latest_24_hours_for_every_query(monkeypatch):
    now = datetime(2026, 7, 14, 12, 0)
    monkeypatch.setattr(store, "_utcnow", lambda: now, raising=False)
    connection = install_engine(
        monkeypatch,
        Result(rows=[{"total": 1, "ok_count": 1, "error_count": 0, "avg_cost_ms": 5}]),
        Result(scalar_value=5),
        Result(rows=[]),
        Result(rows=[]),
        Result(rows=[]),
    )

    store.get_log_stats(LogFilters())

    for sql, params in connection.statements:
        assert "created_at >= :from_time" in sql
        assert "created_at <= :to_time" in sql
        assert params["from_time"] == now - timedelta(hours=24)
        assert params["to_time"] == now


@pytest.mark.parametrize(
    ("filters", "expected_from", "expected_to"),
    [
        (
            LogFilters(from_time=datetime(2026, 7, 1, 8, 0)),
            datetime(2026, 7, 1, 8, 0),
            datetime(2026, 7, 2, 8, 0),
        ),
        (
            LogFilters(to_time=datetime(2026, 7, 2, 8, 0)),
            datetime(2026, 7, 1, 8, 0),
            datetime(2026, 7, 2, 8, 0),
        ),
    ],
)
def test_stats_completes_one_sided_windows_to_24_hours(
    monkeypatch, filters, expected_from, expected_to
):
    connection = install_engine(
        monkeypatch,
        Result(rows=[{"total": 0, "ok_count": 0, "error_count": 0, "avg_cost_ms": 0}]),
        Result(rows=[]),
        Result(rows=[]),
        Result(rows=[]),
    )

    store.get_log_stats(filters)

    for _, params in connection.statements:
        assert params["from_time"] == expected_from
        assert params["to_time"] == expected_to


def test_stats_top_tools_adds_tool_only_predicate(monkeypatch):
    connection = install_engine(
        monkeypatch,
        Result(rows=[{"total": 0, "ok_count": 0, "error_count": 0, "avg_cost_ms": 0}]),
        Result(rows=[]),
        Result(rows=[{"event_name": "wecom_list_reports", "count": 2}]),
        Result(rows=[]),
    )

    stats = store.get_log_stats(LogFilters())

    assert stats["top_tools"] == [{"event_name": "wecom_list_reports", "count": 2}]
    ranking = [entry for entry in connection.statements if "GROUP BY event_name" in entry[0]]
    assert len(ranking) == 1
    assert "category = :top_tool_category" in ranking[0][0]
    assert ranking[0][1]["top_tool_category"] == "tool"


@pytest.mark.parametrize("category", ["protocol", "auth"])
def test_stats_non_tool_category_returns_no_top_tools(monkeypatch, category):
    connection = install_engine(
        monkeypatch,
        Result(rows=[{"total": 0, "ok_count": 0, "error_count": 0, "avg_cost_ms": 0}]),
        Result(rows=[]),
        Result(rows=[]),
    )

    stats = store.get_log_stats(LogFilters(category=category))

    assert stats["top_tools"] == []
    assert all("GROUP BY event_name" not in sql for sql, _ in connection.statements)


def test_legacy_migration_is_bounded_mapped_and_marks_completion(monkeypatch):
    tenant_created_at = datetime(2026, 6, 1, 0, 0)
    connection = install_engine(
        monkeypatch,
        Result(scalar_value=None),
        Result(rows=[("tenant-real", "wbd_abc", tenant_created_at)]),
        Result(),
        Result(),
    )

    store.migrate_legacy_logs(days=90)

    sql = "\n".join(statement for statement, _ in connection.statements)
    assert "INTERVAL 90 DAY" in sql
    assert "INSERT IGNORE" in sql
    assert "`wbd_abc`.`audit_log`" in sql
    assert "legacy_schema" in sql and "legacy_id" in sql
    assert "SELECT tenant_id, schema_name, created_at FROM tenant_config" in sql
    assert "created_at >= :tenant_created_at" in sql
    migration_params = [params for statement, params in connection.statements if "INSERT IGNORE" in statement]
    assert migration_params == [
        {
            "tenant_id": "tenant-real",
            "legacy_schema": "wbd_abc",
            "tenant_created_at": tenant_created_at,
        },
    ]
    marker_index, (marker_sql, marker_params) = next(
        (index, entry)
        for index, entry in enumerate(connection.statements)
        if "INSERT INTO gateway_setting" in entry[0]
    )
    migration_index = next(
        index
        for index, (statement, _) in enumerate(connection.statements)
        if "INSERT IGNORE" in statement
    )
    assert migration_index < marker_index
    assert "ON DUPLICATE KEY UPDATE" in marker_sql
    assert marker_params == {
        "setting_key": "mcp_log_legacy_migration_v1",
        "setting_value": "completed",
    }


def test_legacy_migration_existing_marker_skips_tenant_scan(monkeypatch):
    connection = install_engine(monkeypatch, Result(scalar_value="completed"))

    store.migrate_legacy_logs(days=90)

    assert len(connection.statements) == 1
    marker_sql, marker_params = connection.statements[0]
    assert "SELECT setting_value FROM gateway_setting" in marker_sql
    assert marker_params["setting_key"] == "mcp_log_legacy_migration_v1"
    assert "tenant_config" not in marker_sql


def test_legacy_migration_noncompleted_marker_is_retried_and_overwritten(monkeypatch):
    tenant_created_at = datetime(2026, 6, 1, 0, 0)
    connection = install_engine(
        monkeypatch,
        Result(scalar_value="started"),
        Result(rows=[("tenant-real", "wbd_abc", tenant_created_at)]),
        Result(),
        Result(),
    )

    store.migrate_legacy_logs(days=90)

    sql = "\n".join(statement for statement, _ in connection.statements)
    assert "SELECT tenant_id, schema_name, created_at FROM tenant_config" in sql
    assert "INSERT IGNORE INTO mcp_call_log" in sql
    marker_params = next(
        params
        for statement, params in connection.statements
        if "INSERT INTO gateway_setting" in statement
    )
    assert marker_params["setting_value"] == "completed"


def test_legacy_migration_with_no_tenants_does_not_mark_completion(monkeypatch):
    connection = install_engine(
        monkeypatch,
        Result(scalar_value=None),
        Result(rows=[]),
    )

    store.migrate_legacy_logs(days=90)

    assert len(connection.statements) == 2
    assert all(
        "INSERT INTO gateway_setting" not in statement
        for statement, _ in connection.statements
    )


def test_legacy_migration_does_not_mark_completion_after_tenant_failure(monkeypatch):
    tenant_created_at = datetime(2026, 6, 1, 0, 0)
    connection = install_engine(
        monkeypatch,
        Result(scalar_value=None),
        Result(
            rows=[
                ("tenant-a", "wbd_a", tenant_created_at),
                ("tenant-b", "wbd_b", tenant_created_at),
            ]
        ),
        Result(),
        RuntimeError("legacy table unavailable"),
    )

    store.migrate_legacy_logs(days=90)

    migrations = [
        statement
        for statement, _ in connection.statements
        if "INSERT IGNORE INTO mcp_call_log" in statement
    ]
    marker_writes = [
        statement
        for statement, _ in connection.statements
        if "INSERT INTO gateway_setting" in statement
    ]
    assert len(migrations) == 2
    assert marker_writes == []


def test_legacy_migration_skips_rows_before_current_schema_owner(monkeypatch):
    tenant_created_at = datetime(2026, 7, 10, 9, 30)
    connection = install_engine(
        monkeypatch,
        Result(scalar_value=None),
        Result(rows=[("new-owner", "wbd_reused", tenant_created_at)]),
        Result(),
        Result(),
    )

    store.migrate_legacy_logs(days=90)

    migration_sql, params = next(
        entry for entry in connection.statements if "INSERT IGNORE" in entry[0]
    )
    assert "created_at >= UTC_TIMESTAMP() - INTERVAL 90 DAY" in migration_sql
    assert "created_at >= :tenant_created_at" in migration_sql
    assert params["tenant_created_at"] == tenant_created_at


def test_legacy_migration_invalid_schema_blocks_marker_until_config_is_fixed(
    monkeypatch,
):
    tenant_created_at = datetime(2026, 1, 1)
    connection = install_engine(
        monkeypatch,
        Result(scalar_value=None),
        Result(rows=[("tenant-real", "wbd_bad-name", tenant_created_at)]),
        Result(scalar_value=None),
        Result(rows=[("tenant-real", "wbd_fixed", tenant_created_at)]),
        Result(),
        Result(),
    )

    store.migrate_legacy_logs()
    store.migrate_legacy_logs()

    tenant_scans = [
        statement
        for statement, _ in connection.statements
        if "FROM tenant_config" in statement
    ]
    migrations = [
        statement
        for statement, _ in connection.statements
        if "INSERT IGNORE INTO mcp_call_log" in statement
    ]
    marker_writes = [
        statement
        for statement, _ in connection.statements
        if "INSERT INTO gateway_setting" in statement
    ]
    assert len(tenant_scans) == 2
    assert len(migrations) == 1
    assert "`wbd_fixed`.`audit_log`" in migrations[0]
    assert len(marker_writes) == 1
    assert all("wbd_bad-name" not in statement for statement, _ in connection.statements)


def test_preview_and_delete_use_snapshot_and_exact_bounded_batches(monkeypatch):
    connection = install_engine(
        monkeypatch,
        Result(rows=[(3, 12)]),
        Result(rows=[(2,), (7,), (12,)]),
        Result(rowcount=3),
        Result(rows=[]),
    )
    spec = DeleteSpec(mode="filter", filters=LogFilters(status="error"))

    preview = store.preview_delete(spec)
    deleted = store.delete_matching(spec, preview["max_id"], batch_size=5000)

    assert preview == {"matched_count": 3, "max_id": 12}
    assert deleted == 3
    select_sql, select_params = connection.statements[1]
    delete_sql, delete_params = connection.statements[2]
    assert "id <= :max_id" in select_sql
    assert "LIMIT :batch_size" in select_sql
    assert select_params["batch_size"] == 5000
    assert "DELETE FROM mcp_call_log WHERE id IN" in delete_sql
    assert delete_params["ids"] == [2, 7, 12]
    assert "result_status" not in delete_sql


def test_filter_delete_rejects_an_empty_filter(monkeypatch):
    install_engine(monkeypatch)
    with pytest.raises(ValueError, match="filter delete"):
        store.preview_delete(DeleteSpec(mode="filter"))


def test_delete_matching_repeats_bounded_batches(monkeypatch):
    connection = install_engine(
        monkeypatch,
        Result(rows=[(1,), (2,)]),
        Result(rowcount=2),
        Result(rows=[(3,)]),
        Result(rowcount=1),
        Result(rows=[]),
    )

    deleted = store.delete_matching(DeleteSpec(mode="all"), max_id=3, batch_size=2)

    assert deleted == 3
    selects = [sql for sql, _ in connection.statements if sql.startswith("SELECT id")]
    deletes = [sql for sql, _ in connection.statements if sql.startswith("DELETE")]
    assert len(selects) == 3
    assert len(deletes) == 2
    with pytest.raises(ValueError, match="5000"):
        store.delete_matching(DeleteSpec(mode="all"), max_id=3, batch_size=5001)


def test_retention_defaults_validates_and_upserts(monkeypatch):
    missing = install_engine(monkeypatch, Result(scalar_value=None))
    assert store.get_retention_days() == 90
    assert missing.statements[0][1]["setting_key"] == "mcp_log_retention_days"

    connection = install_engine(monkeypatch, Result())
    assert store.set_retention_days(3650) == 3650
    assert connection.statements[0][1]["days"] == "3650"
    with pytest.raises(ValueError, match="0.*3650"):
        store.set_retention_days(3651)


def test_cleanup_zero_retention_skips_delete(monkeypatch):
    monkeypatch.setattr(store, "get_retention_days", lambda: 0)
    monkeypatch.setattr(
        store,
        "preview_delete",
        lambda spec: (_ for _ in ()).throw(AssertionError("must not preview")),
    )
    assert store.cleanup_expired_logs() == 0


def test_cleanup_uses_snapshot_before_date(monkeypatch):
    now = datetime(2026, 7, 14, 12, 0)
    captured = {}
    monkeypatch.setattr(store, "get_retention_days", lambda: 30)

    def preview(spec):
        captured["spec"] = spec
        return {"matched_count": 2, "max_id": 99}

    monkeypatch.setattr(store, "preview_delete", preview)
    monkeypatch.setattr(
        store,
        "delete_matching",
        lambda spec, max_id, batch_size=5000: captured.update(max_id=max_id) or 2,
    )

    assert store.cleanup_expired_logs(now) == 2
    assert captured["spec"].before_date == now - timedelta(days=30)
    assert captured["max_id"] == 99
