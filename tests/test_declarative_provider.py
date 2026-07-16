from __future__ import annotations

from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from app import admin, admin_connections
from app.auth import ConnectionCtx
from app.connections.models import ConnectionRecord, IssuedToken, ToolPolicy
from app.connections.sync import SyncOrchestrator
from app.connectors.contracts import ConnectionContext
from app.connectors.declarative.http_client import SafeHttpClient
from app.connectors.declarative.provider import (
    DeclarativeConnectorProvider,
    DeclarativeProviderUnavailableError,
)
from app.connectors.declarative.validator import import_openapi_revision
from app.connectors.registry import ConnectorRegistry
from app.connectors.runtime import ConnectionConnectorResolver, ConnectorRuntime
from app.mcp_gateway import ConnectionMcpGateway
from app.main import create_app


def _document() -> dict:
    return {
        "openapi": "3.0.3",
        "servers": [{"url": "https://api.example.com/v1"}],
        "paths": {
            "/health": {
                "get": {
                    "operationId": "health.get",
                    "responses": {
                        "200": {
                            "description": "ok",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "properties": {"ok": {"type": "boolean"}},
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
    }


def _revision(*, status="published", tenant_id="tenant-a", connection_id="conn-a", revision=1):
    return import_openapi_revision(
        _document(),
        spec_id="spec-a",
        revision=revision,
        tenant_id=tenant_id,
        connection_id=connection_id,
        status=status,
    )


def _sync_revision():
    document = _document()
    document["x-sync-spec"] = {
        "resource_key": "health",
        "operation_key": "health.get",
        "primary_key_pointer": "/ok",
        "field_mappings": {"ok": "/ok"},
    }
    return import_openapi_revision(
        document,
        spec_id="spec-a",
        revision=1,
        tenant_id="tenant-a",
        connection_id="conn-a",
        status="published",
    )


def _context(*, tenant_id="tenant-a", connection_id="conn-a", revision=1):
    return ConnectionContext(
        connection=ConnectionRecord(
            connection_id=connection_id,
            tenant_id=tenant_id,
            connector_key="http_declarative",
            display_name="API",
            status="active",
            data_mode="direct",
            public_config={"spec_id": "spec-a", "revision": revision},
            config_version=revision,
        )
    )


@pytest.mark.asyncio
async def test_provider_derives_spec_executes_and_closes_exact_host_client():
    closed = []

    def client_factory(revision):
        client = SafeHttpClient._for_test(
            revision.allowed_hosts,
            resolver=lambda host, port: ["93.184.216.34"],
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"ok": True}, request=request)
            ),
        )
        original_close = client.aclose

        async def close():
            closed.append(True)
            await original_close()

        client.aclose = close
        return client

    provider = DeclarativeConnectorProvider._for_test(
        revision_loader=lambda spec_id, revision, tenant_id, connection_id: _revision(),
        client_factory=client_factory,
    )
    context = _context()

    assert provider.spec_for(context).tool("health.get").mcp_name == "health.get"
    assert provider.spec_for(context).config_schema["required"] == [
        "spec_id",
        "revision",
    ]
    async with provider.connect(context) as connector:
        result = await connector.execute(context, "health.get", {})

    assert result.data == {"ok": True}
    assert closed == [True]


@pytest.mark.parametrize(
    "loaded",
    [
        _revision(status="draft"),
        _revision(tenant_id="tenant-b"),
        _revision(connection_id="conn-b"),
        _revision(revision=2),
    ],
)
def test_provider_fails_closed_for_unpublished_or_wrong_scope(loaded):
    provider = DeclarativeConnectorProvider(
        revision_loader=lambda *args: loaded,
    )
    with pytest.raises(DeclarativeProviderUnavailableError):
        provider.spec_for(_context())


def test_provider_sanitizes_corrupt_storage_failure():
    provider = DeclarativeConnectorProvider(
        revision_loader=lambda *args: (_ for _ in ()).throw(
            ValueError("secret=stored-corruption")
        )
    )
    with pytest.raises(DeclarativeProviderUnavailableError) as exc:
        provider.spec_for(_context())
    assert "secret" not in str(exc.value)


@pytest.mark.asyncio
async def test_runtime_resolves_declarative_spec_and_connector_per_connection():
    revision = _revision()
    provider = DeclarativeConnectorProvider._for_test(
        revision_loader=lambda *args: revision,
        client_factory=lambda loaded: SafeHttpClient._for_test(
            loaded.allowed_hosts,
            resolver=lambda host, port: ["93.184.216.34"],
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"ok": True}, request=request)
            ),
        ),
    )
    resolver = ConnectionConnectorResolver(
        ConnectorRegistry(), declarative_provider=provider
    )
    runtime = ConnectorRuntime(ConnectorRegistry(), connector_resolver=resolver)
    context = _context()

    assert [tool.tool_key for tool in runtime.list_enabled_tools(context)] == [
        "health.get"
    ]
    result = await runtime.execute(context, "health.get", {})
    assert result.data == {"ok": True}


def test_registry_reserves_declarative_connector_key():
    revision = _revision()

    class FakeStaticConnector:
        def spec(self):
            return revision.connector_spec()

    with pytest.raises(ValueError, match="reserved"):
        ConnectorRegistry([FakeStaticConnector()])


def test_default_gateway_wires_production_declarative_provider():
    gateway = ConnectionMcpGateway()
    resolver = gateway._runtime._connector_resolver
    assert isinstance(
        resolver._declarative_provider, DeclarativeConnectorProvider
    )


def test_app_sync_and_gateway_share_exact_connector_resolver():
    gateway = ConnectionMcpGateway()
    app = create_app(gateway=gateway)
    assert (
        app.state.connection_sync_orchestrator._connector_resolver
        is gateway._runtime._connector_resolver
    )


def test_admin_lifecycle_then_gateway_lists_and_executes_dynamic_revision(monkeypatch):
    state = {"record": None}
    revisions = {}
    policy_tools = ["health.get"]
    raw_token = "mcp_lifecycle_token"

    def revision_loader(spec_id, revision, tenant_id, connection_id):
        return revisions.get((spec_id, revision, tenant_id, connection_id))

    provider = DeclarativeConnectorProvider._for_test(
        revision_loader=revision_loader,
        client_factory=lambda revision: SafeHttpClient._for_test(
            revision.allowed_hosts,
            resolver=lambda host, port: ["93.184.216.34"],
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"ok": True}, request=request)
            ),
        ),
    )
    registry = ConnectorRegistry()
    connector_resolver = ConnectionConnectorResolver(
        registry, declarative_provider=provider
    )
    runtime = ConnectorRuntime(registry, connector_resolver=connector_resolver)

    class Resolver:
        def resolve(self, connection_id, token):
            record = state["record"]
            if (
                record is None
                or record.status != "active"
                or record.connection_id != connection_id
                or token != raw_token
            ):
                return None
            return ConnectionCtx(
                tenant_id=record.tenant_id,
                connection_id=record.connection_id,
                connector_key=record.connector_key,
                data_mode=record.data_mode,
                public_config=record.public_config,
                config_version=record.config_version,
            )

        def resolve_legacy(self, token):
            return None

        def execution_context(self, ctx):
            return ConnectionContext(connection=state["record"], credentials={})

    gateway = ConnectionMcpGateway(resolver=Resolver(), runtime=runtime)
    app = create_app(gateway=gateway)
    app.router.lifespan_context = lambda _app: gateway.run()
    monkeypatch.setattr(admin, "_is_authed", lambda request: True)
    monkeypatch.setattr(admin_connections, "write_event", lambda event: True)

    def create(record, credentials):
        state["record"] = record
        return record, IssuedToken("token-id", raw_token, "prefix")

    monkeypatch.setattr(admin_connections.store, "create_connection_with_token", create)
    monkeypatch.setattr(
        admin_connections.store,
        "get_connection",
        lambda connection_id, tenant_id=None: (
            state["record"]
            if state["record"] is not None
            and state["record"].connection_id == connection_id
            and (tenant_id is None or tenant_id == state["record"].tenant_id)
            else None
        ),
    )
    monkeypatch.setattr(admin_connections.store, "list_connection_tokens", lambda cid: [])
    monkeypatch.setattr(
        admin_connections.store,
        "save_declarative_revision",
        lambda revision, **kwargs: revisions.__setitem__(
            (
                revision.spec_id,
                revision.revision,
                revision.tenant_id,
                revision.connection_id,
            ),
            revision,
        ),
    )
    monkeypatch.setattr(
        admin_connections.store,
        "get_declarative_revision",
        revision_loader,
    )

    def publish(spec_id, revision, tenant_id, connection_id, **kwargs):
        key = (spec_id, revision, tenant_id, connection_id)
        published = replace(revisions[key], status="published")
        revisions[key] = published
        config = dict(state["record"].public_config)
        if state["record"].status == "draft":
            config.update({"spec_id": spec_id, "revision": revision})
        else:
            config.update(
                {"pending_spec_id": spec_id, "pending_revision": revision}
            )
        state["record"] = replace(
            state["record"],
            public_config=config,
            config_version=state["record"].config_version + 1,
        )
        return published

    def activate(spec_id, revision, tenant_id, connection_id, **kwargs):
        config = dict(state["record"].public_config)
        config.update({"spec_id": spec_id, "revision": revision})
        config.pop("pending_spec_id", None)
        config.pop("pending_revision", None)
        state["record"] = replace(
            state["record"],
            status="active",
            public_config=config,
            config_version=state["record"].config_version + 1,
        )
        return state["record"]

    def disable(connection_id, tenant_id):
        state["record"] = replace(
            state["record"],
            status="disabled",
            config_version=state["record"].config_version + 1,
        )
        return state["record"]

    monkeypatch.setattr(admin_connections.store, "publish_declarative_revision", publish)
    monkeypatch.setattr(admin_connections.store, "activate_declarative_revision", activate)
    monkeypatch.setattr(admin_connections.store, "disable_connection", disable)
    monkeypatch.setattr(
        admin_connections.store,
        "list_tool_policies",
        lambda connection_id: [
            ToolPolicy(connection_id, tool_key, True, {})
            for tool_key in policy_tools
        ],
    )

    with TestClient(app) as client:
        created = client.post(
            "/admin/tenants/tenant-a/connections",
            json={
                "connector_key": "http_declarative",
                "display_name": "API",
                "data_mode": "direct",
                "status": "draft",
                "public_config": {},
                "credentials": {},
            },
        )
        connection_id = created.json()["connection"]["connection_id"]
        imported = client.post(
            f"/admin/connections/{connection_id}/specs/import",
            json={"document": _document(), "spec_id": "spec-a", "revision": 1},
        )
        assert imported.status_code == 201, imported.text
        published = client.post(
            f"/admin/connections/{connection_id}/specs/spec-a/revisions/1/publish"
        )
        activated = client.post(
            f"/admin/connections/{connection_id}/specs/spec-a/revisions/1/activate"
        )
        disabled = client.post(f"/admin/connections/{connection_id}/disable")
        v2_document = _document()
        v2_document["paths"]["/health"]["get"]["operationId"] = "health.v2"
        imported_v2 = client.post(
            f"/admin/connections/{connection_id}/specs/import",
            json={"document": v2_document, "spec_id": "spec-b", "revision": 2},
        )
        published_v2 = client.post(
            f"/admin/connections/{connection_id}/specs/spec-b/revisions/2/publish"
        )
        policy_tools[:] = ["health.v2"]
        activated_v2 = client.post(
            f"/admin/connections/{connection_id}/specs/spec-b/revisions/2/activate"
        )
        headers = {
            "Authorization": f"Bearer {raw_token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        tools = client.post(
            f"/mcp/{connection_id}",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        called = client.post(
            f"/mcp/{connection_id}",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "health.v2", "arguments": {}},
            },
        )

    assert [
        created.status_code,
        imported.status_code,
        published.status_code,
        activated.status_code,
        disabled.status_code,
        imported_v2.status_code,
        published_v2.status_code,
        activated_v2.status_code,
    ] == [201, 201, 200, 200, 200, 201, 200, 200]
    assert tools.status_code == 200
    assert [tool["name"] for tool in tools.json()["result"]["tools"]] == [
        "health.v2"
    ]
    assert called.status_code == 200
    assert called.json()["result"]["structuredContent"] == {"ok": True}


@pytest.mark.asyncio
async def test_sync_orchestrator_uses_same_connection_scoped_provider():
    loaded = _sync_revision()
    provider = DeclarativeConnectorProvider._for_test(
        revision_loader=lambda *args: loaded,
        client_factory=lambda revision: SafeHttpClient._for_test(
            revision.allowed_hosts,
            resolver=lambda host, port: ["93.184.216.34"],
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"ok": True}, request=request)
            ),
        ),
    )
    registry = ConnectorRegistry()
    resolver = ConnectionConnectorResolver(registry, declarative_provider=provider)
    record = replace(
        _context().connection,
        data_mode="stored",
        public_config={
            "spec_id": "spec-a",
            "revision": 1,
            "sync_resources": ["health"],
        },
    )

    class Contexts:
        def build(self, connection):
            return ConnectionContext(connection=connection)

    orchestrator = SyncOrchestrator(
        registry,
        contexts=Contexts(),
        connector_resolver=resolver,
        connection_refresher=lambda connection_id, tenant_id: record,
    )
    result = await orchestrator.run_connection(record, "health")

    assert result.status == "ok"
    assert result.connection_id == "conn-a"
