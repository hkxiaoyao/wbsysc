from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.tenant_auth.models import IssuedTenantSession, TenantAccount, TenantPrincipal
from app.tenant_auth import router as tenant_auth_router
from app.main import create_app


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(tenant_auth_router.router)
    return TestClient(app)


def _same_origin_headers() -> dict[str, str]:
    return {"Origin": "http://testserver"}


def _issued() -> IssuedTenantSession:
    return IssuedTenantSession(
        session_id="session-a",
        tenant_id="tenant-a",
        raw_value="tenant-session-raw-value",
        expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1),
    )


def test_tenant_login_sets_distinct_http_only_cookie_without_returning_session(monkeypatch):
    monkeypatch.setattr(
        tenant_auth_router.store,
        "authenticate",
        lambda tenant_id, password: TenantAccount(tenant_id, "active"),
    )
    monkeypatch.setattr(tenant_auth_router.store, "issue_session", lambda *args, **kwargs: _issued())
    tenant_auth_router.reset_login_limiter()

    response = _client().post(
        "/tenant/login",
        headers=_same_origin_headers(),
        json={"tenant_id": "tenant-a", "password": "Tenant-Secure-123"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "tenant_id": "tenant-a"}
    cookie = response.headers["set-cookie"]
    assert "wbg_tenant_session=" in cookie
    assert "HttpOnly" in cookie
    assert "Path=/tenant" in cookie
    assert "wbg_admin_session" not in cookie
    assert "tenant-session-raw-value" not in response.text


def test_tenant_session_rejects_admin_cookie_and_accepts_tenant_cookie(monkeypatch):
    monkeypatch.setattr(
        tenant_auth_router.store,
        "resolve_session",
        lambda raw: (
            TenantPrincipal(principal_type="tenant", tenant_id="tenant-a")
            if raw == "tenant-session-raw-value"
            else None
        ),
    )
    client = _client()

    client.cookies.set("wbg_admin_session", "admin-session-value")
    rejected = client.get("/tenant/session")
    client.cookies.set("wbg_tenant_session", "tenant-session-raw-value", path="/tenant")
    accepted = client.get("/tenant/session")

    assert rejected.status_code == 401
    assert accepted.json() == {"authed": True, "tenant_id": "tenant-a"}


def test_cross_site_origin_cannot_change_tenant_password(monkeypatch):
    monkeypatch.setattr(
        tenant_auth_router.store,
        "resolve_session",
        lambda raw: TenantPrincipal(principal_type="tenant", tenant_id="tenant-a"),
    )

    client = _client()
    client.cookies.set("wbg_tenant_session", "tenant-session-raw-value", path="/tenant")
    response = client.post(
        "/tenant/password/change",
        headers={"Origin": "https://attacker.invalid"},
        json={
            "current_password": "Tenant-Secure-123",
            "new_password": "Replacement-Secure-456",
        },
    )

    assert response.status_code == 403


def test_tenant_login_is_rate_limited_without_revealing_account_state(monkeypatch):
    monkeypatch.setattr(tenant_auth_router.store, "authenticate", lambda *args: None)
    tenant_auth_router.reset_login_limiter()
    client = _client()

    responses = [
        client.post(
            "/tenant/login",
            headers=_same_origin_headers(),
            json={"tenant_id": "tenant-a", "password": "Wrong-Secure-456"},
        )
        for _ in range(6)
    ]

    assert [response.status_code for response in responses[:5]] == [401] * 5
    assert responses[5].status_code == 429
    assert responses[5].json() == {"detail": "请求过于频繁"}


def test_password_change_reauthenticates_updates_hash_and_clears_cookie(monkeypatch):
    events = []
    monkeypatch.setattr(
        tenant_auth_router.store,
        "resolve_session",
        lambda raw: TenantPrincipal(principal_type="tenant", tenant_id="tenant-a"),
    )
    monkeypatch.setattr(
        tenant_auth_router.store,
        "authenticate",
        lambda tenant_id, password: TenantAccount(tenant_id, "active"),
    )
    monkeypatch.setattr(
        tenant_auth_router.store,
        "upsert_account",
        lambda tenant_id, password: events.append((tenant_id, password)),
    )

    client = _client()
    client.cookies.set("wbg_tenant_session", "tenant-session-raw-value", path="/tenant")
    response = client.post(
        "/tenant/password/change",
        headers=_same_origin_headers(),
        json={
            "current_password": "Tenant-Secure-123",
            "new_password": "Replacement-Secure-456",
        },
    )

    assert response.status_code == 200
    assert events == [("tenant-a", "Replacement-Secure-456")]
    assert 'wbg_tenant_session="";' in response.headers["set-cookie"]
    assert "Path=/tenant" in response.headers["set-cookie"]


def test_main_application_registers_tenant_auth_routes():
    paths = {getattr(route, "path", "") for route in create_app().routes}

    assert "/tenant/login" in paths
    assert "/tenant/session" in paths
    assert "/tenant/password/change" in paths


def test_unavailable_proxy_ip_does_not_create_a_global_login_bucket():
    limiter = tenant_auth_router.TenantLoginLimiter(
        pair_limit=5,
        ip_limit=1,
        window_seconds=900,
    )

    limiter.record_failure("tenant-a", "")

    assert limiter.limited("tenant-a", "") is False
    assert limiter.limited("tenant-b", "") is False
