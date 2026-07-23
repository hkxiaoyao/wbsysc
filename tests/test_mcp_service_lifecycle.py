from __future__ import annotations

from dataclasses import replace
from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import admin, admin_connections, db
from app.connections import store as connection_store
from app.connections.models import ConnectionRecord
from app.mcp_services import store as service_store
from app.mcp_services.models import McpService, ServiceToolBinding
from app import tenant_lifecycle


def _connection(**changes) -> ConnectionRecord:
    item = ConnectionRecord(
        connection_id="conn-a",
        tenant_id="tenant-a",
        connector_key="http_declarative",
        connection_alias="declarative",
        display_name="Declarative",
        status="disabled",
        data_mode="direct",
        public_config={},
        config_version=3,
    )
    return replace(item, **changes)


def _service(service_id: str = "service-a", display_name: str = "Operations") -> McpService:
    return McpService(
        service_id=service_id,
        tenant_id="tenant-a",
        display_name=display_name,
        service_key=service_id,
        status="active",
        config_version=1,
    )


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


class _Engine:
    def __init__(self, connection):
        self.connection = connection

    def connect(self):
        return self.connection

    def begin(self):
        return self.connection


class _LifecycleConnection:
    def __init__(
        self,
        *,
        service_rows=(),
        revision_status: str = "draft",
        public_config: dict | None = None,
        revision_binding: bool = False,
        hidden_binding: bool = False,
        connector_key: str = "http_declarative",
    ):
        self.service_rows = list(service_rows)
        self.revision_status = revision_status
        self.public_config = dict(public_config or {})
        self.revision_binding = revision_binding
        self.hidden_binding = hidden_binding
        self.connector_key = connector_key
        self.statements: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        values = dict(params or {})
        self.statements.append((sql, values))
        if sql.startswith("DELETE FROM connection_instance"):
            return _Result(rowcount=1)
        if sql.startswith("DELETE FROM declarative_spec_operation"):
            return _Result(rowcount=2)
        if sql.startswith("DELETE FROM declarative_spec_revision"):
            return _Result(rowcount=1)
        if "FROM connection_instance" in sql and "LIMIT 1" in sql:
            if values.get("tenant_id") != "tenant-a":
                return _Result(row=None)
            row = _connection(
                connector_key=self.connector_key,
                public_config=self.public_config,
            )
            return _Result(
                row={
                    "connection_id": row.connection_id,
                    "tenant_id": row.tenant_id,
                    "connector_key": row.connector_key,
                    "connection_alias": row.connection_alias,
                    "display_name": row.display_name,
                    "status": row.status,
                    "data_mode": row.data_mode,
                    "public_config_json": __import__("json").dumps(row.public_config),
                    "config_version": row.config_version,
                }
            )
        if "FROM declarative_spec_revision" in sql:
            return _Result(row={"status": self.revision_status})
        if "FROM mcp_service_tool_binding" in sql and "declarative_spec_operation" in sql:
            return _Result(scalar=1 if self.revision_binding else None)
        if "FROM mcp_service_tool_binding" in sql and "JOIN mcp_service" in sql:
            rows = self.service_rows if values.get("tenant_id") == "tenant-a" else []
            return _Result(rows=rows)
        if "FROM mcp_service_tool_binding" in sql:
            return _Result(
                scalar=1 if self.hidden_binding or self.service_rows else None
            )
        if sql.startswith("UPDATE connection_instance SET status='disabled'"):
            return _Result(rowcount=1)
        return _Result()


class _BindingOrderConnection:
    def __init__(self):
        self.statements: list[tuple[str, dict]] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        values = dict(params or {})
        self.statements.append((sql, values))
        if "FROM connection_instance" in sql and "connection_id IN" in sql:
            return _Result(
                rows=[{"connection_id": "conn-a", "tenant_id": "tenant-a"}]
            )
        if "FROM mcp_service" in sql and "LIMIT 1 FOR UPDATE" in sql:
            return _Result(
                row={
                    "service_id": "service-a",
                    "tenant_id": "tenant-a",
                    "display_name": "Operations",
                    "service_key": "operations",
                    "status": "active",
                    "config_version": 1,
                }
            )
        if "FROM mcp_service_tool_binding" in sql:
            return _Result(rows=[])
        if sql.startswith("UPDATE mcp_service SET config_version"):
            return _Result(rowcount=1)
        return _Result()


def _service_rows():
    return [
        {
            "service_id": "service-a",
            "tenant_id": "tenant-a",
            "display_name": "Operations",
            "service_key": "operations",
            "status": "active",
            "config_version": 4,
        },
        {
            "service_id": "service-b",
            "tenant_id": "tenant-a",
            "display_name": "Finance",
            "service_key": "finance",
            "status": "disabled",
            "config_version": 2,
        },
    ]


class _TenantRetirementConnection:
    def __init__(self, *, fail_fragment: str | None = None, tenant_exists: bool = True):
        self.fail_fragment = fail_fragment
        self.tenant_exists = tenant_exists
        self.statements: list[tuple[str, dict]] = []

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        values = dict(params or {})
        self.statements.append((sql, values))
        if self.fail_fragment and self.fail_fragment in sql:
            raise RuntimeError("injected lifecycle failure")
        if sql.startswith("SELECT tenant_id, schema_name FROM tenant_config"):
            return _Result(
                row={"tenant_id": "tenant-a", "schema_name": "wbd_private"}
                if self.tenant_exists
                else None
            )
        if sql.startswith("SELECT connection_id, config_version"):
            return _Result(rows=[
                {"connection_id": "conn-b", "config_version": 8},
                {"connection_id": "conn-z", "config_version": 3},
            ])
        if sql.startswith("SELECT service_id, config_version"):
            return _Result(rows=[
                {"service_id": "service-a", "config_version": 4},
                {"service_id": "service-y", "config_version": 2},
            ])
        if sql.startswith("UPDATE mcp_service_token"):
            return _Result(rowcount=3)
        if sql.startswith("UPDATE connection_token"):
            return _Result(rowcount=5)
        if sql.startswith("DELETE FROM tenant_config"):
            return _Result(rowcount=1)
        return _Result(rowcount=1)


class _TenantRetirementEngine:
    def __init__(self, connection):
        self.connection = connection
        self.committed = False

    def begin(self):
        engine = self

        class Transaction:
            def __enter__(self):
                return engine.connection

            def __exit__(self, exc_type, _exc, _traceback):
                engine.committed = exc_type is None
                return False

        return Transaction()


class _StatefulRetirementConnection(_TenantRetirementConnection):
    def __init__(self, *, fail_fragment: str | None = None):
        super().__init__(fail_fragment=None)
        self.failure_after = fail_fragment
        self.state = {
            "tenants": {
                "tenant-a": {"schema_name": "wbd_a", "mcp_token": "legacy-a"},
                "tenant-b": {"schema_name": "wbd_b", "mcp_token": "legacy-b"},
            },
            "connections": {
                "conn-a": {"tenant_id": "tenant-a", "status": "active", "config_version": 3},
                "conn-b": {"tenant_id": "tenant-b", "status": "active", "config_version": 7},
            },
            "services": {
                "service-a": {"tenant_id": "tenant-a", "status": "active", "config_version": 4},
                "service-b": {"tenant_id": "tenant-b", "status": "active", "config_version": 9},
            },
            "service_tokens": [
                {"id": "sta", "service_id": "service-a", "revoked": False, "encrypted": "cipher-a"},
                {"id": "stb", "service_id": "service-b", "revoked": False, "encrypted": "cipher-b"},
            ],
            "connection_tokens": [
                {"id": "cta", "connection_id": "conn-a", "revoked": False},
                {"id": "ctb", "connection_id": "conn-b", "revoked": False},
            ],
            "accounts": {"tenant-a", "tenant-b"},
            "sessions": {"tenant-a": ["sa"], "tenant-b": ["sb"]},
            "verify": {"tenant-a", "tenant-b"},
            "audit": [],
        }

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        values = dict(params or {})
        self.statements.append((sql, values))
        tenant_id = values.get("tenant_id")
        result = _Result(rowcount=1)
        if sql.startswith("SELECT tenant_id, schema_name FROM tenant_config"):
            tenant = self.state["tenants"].get(tenant_id)
            result = _Result(row={"tenant_id": tenant_id, **tenant} if tenant else None)
        elif sql.startswith("SELECT connection_id, config_version"):
            result = _Result(rows=[
                {"connection_id": key, "config_version": row["config_version"]}
                for key, row in sorted(self.state["connections"].items())
                if row["tenant_id"] == tenant_id
            ])
        elif sql.startswith("SELECT service_id, config_version"):
            result = _Result(rows=[
                {"service_id": key, "config_version": row["config_version"]}
                for key, row in sorted(self.state["services"].items())
                if row["tenant_id"] == tenant_id
            ])
        elif sql.startswith("UPDATE mcp_service SET"):
            for row in self.state["services"].values():
                if row["tenant_id"] == tenant_id and row["status"] != "disabled":
                    row["status"] = "disabled"
                    row["config_version"] += 1
        elif sql.startswith("UPDATE mcp_service_token"):
            count = 0
            for token in self.state["service_tokens"]:
                owner = self.state["services"].get(token["service_id"])
                if owner and owner["tenant_id"] == tenant_id and (
                    not token["revoked"] or token["encrypted"] is not None
                ):
                    token["revoked"] = True
                    token["encrypted"] = None
                    count += 1
            result = _Result(rowcount=count)
        elif sql.startswith("UPDATE connection_instance SET"):
            for row in self.state["connections"].values():
                if row["tenant_id"] == tenant_id and row["status"] != "disabled":
                    row["status"] = "disabled"
                    row["config_version"] += 1
        elif sql.startswith("UPDATE connection_token"):
            count = 0
            for token in self.state["connection_tokens"]:
                owner = self.state["connections"].get(token["connection_id"])
                if owner and owner["tenant_id"] == tenant_id and not token["revoked"]:
                    token["revoked"] = True
                    count += 1
            result = _Result(rowcount=count)
        elif sql.startswith("DELETE FROM tenant_session"):
            self.state["sessions"].pop(tenant_id, None)
        elif sql.startswith("DELETE FROM tenant_account"):
            self.state["accounts"].discard(tenant_id)
        elif sql.startswith("DELETE FROM domain_verify_file"):
            self.state["verify"].discard(tenant_id)
        elif sql.startswith("INSERT INTO mcp_call_log"):
            self.state["audit"].append(dict(values))
        elif sql.startswith("DELETE FROM tenant_config"):
            existed = self.state["tenants"].pop(tenant_id, None) is not None
            result = _Result(rowcount=1 if existed else 0)
        if self.failure_after and self.failure_after in sql:
            raise RuntimeError("injected lifecycle failure")
        return result


class _StatefulRetirementEngine(_TenantRetirementEngine):
    def begin(self):
        engine = self
        snapshot = deepcopy(engine.connection.state)

        class Transaction:
            def __enter__(self):
                return engine.connection

            def __exit__(self, exc_type, _exc, _traceback):
                engine.committed = exc_type is None
                if exc_type is not None:
                    engine.connection.state = snapshot
                return False

        return Transaction()


def test_tenant_retirement_is_one_ordered_history_preserving_transaction(monkeypatch):
    connection = _TenantRetirementConnection()
    engine = _TenantRetirementEngine(connection)
    monkeypatch.setattr(tenant_lifecycle, "get_engine", lambda: engine)

    result = tenant_lifecycle.retire_tenant(
        "tenant-a", request_id="request-a", client_ip="127.0.0.1"
    )

    assert engine.committed is True
    assert result.connection_versions == (("conn-b", 8), ("conn-z", 3))
    assert result.service_ids == ("service-a", "service-y")
    sql = [statement for statement, _ in connection.statements]
    assert "FROM tenant_config" in sql[0] and "FOR UPDATE" in sql[0]
    assert "FROM connection_instance" in sql[1] and "ORDER BY connection_id" in sql[1]
    assert "FROM mcp_service" in sql[2] and "ORDER BY service_id" in sql[2]
    assert sql[-1].startswith("DELETE FROM tenant_config")
    audit_index = next(i for i, value in enumerate(sql) if value.startswith("INSERT INTO mcp_call_log"))
    assert audit_index == len(sql) - 2
    audit_params = connection.statements[audit_index][1]
    assert audit_params["params_summary"] == (
        "services=2,connections=2,service_tokens=3,connection_tokens=5"
    )
    assert "wbd_private" not in repr(audit_params)
    assert all("DROP " not in value.upper() for value in sql)
    protected = (
        "mcp_service ", "mcp_service_token", "connection_instance",
        "connection_credential", "connection_token", "mcp_call_log",
    )
    assert not any(
        value.startswith("DELETE FROM") and any(name in value for name in protected)
        for value in sql
    )


@pytest.mark.parametrize(
    "failure",
    [
        "UPDATE mcp_service SET",
        "UPDATE mcp_service_token",
        "UPDATE connection_instance SET",
        "UPDATE connection_token",
        "DELETE FROM tenant_session",
        "DELETE FROM tenant_account",
        "DELETE FROM domain_verify_file",
        "INSERT INTO mcp_call_log",
        "DELETE FROM tenant_config",
    ],
)
def test_tenant_retirement_failure_never_commits_or_invalidates(
    monkeypatch, failure
):
    connection = _TenantRetirementConnection(fail_fragment=failure)
    engine = _TenantRetirementEngine(connection)
    invalidations = []
    monkeypatch.setattr(tenant_lifecycle, "get_engine", lambda: engine)
    monkeypatch.setattr(
        tenant_lifecycle.connection_store,
        "invalidate_connection_cache",
        lambda *args: invalidations.append(args),
    )

    with pytest.raises(RuntimeError, match="injected lifecycle failure"):
        tenant_lifecycle.retire_tenant("tenant-a")

    assert engine.committed is False
    assert invalidations == []


@pytest.mark.parametrize(
    "failure",
    [
        "UPDATE mcp_service SET",
        "UPDATE mcp_service_token",
        "UPDATE connection_instance SET",
        "UPDATE connection_token",
        "DELETE FROM tenant_session",
        "DELETE FROM tenant_account",
        "DELETE FROM domain_verify_file",
        "INSERT INTO mcp_call_log",
        "DELETE FROM tenant_config",
    ],
)
def test_failure_after_each_retirement_phase_restores_exact_state(monkeypatch, failure):
    connection = _StatefulRetirementConnection(fail_fragment=failure)
    original = deepcopy(connection.state)
    engine = _StatefulRetirementEngine(connection)
    monkeypatch.setattr(tenant_lifecycle, "get_engine", lambda: engine)

    with pytest.raises(RuntimeError, match="injected lifecycle failure"):
        tenant_lifecycle.retire_tenant("tenant-a")

    assert connection.state == original
    assert engine.committed is False


def test_successful_retirement_isolates_tenant_b_and_retry_catches_new_token(monkeypatch):
    connection = _StatefulRetirementConnection(fail_fragment="INSERT INTO mcp_call_log")
    engine = _StatefulRetirementEngine(connection)
    monkeypatch.setattr(tenant_lifecycle, "get_engine", lambda: engine)
    before_b = {
        key: deepcopy(value)
        for key, value in connection.state.items()
        if key in {"connections", "services", "service_tokens", "connection_tokens"}
    }

    with pytest.raises(RuntimeError):
        tenant_lifecycle.retire_tenant("tenant-a")
    connection.state["service_tokens"].append(
        {"id": "issued-after-rollback", "service_id": "service-a", "revoked": False, "encrypted": "cipher-new"}
    )
    connection.failure_after = None
    result = tenant_lifecycle.retire_tenant("tenant-a")

    assert result.service_token_count == 2
    assert "tenant-a" not in connection.state["tenants"]
    assert connection.state["connections"]["conn-a"]["status"] == "disabled"
    assert connection.state["services"]["service-a"]["status"] == "disabled"
    assert all(
        token["revoked"] and token["encrypted"] is None
        for token in connection.state["service_tokens"]
        if token["service_id"] == "service-a"
    )
    assert next(
        token for token in connection.state["connection_tokens"]
        if token["connection_id"] == "conn-a"
    )["revoked"] is True
    assert connection.state["connections"]["conn-b"] == before_b["connections"]["conn-b"]
    assert connection.state["services"]["service-b"] == before_b["services"]["service-b"]
    assert next(token for token in connection.state["service_tokens"] if token["id"] == "stb") == next(
        token for token in before_b["service_tokens"] if token["id"] == "stb"
    )
    assert connection.state["audit"][-1]["tenant_id"] == "tenant-a"


def test_post_commit_invalidation_continues_and_reloads_last(monkeypatch):
    events = []
    retirement = tenant_lifecycle.TenantRetirement(
        "tenant-a", (("conn-a", 3), ("conn-b", 4)), ("svc-a", "svc-b"), 1, 1
    )

    def invalidate_connection(connection_id, version):
        events.append(("connection", connection_id, version))
        if connection_id == "conn-a":
            raise RuntimeError("cache unavailable")

    def invalidate_service(service_id):
        events.append(("service", service_id))
        if service_id == "svc-a":
            raise RuntimeError("cache unavailable")

    monkeypatch.setattr(
        tenant_lifecycle.connection_store,
        "invalidate_connection_cache",
        invalidate_connection,
    )
    monkeypatch.setattr(
        tenant_lifecycle.service_store,
        "invalidate_service_cache",
        invalidate_service,
    )

    tenant_lifecycle.invalidate_retired_tenant(
        retirement, reload_tenants=lambda: events.append(("reload",))
    )

    assert events == [
        ("connection", "conn-a", 3),
        ("connection", "conn-b", 4),
        ("service", "svc-a"),
        ("service", "svc-b"),
        ("reload",),
    ]


def test_service_activation_locks_live_tenant_before_retained_service(monkeypatch):
    class ActivationConnection:
        def __init__(self, enabled):
            self.enabled = enabled
            self.statements = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement, params=None):
            sql = " ".join(str(statement).split())
            self.statements.append((sql, dict(params or {})))
            if sql.startswith("SELECT enabled FROM tenant_config"):
                return _Result(scalar=1 if self.enabled else None)
            if sql.startswith("SELECT service_id, tenant_id"):
                return _Result(row={
                    "service_id": "service-a",
                    "tenant_id": "tenant-a",
                    "display_name": "Operations",
                    "service_key": "operations",
                    "status": "disabled",
                    "config_version": 4,
                })
            return _Result(rowcount=1)

    live = ActivationConnection(True)
    monkeypatch.setattr(db, "get_engine", lambda: _Engine(live))
    updated = service_store.update_service_status(
        "service-a", "tenant-a", "active", expected_config_version=4
    )
    assert updated.status == "active"
    assert "FROM tenant_config" in live.statements[0][0]
    assert "FOR UPDATE" in live.statements[0][0]
    assert "FROM mcp_service" in live.statements[1][0]

    deleted = ActivationConnection(False)
    monkeypatch.setattr(db, "get_engine", lambda: _Engine(deleted))
    with pytest.raises(service_store.ServiceOwnershipError):
        service_store.update_service_status(
            "service-a", "tenant-a", "active", expected_config_version=4
        )
    assert len(deleted.statements) == 1
    assert "FROM tenant_config" in deleted.statements[0][0]


def test_declarative_activation_locks_live_tenant_before_connection_or_revision(
    monkeypatch,
):
    from app.connectors.declarative import validator

    class ActivationConnection:
        def __init__(self, enabled):
            self.enabled = enabled
            self.statements = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement, params=None):
            sql = " ".join(str(statement).split())
            self.statements.append((sql, dict(params or {})))
            if sql.startswith("SELECT enabled FROM tenant_config"):
                return _Result(scalar=1 if self.enabled else None)
            if sql.startswith("SELECT connection_id, tenant_id"):
                return _Result(row={
                    "connection_id": "conn-a",
                    "tenant_id": "tenant-a",
                    "connector_key": "http_declarative",
                    "connection_alias": "declarative",
                    "display_name": "Declarative",
                    "status": "disabled",
                    "data_mode": "direct",
                    "public_config_json": '{"spec_id":"spec-a","revision":1}',
                    "config_version": 3,
                })
            if sql.startswith("SELECT spec_id, revision"):
                return _Result(row={
                    "spec_id": "spec-a",
                    "revision": 1,
                    "tenant_id": "tenant-a",
                    "connection_id": "conn-a",
                    "status": "published",
                    "spec_json": "{}",
                })
            return _Result(rowcount=1)

    live = ActivationConnection(True)
    monkeypatch.setattr(db, "get_engine", lambda: _Engine(live))
    monkeypatch.setattr(
        connection_store,
        "_declarative_revision_from_row",
        lambda row: SimpleNamespace(status="published"),
    )
    monkeypatch.setattr(
        validator, "validate_revision", lambda revision, data_mode=None: revision
    )
    activated = connection_store.activate_declarative_revision(
        "spec-a", 1, "tenant-a", "conn-a", expected_config_version=3
    )
    assert activated is not None and activated.status == "active"
    assert "FROM tenant_config" in live.statements[0][0]
    assert "FROM connection_instance" in live.statements[1][0]
    assert "FROM declarative_spec_revision" in live.statements[2][0]

    deleted = ActivationConnection(False)
    monkeypatch.setattr(db, "get_engine", lambda: _Engine(deleted))
    with pytest.raises(ValueError, match="tenant is not active"):
        connection_store.activate_declarative_revision(
            "spec-a", 1, "tenant-a", "conn-a", expected_config_version=3
        )
    assert len(deleted.statements) == 1


def test_list_service_references_is_tenant_scoped(monkeypatch):
    connection = _LifecycleConnection(service_rows=_service_rows())
    monkeypatch.setattr(db, "get_engine", lambda: _Engine(connection))

    items = service_store.list_service_references("conn-a", "tenant-a")
    foreign_probe = service_store.list_service_references("conn-a", "tenant-b")

    assert [(item.service_id, item.display_name) for item in items] == [
        ("service-a", "Operations"),
        ("service-b", "Finance"),
    ]
    assert foreign_probe == []
    reference_queries = [
        (sql, params)
        for sql, params in connection.statements
        if "FROM mcp_service_tool_binding" in sql
    ]
    assert all("tenant_id=:tenant_id" in sql for sql, _ in reference_queries)
    assert reference_queries[-1][1]["tenant_id"] == "tenant-b"


def test_referenced_connection_delete_returns_safe_conflict_payload(monkeypatch):
    app = FastAPI()
    app.include_router(admin_connections.router)
    monkeypatch.setattr(admin, "_is_authed", lambda request: True)
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda *args: _connection())
    monkeypatch.setattr(admin_connections, "write_event", lambda event: True)

    def reject(*args):
        raise service_store.ServiceReferenceConflictError(
            (_service(), _service("service-b", "Finance"))
        )

    monkeypatch.setattr(admin_connections.store, "delete_connection", reject)

    response = TestClient(app).delete(
        "/admin/tenants/tenant-a/connections/conn-a"
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "message": "connection is referenced by MCP services",
        "services": [
            {"service_id": "service-a", "display_name": "Operations"},
            {"service_id": "service-b", "display_name": "Finance"},
        ],
    }


def test_cross_tenant_delete_does_not_probe_or_reveal_foreign_services(monkeypatch):
    app = FastAPI()
    app.include_router(admin_connections.router)
    monkeypatch.setattr(admin, "_is_authed", lambda request: True)
    monkeypatch.setattr(
        admin_connections.store,
        "get_connection",
        lambda connection_id, tenant_id=None: (
            _connection() if tenant_id in {None, "tenant-a"} else None
        ),
    )
    called = []
    monkeypatch.setattr(
        admin_connections.store,
        "delete_connection",
        lambda *args: called.append(args),
    )

    response = TestClient(app).delete(
        "/admin/tenants/tenant-b/connections/conn-a"
    )

    assert response.status_code == 404
    assert "service" not in response.text.lower()
    assert called == []


def test_unreferenced_connection_delete_removes_only_connection_row(monkeypatch):
    connection = _LifecycleConnection()
    monkeypatch.setattr(db, "get_engine", lambda: _Engine(connection))
    monkeypatch.setattr(service_store, "invalidate_services_for_connection", lambda value: None)

    assert connection_store.delete_connection("conn-a", "tenant-a") is True

    deletes = [sql for sql, _ in connection.statements if sql.startswith("DELETE FROM")]
    assert deletes == [
        "DELETE FROM connection_instance WHERE connection_id=:connection_id AND tenant_id=:tenant_id"
    ]


def test_wecom_connection_delete_removes_its_domain_file_in_same_transaction(
    monkeypatch,
):
    connection = _LifecycleConnection(connector_key="wecom")
    monkeypatch.setattr(db, "get_engine", lambda: _Engine(connection))
    monkeypatch.setattr(
        service_store,
        "invalidate_services_for_connection",
        lambda value: None,
    )

    assert connection_store.delete_connection("conn-a", "tenant-a") is True

    deletes = [sql for sql, _ in connection.statements if sql.startswith("DELETE FROM")]
    assert deletes == [
        "DELETE FROM domain_verify_file WHERE connection_id=:connection_id AND tenant_id=:tenant_id",
        "DELETE FROM connection_instance WHERE connection_id=:connection_id AND tenant_id=:tenant_id",
    ]


def test_referenced_connection_store_guard_performs_no_delete(monkeypatch):
    connection = _LifecycleConnection(service_rows=_service_rows())
    monkeypatch.setattr(db, "get_engine", lambda: _Engine(connection))

    with pytest.raises(service_store.ServiceReferenceConflictError) as raised:
        connection_store.delete_connection("conn-a", "tenant-a")

    assert [item.service_id for item in raised.value.services] == [
        "service-a",
        "service-b",
    ]
    assert not any(sql.startswith("DELETE FROM") for sql, _ in connection.statements)


def test_connection_delete_locks_connection_before_service_metadata(monkeypatch):
    connection = _LifecycleConnection(service_rows=_service_rows())
    monkeypatch.setattr(db, "get_engine", lambda: _Engine(connection))

    with pytest.raises(service_store.ServiceReferenceConflictError):
        connection_store.delete_connection("conn-a", "tenant-a")

    locking_reads = [
        sql for sql, _ in connection.statements if "FOR UPDATE" in sql
    ]
    assert "FROM connection_instance" in locking_reads[0]
    assert "JOIN mcp_service" in locking_reads[1]


def test_binding_replacement_locks_connections_before_service(monkeypatch):
    connection = _BindingOrderConnection()
    monkeypatch.setattr(db, "get_engine", lambda: _Engine(connection))
    item = ServiceToolBinding(
        binding_id="binding-a",
        service_id="service-a",
        connection_id="conn-a",
        source_tool_key="users.get",
        tool_alias="users_get",
        binding_status="active",
        policy={},
    )

    service_store.replace_bindings(
        "service-a",
        "tenant-a",
        [item],
        expected_config_version=1,
    )

    locking_reads = [
        sql for sql, _ in connection.statements if "FOR UPDATE" in sql
    ]
    assert "FROM connection_instance" in locking_reads[0]
    assert "FROM mcp_service" in locking_reads[1]


def test_foreign_or_corrupt_binding_blocks_delete_without_revealing_metadata(monkeypatch):
    connection = _LifecycleConnection(hidden_binding=True)
    monkeypatch.setattr(db, "get_engine", lambda: _Engine(connection))

    with pytest.raises(service_store.ServiceReferenceConflictError) as raised:
        connection_store.delete_connection("conn-a", "tenant-a")

    assert raised.value.services == ()
    assert not any(sql.startswith("DELETE FROM") for sql, _ in connection.statements)


def test_published_active_revision_cannot_be_deleted(monkeypatch):
    connection = _LifecycleConnection(
        revision_status="published",
        public_config={"spec_id": "spec-a", "revision": 2},
    )
    monkeypatch.setattr(db, "get_engine", lambda: _Engine(connection))

    with pytest.raises(connection_store.DeclarativeRevisionInUseError):
        connection_store.delete_declarative_revision(
            "spec-a", 2, "tenant-a", "conn-a"
        )

    assert not any(sql.startswith("DELETE FROM") for sql, _ in connection.statements)


@pytest.mark.parametrize(
    "public_config",
    [
        {"spec_id": "spec-a", "revision": "2"},
        {"spec_id": "spec-a", "revision": True},
        {"spec_id": "spec-a"},
        {"revision": 2},
        {"spec_id": "", "revision": 2},
        {"pending_spec_id": "spec-a", "pending_revision": "2"},
        {"pending_spec_id": "spec-a"},
        {"pending_revision": 2},
    ],
)
def test_malformed_revision_selectors_fail_closed(monkeypatch, public_config):
    connection = _LifecycleConnection(
        revision_status="published",
        public_config=public_config,
    )
    monkeypatch.setattr(db, "get_engine", lambda: _Engine(connection))

    with pytest.raises(connection_store.DeclarativeRevisionInUseError):
        connection_store.delete_declarative_revision(
            "spec-a", 2, "tenant-a", "conn-a"
        )

    assert not any(sql.startswith("DELETE FROM") for sql, _ in connection.statements)


def test_revision_delete_locks_connection_before_revision(monkeypatch):
    connection = _LifecycleConnection(revision_status="draft")
    monkeypatch.setattr(db, "get_engine", lambda: _Engine(connection))

    assert connection_store.delete_declarative_revision(
        "spec-a", 2, "tenant-a", "conn-a"
    ) is True

    locking_reads = [
        sql for sql, _ in connection.statements if "FOR UPDATE" in sql
    ]
    assert "FROM connection_instance" in locking_reads[0]
    assert "FROM declarative_spec_revision" in locking_reads[1]


def test_published_revision_used_by_service_binding_cannot_be_deleted(monkeypatch):
    connection = _LifecycleConnection(
        revision_status="published",
        revision_binding=True,
    )
    monkeypatch.setattr(db, "get_engine", lambda: _Engine(connection))

    with pytest.raises(connection_store.DeclarativeRevisionInUseError):
        connection_store.delete_declarative_revision(
            "spec-a", 2, "tenant-a", "conn-a"
        )

    probe = next(
        (sql, params)
        for sql, params in connection.statements
        if "declarative_spec_operation" in sql
    )
    assert all(
        f"{field}=:{field}" in probe[0]
        for field in ("spec_id", "revision", "tenant_id", "connection_id")
    )
    assert probe[1] == {
        "spec_id": "spec-a",
        "revision": 2,
        "tenant_id": "tenant-a",
        "connection_id": "conn-a",
    }


def test_draft_revision_delete_removes_only_revision_storage(monkeypatch):
    connection = _LifecycleConnection(revision_status="draft")
    monkeypatch.setattr(db, "get_engine", lambda: _Engine(connection))

    assert connection_store.delete_declarative_revision(
        "spec-a", 2, "tenant-a", "conn-a"
    ) is True

    deletes = [sql for sql, _ in connection.statements if sql.startswith("DELETE FROM")]
    assert [sql.split()[2] for sql in deletes] == [
        "declarative_spec_operation",
        "declarative_spec_revision",
    ]


def test_unknown_revision_status_fails_closed(monkeypatch):
    connection = _LifecycleConnection(revision_status="corrupt")
    monkeypatch.setattr(db, "get_engine", lambda: _Engine(connection))

    with pytest.raises(connection_store.DeclarativeRevisionInUseError):
        connection_store.delete_declarative_revision(
            "spec-a", 2, "tenant-a", "conn-a"
        )

    assert not any(sql.startswith("DELETE FROM") for sql, _ in connection.statements)


def test_disable_remains_allowed_and_invalidates_referencing_services(monkeypatch):
    connection = _LifecycleConnection(service_rows=_service_rows())
    monkeypatch.setattr(db, "get_engine", lambda: _Engine(connection))
    invalidated = []
    monkeypatch.setattr(
        service_store,
        "invalidate_services_for_connection",
        invalidated.append,
    )

    disabled = connection_store.disable_connection("conn-a", "tenant-a")

    assert disabled is not None and disabled.status == "disabled"
    assert invalidated == ["conn-a"]
