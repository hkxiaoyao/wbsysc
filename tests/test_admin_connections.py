from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import admin, admin_connections
from app.connections.models import ConnectionRecord, IssuedToken
from app.connectors.contracts import ConnectorSpec, ToolSpec
from app.connectors.registry import ConnectorRegistry
from app.connectors.runtime import PolicyGuard


class _Connector:
    def spec(self):
        return ConnectorSpec(
            connector_key="sample",
            version="1.0.0",
            supports_data_modes=("direct",),
            config_schema={
                "type": "object",
                "required": ["base_url"],
                "properties": {"base_url": {"type": "string"}},
                "additionalProperties": False,
            },
            credential_schema={
                "type": "object",
                "required": ["api_key"],
                "properties": {"api_key": {"type": "string", "minLength": 8}},
                "additionalProperties": False,
            },
            tools=(
                ToolSpec(
                    tool_key="items.list",
                    mcp_name="items_list",
                    description="List items",
                    input_schema={"type": "object"},
                    output_schema=None,
                    operation_kind="read",
                    default_timeout_ms=1000,
                    cache_ttl_seconds=10,
                ),
            ),
        )


def _record(**changes):
    base = ConnectionRecord(
        connection_id="conn-a",
        tenant_id="tenant-a",
        connector_key="sample",
        display_name="Sample",
        status="active",
        data_mode="direct",
        public_config={"base_url": "https://api.example.com"},
        config_version=1,
    )
    return replace(base, **changes)


def _client(monkeypatch, *, authed=True):
    app = FastAPI()
    app.state.connector_registry = ConnectorRegistry([_Connector()])
    app.state.connection_sync_orchestrator = SimpleNamespace()
    app.include_router(admin_connections.router)
    monkeypatch.setattr(admin, "_is_authed", lambda request: authed)
    return TestClient(app)


def test_connection_api_requires_admin_session(monkeypatch):
    client = _client(monkeypatch, authed=False)
    assert client.get("/admin/tenants/tenant-a/connections").status_code == 401
    assert client.post("/admin/tenants/tenant-a/connections", json={}).status_code == 401


def test_tenant_scoped_lookup_returns_generic_404(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda cid, tid=None: None)
    response = client.get("/admin/tenants/tenant-a/connections/conn-b")
    assert response.status_code == 404
    assert response.json() == {"detail": "connection not found"}


def test_create_returns_raw_token_once_and_redacts_credentials(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(
        admin_connections.store,
        "create_connection_with_token",
        lambda record, credentials: (
            record,
            IssuedToken("token-id", "mcp_one_time_secret", "abc123"),
        ),
    )
    monkeypatch.setattr(admin_connections, "write_event", lambda event: True)
    response = client.post(
        "/admin/tenants/tenant-a/connections",
        json={
            "connector_key": "sample",
            "display_name": "Sample",
            "data_mode": "direct",
            "public_config": {"base_url": "https://api.example.com"},
            "credentials": {"api_key": "credential-secret"},
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["initial_token"] == "mcp_one_time_secret"
    assert "credential-secret" not in repr(body)
    assert "credentials" not in body["connection"]


def test_list_and_detail_never_return_raw_tokens_or_secret_config(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(admin_connections.store, "list_connections", lambda tenant_id: [_record()])
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda cid, tid=None: _record())
    monkeypatch.setattr(
        admin_connections.store,
        "list_connection_tokens",
        lambda connection_id: [{"token_id": "t1", "prefix": "abc123", "label": "cli"}],
    )
    listed = client.get("/admin/tenants/tenant-a/connections").json()
    detail = client.get("/admin/tenants/tenant-a/connections/conn-a").json()
    assert "token_hmac" not in repr((listed, detail))
    assert "raw_value" not in repr((listed, detail))
    assert detail["tokens"] == [{"token_id": "t1", "prefix": "abc123", "label": "cli"}]


def test_invalid_config_credentials_and_policy_fail_before_store(monkeypatch):
    client = _client(monkeypatch)
    calls = []
    monkeypatch.setattr(
        admin_connections.store,
        "create_connection_with_token",
        lambda *a, **k: calls.append("create"),
    )

    invalid_config = client.post(
        "/admin/tenants/tenant-a/connections",
        json={
            "connector_key": "sample",
            "display_name": "Sample",
            "data_mode": "direct",
            "public_config": {},
            "credentials": {"api_key": "credential-secret"},
        },
    )
    invalid_credentials = client.post(
        "/admin/tenants/tenant-a/connections",
        json={
            "connector_key": "sample",
            "display_name": "Sample",
            "data_mode": "direct",
            "public_config": {"base_url": "https://api.example.com"},
            "credentials": {"api_key": "short"},
        },
    )
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda cid, tid=None: _record())
    invalid_policy = client.put(
        "/admin/tenants/tenant-a/connections/conn-a/tools",
        json={"policies": [{"tool_key": "unknown", "enabled": True}]},
    )

    assert invalid_config.status_code == 422
    assert invalid_credentials.status_code == 422
    assert invalid_policy.status_code == 422
    assert calls == []


def test_global_connection_detail_uses_resolved_tenant_and_redacts_config(monkeypatch):
    client = _client(monkeypatch)
    spec = client.app.state.connector_registry.validated_spec("sample")
    object.__setattr__(
        spec,
        "config_schema",
        {
            "type": "object",
            "properties": {
                "base_url": {"type": "string"},
                "api_token": {"type": "string", "x-sensitive": True},
            },
        },
    )
    record = _record(
        public_config={
            "base_url": "https://api.example.com",
            "api_token": "config-secret",
        }
    )
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda cid, tid=None: record)
    monkeypatch.setattr(admin_connections.store, "list_connection_tokens", lambda cid: [])

    response = client.get("/admin/connections/conn-a")

    assert response.status_code == 200
    assert "config-secret" not in repr(response.json())
    assert response.json()["connection"]["public_config"] == {
        "base_url": "https://api.example.com"
    }


def test_declarative_revision_validate_publish_activate_lifecycle(monkeypatch):
    client = _client(monkeypatch)
    record = _record()
    revision = SimpleNamespace(spec_id="spec-a", revision=2, status="draft")
    published = SimpleNamespace(spec_id="spec-a", revision=2, status="published")
    calls = []
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda cid, tid=None: record)
    monkeypatch.setattr(
        admin_connections.store,
        "get_declarative_revision",
        lambda *args: revision,
        raising=False,
    )
    monkeypatch.setattr(admin_connections, "validate_revision", lambda item, data_mode=None: item)
    monkeypatch.setattr(
        admin_connections.store,
        "publish_declarative_revision",
        lambda *args: calls.append("publish") or published,
        raising=False,
    )
    monkeypatch.setattr(
        admin_connections.store,
        "activate_declarative_revision",
        lambda *args: calls.append("activate") or replace(record, config_version=2),
        raising=False,
    )
    monkeypatch.setattr(admin_connections, "write_event", lambda event: True)

    validated = client.post(
        "/admin/tenants/tenant-a/connections/conn-a/specs/spec-a/revisions/2/validate"
    )
    published_response = client.post(
        "/admin/tenants/tenant-a/connections/conn-a/specs/spec-a/revisions/2/publish"
    )
    activated = client.post(
        "/admin/tenants/tenant-a/connections/conn-a/specs/spec-a/revisions/2/activate"
    )

    assert validated.json()["valid"] is True
    assert published_response.json()["status"] == "published"
    assert activated.json()["connection"]["config_version"] == 2
    assert calls == ["publish", "activate"]


def test_disabled_connection_cannot_run_manual_sync(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(
        admin_connections.store,
        "get_connection",
        lambda cid, tid=None: _record(status="disabled", data_mode="stored"),
    )
    response = client.post("/admin/tenants/tenant-a/connections/conn-a/sync")
    assert response.status_code == 409


def test_global_mutation_alias_resolves_owner_before_disable(monkeypatch):
    client = _client(monkeypatch)
    record = _record()
    calls = []
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda cid, tid=None: record)
    monkeypatch.setattr(
        admin_connections.store,
        "disable_connection",
        lambda cid, tid: calls.append((cid, tid)) or replace(record, status="disabled"),
    )
    monkeypatch.setattr(admin_connections, "write_event", lambda event: True)

    response = client.post("/admin/connections/conn-a/disable")

    assert response.status_code == 200
    assert calls == [("conn-a", "tenant-a")]


def test_tool_policy_payload_is_accepted_by_runtime_guard(monkeypatch):
    client = _client(monkeypatch)
    record = _record()
    captured = []
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda *args: record)
    monkeypatch.setattr(
        admin_connections.store,
        "replace_tool_policies",
        lambda cid, tid, policies: captured.extend(policies) or True,
    )
    monkeypatch.setattr(admin_connections, "write_event", lambda event: True)

    response = client.put(
        "/admin/connections/conn-a/tools",
        json={
            "policies": [
                {
                    "tool_key": "items.list",
                    "enabled": True,
                    "rate_limit_per_minute": 12,
                }
            ]
        },
    )

    tool = client.app.state.connector_registry.validated_spec("sample").tools[0]
    resolved = PolicyGuard().resolve(tool, captured[0])
    assert response.status_code == 200
    assert resolved.rate_limit.limit == 12
    assert resolved.rate_limit.window_seconds == 60


def test_explicit_token_rotation_returns_new_raw_token_once(monkeypatch):
    client = _client(monkeypatch)
    record = _record()
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda *args: record)
    monkeypatch.setattr(
        admin_connections.store,
        "rotate_token",
        lambda cid, tid, label="": IssuedToken(
            "new-token", "mcp_rotated_once", "rotated-prefix"
        ),
        raising=False,
    )
    monkeypatch.setattr(admin_connections, "write_event", lambda event: True)

    response = client.post(
        "/admin/connections/conn-a/tokens/rotate", json={"label": "cli"}
    )

    assert response.status_code == 201
    assert response.json()["token"] == "mcp_rotated_once"
