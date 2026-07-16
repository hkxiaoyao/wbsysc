from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import admin, admin_connections
from app.connections.models import ConnectionRecord, IssuedToken
from app.connectors.contracts import (
    ConnectionContext,
    ConnectorSpec,
    SyncResult,
    ToolSpec,
)
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


def test_validated_update_passes_owned_version_to_atomic_store_mutation(monkeypatch):
    client = _client(monkeypatch)
    record = _record(config_version=7)
    captured = []
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda *args: record)

    def update(*args, **kwargs):
        captured.append(kwargs["expected_config_version"])
        return replace(record, display_name="Updated", config_version=8)

    monkeypatch.setattr(admin_connections.store, "update_connection", update)
    monkeypatch.setattr(admin_connections, "write_event", lambda event: True)

    response = client.put(
        "/admin/connections/conn-a",
        json={
            "display_name": "Updated",
            "data_mode": "direct",
            "public_config": {"base_url": "https://api.example.com"},
            "status": "active",
        },
    )

    assert response.status_code == 200
    assert captured == [7]


def test_store_version_conflict_is_a_safe_http_409(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda *args: _record())

    def conflict(*args, **kwargs):
        raise admin_connections.store.ConnectionVersionConflictError

    monkeypatch.setattr(admin_connections.store, "update_connection", conflict)

    response = client.put(
        "/admin/connections/conn-a",
        json={
            "display_name": "Updated",
            "data_mode": "direct",
            "public_config": {"base_url": "https://api.example.com"},
            "status": "active",
        },
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "connection configuration changed"}


def test_validated_credentials_and_policies_pass_owned_version_to_store(monkeypatch):
    client = _client(monkeypatch)
    record = _record(config_version=7)
    captured = []
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda *args: record)
    monkeypatch.setattr(
        admin_connections.store,
        "replace_credentials",
        lambda *args, **kwargs: captured.append(
            ("credentials", kwargs["expected_config_version"])
        )
        or True,
    )
    monkeypatch.setattr(
        admin_connections.store,
        "replace_tool_policies",
        lambda *args, **kwargs: captured.append(
            ("policies", kwargs["expected_config_version"])
        )
        or True,
    )
    monkeypatch.setattr(admin_connections, "write_event", lambda event: True)

    credentials = client.put(
        "/admin/connections/conn-a/credentials",
        json={"credentials": {"api_key": "credential-secret"}},
    )
    policies = client.put(
        "/admin/connections/conn-a/tools",
        json={"policies": [{"tool_key": "items.list", "enabled": True}]},
    )

    assert credentials.status_code == 200
    assert policies.status_code == 200
    assert captured == [("credentials", 7), ("policies", 7)]


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
    record = _record(connector_key="http_declarative", status="draft")
    record = replace(
        record, public_config={"spec_id": "spec-a", "revision": 2}
    )
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
        admin_connections,
        "_declarative_candidate_spec",
        lambda *args: ConnectorSpec(connector_key="http_declarative", tools=()),
    )
    monkeypatch.setattr(
        admin_connections, "_load_connection_credentials", lambda *args: {}
    )
    monkeypatch.setattr(
        admin_connections.store,
        "publish_declarative_revision",
        lambda *args, **kwargs: calls.append("publish") or published,
        raising=False,
    )
    monkeypatch.setattr(
        admin_connections.store,
        "activate_declarative_revision",
        lambda *args, **kwargs: calls.append("activate")
        or replace(record, status="active", config_version=2),
        raising=False,
    )
    monkeypatch.setattr(admin_connections.store, "list_tool_policies", lambda cid: [])
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
        lambda cid, tid, policies, **kwargs: captured.extend(policies) or True,
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


def test_safe_config_projects_only_declared_nested_and_array_fields():
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "nested": {
                "type": "object",
                "properties": {"safe": {"type": "string"}},
                "additionalProperties": False,
            },
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"safe": {"type": "string"}},
                    "additionalProperties": False,
                },
            },
            "metadata": {
                "type": "object",
                "additionalProperties": {"type": "string"},
            },
        },
        "additionalProperties": False,
    }
    value = {
        "name": "visible",
        "unknown": "must-not-leak",
        "nested": {"safe": "visible", "extra": "must-not-leak"},
        "items": [{"safe": "visible", "secret": "must-not-leak"}],
        "metadata": {"region": "cn", "token": "must-not-leak"},
    }

    projected = admin_connections._safe_config(value, schema)

    assert projected == {
        "name": "visible",
        "nested": {"safe": "visible"},
        "items": [{"safe": "visible"}],
        "metadata": {"region": "cn"},
    }
    assert "must-not-leak" not in repr(projected)


def test_safe_config_omits_declared_scalars_with_wrong_runtime_types():
    projected = admin_connections._safe_config(
        {
            "name": 7,
            "count": True,
            "ratio": 2,
            "enabled": 1,
            "nullable": None,
            "items": "not-an-array",
        },
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "count": {"type": "integer"},
                "ratio": {"type": "number"},
                "enabled": {"type": "boolean"},
                "nullable": {"type": "null"},
                "items": {"type": "array", "items": {"type": "string"}},
            },
            "additionalProperties": False,
        },
    )

    assert projected == {"ratio": 2, "nullable": None}


def test_declarative_create_is_draft_and_does_not_require_static_registry(monkeypatch):
    client = _client(monkeypatch)
    captured = []
    monkeypatch.setattr(
        admin_connections.store,
        "create_connection_with_token",
        lambda record, credentials: (
            captured.append((record, credentials)) or record,
            IssuedToken("token-id", "mcp_once", "prefix"),
        ),
    )
    monkeypatch.setattr(admin_connections, "write_event", lambda event: True)

    response = client.post(
        "/admin/tenants/tenant-a/connections",
        json={
            "connector_key": "http_declarative",
            "display_name": "Declared API",
            "data_mode": "direct",
            "status": "draft",
            "public_config": {},
            "credentials": {},
        },
    )

    assert response.status_code == 201
    assert captured[0][0].status == "draft"
    assert captured[0][0].public_config == {}


def test_declarative_create_rejects_prebound_config_or_credentials(monkeypatch):
    client = _client(monkeypatch)
    for payload in (
        {"public_config": {"spec_id": "forged"}, "credentials": {}},
        {"public_config": {}, "credentials": {"api_key": "forged-secret"}},
    ):
        response = client.post(
            "/admin/tenants/tenant-a/connections",
            json={
                "connector_key": "http_declarative",
                "display_name": "Declared API",
                "data_mode": "direct",
                "status": "draft",
                **payload,
            },
        )
        assert response.status_code == 422


def test_spec_lifecycle_rejects_non_declarative_connection(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda *args: _record())
    response = client.post(
        "/admin/connections/conn-a/specs/spec-a/revisions/1/publish"
    )
    assert response.status_code == 422


def test_active_declarative_connection_rejects_import_and_publish(monkeypatch):
    client = _client(monkeypatch)
    record = _record(
        connector_key="http_declarative",
        public_config={"spec_id": "spec-a", "revision": 1},
    )
    calls = []
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda *args: record)
    monkeypatch.setattr(
        admin_connections.store,
        "save_declarative_revision",
        lambda *args, **kwargs: calls.append("import"),
    )
    monkeypatch.setattr(
        admin_connections.store,
        "publish_declarative_revision",
        lambda *args, **kwargs: calls.append("publish"),
    )

    imported = client.post(
        "/admin/connections/conn-a/specs/import",
        json={"document": {}, "spec_id": "spec-b", "revision": 2},
    )
    published = client.post(
        "/admin/connections/conn-a/specs/spec-b/revisions/2/publish"
    )

    assert imported.status_code == 409
    assert published.status_code == 409
    assert calls == []


def test_generic_update_cannot_activate_a_disabled_declarative_connection(monkeypatch):
    client = _client(monkeypatch)
    record = _record(
        connector_key="http_declarative",
        status="disabled",
        public_config={
            "spec_id": "spec-a",
            "revision": 1,
            "pending_spec_id": "spec-b",
            "pending_revision": 2,
        },
        config_version=7,
    )
    calls = []
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda *args: record)
    monkeypatch.setattr(
        admin_connections,
        "_spec_for_record",
        lambda *args: ConnectorSpec(
            connector_key="http_declarative",
            tools=(),
            config_schema={
                "type": "object",
                "properties": {
                    "spec_id": {"type": "string"},
                    "revision": {"type": "integer"},
                    "pending_spec_id": {"type": "string"},
                    "pending_revision": {"type": "integer"},
                },
                "additionalProperties": False,
            },
            supports_data_modes=("direct",),
        ),
    )
    monkeypatch.setattr(
        admin_connections.store,
        "update_connection",
        lambda *args, **kwargs: calls.append("update"),
    )

    response = client.put(
        "/admin/connections/conn-a",
        json={
            "display_name": "API",
            "data_mode": "direct",
            "public_config": record.public_config,
            "status": "active",
        },
    )

    assert response.status_code == 409
    assert calls == []


def test_declarative_import_passes_owned_version_to_atomic_save(monkeypatch):
    client = _client(monkeypatch)
    record = _record(
        connector_key="http_declarative", status="draft", config_version=7
    )
    revision = SimpleNamespace(spec_id="spec-b", revision=2, status="draft")
    captured = []
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda *args: record)
    monkeypatch.setattr(
        admin_connections,
        "import_openapi_revision",
        lambda *args, **kwargs: revision,
    )
    monkeypatch.setattr(admin_connections, "validate_revision", lambda *args, **kwargs: revision)
    monkeypatch.setattr(
        admin_connections.store,
        "save_declarative_revision",
        lambda *args, **kwargs: captured.append(kwargs.get("expected_config_version")),
    )
    monkeypatch.setattr(admin_connections, "write_event", lambda event: True)

    response = client.post(
        "/admin/connections/conn-a/specs/import",
        json={"document": {}, "spec_id": "spec-b", "revision": 2},
    )

    assert response.status_code == 201
    assert captured == [7]


def test_disabled_pending_revision_drives_credentials_tools_and_real_test(monkeypatch):
    client = _client(monkeypatch)
    record = _record(
        connector_key="http_declarative",
        status="disabled",
        public_config={
            "spec_id": "spec-a",
            "revision": 1,
            "pending_spec_id": "spec-b",
            "pending_revision": 2,
        },
        config_version=7,
    )
    pending_tool = ToolSpec(
        tool_key="new.health",
        mcp_name="new.health",
        description="New health",
        input_schema={"type": "object", "additionalProperties": False},
        output_schema=None,
        operation_kind="read",
        default_timeout_ms=1000,
        cache_ttl_seconds=None,
    )
    pending_spec = ConnectorSpec(
        connector_key="http_declarative",
        tools=(pending_tool,),
        credential_schema={
            "type": "object",
            "required": ["new_key"],
            "properties": {"new_key": {"type": "string"}},
            "additionalProperties": False,
        },
    )
    selected = []
    mutations = []
    executed = []
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda *args: record)

    def candidate(request, candidate_record, spec_id, revision):
        selected.append((spec_id, revision))
        assert (spec_id, revision) == ("spec-b", 2)
        return pending_spec

    monkeypatch.setattr(admin_connections, "_declarative_candidate_spec", candidate)
    monkeypatch.setattr(
        admin_connections.store,
        "replace_credentials",
        lambda *args, **kwargs: mutations.append(
            ("credentials", kwargs["expected_config_version"])
        )
        or True,
    )
    monkeypatch.setattr(
        admin_connections.store,
        "list_tool_policies",
        lambda connection_id: [
            admin_connections.ToolPolicy(connection_id, "new.health", True, {})
        ],
    )
    monkeypatch.setattr(
        admin_connections.store,
        "replace_tool_policies",
        lambda *args, **kwargs: mutations.append(
            ("policies", kwargs["expected_config_version"])
        )
        or True,
    )
    monkeypatch.setattr(admin_connections, "write_event", lambda event: True)

    class Resolver:
        def execution_context(self, ctx):
            selected.append(
                (ctx.public_config.get("spec_id"), ctx.public_config.get("revision"))
            )
            return ConnectionContext(connection=replace(record, public_config=dict(ctx.public_config)), credentials={"new_key": "secret"})

    class Runtime:
        def list_enabled_tools(self, context):
            return (pending_tool,)

        async def execute(self, context, tool_key, args):
            executed.append((context.public_config, tool_key))
            return SimpleNamespace(status="ok")

    client.app.state.mcp_gateway = SimpleNamespace(
        resolver=Resolver(), _runtime=Runtime()
    )

    credentials = client.put(
        "/admin/connections/conn-a/credentials",
        json={"credentials": {"new_key": "new-secret"}},
    )
    listed = client.get("/admin/connections/conn-a/tools")
    policies = client.put(
        "/admin/connections/conn-a/tools",
        json={"policies": [{"tool_key": "new.health", "enabled": True}]},
    )
    tested = client.post("/admin/connections/conn-a/test")

    assert [credentials.status_code, listed.status_code, policies.status_code, tested.status_code] == [200, 200, 200, 200]
    assert listed.json()["items"][0]["tool_key"] == "new.health"
    assert mutations == [("credentials", 7), ("policies", 7)]
    assert selected and all(item == ("spec-b", 2) for item in selected)
    assert executed == [
        (
            {
                "spec_id": "spec-b",
                "revision": 2,
                "pending_spec_id": "spec-b",
                "pending_revision": 2,
            },
            "new.health",
        )
    ]


def test_activation_requires_complete_exact_pending_tool_policies(monkeypatch):
    client = _client(monkeypatch)
    record = _record(
        connector_key="http_declarative",
        status="disabled",
        public_config={
            "spec_id": "spec-a",
            "revision": 1,
            "pending_spec_id": "spec-b",
            "pending_revision": 2,
        },
        config_version=7,
    )
    tools = tuple(
        ToolSpec(
            tool_key=key,
            mcp_name=key,
            description=key,
            input_schema={"type": "object"},
            output_schema=None,
            operation_kind="read",
            default_timeout_ms=1000,
            cache_ttl_seconds=None,
        )
        for key in ("new.one", "new.two")
    )
    configured = [admin_connections.ToolPolicy("conn-a", "new.one", True, {})]
    activated = []
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda *args: record)
    monkeypatch.setattr(
        admin_connections,
        "_declarative_candidate_spec",
        lambda *args: ConnectorSpec(connector_key="http_declarative", tools=tools),
    )
    monkeypatch.setattr(admin_connections, "_load_connection_credentials", lambda *args: {})
    monkeypatch.setattr(
        admin_connections.store,
        "list_tool_policies",
        lambda connection_id: list(configured),
    )
    monkeypatch.setattr(
        admin_connections.store,
        "activate_declarative_revision",
        lambda *args, **kwargs: activated.append(kwargs["expected_config_version"])
        or replace(record, status="active", config_version=8),
    )
    monkeypatch.setattr(admin_connections, "write_event", lambda event: True)

    missing = client.post(
        "/admin/connections/conn-a/specs/spec-b/revisions/2/activate"
    )
    configured[:] = [
        admin_connections.ToolPolicy("conn-a", "new.one", True, {}),
        admin_connections.ToolPolicy("conn-a", "stale.old", False, {}),
    ]
    stale = client.post(
        "/admin/connections/conn-a/specs/spec-b/revisions/2/activate"
    )
    configured[:] = [
        admin_connections.ToolPolicy("conn-a", tool.tool_key, True, {})
        for tool in tools
    ]
    complete = client.post(
        "/admin/connections/conn-a/specs/spec-b/revisions/2/activate"
    )

    assert missing.status_code == 409
    assert stale.status_code == 409
    assert complete.status_code == 200
    assert activated == [7]


def test_connection_test_executes_real_safe_read_operation(monkeypatch):
    client = _client(monkeypatch)
    record = _record()
    calls = []

    class Resolver:
        def execution_context(self, ctx):
            return ConnectionContext(connection=record, credentials={"api_key": "secret"})

    class Runtime:
        def list_enabled_tools(self, context):
            return client.app.state.connector_registry.validated_spec("sample").tools

        async def execute(self, context, tool_key, args):
            calls.append((context.connection_id, tool_key, args))
            return SimpleNamespace(status="ok", data={"ok": True})

    client.app.state.mcp_gateway = SimpleNamespace(
        resolver=Resolver(), _runtime=Runtime()
    )
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda *args: record)
    monkeypatch.setattr(admin_connections, "write_event", lambda event: True)

    response = client.post("/admin/connections/conn-a/test")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "status": "ok"}
    assert calls == [("conn-a", "items.list", {})]


def test_activation_rejects_missing_revision_credentials_before_store(monkeypatch):
    client = _client(monkeypatch)
    record = _record(
        connector_key="http_declarative",
        status="draft",
        public_config={"spec_id": "spec-a", "revision": 1},
    )
    calls = []
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda *args: record)
    monkeypatch.setattr(
        admin_connections,
        "_declarative_candidate_spec",
        lambda request, record, spec_id, revision: ConnectorSpec(
            connector_key="http_declarative",
            tools=(),
            credential_schema={
                "type": "object",
                "required": ["api_key"],
                "properties": {"api_key": {"type": "string"}},
                "additionalProperties": False,
            },
        ),
        raising=False,
    )
    monkeypatch.setattr(
        admin_connections,
        "_load_connection_credentials",
        lambda request, record: {},
        raising=False,
    )
    monkeypatch.setattr(
        admin_connections.store,
        "activate_declarative_revision",
        lambda *args: calls.append("activate"),
    )

    response = client.post(
        "/admin/connections/conn-a/specs/spec-a/revisions/1/activate"
    )

    assert response.status_code == 422
    assert calls == []


def test_manual_sync_writes_safe_management_audit(monkeypatch):
    client = _client(monkeypatch)
    record = _record(data_mode="stored")
    events = []

    class Orchestrator:
        async def run_connection(self, connection):
            return SyncResult.ok(connection.connection_id, "health", {"stored": 1})

    client.app.state.connection_sync_orchestrator = Orchestrator()
    monkeypatch.setattr(admin_connections.store, "get_connection", lambda *args: record)
    monkeypatch.setattr(admin_connections, "write_event", events.append)

    response = client.post("/admin/connections/conn-a/sync")

    assert response.status_code == 200
    assert any(event.event_name == "connection_sync_triggered" for event in events)
    assert "secret" not in repr(events)
