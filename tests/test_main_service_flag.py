from __future__ import annotations

import inspect
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import main


def _app(monkeypatch, *, enabled: bool):
    current = main.get_settings()
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(
            mcp_service_enabled=enabled,
            app_env=current.app_env,
            wecom_use_mock=current.wecom_use_mock,
        ),
    )
    return main.create_app()


def test_health_reports_effective_service_flag_as_boolean(monkeypatch):
    disabled = TestClient(_app(monkeypatch, enabled=False)).get("/health")
    enabled = TestClient(_app(monkeypatch, enabled=True)).get("/health")

    assert disabled.status_code == 200
    assert disabled.json()["mcp_service_enabled"] is False
    assert disabled.json()["mcp_service_legacy_enabled"] is False
    assert enabled.status_code == 200
    assert enabled.json()["mcp_service_enabled"] is True
    assert enabled.json()["mcp_service_legacy_enabled"] is True


def test_only_admin_legacy_cleanup_routes_remain_when_runtime_is_disabled(
    monkeypatch,
):
    app = _app(monkeypatch, enabled=False)
    paths = {getattr(route, "path", "") for route in app.routes}
    client = TestClient(app)

    assert "/tenant/services" not in paths
    assert client.get("/tenant/services").status_code == 404
    assert client.get("/admin/tenants/tenant-a/services").status_code == 401
    assert client.post("/admin/tenants/tenant-a/services").status_code == 405


def test_enabled_flag_mounts_only_legacy_service_runtime_not_management(monkeypatch):
    app = _app(monkeypatch, enabled=True)
    paths = {getattr(route, "path", "") for route in app.routes}

    assert "/tenant/services" not in paths
    assert "/mcp/service/{service_id}" in paths


def test_startup_no_longer_backfills_default_services():
    assert "migrate_default_services" not in inspect.getsource(main.lifespan)


def test_connection_and_legacy_mounts_survive_flag_with_service_precedence(monkeypatch):
    disabled_paths = [
        getattr(route, "path", "") for route in _app(monkeypatch, enabled=False).routes
    ]
    enabled_paths = [
        getattr(route, "path", "") for route in _app(monkeypatch, enabled=True).routes
    ]

    for paths in (disabled_paths, enabled_paths):
        assert "/mcp/{connection_id}" in paths
        assert "/mcp" in paths
        assert paths.index("/mcp/{connection_id}") < paths.index("/mcp")
    assert enabled_paths.index("/mcp/service/{service_id}") < enabled_paths.index(
        "/mcp/{connection_id}"
    )
