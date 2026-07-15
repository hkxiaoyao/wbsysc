import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient

from app import main
from app.auth import ConnectionCtx
from app.connections.models import ConnectionRecord, ToolPolicy
from app.connectors import (
    ConnectionContext,
    ConnectorRegistry,
    ConnectorRuntime,
    ConnectorSpec,
    ExecutionResult,
    ToolSpec,
)
from app.mcp_gateway import (
    ConnectionMcpGateway,
    ConnectionResolver,
    default_wecom_connection_id,
)


TOOLS_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
def bearer(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }


def tool_names(payload: dict[str, Any]) -> set[str]:
    return {tool["name"] for tool in payload["result"]["tools"]}


@dataclass
class _FakeResolver:
    by_connection: dict[tuple[str, str], ConnectionCtx]
    legacy_tokens: dict[str, ConnectionCtx]

    def resolve(self, connection_id: str, bearer_token: str) -> ConnectionCtx | None:
        return self.by_connection.get((connection_id, bearer_token))

    def resolve_legacy(self, bearer_token: str) -> ConnectionCtx | None:
        return self.legacy_tokens.get(bearer_token)

    def execution_context(self, ctx: ConnectionCtx) -> ConnectionContext:
        return ConnectionContext(
            connection=ConnectionRecord(
                connection_id=ctx.connection_id,
                tenant_id=ctx.tenant_id,
                connector_key=ctx.connector_key,
                display_name="test connection",
                status="active",
                data_mode=ctx.data_mode,
                public_config=dict(ctx.public_config),
                config_version=ctx.config_version,
            ),
            credentials={},
        )


class _FakeConnector:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.spec_calls = 0

    def spec(self) -> ConnectorSpec:
        self.spec_calls += 1
        return ConnectorSpec(
            connector_key="fake",
            tools=(
                ToolSpec(
                    tool_key="reports.list",
                    mcp_name="wecom_list_reports",
                    description="List reports.",
                    input_schema={
                        "type": "object",
                        "required": ["starttime", "endtime"],
                        "properties": {
                            "starttime": {"type": "integer"},
                            "endtime": {"type": "integer"},
                        },
                        "additionalProperties": False,
                    },
                    output_schema={"type": "object"},
                    operation_kind="read",
                    default_timeout_ms=1_000,
                    cache_ttl_seconds=None,
                ),
                ToolSpec(
                    tool_key="reports.hidden",
                    mcp_name="wecom_hidden_report",
                    description="Must not be exposed.",
                    input_schema={"type": "object"},
                    output_schema={"type": "object"},
                    operation_kind="read",
                    default_timeout_ms=1_000,
                    cache_ttl_seconds=None,
                ),
            ),
        )

    async def execute(
        self,
        context: ConnectionContext,
        tool_key: str,
        args: dict[str, Any],
    ) -> ExecutionResult:
        self.calls.append((tool_key, dict(args)))
        return ExecutionResult.ok({"tenant": context.tenant_id, "records": []})

    async def sync(self, context: ConnectionContext, resource_key: str):  # pragma: no cover
        raise NotImplementedError


def _connection(connection_id: str, tenant_id: str = "tenant-a") -> ConnectionCtx:
    return ConnectionCtx(
        tenant_id=tenant_id,
        connection_id=connection_id,
        connector_key="fake",
        data_mode="stored",
        public_config={"schema_name": "test_schema"},
        config_version=1,
    )


def _client(monkeypatch):
    active_connection = _connection("conn-a")
    resolver = _FakeResolver(
        by_connection={("conn-a", "token-a"): active_connection},
        legacy_tokens={"legacy-token": active_connection},
    )
    connector = _FakeConnector()
    runtime = ConnectorRuntime(
        ConnectorRegistry([connector]),
        policy_store={
            ("conn-a", "reports.hidden"): ToolPolicy(
                connection_id="conn-a",
                tool_name="reports.hidden",
                enabled=False,
                policy={},
            )
        },
    )
    gateway = ConnectionMcpGateway(resolver=resolver, runtime=runtime)
    app = main.create_app(gateway=gateway)
    app.router.lifespan_context = lambda _app: gateway.run()
    return TestClient(app), active_connection, connector


def _mcp_post(client: TestClient, path: str, token: str, payload: dict[str, Any]):
    return client.post(path, headers=bearer(token), json=payload)


def test_connection_endpoint_lists_only_enabled_tools(monkeypatch):
    client, active_connection, _connector = _client(monkeypatch)

    with client:
        response = _mcp_post(
            client,
            f"/mcp/{active_connection.connection_id}",
            "token-a",
            TOOLS_LIST,
        )

    assert response.status_code == 200
    assert tool_names(response.json()) == {"wecom_list_reports"}


def test_wrong_path_and_valid_token_is_not_authorized(monkeypatch):
    client, _active_connection, connector = _client(monkeypatch)
    connector.spec_calls = 0

    with client:
        response = client.post("/mcp/conn-b", headers=bearer("token-a"), json=TOOLS_LIST)

    assert response.status_code == 401
    assert connector.spec_calls == 0
    assert connector.calls == []


def test_legacy_mcp_path_resolves_default_wecom_connection(monkeypatch):
    client, _active_connection, _connector = _client(monkeypatch)

    with client:
        response = _mcp_post(client, "/mcp", "legacy-token", TOOLS_LIST)

    assert response.status_code == 200
    assert tool_names(response.json()) == {"wecom_list_reports"}


def test_low_level_server_validates_tool_input_before_connector_execution(monkeypatch):
    client, active_connection, connector = _client(monkeypatch)
    invalid_call = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "wecom_list_reports",
            "arguments": {"starttime": "not-an-integer", "endtime": 2},
        },
    }

    with client:
        response = _mcp_post(
            client,
            f"/mcp/{active_connection.connection_id}",
            "token-a",
            invalid_call,
        )

    assert response.status_code == 200
    assert response.json()["result"]["isError"] is True
    assert connector.calls == []


def test_cache_invalidation_retires_only_the_exact_connection_version():
    active_connection = _connection("conn-a")
    other_connection = _connection("conn-b", tenant_id="tenant-b")
    resolver = _FakeResolver(
        by_connection={},
        legacy_tokens={},
    )
    connector = _FakeConnector()
    gateway = ConnectionMcpGateway(
        resolver=resolver,
        runtime=ConnectorRuntime(ConnectorRegistry([connector])),
    )

    async def exercise() -> None:
        async with gateway.run():
            await gateway._manager_for(active_connection)
            await gateway._manager_for(other_connection)

            assert await gateway.invalidate_connection("conn-a", 1) is True
            assert set(gateway.cached_session_keys) == {("conn-b", 1)}
            assert await gateway.invalidate_connection("conn-a", 1) is False

    asyncio.run(exercise())


def test_resolver_never_falls_back_from_wrong_path_and_maps_legacy_token_to_default():
    resolved_id = default_wecom_connection_id("tenant-a")
    record = ConnectionRecord(
        connection_id=resolved_id,
        tenant_id="tenant-a",
        connector_key="wecom",
        display_name="legacy",
        status="active",
        data_mode="stored",
        public_config={"schema_name": "tenant_a"},
        config_version=7,
    )
    calls = []

    def token_resolver(token, connection_id):
        calls.append((token, connection_id))
        return record if (token, connection_id) == ("legacy-token", resolved_id) else None

    resolver = ConnectionResolver(
        token_resolver=token_resolver,
        legacy_tenant_lookup=lambda token: SimpleNamespace(tenant_id="tenant-a"),
        legacy_tenant_reload=lambda: None,
        credential_loader=lambda _connection_id: {},
    )

    assert resolver.resolve("conn-b", "legacy-token") is None
    legacy = resolver.resolve_legacy("legacy-token")

    assert legacy is not None
    assert legacy.connection_id == resolved_id
    assert calls == [("legacy-token", "conn-b"), ("legacy-token", resolved_id)]
