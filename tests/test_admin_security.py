from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Response
from fastapi.testclient import TestClient

from app import admin
from app import tenant as tenant_module


def test_tenant_item_redacts_token(monkeypatch):
    monkeypatch.setattr(admin, "get_verify_by_tenant", lambda tenant_id: None)
    row = (
        "tenant-a", "客户A", "ww123", "secret-token-1234", "wbd_123", 30,
        "report", "", 0, 1, 1, "created", "updated", "", "direct",
    )
    item = admin._tenant_item(row)
    assert "mcp_token" not in item
    assert item["has_mcp_token"] is True
    assert item["mcp_token_hint"] == "1234"
    assert item["data_mode"] == "direct"


@pytest.mark.parametrize("legacy_token", ["a", "abcd"])
def test_tenant_item_masks_complete_legacy_short_token(monkeypatch, legacy_token):
    monkeypatch.setattr(admin, "get_verify_by_tenant", lambda tenant_id: None)
    row = (
        "tenant-a", "客户A", "ww123", legacy_token, "wbd_123", 30,
        "report", "", 0, 1, 1, "created", "updated", "", "stored",
    )

    item = admin._tenant_item(row)

    assert item["has_mcp_token"] is True
    assert item["mcp_token_hint"] == "****"
    assert item["mcp_token_hint"] != legacy_token


def test_direct_tenant_cannot_trigger_sync(monkeypatch):
    request = SimpleNamespace(cookies={}, headers={"Authorization": "Bearer admin"})
    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    monkeypatch.setattr(tenant_module, "reload_tenants", lambda: None)
    monkeypatch.setattr(
        tenant_module,
        "get_all_tenants",
        lambda: [SimpleNamespace(tenant_id="tenant-a", data_mode="direct")],
    )
    with pytest.raises(HTTPException) as exc:
        admin.trigger_sync("tenant-a", request)
    assert exc.value.status_code == 409


def test_mcp_config_requires_admin_session():
    request = SimpleNamespace(cookies={}, headers={})
    with pytest.raises(HTTPException) as exc:
        admin.get_mcp_config("tenant-a", request, Response())
    assert exc.value.status_code == 401


def test_mcp_config_returns_token_after_auth(monkeypatch):
    class Result:
        def fetchone(self):
            return ("tenant-a", "token-1234", "mcp.example.com")

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def execute(self, statement, values):
            return Result()

    class Engine:
        def connect(self):
            return Connection()

    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    monkeypatch.setattr(admin, "get_engine", lambda: Engine())
    request = SimpleNamespace(cookies={}, headers={})
    result = admin.get_mcp_config("tenant-a", request, Response())
    assert result == {
        "tenant_id": "tenant-a",
        "mcp_token": "token-1234",
        "trusted_domain": "mcp.example.com",
    }


def test_mcp_config_response_disables_caching(monkeypatch):
    class Result:
        def fetchone(self):
            return ("tenant-a", "token-1234", "mcp.example.com")

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def execute(self, statement, values):
            return Result()

    class Engine:
        def connect(self):
            return Connection()

    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    monkeypatch.setattr(admin, "get_engine", lambda: Engine())
    test_app = FastAPI()
    test_app.include_router(admin.router)

    response = TestClient(test_app).get("/admin/tenants/tenant-a/mcp-config")

    assert response.status_code == 200
    assert response.headers.get("cache-control") == "no-store"


def test_create_tenant_requires_mcp_token(monkeypatch):
    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def execute(self, statement, values):
            return SimpleNamespace()

    class Engine:
        def begin(self):
            return Connection()

    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    monkeypatch.setattr(admin, "ensure_domain_tables", lambda: None)
    monkeypatch.setattr(admin, "get_engine", lambda: Engine())
    monkeypatch.setattr(admin, "encrypt_secret", lambda value: f"enc:{value}")
    monkeypatch.setattr(admin, "ensure_schema", lambda schema: None)
    monkeypatch.setattr(admin, "reload_tenants", lambda: None)
    body = admin.TenantUpsert(
        tenant_id="tenant-a",
        corpid="ww123",
        secret="app-secret",
        mcp_token="",
    )
    request = SimpleNamespace(cookies={}, headers={})

    with pytest.raises(HTTPException) as exc:
        admin.create_tenant(body, request)

    assert exc.value.status_code == 400


def test_create_tenant_rejects_short_mcp_token(monkeypatch):
    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def execute(self, statement, values):
            return SimpleNamespace()

    class Engine:
        def begin(self):
            return Connection()

    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    monkeypatch.setattr(admin, "ensure_domain_tables", lambda: None)
    monkeypatch.setattr(admin, "get_engine", lambda: Engine())
    monkeypatch.setattr(admin, "encrypt_secret", lambda value: f"enc:{value}")
    monkeypatch.setattr(admin, "ensure_schema", lambda schema: None)
    monkeypatch.setattr(admin, "reload_tenants", lambda: None)
    body = admin.TenantUpsert(
        tenant_id="tenant-a",
        corpid="ww123",
        secret="app-secret",
        mcp_token="too-short",
    )

    with pytest.raises(HTTPException) as exc:
        admin.create_tenant(body, SimpleNamespace(cookies={}, headers={}))

    assert exc.value.status_code == 400


def test_create_tenant_writes_data_mode(monkeypatch):
    executed = {}

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def execute(self, statement, values):
            executed["sql"] = str(statement)
            executed["values"] = values

    class Engine:
        def begin(self):
            return Connection()

    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    monkeypatch.setattr(admin, "ensure_domain_tables", lambda: None)
    monkeypatch.setattr(admin, "get_engine", lambda: Engine())
    monkeypatch.setattr(admin, "encrypt_secret", lambda value: f"enc:{value}")
    monkeypatch.setattr(admin, "ensure_schema", lambda schema: None)
    monkeypatch.setattr(admin, "reload_tenants", lambda: None)
    body = admin.TenantUpsert(
        tenant_id="tenant-a",
        corpid="ww123",
        secret="app-secret",
        mcp_token="token-1234567890",
        data_mode="direct",
    )

    admin.create_tenant(body, SimpleNamespace(cookies={}, headers={}))

    assert "data_mode" in executed["sql"]
    assert executed["values"]["dm"] == "direct"


def test_update_tenant_keeps_existing_token_when_blank(monkeypatch):
    executed = {}

    class Result:
        def fetchone(self):
            return ("encrypted-secret", "encrypted-contact", "existing-token")

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def execute(self, statement, values):
            sql = str(statement)
            if sql.lstrip().startswith("SELECT"):
                return Result()
            executed["sql"] = sql
            executed["values"] = values
            return SimpleNamespace()

    class Engine:
        def connect(self):
            return Connection()

        def begin(self):
            return Connection()

    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    monkeypatch.setattr(admin, "ensure_domain_tables", lambda: None)
    monkeypatch.setattr(admin, "get_engine", lambda: Engine())
    monkeypatch.setattr(admin, "ensure_schema", lambda schema: None)
    monkeypatch.setattr(admin, "reload_tenants", lambda: None)
    body = admin.TenantUpsert(
        tenant_id="tenant-a",
        corpid="ww123",
        mcp_token="",
        data_mode="direct",
    )

    admin.update_tenant("tenant-a", body, SimpleNamespace(cookies={}, headers={}))

    assert executed["values"]["mt"] == "existing-token"
    assert executed["values"]["dm"] == "direct"
    assert "data_mode=:dm" in executed["sql"]


def test_update_tenant_rejects_short_replacement_token(monkeypatch):
    class Result:
        def fetchone(self):
            return ("encrypted-secret", "encrypted-contact", "existing-legacy-token")

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def execute(self, statement, values):
            if str(statement).lstrip().startswith("SELECT"):
                return Result()
            return SimpleNamespace()

    class Engine:
        def connect(self):
            return Connection()

        def begin(self):
            return Connection()

    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    monkeypatch.setattr(admin, "ensure_domain_tables", lambda: None)
    monkeypatch.setattr(admin, "get_engine", lambda: Engine())
    monkeypatch.setattr(admin, "ensure_schema", lambda schema: None)
    monkeypatch.setattr(admin, "reload_tenants", lambda: None)
    body = admin.TenantUpsert(
        tenant_id="tenant-a",
        corpid="ww123",
        mcp_token="too-short",
    )

    with pytest.raises(HTTPException) as exc:
        admin.update_tenant("tenant-a", body, SimpleNamespace(cookies={}, headers={}))

    assert exc.value.status_code == 400


def test_tenant_list_falls_back_only_for_missing_data_mode_column(monkeypatch, caplog):
    calls = []

    class UnknownDataModeColumn(Exception):
        def __init__(self):
            super().__init__("statement failed")
            self.orig = SimpleNamespace(
                args=(1054, "Unknown column 'data_mode' in 'field list'")
            )

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def execute(self, statement):
            calls.append(str(statement))
            if len(calls) == 1:
                raise UnknownDataModeColumn()
            return Result([(
                "tenant-a", "客户A", "ww123", "legacy-token-1234", "wbd_123", 30,
                "report", "", 0, 1, 1, "created", "updated",
            )])

    class Engine:
        def connect(self):
            return Connection()

    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    monkeypatch.setattr(admin, "ensure_domain_tables", lambda: None)
    monkeypatch.setattr(admin, "get_engine", lambda: Engine())
    monkeypatch.setattr(admin, "get_verify_by_tenant", lambda tenant_id: None)

    with caplog.at_level("WARNING", logger="wecom-gateway"):
        result = admin.list_tenants(SimpleNamespace(cookies={}, headers={}))

    assert len(calls) == 2
    assert result["items"][0]["data_mode"] == "stored"
    assert "data_mode" in caplog.text


def test_tenant_list_does_not_hide_unrelated_sql_errors(monkeypatch):
    calls = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def execute(self, statement):
            calls.append(str(statement))
            raise RuntimeError("database connection lost")

    class Engine:
        def connect(self):
            return Connection()

    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    monkeypatch.setattr(admin, "ensure_domain_tables", lambda: None)
    monkeypatch.setattr(admin, "get_engine", lambda: Engine())

    with pytest.raises(RuntimeError, match="database connection lost"):
        admin.list_tenants(SimpleNamespace(cookies={}, headers={}))

    assert len(calls) == 1


def test_direct_tenant_cannot_trigger_rollback(monkeypatch):
    request = SimpleNamespace(cookies={}, headers={})
    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    monkeypatch.setattr(tenant_module, "reload_tenants", lambda: None)
    monkeypatch.setattr(
        tenant_module,
        "get_all_tenants",
        lambda: [SimpleNamespace(tenant_id="tenant-a", data_mode="direct")],
    )

    with pytest.raises(HTTPException) as exc:
        admin.trigger_sync("tenant-a", request, reset_cursor=True)

    assert exc.value.status_code == 409


def test_direct_tenant_cannot_run_sync_diagnosis(monkeypatch):
    from app.wecom import dispatch

    class Result:
        def fetchone(self):
            return (0, None)

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def execute(self, statement):
            return Result()

    class Engine:
        def connect(self):
            return Connection()

    request = SimpleNamespace(cookies={}, headers={})
    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    monkeypatch.setattr(admin, "get_engine", lambda: Engine())
    monkeypatch.setattr(tenant_module, "reload_tenants", lambda: None)
    monkeypatch.setattr(
        tenant_module,
        "get_all_tenants",
        lambda: [SimpleNamespace(
            tenant_id="tenant-a",
            data_mode="direct",
            schema_name="wbd_123",
        )],
    )
    monkeypatch.setattr(dispatch, "diagnose_report_pull", lambda tenant, lookback_days: {})

    with pytest.raises(HTTPException) as exc:
        admin.sync_diagnose("tenant-a", request)

    assert exc.value.status_code == 409
