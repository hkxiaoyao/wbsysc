from dataclasses import FrozenInstanceError
import json

import pytest

from app import db
from app.connections import store as connection_store
from app.connections.models import ConnectionRecord
from app.mcp_services import store
from app.mcp_services.models import McpService, ServiceToolBinding


class Result:
    def __init__(self, row=None, rows=(), scalar_value=None, rowcount=0):
        self.row = row
        self.rows = list(rows)
        self.scalar_value = scalar_value
        self.rowcount = rowcount

    def fetchone(self):
        return self.row

    def scalar(self):
        return self.scalar_value

    def mappings(self):
        return self

    def all(self):
        return self.rows


class StatefulConnection:
    def __init__(self):
        self.statements = []
        self.tenants = {"tenant-a": 1}
        self.services = {
            "service-a": {
                "service_id": "service-a",
                "tenant_id": "tenant-a",
                "display_name": "Operations",
                "service_key": "operations",
                "status": "draft",
                "config_version": 1,
            }
        }
        self.connections = {
            "conn-a": {
                "connection_id": "conn-a",
                "tenant_id": "tenant-a",
                "connector_key": "wecom",
                "connection_alias": "hq_wecom",
                "display_name": "HQ WeCom",
                "status": "active",
                "data_mode": "stored",
                "public_config_json": "{}",
                "config_version": 1,
            },
            "tenant-b-conn": {
                "connection_id": "tenant-b-conn",
                "tenant_id": "tenant-b",
                "connector_key": "wecom",
                "connection_alias": "other_wecom",
                "display_name": "Other WeCom",
                "status": "active",
                "data_mode": "stored",
                "public_config_json": "{}",
                "config_version": 1,
            },
        }
        self.bindings = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        params = params or {}
        self.statements.append((sql, params))
        if sql.startswith("SELECT enabled FROM tenant_config"):
            return Result(scalar_value=self.tenants.get(params["tenant_id"]))
        if "FROM mcp_service" in sql and "mcp_service_tool_binding" not in sql:
            service = self.services.get(params.get("service_id"))
            if service is not None and "tenant_id=:tenant_id" in sql:
                if service["tenant_id"] != params["tenant_id"]:
                    service = None
            if "LIMIT 1" in sql:
                return Result(dict(service) if service else None)
            rows = [
                dict(value)
                for value in self.services.values()
                if value["tenant_id"] == params["tenant_id"]
            ]
            return Result(rows=rows)
        if "FROM connection_instance" in sql and "connection_id IN" in sql:
            rows = [
                {"connection_id": connection_id, "tenant_id": row["tenant_id"]}
                for connection_id, row in self.connections.items()
                if connection_id in params.values()
            ]
            return Result(rows=rows)
        if "FROM connection_instance" in sql and "connection_alias=:connection_alias" in sql:
            row = next(
                (
                    {"connection_id": connection_id}
                    for connection_id, value in self.connections.items()
                    if value["tenant_id"] == params["tenant_id"]
                    and value["connection_alias"] == params["connection_alias"]
                ),
                None,
            )
            return Result(row)
        if "FROM connection_instance" in sql and "LIMIT 1" in sql:
            row = self.connections.get(params["connection_id"])
            if row is not None and row["tenant_id"] != params.get("tenant_id", row["tenant_id"]):
                row = None
            return Result(dict(row) if row else None)
        if sql.startswith("DELETE FROM mcp_service_tool_binding"):
            self.bindings = {
                key: value
                for key, value in self.bindings.items()
                if value["service_id"] != params["service_id"]
            }
            return Result()
        if sql.startswith("INSERT INTO mcp_service_tool_binding"):
            self.bindings[params["binding_id"]] = dict(params)
            return Result(rowcount=1)
        if sql.startswith("UPDATE mcp_service SET config_version"):
            service = self.services[params["service_id"]]
            if service["config_version"] != params["expected_config_version"]:
                return Result(rowcount=0)
            service["config_version"] += 1
            return Result(rowcount=1)
        if sql.startswith("UPDATE mcp_service SET status"):
            service = self.services.get(params["service_id"])
            if (
                service is None
                or service["tenant_id"] != params["tenant_id"]
                or service["config_version"] != params["expected_config_version"]
            ):
                return Result(rowcount=0)
            service["status"] = params["status"]
            service["config_version"] += 1
            return Result(rowcount=1)
        if "FROM mcp_service_tool_binding" in sql:
            rows = [
                dict(value)
                for value in self.bindings.values()
                if value["service_id"] == params["service_id"]
            ]
            return Result(rows=rows)
        if sql.startswith("UPDATE connection_instance SET connection_alias"):
            row = self.connections[params["connection_id"]]
            row["connection_alias"] = params["connection_alias"]
            row["config_version"] += 1
            return Result(rowcount=1)
        return Result()


class FakeEngine:
    def __init__(self, connection):
        self.connection = connection

    def begin(self):
        return self.connection

    def connect(self):
        return self.connection


def service(**changes):
    values = {
        "service_id": "service-a",
        "tenant_id": "tenant-a",
        "display_name": "Operations",
        "service_key": "operations",
        "status": "draft",
        "config_version": 1,
    }
    values.update(changes)
    return McpService(**values)


def binding(**changes):
    values = {
        "binding_id": "binding-a",
        "service_id": "service-a",
        "connection_id": "conn-a",
        "source_tool_key": "users.get",
        "tool_alias": "hq_wecom.get_users",
        "binding_status": "active",
        "policy": {"rate_limit": 10},
    }
    values.update(changes)
    return ServiceToolBinding(**values)


def test_service_contracts_are_immutable_and_validate_tool_identifiers():
    item = service()
    with pytest.raises(FrozenInstanceError):
        item.status = "active"

    with pytest.raises(ValueError, match="source_tool_key"):
        binding(source_tool_key="not a safe tool key")
    with pytest.raises(ValueError, match="tool_alias"):
        binding(tool_alias="9invalid")


def test_replace_bindings_rejects_cross_tenant_connection(monkeypatch):
    connection = StatefulConnection()
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(connection))

    with pytest.raises(store.ServiceOwnershipError):
        store.replace_bindings(
            "service-a",
            "tenant-a",
            [binding(connection_id="tenant-b-conn")],
            expected_config_version=1,
        )

    assert not any(sql.startswith("DELETE") for sql, _ in connection.statements)


def test_replace_bindings_rejects_duplicate_materialized_alias_before_writing(monkeypatch):
    connection = StatefulConnection()
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(connection))

    with pytest.raises(ValueError, match="duplicate tool_alias"):
        store.replace_bindings(
            "service-a",
            "tenant-a",
            [binding(), binding(binding_id="binding-b", source_tool_key="users.list")],
            expected_config_version=1,
        )

    assert connection.statements == []


def test_replace_bindings_locks_rows_and_increments_snapshot_version(monkeypatch):
    connection = StatefulConnection()
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(connection))

    updated = store.replace_bindings(
        "service-a", "tenant-a", [binding()], expected_config_version=1
    )

    assert updated.config_version == 2
    assert any("FROM mcp_service" in sql and "FOR UPDATE" in sql for sql, _ in connection.statements)
    assert any("FROM connection_instance" in sql and "FOR UPDATE" in sql for sql, _ in connection.statements)
    assert json.loads(connection.bindings["binding-a"]["policy_json"]) == {"rate_limit": 10}


def test_replace_bindings_rejects_stale_config_version_without_writing(monkeypatch):
    connection = StatefulConnection()
    connection.services["service-a"]["config_version"] = 2
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(connection))

    with pytest.raises(store.ServiceVersionConflictError):
        store.replace_bindings(
            "service-a", "tenant-a", [binding()], expected_config_version=1
        )

    assert not any(sql.startswith("DELETE") for sql, _ in connection.statements)


@pytest.mark.parametrize(
    ("current_status", "target_status"),
    [
        ("draft", "active"),
        ("draft", "disabled"),
        ("active", "disabled"),
        ("disabled", "draft"),
        ("disabled", "active"),
    ],
)
def test_update_service_status_enforces_transition_matrix_and_version(
    monkeypatch, current_status, target_status
):
    connection = StatefulConnection()
    connection.services["service-a"]["status"] = current_status
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(connection))

    updated = store.update_service_status(
        "service-a",
        "tenant-a",
        target_status,
        expected_config_version=1,
    )

    assert updated.status == target_status
    assert updated.config_version == 2
    if target_status == "active":
        assert "FROM tenant_config" in connection.statements[0][0]
        assert "FOR UPDATE" in connection.statements[0][0]
        assert "FROM mcp_service" in connection.statements[1][0]
    lock_sql, lock_params = next(
        (sql, params)
        for sql, params in connection.statements
        if sql.startswith("SELECT service_id") and "FOR UPDATE" in sql
    )
    assert "service_id=:service_id AND tenant_id=:tenant_id" in lock_sql
    assert lock_params == {"service_id": "service-a", "tenant_id": "tenant-a"}
    update_sql, params = next(
        (sql, params)
        for sql, params in connection.statements
        if sql.startswith("UPDATE mcp_service SET status")
    )
    assert "tenant_id=:tenant_id" in update_sql
    assert "config_version=:expected_config_version" in update_sql
    assert params["tenant_id"] == "tenant-a"


@pytest.mark.parametrize("tenant_state", [None, 0])
def test_service_activation_rejects_missing_or_disabled_tenant_before_service_lock(
    monkeypatch, tenant_state
):
    connection = StatefulConnection()
    if tenant_state is None:
        connection.tenants.pop("tenant-a")
    else:
        connection.tenants["tenant-a"] = tenant_state
    connection.services["service-a"]["status"] = "disabled"
    original = dict(connection.services["service-a"])
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(connection))

    with pytest.raises(store.ServiceOwnershipError):
        store.update_service_status(
            "service-a", "tenant-a", "active", expected_config_version=1
        )

    assert connection.services["service-a"] == original
    assert len(connection.statements) == 1
    assert "FROM tenant_config" in connection.statements[0][0]
    assert "FOR UPDATE" in connection.statements[0][0]


def test_update_service_status_rejects_invalid_stale_and_foreign_changes(monkeypatch):
    connection = StatefulConnection()
    connection.services["service-a"]["status"] = "active"
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(connection))

    with pytest.raises(ValueError, match="transition"):
        store.update_service_status(
            "service-a", "tenant-a", "draft", expected_config_version=1
        )
    with pytest.raises(store.ServiceVersionConflictError):
        store.update_service_status(
            "service-a", "tenant-a", "disabled", expected_config_version=2
        )
    with pytest.raises(store.ServiceOwnershipError):
        store.update_service_status(
            "service-a", "tenant-b", "disabled", expected_config_version=1
        )

    foreign_lock = next(
        (sql, params)
        for sql, params in connection.statements
        if "FOR UPDATE" in sql and params.get("tenant_id") == "tenant-b"
    )
    assert "service_id=:service_id AND tenant_id=:tenant_id" in foreign_lock[0]
    assert not any(
        sql.startswith("UPDATE mcp_service SET status")
        for sql, _ in connection.statements
    )


def test_replace_bindings_rejects_disabled_service(monkeypatch):
    connection = StatefulConnection()
    connection.services["service-a"]["status"] = "disabled"
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(connection))

    with pytest.raises(ValueError, match="disabled"):
        store.replace_bindings(
            "service-a", "tenant-a", [binding()], expected_config_version=1
        )

    assert not any(sql.startswith("DELETE") for sql, _ in connection.statements)


def test_connection_alias_change_does_not_rewrite_materialized_tool_alias(monkeypatch):
    connection = StatefulConnection()
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(connection))

    store.replace_bindings(
        "service-a",
        "tenant-a",
        [binding(tool_alias="hq_wecom.get_users")],
        expected_config_version=1,
    )
    connection_store.update_connection_alias("conn-a", "tenant-a", "renamed")

    loaded = store.list_bindings("service-a", "tenant-a")
    assert loaded[0].tool_alias == "hq_wecom.get_users"
    assert loaded[0].policy == {"rate_limit": 10}


def test_connection_alias_defaults_deterministically_and_is_validated(monkeypatch):
    connection = StatefulConnection()
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(connection))
    record = ConnectionRecord(
        connection_id="123-existing-id",
        tenant_id="tenant-a",
        connector_key="wecom",
        display_name="WeCom",
        status="active",
        data_mode="stored",
        public_config={},
        config_version=1,
    )

    connection_store.create_connection(record)

    insert = next(
        params
        for sql, params in connection.statements
        if sql.startswith("INSERT INTO connection_instance")
    )
    assert insert["connection_alias"].startswith("conn_")
    assert len(insert["connection_alias"]) <= 64
    with pytest.raises(ValueError, match="connection_alias"):
        connection_store.update_connection_alias("conn-a", "tenant-a", "bad alias")


def test_create_connection_rejects_alias_owned_by_another_connection(monkeypatch):
    class AliasConflictConnection:
        def __init__(self):
            self.statements = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, statement, params=None):
            sql = " ".join(str(statement).split())
            params = params or {}
            self.statements.append((sql, params))
            if "tenant_id=:tenant_id" in sql and "connection_alias=:connection_alias" in sql:
                return Result({"connection_id": "conn-a"})
            return Result()

    connection = AliasConflictConnection()
    monkeypatch.setattr(db, "get_engine", lambda: FakeEngine(connection))
    record = ConnectionRecord(
        connection_id="conn-b",
        tenant_id="tenant-a",
        connector_key="wecom",
        display_name="Second WeCom",
        status="active",
        data_mode="stored",
        public_config={},
        config_version=1,
        connection_alias="shared_wecom",
    )

    with pytest.raises(
        connection_store.ConnectionAliasConflictError,
        match="connection alias",
    ):
        connection_store.create_connection(record, credentials={"secret": "new-secret"})

    assert not any(
        sql.startswith(("INSERT", "UPDATE", "DELETE"))
        for sql, _ in connection.statements
    )
