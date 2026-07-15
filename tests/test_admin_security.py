from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Response
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Mount

from app import admin
from app import auth as auth_module
from app import tenant as tenant_module


def _auth_test_client():
    app = FastAPI()
    app.add_middleware(auth_module.BearerTokenMiddleware)

    @app.get("/secure")
    def secure():
        return {"tenant": auth_module.current_ctx().tenant_id}

    return TestClient(app)


@pytest.mark.parametrize(
    ("headers", "event_name"),
    [({}, "auth_missing"), ({"Authorization": "Bearer invalid-token"}, "auth_invalid")],
)
def test_bearer_auth_failures_emit_safe_rate_limited_events(
    monkeypatch, headers, event_name
):
    events = []
    monkeypatch.setattr(auth_module, "write_event", events.append)
    monkeypatch.setattr(auth_module, "_auth_write_limiter", auth_module.AuthWriteLimiter())
    monkeypatch.setattr(auth_module, "get_tenant_by_token", lambda token: None)
    monkeypatch.setattr(auth_module, "reload_tenants", lambda: None)

    response = _auth_test_client().get("/secure", headers=headers)

    assert response.status_code == 401
    assert len(events) == 1
    assert events[0].category == "auth"
    assert events[0].event_name == event_name
    assert events[0].result_status == "denied"
    assert events[0].tenant_id == ""
    assert "invalid-token" not in repr(events[0])


def test_bearer_auth_success_emits_tenant_event_without_token(monkeypatch):
    events = []
    tenant = SimpleNamespace(
        tenant_id="tenant-a",
        corpid="ww123",
        secret="corp-secret",
        schema_name="wbd_123",
        contact_secret="contact-secret",
        checkin_userids=[],
        enabled_modules=set(),
        data_mode="stored",
    )
    monkeypatch.setattr(auth_module, "write_event", events.append)
    monkeypatch.setattr(auth_module, "_auth_write_limiter", auth_module.AuthWriteLimiter())
    monkeypatch.setattr(auth_module, "get_tenant_by_token", lambda token: tenant)

    response = _auth_test_client().get(
        "/secure", headers={"Authorization": "Bearer valid-token"}
    )

    assert response.status_code == 200
    assert response.json() == {"tenant": "tenant-a"}
    assert len(events) == 1
    assert events[0].event_name == "auth_ok"
    assert events[0].tenant_id == "tenant-a"
    assert events[0].result_status == "ok"
    for sensitive in ("valid-token", "corp-secret", "contact-secret"):
        assert sensitive not in repr(events[0])


def test_connection_auth_binds_context_to_path_and_audits_only_resolved_connection(monkeypatch):
    events = []
    connection = auth_module.ConnectionCtx(
        tenant_id="tenant-a",
        connection_id="conn-a",
        connector_key="wecom",
        data_mode="stored",
        public_config={"schema_name": "tenant_a"},
        config_version=4,
    )

    class Resolver:
        def resolve(self, connection_id, token):
            if (connection_id, token) == ("conn-a", "valid-token"):
                return connection
            return None

        def resolve_legacy(self, token):  # pragma: no cover - dynamic mount only
            return None

    protected = FastAPI()
    protected.add_middleware(auth_module.BearerTokenMiddleware, resolver=Resolver())

    @protected.get("/")
    def context_view():
        ctx = auth_module.current_ctx()
        return {"tenant": ctx.tenant_id, "connection": ctx.connection_id}

    app = Starlette(routes=[Mount("/mcp/{connection_id}", app=protected)])
    monkeypatch.setattr(auth_module, "write_event", events.append)
    monkeypatch.setattr(auth_module, "_auth_write_limiter", auth_module.AuthWriteLimiter())

    client = TestClient(app)
    rejected = client.get("/mcp/conn-b/", headers={"Authorization": "Bearer valid-token"})
    accepted = client.get("/mcp/conn-a/", headers={"Authorization": "Bearer valid-token"})

    assert rejected.status_code == 401
    assert accepted.json() == {"tenant": "tenant-a", "connection": "conn-a"}
    assert events[0].target == ""
    assert events[1].target == "conn-a"
    assert "valid-token" not in repr(events)


def test_auth_event_hashes_mcp_session_id_fallback(monkeypatch):
    events = []
    raw_session = "opaque-session-value"
    request = SimpleNamespace(
        scope={
            "client": ("192.0.2.1", 1234),
            "headers": [(b"mcp-session-id", raw_session.encode())],
        },
        headers={"mcp-session-id": raw_session},
        method="POST",
    )
    monkeypatch.setattr(auth_module, "write_event", events.append)
    monkeypatch.setattr(auth_module, "_auth_write_limiter", auth_module.AuthWriteLimiter())

    auth_module._record_auth(request, "auth_invalid")

    assert len(events) == 1
    assert events[0].request_id.startswith("sha256:")
    assert len(events[0].request_id) == 39
    assert raw_session not in repr(events[0])


@pytest.mark.parametrize(
    "failure_point",
    ["limiter", "request_id", "safe_summary", "event", "write_event"],
)
def test_auth_audit_failures_never_change_authentication_result(
    monkeypatch, caplog, failure_point
):
    leaked = "secret=audit-failure"

    def fail(*args, **kwargs):
        raise RuntimeError(leaked)

    if failure_point == "limiter":
        monkeypatch.setattr(
            auth_module,
            "_auth_write_limiter",
            SimpleNamespace(allow_with_notice=fail),
        )
    else:
        monkeypatch.setattr(auth_module, "_auth_write_limiter", auth_module.AuthWriteLimiter())
        target = {
            "request_id": "request_id_from_scope",
            "safe_summary": "safe_summary",
            "event": "McpLogEvent",
            "write_event": "write_event",
        }[failure_point]
        monkeypatch.setattr(auth_module, target, fail)

    with caplog.at_level("WARNING", logger="app.auth"):
        response = _auth_test_client().get("/secure")

    assert response.status_code == 401
    assert response.json() == {"errcode": 401, "errmsg": "缺少 Bearer Token"}
    assert "RuntimeError" in caplog.text
    assert leaked not in caplog.text


def test_auth_rate_limit_warning_is_emitted_once_per_active_bucket(
    monkeypatch, caplog
):
    events = []
    request = SimpleNamespace(
        scope={"client": ("192.0.2.1", 1234)},
        headers={},
        method="GET",
    )
    monkeypatch.setattr(auth_module, "write_event", events.append)
    monkeypatch.setattr(
        auth_module,
        "_auth_write_limiter",
        auth_module.AuthWriteLimiter(limit=1, window_seconds=60),
    )

    with caplog.at_level("WARNING", logger="app.auth"):
        for _ in range(3):
            auth_module._record_auth(request, "auth_invalid")

    assert len(events) == 1
    assert caplog.text.count("MCP auth audit rate limit reached") == 1


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


@pytest.mark.parametrize(
    "token",
    [" token-1234567890", "token-1234567890 "],
)
def test_mcp_token_validation_rejects_surrounding_whitespace(token):
    with pytest.raises(HTTPException) as exc:
        admin._validate_mcp_token(token)

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


def test_create_tenant_redacts_database_error_details(monkeypatch, caplog):
    leaked = "mysql://admin:db-secret@host/db?access_token=token-secret"

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def execute(self, statement, values):
            raise RuntimeError(leaked)

    class Engine:
        def begin(self):
            return Connection()

    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    monkeypatch.setattr(admin, "ensure_domain_tables", lambda: None)
    monkeypatch.setattr(admin, "get_engine", lambda: Engine())
    monkeypatch.setattr(admin, "encrypt_secret", lambda value: f"enc:{value}")
    body = admin.TenantUpsert(
        tenant_id="tenant-a",
        corpid="ww123",
        secret="app-secret",
        mcp_token="token-1234567890",
    )

    with pytest.raises(HTTPException) as exc:
        admin.create_tenant(body, SimpleNamespace(cookies={}, headers={}))

    assert exc.value.status_code == 400
    assert exc.value.detail == "写入失败，可能租户 ID、企业 ID 或 MCP Token 重复"
    assert leaked not in exc.value.detail
    assert leaked not in caplog.text
    assert "RuntimeError" in caplog.text


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
