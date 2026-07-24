import re
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.connections import store as connection_store
from app.main import create_app
from app.mcp_log_models import LogFilters
from app import mcp_log_store
from app.tenant_auth import store as tenant_auth_store
from app.tenant_auth.models import TenantPrincipal


def _tenant_client(monkeypatch) -> TestClient:
    monkeypatch.setattr(
        tenant_auth_store,
        "resolve_session",
        lambda raw: (
            TenantPrincipal(principal_type="tenant", tenant_id="tenant-a")
            if raw == "tenant-session-a"
            else None
        ),
    )
    client = TestClient(create_app())
    client.cookies.set("wbg_tenant_session", "tenant-session-a", path="/tenant")
    return client


def _spy_on_tenant_console_stores(monkeypatch) -> list[str]:
    calls = []
    monkeypatch.setattr(
        connection_store,
        "list_connections",
        lambda *args: calls.append("connections") or [],
    )
    monkeypatch.setattr(
        mcp_log_store,
        "list_logs",
        lambda *args: calls.append("logs") or {"items": [], "total": 0},
    )
    monkeypatch.setattr(
        mcp_log_store,
        "get_log_stats",
        lambda *args: calls.append("stats") or {},
    )
    return calls


def test_admin_and_tenant_shells_share_the_production_build_and_stable_assets():
    client = TestClient(create_app())

    admin_response = client.get("/admin/ui/")
    tenant_response = client.get("/tenant/ui/")

    assert admin_response.status_code == 200
    assert tenant_response.status_code == 200
    assert "text/html" in admin_response.headers["content-type"]
    assert "text/html" in tenant_response.headers["content-type"]
    assert tenant_response.text == admin_response.text

    asset_match = re.search(r'["\'](/admin/ui/assets/[^"\']+\.js)["\']', tenant_response.text)
    assert asset_match is not None
    asset_response = client.get(asset_match.group(1))
    assert asset_response.status_code == 200
    assert "javascript" in asset_response.headers["content-type"]


def test_tenant_entry_points_redirect_to_the_tenant_shell():
    client = TestClient(create_app(), follow_redirects=False)

    for path in ("/tenant", "/tenant/", "/tenant/index.html"):
        response = client.get(path)
        assert response.status_code == 307
        assert response.headers["location"] == "/tenant/ui/"


def test_tenant_console_routes_require_a_tenant_session():
    client = TestClient(create_app())

    for path in ("/tenant/overview", "/tenant/connections", "/tenant/mcp-logs"):
        assert client.get(path).status_code == 401


@pytest.mark.parametrize(
    "path",
    ("/tenant/overview", "/tenant/connections", "/tenant/mcp-logs"),
)
def test_invalid_tenant_read_input_does_not_bypass_session_authentication(path):
    client = TestClient(create_app())

    response = client.request(
        "GET",
        f"{path}?tenant.id=tenant-b",
        content=b"tenant_id=tenant-b",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    assert response.status_code == 401


def test_tenant_connections_use_only_the_session_tenant(monkeypatch):
    captured = []
    monkeypatch.setattr(
        connection_store,
        "list_connections",
        lambda tenant_id: captured.append(tenant_id)
        or [
            SimpleNamespace(
                connection_id="conn-a",
                tenant_id=tenant_id,
                connector_key="wecom",
                connection_alias="hq_wecom",
                display_name="Headquarters",
                status="active",
                data_mode="direct",
                public_config={"corp_id": "safe-public-value"},
                config_version=3,
            )
        ],
    )

    response = _tenant_client(monkeypatch).get("/tenant/connections")

    assert response.status_code == 200
    assert captured == ["tenant-a"]
    assert response.json() == {
        "items": [
            {
                "connection_id": "conn-a",
                "tenant_id": "tenant-a",
                "connector_key": "wecom",
                "connection_alias": "hq_wecom",
                "display_name": "Headquarters",
                "status": "active",
                "data_mode": "direct",
                "public_config": {"corp_id": "safe-public-value"},
                "config_version": 3,
            }
        ]
    }


def test_tenant_logs_reject_tenant_id_query_without_calling_store(monkeypatch):
    calls = []
    monkeypatch.setattr(
        mcp_log_store,
        "list_logs",
        lambda *args: calls.append(args) or {"items": [], "total": 0},
    )

    response = _tenant_client(monkeypatch).get(
        "/tenant/mcp-logs?tenant_id=tenant-b"
    )

    assert response.status_code == 422
    assert calls == []


def test_tenant_routes_reject_tenant_id_body_without_calling_store(monkeypatch):
    calls = []
    monkeypatch.setattr(
        connection_store,
        "list_connections",
        lambda *args: calls.append(args) or [],
    )

    response = _tenant_client(monkeypatch).request(
        "GET",
        "/tenant/connections",
        json={"tenant_id": "tenant-b"},
    )

    assert response.status_code == 422
    assert calls == []


@pytest.mark.parametrize(
    "path",
    ("/tenant/overview", "/tenant/connections", "/tenant/mcp-logs"),
)
@pytest.mark.parametrize(
    ("body", "content_type"),
    (
        (b"tenant_id=tenant-b", "application/x-www-form-urlencoded"),
        (b'{"tenant_id":"tenant-b"', "application/json"),
        (b"tenant-b", "text/plain"),
        (b" ", "application/octet-stream"),
    ),
)
def test_tenant_read_routes_reject_every_nonempty_body_before_store(
    monkeypatch, path, body, content_type
):
    calls = _spy_on_tenant_console_stores(monkeypatch)
    client = _tenant_client(monkeypatch)

    response = client.request(
        "GET",
        path,
        content=body,
        headers={"Content-Type": content_type},
    )

    assert response.status_code == 422
    assert calls == []


@pytest.mark.parametrize(
    ("path", "query"),
    (
        ("/tenant/overview", "tenant.id=tenant-b"),
        ("/tenant/overview", "tenant%5Bid%5D=tenant-b"),
        ("/tenant/overview", "Tenant_ID=tenant-b"),
        ("/tenant/overview", "tenant+id=tenant-b"),
        ("/tenant/overview", "tenant%2Fid=tenant-b"),
        ("/tenant/overview", "page=1"),
        ("/tenant/connections", "tenant.id=tenant-b"),
        ("/tenant/connections", "tenant%5Bid%5D=tenant-b"),
        ("/tenant/connections", "Tenant-ID=tenant-b"),
        ("/tenant/connections", "tenant+id=tenant-b"),
        ("/tenant/connections", "tenant.id=tenant-a&tenant.id=tenant-b"),
        ("/tenant/connections", "unknown=value"),
        ("/tenant/mcp-logs", "tenant.id=tenant-b"),
        ("/tenant/mcp-logs", "tenant%5Bid%5D=tenant-b"),
        ("/tenant/mcp-logs", "TENANT_ID=tenant-b"),
        ("/tenant/mcp-logs", "tenant+id=tenant-b"),
        ("/tenant/mcp-logs", "tenant%3Aid=tenant-b"),
        ("/tenant/mcp-logs", "unknown=value"),
        ("/tenant/mcp-logs", "page=1&page=2"),
        ("/tenant/mcp-logs", "service_id=a"),
        ("/tenant/mcp-logs", "tool_alias=a"),
    ),
)
def test_tenant_read_routes_reject_unknown_or_repeated_query_before_store(
    monkeypatch, path, query
):
    calls = _spy_on_tenant_console_stores(monkeypatch)
    client = _tenant_client(monkeypatch)

    response = client.get(f"{path}?{query}")

    assert response.status_code == 422
    assert calls == []


def test_tenant_logs_use_connection_and_source_key_without_service_scope(monkeypatch):
    captured = {}

    def fake_list(filters, page, page_size):
        captured.update(filters=filters, page=page, page_size=page_size)
        return {"items": [], "total": 0}

    monkeypatch.setattr(mcp_log_store, "list_logs", fake_list)

    response = _tenant_client(monkeypatch).get(
        "/tenant/mcp-logs",
        params={
            "connection_id": "conn-a",
            "source_tool_key": "users.get",
            "status": "ok",
            "page": 2,
            "page_size": 50,
        },
    )

    assert response.status_code == 200
    assert captured == {
        "filters": LogFilters(
            tenant_id="tenant-a",
            connection_id="conn-a",
            tool_key="users.get",
            status="ok",
        ),
        "page": 2,
        "page_size": 50,
    }
    assert response.json() == {"items": [], "total": 0, "page": 2, "page_size": 50}


def test_tenant_overview_uses_connections_as_the_mcp_instance_boundary(monkeypatch):
    captured = {"connections": [], "logs": []}
    monkeypatch.setattr(
        connection_store,
        "list_connections",
        lambda tenant_id: captured["connections"].append(tenant_id)
        or [SimpleNamespace(status="active"), SimpleNamespace(status="disabled")],
    )
    def fake_stats(filters):
        captured["logs"].append(filters)
        return {
            "total": 7,
            "success_rate": 80.0,
            "error_count": 1,
            "avg_cost_ms": 12.5,
            "p95_cost_ms": 25,
            "trend": [],
            "top_tools": [],
            "status_distribution": [],
        }

    monkeypatch.setattr(mcp_log_store, "get_log_stats", fake_stats)

    response = _tenant_client(monkeypatch).get("/tenant/overview")

    assert response.status_code == 200
    assert captured == {
        "connections": ["tenant-a"],
        "logs": [LogFilters(tenant_id="tenant-a")],
    }
    assert response.json() == {
        "tenant_id": "tenant-a",
        "connections": {"total": 2, "active": 1},
        "mcp": {"total": 2, "active": 1},
        "logs": {
            "total": 7,
            "success_rate": 80.0,
            "error_count": 1,
            "avg_cost_ms": 12.5,
            "p95_cost_ms": 25,
            "trend": [],
            "top_tools": [],
            "status_distribution": [],
        },
    }
