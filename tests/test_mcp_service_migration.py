from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from app import db
from app.connectors import ConnectorRegistry, ConnectorSpec, ToolSpec
from app.mcp_services import store


def test_existing_service_token_schema_has_expiry_and_usage_lifecycle_columns():
    migration = Path("sql/008_mcp_service.sql").read_text(encoding="utf-8")
    runtime_ddl = next(
        ddl for ddl in store._SERVICE_DDLS if "mcp_service_token" in ddl
    )

    for schema in (migration, runtime_ddl):
        normalized = schema.replace("`", "")
        assert "expires_at DATETIME NULL" in normalized
        assert "last_used_at DATETIME NULL" in normalized
        assert "idx_mcp_service_token_service (service_id, revoked_at, expires_at)" in normalized


class _Result:
    def __init__(self, *, row=None, rows=(), scalar=None, rowcount=0):
        self._row = row
        self._rows = list(rows)
        self._scalar = scalar
        self.rowcount = rowcount

    def fetchone(self):
        return self._row

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def scalar(self):
        return self._scalar


class _Connection:
    def __init__(self, engine):
        self.engine = engine

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is None:
            self.engine.commits += 1
        return False

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        params = params or {}
        self.engine.statements.append((sql, params))
        if "FROM connection_instance c" in sql:
            rows = [
                {
                    **row,
                    "state_json": (
                        '{"status":"completed"}'
                        if row["connection_id"] in self.engine.watermarked
                        else None
                    ),
                }
                for row in self.engine.connections
            ]
            return _Result(rows=rows)
        if "FROM connection_tool_policy" in sql:
            return _Result(
                rows=[
                    {
                        "tool_name": "users.disabled",
                        "enabled": 0,
                        "policy_json": "{}",
                    }
                ]
            )
        if "FROM mcp_service" in sql and "WHERE service_id=:service_id" in sql:
            return _Result(row=self.engine.service_rows.get(params["service_id"]))
        if (
            "FROM mcp_service" in sql
            and "tenant_id=:tenant_id" in sql
            and "service_key=:service_key" in sql
        ):
            row = next(
                (
                    item
                    for item in self.engine.service_rows.values()
                    if item["tenant_id"] == params["tenant_id"]
                    and item["service_key"] == params["service_key"]
                ),
                None,
            )
            return _Result(row=row)
        if sql.startswith("INSERT INTO mcp_service "):
            if params["connection_id"] == self.engine.fail_connection_id:
                raise RuntimeError("service transaction failed")
            key_owner = next(
                (
                    item
                    for item in self.engine.service_rows.values()
                    if item["tenant_id"] == params["tenant_id"]
                    and item["service_key"] == params["service_key"]
                ),
                None,
            )
            if key_owner is None and params["service_id"] not in self.engine.service_rows:
                self.engine.service_rows[params["service_id"]] = {
                    "service_id": params["service_id"],
                    "tenant_id": params["tenant_id"],
                    "display_name": params["display_name"],
                    "service_key": params["service_key"],
                    "status": params["status"],
                    "config_version": 1,
                }
            return _Result(rowcount=1)
        if sql.startswith("UPDATE mcp_service SET"):
            row = self.engine.service_rows[params["service_id"]]
            row.update(
                display_name=params["display_name"],
                status=params["status"],
            )
            row["config_version"] += 1
            return _Result(rowcount=1)
        if sql.startswith("DELETE FROM mcp_service_tool_binding"):
            self.engine.bindings[:] = [
                item
                for item in self.engine.bindings
                if item["service_id"] != params["service_id"]
            ]
            return _Result(rowcount=1)
        if sql.startswith("INSERT INTO mcp_service_tool_binding"):
            self.engine.bindings.append(dict(params))
            return _Result(rowcount=1)
        if sql.startswith("INSERT INTO connection_sync_state"):
            self.engine.watermarked.add(params["connection_id"])
            return _Result(rowcount=1)
        return _Result()


class _Engine:
    def __init__(self, *, connection_count=1, fail_connection_id=None):
        self.connections = [
            {
                "connection_id": f"conn-{index}",
                "tenant_id": "tenant-a",
                "connection_alias": f"conn_{index}",
                "connector_key": "wecom",
                "display_name": f"Connection {index}",
                "status": "active",
            }
            for index in range(1, connection_count + 1)
        ]
        self.fail_connection_id = fail_connection_id
        self.service_rows = {}
        self.bindings = []
        self.watermarked = set()
        self.statements = []
        self.commits = 0

    def connect(self):
        return _Connection(self)

    def begin(self):
        return _Connection(self)


def _tool(tool_key: str, mcp_name: str) -> ToolSpec:
    return ToolSpec(
        tool_key=tool_key,
        mcp_name=mcp_name,
        description=tool_key,
        input_schema={"type": "object"},
        output_schema=None,
        operation_kind="read",
        default_timeout_ms=1_000,
        cache_ttl_seconds=None,
    )


@dataclass
class _Connector:
    def spec(self):
        return ConnectorSpec(
            connector_key="wecom",
            tools=(
                _tool("users.get", "wecom_get_users"),
                _tool("users.disabled", "wecom_disabled"),
            ),
        )

    async def execute(self, context, tool_key, args):  # pragma: no cover
        raise NotImplementedError

    async def sync(self, context, resource_key):  # pragma: no cover
        raise NotImplementedError


def test_default_service_backfill_is_deterministic_idempotent_and_token_free(
    monkeypatch,
):
    engine = _Engine()
    registry = ConnectorRegistry([_Connector()])
    monkeypatch.setattr(db, "get_engine", lambda: engine)

    assert store.migrate_default_services(registry, enabled=True) == 1
    assert store.migrate_default_services(registry, enabled=True) == 0

    service_id = store.default_service_id("conn-1")
    assert set(engine.service_rows) == {service_id}
    assert [(item["source_tool_key"], item["tool_alias"]) for item in engine.bindings] == [
        ("users.get", "wecom_get_users")
    ]
    assert all("mcp_service_token" not in sql for sql, _ in engine.statements)
    service_write = next(
        index
        for index, (sql, _) in enumerate(engine.statements)
        if sql.startswith("INSERT INTO mcp_service ")
    )
    watermark_write = next(
        index
        for index, (sql, _) in enumerate(engine.statements)
        if sql.startswith("INSERT INTO connection_sync_state")
    )
    assert service_write < watermark_write


def test_default_service_backfill_writes_no_watermark_until_all_services_commit(
    monkeypatch,
):
    engine = _Engine(connection_count=2, fail_connection_id="conn-2")
    registry = ConnectorRegistry([_Connector()])
    monkeypatch.setattr(db, "get_engine", lambda: engine)

    with pytest.raises(RuntimeError, match="service transaction failed"):
        store.migrate_default_services(registry, enabled=True)

    assert engine.watermarked == set()
    assert not any(
        sql.startswith("INSERT INTO connection_sync_state")
        for sql, _ in engine.statements
    )


def test_disabled_default_service_backfill_does_not_access_database(monkeypatch):
    monkeypatch.setattr(db, "get_engine", lambda: pytest.fail("database accessed"))

    assert store.migrate_default_services(ConnectorRegistry(), enabled=False) == 0


def test_default_service_backfill_rejects_service_key_owned_by_another_id(
    monkeypatch,
):
    engine = _Engine()
    service_key = store._default_service_key("conn-1")
    engine.service_rows["service-other"] = {
        "service_id": "service-other",
        "tenant_id": "tenant-a",
        "display_name": "Other",
        "service_key": service_key,
        "status": "active",
        "config_version": 3,
    }
    registry = ConnectorRegistry([_Connector()])
    monkeypatch.setattr(db, "get_engine", lambda: engine)

    with pytest.raises(store.ServiceOwnershipError):
        store.migrate_default_services(registry, enabled=True)

    assert engine.bindings == []
    assert engine.watermarked == set()


def test_default_service_backfill_converges_existing_status_and_bindings(monkeypatch):
    engine = _Engine()
    service_id = store.default_service_id("conn-1")
    engine.service_rows[service_id] = {
        "service_id": service_id,
        "tenant_id": "tenant-a",
        "display_name": "Old",
        "service_key": store._default_service_key("conn-1"),
        "status": "disabled",
        "config_version": 4,
    }
    engine.bindings = [
        {
            "binding_id": "stale-binding",
            "service_id": service_id,
            "connection_id": "conn-1",
            "source_tool_key": "users.disabled",
            "tool_alias": "wecom_disabled",
        }
    ]
    registry = ConnectorRegistry([_Connector()])
    monkeypatch.setattr(db, "get_engine", lambda: engine)

    assert store.migrate_default_services(registry, enabled=True) == 1

    assert engine.service_rows[service_id]["status"] == "active"
    assert [item["source_tool_key"] for item in engine.bindings] == ["users.get"]
