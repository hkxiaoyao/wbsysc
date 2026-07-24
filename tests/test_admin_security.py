from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Response
from fastapi.testclient import TestClient
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.routing import Mount

from app import admin
from app import auth as auth_module
from app import tenant as tenant_module


def _legacy_tenant_body(**overrides):
    """Adapter for historical direct-call tests; HTTP contracts use strict models."""
    values = {
        "tenant_id": "tenant-a",
        "display_name": "",
        "enabled": True,
        "tenant_password": None,
    }
    values.update(overrides)
    if isinstance(values["tenant_password"], str):
        values["tenant_password"] = admin.SecretStr(values["tenant_password"])
    return SimpleNamespace(**values)


def _same_origin_request():
    return SimpleNamespace(
        cookies={},
        headers={"origin": "http://testserver"},
        base_url="http://testserver/",
    )


class _AtomicResult:
    def __init__(self, row=None):
        self._row = row
        self.rowcount = 1

    def fetchone(self):
        return self._row


class _AtomicConnection:
    def __init__(self, state, *, transactional):
        self.state = state
        self.transactional = transactional
        self.statements = []
        self._snapshot = None

    def __enter__(self):
        if self.transactional:
            self._snapshot = deepcopy(self.state)
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is not None and self.transactional:
            self.state.clear()
            self.state.update(self._snapshot)
        return False

    def execute(self, statement, values=None):
        sql = str(statement)
        params = dict(values or {})
        self.statements.append((sql, params))
        if sql.lstrip().startswith("SELECT secret_encrypted"):
            config = self.state["config"]
            return _AtomicResult(
                (config["secret"], config["contact_secret"], config["mcp_token"])
            )
        if sql.lstrip().startswith("INSERT INTO tenant_config"):
            self.state["config"] = {
                "tenant_id": params["t"],
                "display_name": params["dn"],
                "enabled": bool(params["en"]),
            }
        elif sql.lstrip().startswith("UPDATE tenant_config SET"):
            self.state["config"].update(
                display_name=params["dn"], enabled=bool(params["en"])
            )
        return _AtomicResult()


class _AtomicEngine:
    def __init__(self, state):
        self.state = state
        self.transactions = []

    def connect(self):
        return _AtomicConnection(self.state, transactional=False)

    def begin(self):
        connection = _AtomicConnection(self.state, transactional=True)
        self.transactions.append(connection)
        return connection


def _auth_test_client():
    app = FastAPI()
    app.add_middleware(auth_module.BearerTokenMiddleware)

    @app.get("/secure")
    def secure():
        return {"tenant": auth_module.current_ctx().tenant_id}

    return TestClient(app)


def _isolated_admin_service_app():
    from app.mcp_services.router import admin_router

    app = FastAPI()
    app.include_router(admin_router)
    return app


def test_main_app_retains_authenticated_legacy_service_cleanup_only(monkeypatch):
    from app.main import create_app
    from app.mcp_services import router as service_router

    client = TestClient(create_app())
    unauthenticated = client.get("/admin/tenants/tenant-a/services")
    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    monkeypatch.setattr(service_router.manager, "list_services", lambda tenant_id: [])
    cleanup_list = client.get("/admin/tenants/tenant-a/services")
    create = client.post(
        "/admin/tenants/tenant-a/services",
        headers={"Origin": "http://testserver"},
        json={"display_name": "Operations", "service_key": "operations"},
    )

    assert unauthenticated.status_code == 401
    assert cleanup_list.status_code == 200
    assert cleanup_list.json() == {"items": []}
    assert create.status_code == 405


def test_main_app_legacy_cleanup_can_disable_but_not_reactivate(monkeypatch):
    from app.main import create_app
    from app.mcp_services import router as service_router

    calls = []
    monkeypatch.setattr(
        service_router.manager,
        "update_status",
        lambda tenant_id, service_id, status, version: calls.append(
            (tenant_id, service_id, status, version)
        )
        or SimpleNamespace(
            service_id=service_id,
            tenant_id=tenant_id,
            display_name="Legacy",
            service_key="legacy",
            status=status,
            config_version=version + 1,
        ),
    )
    client = TestClient(create_app())
    headers = {"Origin": "http://testserver"}

    unauthenticated = client.patch(
        "/admin/tenants/tenant-a/services/service-a",
        headers=headers,
        json={"status": "active", "expected_config_version": 1},
    )
    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    rejected = client.patch(
        "/admin/tenants/tenant-a/services/service-a",
        headers=headers,
        json={"status": "active", "expected_config_version": 1},
    )
    disabled = client.patch(
        "/admin/tenants/tenant-a/services/service-a",
        headers=headers,
        json={"status": "disabled", "expected_config_version": 1},
    )

    assert unauthenticated.status_code == 401
    assert rejected.status_code == 422
    assert disabled.status_code == 200
    assert calls == [("tenant-a", "service-a", "disabled", 1)]


def test_admin_service_mutations_require_same_origin_after_auth(monkeypatch):
    from app.mcp_services import router as service_router

    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    monkeypatch.setattr(
        service_router.manager,
        "create_service",
        lambda tenant_id, display_name, service_key: SimpleNamespace(
            service_id="service-a",
            tenant_id=tenant_id,
            display_name=display_name,
            service_key=service_key,
            status="draft",
            config_version=1,
        ),
    )
    client = TestClient(_isolated_admin_service_app())

    missing = client.post(
        "/admin/tenants/tenant-a/services",
        json={"display_name": "Operations", "service_key": "operations"},
    )
    cross_site = client.post(
        "/admin/tenants/tenant-a/services",
        headers={"Origin": "https://attacker.invalid"},
        json={"display_name": "Operations", "service_key": "operations"},
    )
    accepted = client.post(
        "/admin/tenants/tenant-a/services",
        headers={"Origin": "http://testserver"},
        json={"display_name": "Operations", "service_key": "operations"},
    )

    assert missing.status_code == 403
    assert cross_site.status_code == 403
    assert accepted.status_code == 201
    assert accepted.json()["service"]["tenant_id"] == "tenant-a"


def test_admin_reveal_auth_csrf_and_boundary_failures_are_no_store_and_audited(
    monkeypatch,
):
    from app.mcp_services import router as service_router
    from app.mcp_services.store import TokenUnavailableError

    events = []
    manager_calls = []

    def unavailable(*args):
        manager_calls.append(args)
        raise TokenUnavailableError("unavailable")

    monkeypatch.setattr(service_router, "write_event", events.append)
    monkeypatch.setattr(
        service_router.manager,
        "reveal_token",
        unavailable,
    )
    service_router.reset_reveal_limiter()
    client = TestClient(_isolated_admin_service_app())
    path = "/admin/tenants/tenant-a/services/service-a/tokens/token-a/reveal"

    unauthenticated = client.post(path, headers={"Origin": "http://testserver"})
    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    cross_site = client.post(path, headers={"Origin": "https://attacker.invalid"})
    wrong_tenant = client.post(
        "/admin/tenants/tenant-b/services/service-a/tokens/token-a/reveal",
        headers={"Origin": "http://testserver"},
    )
    wrong_service = client.post(
        "/admin/tenants/tenant-a/services/service-b/tokens/token-a/reveal",
        headers={"Origin": "http://testserver"},
    )
    wrong_token = client.post(
        "/admin/tenants/tenant-a/services/service-a/tokens/token-b/reveal",
        headers={"Origin": "http://testserver"},
    )

    assert unauthenticated.status_code == 401
    assert cross_site.status_code == 403
    assert wrong_tenant.status_code == 404
    assert wrong_service.status_code == 404
    assert wrong_token.status_code == 404
    assert unauthenticated.headers["cache-control"] == "no-store"
    assert cross_site.headers["cache-control"] == "no-store"
    boundary_responses = (wrong_tenant, wrong_service, wrong_token)
    assert all(
        response.headers["cache-control"] == "no-store"
        for response in boundary_responses
    )
    assert all(
        response.json() == {"detail": "resource not found"}
        for response in boundary_responses
    )
    assert [event.result_status for event in events] == ["denied"] * 5
    assert [event.tenant_id for event in events] == [
        "tenant-a",
        "tenant-a",
        "tenant-b",
        "tenant-a",
        "tenant-a",
    ]
    assert all(event.params_summary == '{"principal_type":"admin"}' for event in events)
    assert "unavailable" not in repr(events)
    assert manager_calls == [
        ("tenant-b", "service-a", "token-a"),
        ("tenant-a", "service-b", "token-a"),
        ("tenant-a", "service-a", "token-b"),
    ]


@pytest.mark.parametrize("audit_behavior", ["false", "throw"])
def test_admin_reveal_requires_accepted_success_audit(monkeypatch, audit_behavior):
    from app.mcp_services import router as service_router

    events = []

    def audit(event):
        events.append(event)
        if audit_behavior == "throw":
            raise RuntimeError("audit unavailable with mcp_svc_secret")
        return False

    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    monkeypatch.setattr(service_router, "write_event", audit)
    monkeypatch.setattr(
        service_router.manager,
        "reveal_token",
        lambda *_args: "mcp_svc_secret",
    )
    service_router.reset_reveal_limiter()

    response = TestClient(_isolated_admin_service_app()).post(
        "/admin/tenants/tenant-a/services/service-a/tokens/token-a/reveal",
        headers={"Origin": "http://testserver"},
    )

    assert response.status_code == 500
    assert response.json() == {"detail": "service operation failed"}
    assert response.headers["cache-control"] == "no-store"
    assert "mcp_svc_secret" not in response.text
    assert len(events) == 1


def test_admin_issue_threads_normalized_expiry(monkeypatch):
    from app.mcp_services import router as service_router

    calls = []
    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    monkeypatch.setattr(
        service_router.manager,
        "issue_token",
        lambda tenant_id, service_id, label, expires_at: calls.append(
            (tenant_id, service_id, label, expires_at)
        )
        or SimpleNamespace(token_id="token-a", raw_value="mcp_svc_secret", prefix="abc"),
    )

    response = TestClient(_isolated_admin_service_app()).post(
        "/admin/tenants/tenant-a/services/service-a/tokens",
        headers={"Origin": "http://testserver"},
        json={"label": "client", "expires_at": "2026-07-23T10:20:30+08:00"},
    )

    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    assert calls == [
        ("tenant-a", "service-a", "client", datetime(2026, 7, 23, 2, 20, 30))
    ]


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


def test_tenant_item_exposes_only_identity_and_login_metadata():
    row = ("tenant-a", "客户A", 1, "created", "updated", 1, "disabled")

    item = admin._tenant_item(row)

    assert set(item) == {
        "tenant_id",
        "display_name",
        "enabled",
        "created_at",
        "updated_at",
        "has_login_account",
        "login_status",
    }
    assert item["has_login_account"] is True
    assert item["login_status"] == "disabled"
    assert item["enabled"] is True


def test_tenant_item_reports_no_login_account_without_inventing_status():
    row = ("tenant-a", "客户A", 1, "created", "updated", 0, "active")

    item = admin._tenant_item(row)

    assert item["has_login_account"] is False
    assert item["login_status"] is None


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


def test_admin_can_reset_tenant_login_password_without_secret_repr(monkeypatch):
    events = []
    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    monkeypatch.setattr(admin, "_tenant_exists", lambda tenant_id: tenant_id == "tenant-a")
    monkeypatch.setattr(admin, "_tenant_enabled", lambda tenant_id: True)
    monkeypatch.setattr(
        admin.tenant_auth_store,
        "upsert_account",
        lambda tenant_id, password, status="active": events.append(
            (tenant_id, password, status)
        ),
    )
    body = admin.TenantPasswordRequest(password="Replacement-Secure-456")

    result = admin.reset_tenant_login_password(
        "tenant-a", body, _same_origin_request()
    )

    assert result == {"ok": True}
    assert events == [("tenant-a", "Replacement-Secure-456", "active")]
    assert "Replacement-Secure-456" not in repr(body)


def test_admin_password_reset_preserves_disabled_tenant_status(monkeypatch):
    events = []
    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    monkeypatch.setattr(admin, "_tenant_exists", lambda tenant_id: True)
    monkeypatch.setattr(admin, "_tenant_enabled", lambda tenant_id: False)
    monkeypatch.setattr(
        admin.tenant_auth_store,
        "upsert_account",
        lambda tenant_id, password, status="active": events.append(status),
    )

    admin.reset_tenant_login_password(
        "tenant-a",
        admin.TenantPasswordRequest(password="Replacement-Secure-456"),
        _same_origin_request(),
    )

    assert events == ["disabled"]


def test_admin_can_disable_tenant_login_and_revoke_sessions(monkeypatch):
    events = []
    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    monkeypatch.setattr(admin, "_tenant_exists", lambda tenant_id: True)
    monkeypatch.setattr(
        admin.tenant_auth_store,
        "set_account_status",
        lambda tenant_id, status: events.append((tenant_id, status)) or True,
    )

    result = admin.set_tenant_login_status(
        "tenant-a",
        admin.TenantLoginStatusRequest(status="disabled"),
        _same_origin_request(),
    )

    assert result == {"ok": True, "status": "disabled"}
    assert events == [("tenant-a", "disabled")]


def test_tenant_cookie_cannot_call_admin_tenant_password_reset():
    test_app = FastAPI()
    test_app.include_router(admin.router)
    client = TestClient(test_app)
    client.cookies.set("wbg_tenant_session", "tenant-session-value", path="/tenant")

    response = client.put(
        "/admin/tenants/tenant-a/login-password",
        json={"password": "Replacement-Secure-456"},
    )

    assert response.status_code == 401


@pytest.mark.parametrize(
    ("path", "payload", "side_effect_name"),
    [
        (
            "/admin/tenants/tenant-a/login-password",
            {"password": "Replacement-Secure-456"},
            "upsert_account",
        ),
        (
            "/admin/tenants/tenant-a/login-status",
            {"status": "disabled"},
            "set_account_status",
        ),
    ],
)
def test_tenant_login_mutations_require_unambiguous_same_origin_after_auth(
    monkeypatch, path, payload, side_effect_name
):
    events = []
    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    monkeypatch.setattr(admin, "_tenant_exists", lambda tenant_id: True)
    monkeypatch.setattr(admin, "_tenant_enabled", lambda tenant_id: True)
    monkeypatch.setattr(
        admin.tenant_auth_store,
        side_effect_name,
        lambda *args, **kwargs: events.append((args, kwargs)) or True,
    )
    client = TestClient(FastAPI())
    client.app.include_router(admin.router)

    missing = client.put(path, json=payload)
    cross_site = client.put(
        path, headers={"Origin": "https://attacker.invalid"}, json=payload
    )
    ambiguous = client.put(
        path,
        headers={"Origin": "http://testserver, https://attacker.invalid"},
        json=payload,
    )
    accepted = client.put(
        path, headers={"Origin": "http://testserver"}, json=payload
    )

    assert [missing.status_code, cross_site.status_code, ambiguous.status_code] == [
        403,
        403,
        403,
    ]
    assert accepted.status_code == 200
    assert len(events) == 1


def test_tenant_login_mutation_checks_auth_before_origin(monkeypatch):
    events = []
    monkeypatch.setattr(
        admin.tenant_auth_store,
        "set_account_status",
        lambda *args: events.append(args) or True,
    )
    client = TestClient(FastAPI())
    client.app.include_router(admin.router)

    response = client.put(
        "/admin/tenants/tenant-a/login-status",
        headers={"Origin": "https://attacker.invalid"},
        json={"status": "disabled"},
    )

    assert response.status_code == 401
    assert events == []


def test_tenant_identity_models_reject_legacy_connection_fields():
    legacy_fields = {
        "corpid": "ww123",
        "secret": "secret",
        "contact_secret": "contact-secret",
        "mcp_token": "token-1234567890",
        "schema_name": "wbd_legacy",
        "data_mode": "direct",
        "sync_interval_min": 10,
        "enabled_modules": "report",
        "checkin_userids": "alice",
        "trusted_domain": "example.com",
    }

    for field, value in legacy_fields.items():
        with pytest.raises(ValidationError):
            admin.TenantCreate(
                tenant_id="tenant-a",
                tenant_password="Initial-Secure-123",
                **{field: value},
            )
        with pytest.raises(ValidationError):
            admin.TenantUpdate(**{field: value})


def test_tenant_identity_models_accept_only_identity_fields():
    created = admin.TenantCreate(
        tenant_id="tenant-a",
        display_name="客户 A",
        tenant_password="Initial-Secure-123",
        enabled=True,
    )
    updated = admin.TenantUpdate(
        display_name="客户 A+",
        tenant_password="Replacement-Secure-456",
        enabled=False,
    )

    assert set(created.model_dump()) == {
        "tenant_id",
        "display_name",
        "tenant_password",
        "enabled",
    }
    assert set(updated.model_dump()) == {
        "display_name",
        "tenant_password",
        "enabled",
    }


def test_tenant_create_requires_password():
    with pytest.raises(ValidationError):
        admin.TenantCreate(tenant_id="tenant-a")


def test_create_tenant_does_not_require_legacy_connection_fields(monkeypatch):
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
    monkeypatch.setattr(admin, "reload_tenants", lambda: None)
    body = admin.TenantCreate(
        tenant_id="tenant-a",
        display_name="客户 A",
        tenant_password="Initial-Secure-123",
    )
    request = _same_origin_request()

    assert admin.create_tenant(body, request) == {"ok": True}


def test_create_tenant_ignores_no_connection_token_because_contract_forbids_it(monkeypatch):
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
    monkeypatch.setattr(admin, "reload_tenants", lambda: None)
    body = admin.TenantCreate(
        tenant_id="tenant-a",
        tenant_password="Initial-Secure-123",
    )

    assert admin.create_tenant(
        body, _same_origin_request()
    ) == {"ok": True}


@pytest.mark.parametrize(
    "token",
    [" token-1234567890", "token-1234567890 "],
)
def test_mcp_token_validation_rejects_surrounding_whitespace(token):
    with pytest.raises(HTTPException) as exc:
        admin._validate_mcp_token(token)

    assert exc.value.status_code == 400


def test_create_tenant_writes_only_identity_columns(monkeypatch):
    executed = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def execute(self, statement, values):
            executed.append((str(statement), dict(values)))

    class Engine:
        def begin(self):
            return Connection()

    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    monkeypatch.setattr(admin, "ensure_domain_tables", lambda: None)
    monkeypatch.setattr(admin, "get_engine", lambda: Engine())
    monkeypatch.setattr(admin, "reload_tenants", lambda: None)
    body = admin.TenantCreate(
        tenant_id="tenant-a",
        display_name="客户 A",
        tenant_password="Initial-Secure-123",
    )

    admin.create_tenant(body, _same_origin_request())

    insert_sql, insert_values = next(
        (sql, values)
        for sql, values in executed
        if str(sql).lstrip().startswith("INSERT INTO tenant_config")
    )
    normalized_sql = " ".join(insert_sql.split())
    assert "(tenant_id, display_name, enabled)" in normalized_sql
    assert set(insert_values) == {"t", "dn", "en"}


def test_create_tenant_provisions_required_login_password(monkeypatch):
    accounts = []

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
    monkeypatch.setattr(admin, "reload_tenants", lambda: None)
    monkeypatch.setattr(
        admin.tenant_auth_store,
        "upsert_account",
        lambda tenant_id, password, status="active", conn=None: accounts.append(
            (tenant_id, password, status)
        ),
    )
    body = _legacy_tenant_body(
        tenant_id="tenant-a",
        corpid="ww123",
        secret="app-secret",
        mcp_token="token-1234567890",
        tenant_password="Initial-Secure-123",
    )

    admin.create_tenant(body, _same_origin_request())

    assert accounts == [("tenant-a", "Initial-Secure-123", "active")]
    assert "Initial-Secure-123" not in repr(body)


def test_create_tenant_validates_login_password_before_database_write(monkeypatch):
    statements = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def execute(self, statement, values):
            statements.append(str(statement))
            return SimpleNamespace()

    class Engine:
        def begin(self):
            return Connection()

    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    monkeypatch.setattr(admin, "ensure_domain_tables", lambda: None)
    monkeypatch.setattr(admin, "get_engine", lambda: Engine())
    body = _legacy_tenant_body(
        tenant_id="tenant-a",
        corpid="ww123",
        secret="app-secret",
        mcp_token="token-1234567890",
        tenant_password="weak",
    )

    with pytest.raises(HTTPException) as exc:
        admin.create_tenant(body, _same_origin_request())

    assert exc.value.status_code == 422
    assert statements == []


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"origin": "https://attacker.invalid"},
        {"origin": "http://testserver, https://attacker.invalid"},
    ],
)
def test_password_bearing_tenant_create_rejects_unsafe_origin_before_writes(
    monkeypatch, headers
):
    events = []
    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    monkeypatch.setattr(admin, "ensure_domain_tables", lambda: events.append("write"))
    body = _legacy_tenant_body(
        tenant_id="tenant-a",
        corpid="ww123",
        secret="app-secret",
        mcp_token="token-1234567890",
        tenant_password="Initial-Secure-123",
    )

    with pytest.raises(HTTPException) as exc:
        admin.create_tenant(
            body,
            SimpleNamespace(
                cookies={}, headers=headers, base_url="http://testserver/"
            ),
        )

    assert exc.value.status_code == 403
    assert events == []


def test_create_with_password_rolls_back_config_when_account_write_fails(monkeypatch):
    state = {}
    engine = _AtomicEngine(state)
    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    monkeypatch.setattr(admin, "ensure_domain_tables", lambda: None)
    monkeypatch.setattr(admin, "get_engine", lambda: engine)

    def fail_account_write(tenant_id, password, status="active", *, conn=None):
        assert conn is engine.transactions[-1]
        state["account"] = {"tenant_id": tenant_id, "status": status}
        raise RuntimeError("injected account failure")

    monkeypatch.setattr(admin.tenant_auth_store, "upsert_account", fail_account_write)
    body = _legacy_tenant_body(
        tenant_id="tenant-a",
        corpid="ww123",
        secret="app-secret",
        mcp_token="token-1234567890",
        tenant_password="Initial-Secure-123",
    )

    with pytest.raises(RuntimeError, match="injected account failure"):
        admin.create_tenant(body, _same_origin_request())

    assert state == {}


def test_create_tenant_does_not_provision_legacy_schema(
    monkeypatch,
):
    state = {}
    engine = _AtomicEngine(state)
    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    monkeypatch.setattr(admin, "ensure_domain_tables", lambda: None)
    monkeypatch.setattr(admin, "get_engine", lambda: engine)
    monkeypatch.setattr(admin, "reload_tenants", lambda: None)

    def write_account(tenant_id, password, status="active", *, conn=None):
        assert conn is engine.transactions[-1]
        state["account"] = {"tenant_id": tenant_id, "status": status}

    monkeypatch.setattr(admin.tenant_auth_store, "upsert_account", write_account)
    body = admin.TenantCreate(
        tenant_id="tenant-a",
        tenant_password="Initial-Secure-123",
    )

    assert admin.create_tenant(body, _same_origin_request()) == {"ok": True}

    assert state["config"]["tenant_id"] == "tenant-a"
    assert state["account"] == {"tenant_id": "tenant-a", "status": "active"}
    assert all(
        "SCHEMA" not in sql.upper()
        for tx in engine.transactions
        for sql, _ in tx.statements
    )


def test_create_tenant_rejects_reuse_when_retained_child_history_exists(monkeypatch):
    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement, values):
            sql = " ".join(str(statement).split())
            row = ("retained-connection",) if sql.startswith(
                "SELECT connection_id FROM connection_instance"
            ) else None
            return SimpleNamespace(fetchone=lambda: row)

    class Engine:
        def begin(self):
            return Connection()

    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    monkeypatch.setattr(admin, "ensure_domain_tables", lambda: None)
    monkeypatch.setattr(admin, "get_engine", lambda: Engine())
    body = _legacy_tenant_body(
        tenant_id="tenant-a",
        corpid="ww123",
        secret="app-secret",
        mcp_token="token-1234567890",
        tenant_password="Initial-Secure-123",
    )

    with pytest.raises(HTTPException) as exc:
        admin.create_tenant(body, _same_origin_request())

    assert exc.value.status_code == 409


def test_create_tenant_locks_exact_absent_tenant_before_ordered_history(monkeypatch):
    statements = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement, values):
            sql = " ".join(str(statement).split())
            statements.append((sql, dict(values)))
            return SimpleNamespace(fetchone=lambda: None)

    class Engine:
        def begin(self):
            return Connection()

    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    monkeypatch.setattr(admin, "ensure_domain_tables", lambda: None)
    monkeypatch.setattr(admin, "get_engine", lambda: Engine())
    monkeypatch.setattr(admin, "reload_tenants", lambda: None)
    body = _legacy_tenant_body(
        tenant_id="tenant-a",
        corpid="ww123",
        secret="app-secret",
        mcp_token="token-1234567890",
        tenant_password="Initial-Secure-123",
    )

    assert admin.create_tenant(
        body, _same_origin_request()
    )["ok"] is True

    assert "FROM tenant_config" in statements[0][0]
    assert "tenant_id=:t" in statements[0][0]
    assert "FOR UPDATE" in statements[0][0]
    assert "FROM connection_instance" in statements[1][0]
    assert "ORDER BY connection_id" in statements[1][0]
    assert "FROM mcp_service" in statements[2][0]
    assert "ORDER BY service_id" in statements[2][0]
    assert statements[3][0].startswith("INSERT INTO tenant_config")


def test_delete_tenant_disables_login_account(monkeypatch):
    from app.tenant_lifecycle import TenantRetirement

    events = []
    retirement = TenantRetirement("tenant-a", (("conn-a", 3),), ("svc-a",), 1, 1)

    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    monkeypatch.setattr(admin, "reload_tenants", lambda: None)
    monkeypatch.setattr(admin, "retire_tenant", lambda *args, **kwargs: retirement)
    monkeypatch.setattr(
        admin,
        "invalidate_retired_tenant",
        lambda result, reload_tenants: events.append(result),
    )

    result = admin.delete_tenant(
        "tenant-a",
        SimpleNamespace(cookies={}, headers={}, client=None, method="DELETE"),
    )

    assert result == {"ok": True}
    assert events == [retirement]


def test_update_tenant_synchronizes_login_password_and_disabled_status(monkeypatch):
    events = []

    class Result:
        def fetchone(self):
            return ("encrypted-secret", "encrypted-contact", "existing-token")

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
    monkeypatch.setattr(admin, "reload_tenants", lambda: None)
    monkeypatch.setattr(
        admin.tenant_auth_store,
        "upsert_account",
        lambda tenant_id, password, status="active", conn=None: events.append(
            (tenant_id, password, status)
        ),
    )
    body = _legacy_tenant_body(
        tenant_id="tenant-a",
        corpid="ww123",
        tenant_password="Replacement-Secure-456",
        enabled=False,
    )

    admin.update_tenant(
        "tenant-a", body, _same_origin_request()
    )

    assert events == [("tenant-a", "Replacement-Secure-456", "disabled")]


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"origin": "https://attacker.invalid"},
        {"origin": "http://testserver, https://attacker.invalid"},
    ],
)
def test_password_bearing_tenant_update_rejects_unsafe_origin_before_writes(
    monkeypatch, headers
):
    events = []
    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    monkeypatch.setattr(admin, "ensure_domain_tables", lambda: events.append("write"))
    body = _legacy_tenant_body(
        tenant_id="tenant-a",
        corpid="ww123",
        tenant_password="Replacement-Secure-456",
    )

    with pytest.raises(HTTPException) as exc:
        admin.update_tenant(
            "tenant-a",
            body,
            SimpleNamespace(
                cookies={}, headers=headers, base_url="http://testserver/"
            ),
        )

    assert exc.value.status_code == 403
    assert events == []


def test_update_with_password_rolls_back_config_when_account_write_fails(monkeypatch):
    original = {
        "config": {
            "tenant_id": "tenant-a",
            "display_name": "Before",
            "secret": "encrypted-secret",
            "contact_secret": "encrypted-contact",
            "mcp_token": "existing-token",
            "enabled": True,
        },
        "account": {"tenant_id": "tenant-a", "status": "disabled"},
        "session_revoked": False,
    }
    state = deepcopy(original)
    engine = _AtomicEngine(state)
    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    monkeypatch.setattr(admin, "ensure_domain_tables", lambda: None)
    monkeypatch.setattr(admin, "get_engine", lambda: engine)

    def fail_account_write(tenant_id, password, status="active", *, conn=None):
        assert conn is engine.transactions[-1]
        state["account"] = {"tenant_id": tenant_id, "status": status}
        state["session_revoked"] = True
        raise RuntimeError("injected account failure")

    monkeypatch.setattr(admin.tenant_auth_store, "upsert_account", fail_account_write)
    body = _legacy_tenant_body(
        tenant_id="tenant-a",
        corpid="ww123",
        display_name="After",
        tenant_password="Replacement-Secure-456",
    )

    with pytest.raises(RuntimeError, match="injected account failure"):
        admin.update_tenant("tenant-a", body, _same_origin_request())

    assert state == original


def test_disable_failure_rolls_back_config_status_and_session_revocation(monkeypatch):
    original = {
        "config": {
            "tenant_id": "tenant-a",
            "display_name": "Before",
            "secret": "encrypted-secret",
            "contact_secret": "encrypted-contact",
            "mcp_token": "existing-token",
            "enabled": True,
        },
        "account": {"tenant_id": "tenant-a", "status": "active"},
        "session_revoked": False,
    }
    state = deepcopy(original)
    engine = _AtomicEngine(state)
    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    monkeypatch.setattr(admin, "ensure_domain_tables", lambda: None)
    monkeypatch.setattr(admin, "get_engine", lambda: engine)

    def fail_after_status_and_revoke(tenant_id, status, *, conn=None):
        assert conn is engine.transactions[-1]
        state["account"] = {"tenant_id": tenant_id, "status": status}
        state["session_revoked"] = True
        raise RuntimeError("injected revoke failure")

    monkeypatch.setattr(
        admin.tenant_auth_store, "set_account_status", fail_after_status_and_revoke
    )
    body = _legacy_tenant_body(tenant_id="tenant-a", corpid="ww123", enabled=False)

    with pytest.raises(RuntimeError, match="injected revoke failure"):
        admin.update_tenant(
            "tenant-a", body, SimpleNamespace(cookies={}, headers={})
        )

    assert state == original


def test_failed_disable_then_retry_and_enable_cannot_revive_old_session(monkeypatch):
    state = {
        "config": {
            "tenant_id": "tenant-a",
            "display_name": "Before",
            "secret": "encrypted-secret",
            "contact_secret": "encrypted-contact",
            "mcp_token": "existing-token",
            "enabled": True,
        },
        "account": {"tenant_id": "tenant-a", "status": "active"},
        "session_revoked": False,
    }
    engine = _AtomicEngine(state)
    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    monkeypatch.setattr(admin, "ensure_domain_tables", lambda: None)
    monkeypatch.setattr(admin, "get_engine", lambda: engine)
    monkeypatch.setattr(admin, "reload_tenants", lambda: None)
    attempts = 0

    def set_status(tenant_id, status, *, conn=None):
        nonlocal attempts
        assert conn is engine.transactions[-1]
        attempts += 1
        state["account"] = {"tenant_id": tenant_id, "status": status}
        if status == "disabled":
            state["session_revoked"] = True
        if attempts == 1:
            raise RuntimeError("injected first disable failure")
        return True

    monkeypatch.setattr(admin.tenant_auth_store, "set_account_status", set_status)

    with pytest.raises(RuntimeError, match="injected first disable failure"):
        admin.update_tenant(
            "tenant-a",
            _legacy_tenant_body(tenant_id="tenant-a", corpid="ww123", enabled=False),
            SimpleNamespace(cookies={}, headers={}),
        )
    assert state["config"]["enabled"] is True
    assert state["account"]["status"] == "active"
    assert state["session_revoked"] is False

    admin.update_tenant(
        "tenant-a",
        _legacy_tenant_body(tenant_id="tenant-a", corpid="ww123", enabled=False),
        SimpleNamespace(cookies={}, headers={}),
    )
    admin.update_tenant(
        "tenant-a",
        _legacy_tenant_body(tenant_id="tenant-a", corpid="ww123", enabled=True),
        SimpleNamespace(cookies={}, headers={}),
    )

    assert state["config"]["enabled"] is True
    assert state["account"]["status"] == "disabled"
    assert state["session_revoked"] is True


def test_disabling_tenant_forces_login_disabled_and_revokes_sessions(monkeypatch):
    events = []

    class Result:
        def fetchone(self):
            return ("encrypted-secret", "encrypted-contact", "existing-token")

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
    monkeypatch.setattr(admin, "reload_tenants", lambda: None)
    monkeypatch.setattr(
        admin.tenant_auth_store,
        "set_account_status",
        lambda tenant_id, status, conn=None: events.append((tenant_id, status)) or True,
    )
    body = _legacy_tenant_body(tenant_id="tenant-a", corpid="ww123", enabled=False)

    admin.update_tenant(
        "tenant-a", body, SimpleNamespace(cookies={}, headers={})
    )

    assert events == [("tenant-a", "disabled")]


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
    body = _legacy_tenant_body(
        tenant_id="tenant-a",
        corpid="ww123",
        secret="app-secret",
        mcp_token="token-1234567890",
        tenant_password="Initial-Secure-123",
    )

    with pytest.raises(HTTPException) as exc:
        admin.create_tenant(body, _same_origin_request())

    assert exc.value.status_code == 400
    assert exc.value.detail == "写入失败，可能租户 ID 重复"
    assert leaked not in exc.value.detail
    assert leaked not in caplog.text
    assert "RuntimeError" in caplog.text


def test_update_tenant_does_not_write_legacy_connection_fields(monkeypatch):
    executed = {}
    account_statuses = []

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
    monkeypatch.setattr(admin, "reload_tenants", lambda: None)
    monkeypatch.setattr(
        admin.tenant_auth_store,
        "set_account_status",
        lambda tenant_id, status: account_statuses.append((tenant_id, status)),
    )
    body = _legacy_tenant_body(
        tenant_id="tenant-a",
        corpid="ww123",
        mcp_token="",
        data_mode="direct",
    )

    admin.update_tenant("tenant-a", body, SimpleNamespace(cookies={}, headers={}))

    assert account_statuses == []
    assert set(executed["values"]) == {"t", "dn", "en"}
    assert "data_mode" not in executed["sql"]
    assert "mcp_token" not in executed["sql"]


def test_update_tenant_contract_rejects_connection_token():
    with pytest.raises(ValidationError):
        admin.TenantUpdate(mcp_token="too-short")


def test_tenant_list_query_has_no_legacy_connection_columns(monkeypatch):
    calls = []

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
            return Result([(
                "tenant-a", "客户A", 1, "created", "updated", 1, "disabled",
            )])

    class Engine:
        def connect(self):
            return Connection()

    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    monkeypatch.setattr(admin, "ensure_domain_tables", lambda: None)
    monkeypatch.setattr(admin, "get_engine", lambda: Engine())
    result = admin.list_tenants(SimpleNamespace(cookies={}, headers={}))

    assert len(calls) == 1
    assert result["items"][0]["login_status"] == "disabled"
    assert all(
        column not in calls[0]
        for column in ("corpid", "mcp_token", "secret_encrypted", "data_mode")
    )


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


def test_connection_credential_error_does_not_leak_secret(monkeypatch, caplog):
    from app import admin_connections
    from app.connections.models import ConnectionRecord
    from app.connectors.contracts import ConnectorSpec

    leaked = "credential-secret-in-database-error"
    record = ConnectionRecord(
        connection_id="conn-a",
        tenant_id="tenant-a",
        connector_key="sample",
        display_name="Sample",
        status="active",
        data_mode="direct",
        public_config={},
        config_version=1,
    )
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda *args: record)
    monkeypatch.setattr(
        admin_connections.store,
        "replace_credentials",
        lambda *args: (_ for _ in ()).throw(RuntimeError(leaked)),
    )
    monkeypatch.setattr(
        admin_connections,
        "_spec",
        lambda request, key: ConnectorSpec(
            connector_key="sample",
            tools=(),
            credential_schema={"type": "object"},
            config_schema={"type": "object"},
        ),
    )
    monkeypatch.setattr(admin, "_require_auth", lambda request: None)
    test_app = FastAPI()
    test_app.include_router(admin_connections.router)

    with caplog.at_level("WARNING", logger="app.admin_connections"):
        response = TestClient(test_app, raise_server_exceptions=False).put(
            "/admin/tenants/tenant-a/connections/conn-a/credentials",
            json={"credentials": {"api_key": "request-secret"}},
        )

    assert response.status_code == 400
    assert response.json() == {"detail": "connection mutation failed"}
    assert leaked not in caplog.text
    assert "request-secret" not in caplog.text
