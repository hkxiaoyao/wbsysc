from types import SimpleNamespace

import pytest
from fastapi import HTTPException

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
        admin.get_mcp_config("tenant-a", request)
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
    result = admin.get_mcp_config("tenant-a", request)
    assert result == {
        "tenant_id": "tenant-a",
        "mcp_token": "token-1234",
        "trusted_domain": "mcp.example.com",
    }


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
        mcp_token="token-1234",
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
