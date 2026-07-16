from pathlib import Path

import pytest

from app import db


ROOT = Path(__file__).resolve().parents[1]


class ScalarResult:
    def __init__(self, value=0):
        self.value = value

    def scalar(self):
        return self.value


class MigrationConnection:
    def __init__(self):
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, statement, params=None):
        self.statements.append((str(statement), params or {}))
        return ScalarResult(0)


class MigrationEngine:
    def __init__(self, connection):
        self.connection = connection

    def begin(self):
        return self.connection

    def connect(self):
        return self.connection


def test_ensure_schema_creates_all_tables_and_repairs_partial_columns(monkeypatch):
    connection = MigrationConnection()
    monkeypatch.setattr(db, "get_engine", lambda: MigrationEngine(connection))

    db.ensure_schema("wbd_abc")

    sql = "\n".join(statement for statement, _ in connection.statements)
    for table in (
        "wecom_report",
        "wecom_approval",
        "wecom_checkin",
        "sync_cursor",
        "audit_log",
    ):
        assert f"CREATE TABLE IF NOT EXISTS `wbd_abc`.`{table}`" in sql
    for table in ("wecom_report", "wecom_approval"):
        for column in ("source_window_start", "source_window_end", "is_partial"):
            assert f"ALTER TABLE `wbd_abc`.`{table}` ADD COLUMN `{column}`" in sql


def test_runtime_migration_repairs_all_required_tenant_columns(monkeypatch):
    connection = MigrationConnection()
    monkeypatch.setattr(db, "get_engine", lambda: MigrationEngine(connection))

    db.ensure_central_columns()

    sql = "\n".join(statement for statement, _ in connection.statements)
    for column in (
        "enabled_modules",
        "checkin_userids",
        "contact_secret_encrypted",
        "trusted_domain",
        "data_mode",
    ):
        assert f"ALTER TABLE tenant_config ADD COLUMN `{column}`" in sql
    assert "ADD COLUMN IF NOT EXISTS" not in sql


def test_startup_migrations_upgrade_center_before_tenant_schemas(monkeypatch):
    events = []
    from app import mcp_log_store
    from app.connections import store as connection_store

    monkeypatch.setattr(
        db, "ensure_central_columns", lambda: events.append("center"), raising=False
    )
    monkeypatch.setattr(
        mcp_log_store,
        "ensure_central_log_tables",
        lambda: events.append("log_tables"),
    )
    monkeypatch.setattr(
        mcp_log_store,
        "migrate_legacy_logs",
        lambda days=90: events.append(f"legacy:{days}"),
    )
    monkeypatch.setattr(
        connection_store,
        "ensure_connection_tables",
        lambda: events.append("connection_tables"),
    )
    monkeypatch.setattr(
        connection_store,
        "migrate_legacy_wecom_connections",
        lambda: events.append("connection_backfill"),
        raising=False,
    )
    monkeypatch.setattr(
        db,
        "get_tenant_schema_names",
        lambda: events.append("enumerate") or ["wbd_a", "wbd_b"],
        raising=False,
    )
    monkeypatch.setattr(
        db, "ensure_schema", lambda schema: events.append(schema)
    )

    db.run_startup_migrations()

    assert events == [
        "center",
        "log_tables",
        "connection_tables",
        "connection_backfill",
        "enumerate",
        "wbd_a",
        "wbd_b",
        "legacy:90",
    ]


def test_startup_orders_connection_tables_before_legacy_wecom_backfill(monkeypatch):
    events = []
    from app.connections import store as connection_store
    from app import mcp_log_store

    monkeypatch.setattr(db, "ensure_central_columns", lambda: events.append("center"))
    monkeypatch.setattr(
        mcp_log_store, "ensure_central_log_tables", lambda: events.append("log_tables")
    )
    monkeypatch.setattr(
        connection_store,
        "ensure_connection_tables",
        lambda: events.append("connection_tables"),
    )
    monkeypatch.setattr(
        connection_store,
        "migrate_legacy_wecom_connections",
        lambda: events.append("connection_backfill"),
    )
    monkeypatch.setattr(db, "get_tenant_schema_names", lambda: events.append("enumerate") or [])
    monkeypatch.setattr(mcp_log_store, "migrate_legacy_logs", lambda days=90: events.append("legacy_logs"))

    db.run_startup_migrations()

    assert events.index("connection_tables") < events.index("connection_backfill")
    assert events.index("log_tables") < events.index("connection_tables")
    assert events.index("connection_backfill") < events.index("enumerate")


def test_startup_migration_failure_propagates(monkeypatch):
    from app import mcp_log_store
    from app.connections import store as connection_store

    monkeypatch.setattr(db, "ensure_central_columns", lambda: None, raising=False)
    monkeypatch.setattr(mcp_log_store, "ensure_central_log_tables", lambda: None)
    monkeypatch.setattr(mcp_log_store, "migrate_legacy_logs", lambda days=90: None)
    monkeypatch.setattr(connection_store, "ensure_connection_tables", lambda: None)
    monkeypatch.setattr(
        connection_store,
        "migrate_legacy_wecom_connections",
        lambda: None,
        raising=False,
    )
    monkeypatch.setattr(
        db, "get_tenant_schema_names", lambda: ["wbd_a"], raising=False
    )
    monkeypatch.setattr(
        db,
        "ensure_schema",
        lambda schema: (_ for _ in ()).throw(RuntimeError("ddl denied")),
    )

    with pytest.raises(RuntimeError, match="ddl denied"):
        db.run_startup_migrations()


def test_central_log_table_failure_stops_startup(monkeypatch):
    from app import mcp_log_store

    events = []
    monkeypatch.setattr(db, "ensure_central_columns", lambda: events.append("center"))
    monkeypatch.setattr(
        mcp_log_store,
        "ensure_central_log_tables",
        lambda: (_ for _ in ()).throw(RuntimeError("central ddl denied")),
    )
    monkeypatch.setattr(
        db,
        "get_tenant_schema_names",
        lambda: events.append("enumerate") or [],
    )

    with pytest.raises(RuntimeError, match="central ddl denied"):
        db.run_startup_migrations()
    assert events == ["center"]


def test_lifespan_runs_migrations_before_mcp_and_scheduler():
    source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    migration = source.index("run_startup_migrations")
    session = source.index("session_manager.run", migration)
    scheduler = source.index("AsyncIOScheduler", session)
    assert migration < session < scheduler


def test_mysql57_upgrade_script_is_complete_and_idempotent():
    sql = (ROOT / "sql" / "004_gateway_hardening.sql").read_text(
        encoding="utf-8"
    )
    lower = sql.lower()

    for column in (
        "enabled_modules",
        "checkin_userids",
        "contact_secret_encrypted",
        "trusted_domain",
        "data_mode",
    ):
        assert column in lower
    for table in (
        "wecom_report",
        "wecom_approval",
        "wecom_checkin",
        "sync_cursor",
        "audit_log",
    ):
        assert f"create table if not exists `', schema_value, '`.`{table}`" in lower
    assert "create database if not exists" in lower
    assert "information_schema.columns" in lower
    assert "add column if not exists" not in lower
    assert "call migrate_gateway_central_columns()" in lower
    assert "call migrate_gateway_business_schema()" in lower


def test_new_database_init_contains_checkin_table_matching_runtime_ddl():
    sql = (ROOT / "sql" / "001_init.sql").read_text(encoding="utf-8")
    runtime = "\n".join(db._BIZ_DDLS)
    assert "CREATE TABLE IF NOT EXISTS `wecom_checkin`" in sql
    for column in (
        "userid",
        "checkin_type",
        "checkin_time",
        "exception_type",
        "location_title",
        "group_name",
        "detail_json",
        "synced_at",
    ):
        assert column in sql
        assert column in runtime


def test_connection_platform_sql_matches_runtime_mysql57_tables():
    sql = (ROOT / "sql" / "006_connection_platform.sql").read_text(encoding="utf-8")
    lower = sql.lower()

    for table in (
        "connection_instance",
        "connection_credential",
        "connection_token",
        "connection_tool_policy",
        "connection_sync_state",
        "declarative_spec_revision",
        "declarative_spec_operation",
    ):
        assert f"create table if not exists `{table}`" in lower
    assert "unique key `uk_connection_token_hmac` (`token_hmac`)" in lower
    assert "add column if not exists" not in lower
    assert "`public_config_json` text" in lower
    assert "`created_at` datetime" in lower
