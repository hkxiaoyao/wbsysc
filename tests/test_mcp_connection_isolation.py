from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from fastapi.testclient import TestClient

from app import main
from app import mcp_gateway
from app.auth import ConnectionCtx
from app.connections.cache import ConnectionCache
from app.connections.models import ConnectionRecord, ToolPolicy
from app.connectors import (
    ConnectionContext,
    ConnectorRegistry,
    ConnectorRuntime,
    ConnectorSpec,
    ExecutionResult,
    ToolSpec,
)
from app.mcp_gateway import ConnectionMcpGateway
from app.mcp_log_models import LogFilters
from app import mcp_log_store


TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}


def _bearer(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }


def _ctx(connection_id: str, tenant_id: str) -> ConnectionCtx:
    return ConnectionCtx(
        tenant_id=tenant_id,
        connection_id=connection_id,
        connector_key="isolated",
        data_mode="stored",
        public_config={"schema_name": f"schema_{connection_id}"},
        config_version=1,
    )


@dataclass
class _Resolver:
    contexts: dict[tuple[str, str], ConnectionCtx]
    credential_reads: list[str] = field(default_factory=list)

    def resolve(self, connection_id: str, bearer_token: str) -> ConnectionCtx | None:
        return self.contexts.get((connection_id, bearer_token))

    def resolve_legacy(self, bearer_token: str) -> None:
        return None

    def execution_context(self, ctx: ConnectionCtx) -> ConnectionContext:
        self.credential_reads.append(ctx.connection_id)
        return ConnectionContext(
            connection=ConnectionRecord(
                connection_id=ctx.connection_id,
                tenant_id=ctx.tenant_id,
                connector_key=ctx.connector_key,
                display_name=ctx.connection_id,
                status="active",
                data_mode=ctx.data_mode,
                public_config=dict(ctx.public_config),
                config_version=ctx.config_version,
            ),
            credentials={"account_marker": f"credential_{ctx.connection_id}"},
        )


class _Connector:
    def spec(self) -> ConnectorSpec:
        return ConnectorSpec(
            connector_key="isolated",
            tools=(
                ToolSpec(
                    tool_key="records.list",
                    mcp_name="isolated_list_records",
                    description="List isolated records.",
                    input_schema={"type": "object"},
                    output_schema={"type": "object"},
                    operation_kind="read",
                    default_timeout_ms=1_000,
                    cache_ttl_seconds=None,
                ),
            ),
        )

    async def execute(self, context, tool_key, args) -> ExecutionResult:
        return ExecutionResult.ok({"connection_id": context.connection_id})

    async def sync(self, context, resource_key):  # pragma: no cover
        raise NotImplementedError


def _client() -> tuple[TestClient, _Resolver]:
    resolver = _Resolver(
        {
            ("connection_a", "token_a_value"): _ctx("connection_a", "tenant_one"),
            ("connection_b", "token_b_value"): _ctx("connection_b", "tenant_one"),
            ("connection_c", "token_c_value"): _ctx("connection_c", "tenant_two"),
        }
    )
    policies = {
        ("connection_a", "records.list"): ToolPolicy(
            "connection_a", "records.list", False, {}
        )
    }
    gateway = ConnectionMcpGateway(
        resolver=resolver,
        runtime=ConnectorRuntime(
            ConnectorRegistry([_Connector()]),
            policy_store=policies,
        ),
    )
    app = main.create_app(gateway=gateway)
    app.router.lifespan_context = lambda _app: gateway.run()
    return TestClient(app), resolver


def _tool_names(response) -> set[str]:
    return {tool["name"] for tool in response.json()["result"]["tools"]}


def test_connection_a_token_cannot_reach_b_endpoint_or_credentials():
    client, resolver = _client()

    with client:
        denied = client.post(
            "/mcp/connection_b",
            headers=_bearer("token_a_value"),
            json=TOOLS_LIST,
        )

    assert denied.status_code == 401
    assert resolver.credential_reads == []


def test_tool_policy_is_scoped_to_one_connection():
    client, _resolver = _client()

    with client:
        connection_a = client.post(
            "/mcp/connection_a",
            headers=_bearer("token_a_value"),
            json=TOOLS_LIST,
        )
        connection_b = client.post(
            "/mcp/connection_b",
            headers=_bearer("token_b_value"),
            json=TOOLS_LIST,
        )

    assert _tool_names(connection_a) == set()
    assert _tool_names(connection_b) == {"isolated_list_records"}


def test_connection_cache_never_returns_or_invalidates_another_connection_value():
    cache = ConnectionCache(clock=lambda: 10.0)

    async def exercise() -> None:
        assert await cache.put(
            "connection_a", "records.list", {"owner": "connection_a"}, ttl_seconds=60
        )
        assert await cache.put(
            "connection_b", "records.list", {"owner": "connection_b"}, ttl_seconds=60
        )
        assert await cache.get("connection_a", "records.list") == {
            "owner": "connection_a"
        }
        assert await cache.get("connection_b", "records.list") == {
            "owner": "connection_b"
        }
        assert await cache.invalidate_connection("connection_a") == 1
        assert await cache.get("connection_a", "records.list") is None
        assert await cache.get("connection_b", "records.list") == {
            "owner": "connection_b"
        }

    asyncio.run(exercise())


class _Rows:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def all(self) -> list[dict[str, Any]]:
        return self._rows


class _Result:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def scalar(self) -> int:
        return len(self._rows)

    def mappings(self) -> _Rows:
        return _Rows(self._rows)


class _LogConnection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.statements: list[tuple[str, dict[str, Any]]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, params=None) -> _Result:
        bound = dict(params or {})
        self.statements.append((str(statement), bound))
        return _Result(self.rows)


class _LogEngine:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._connection = _LogConnection(rows)

    def connect(self) -> _LogConnection:
        return self._connection


def test_credential_loader_binds_only_the_resolved_connection_id(monkeypatch):
    engine = _LogEngine(
        [{"credential_key": "account_marker", "encrypted_value": b"ciphertext"}]
    )
    monkeypatch.setattr(mcp_gateway.connection_store, "_engine", lambda: engine)
    monkeypatch.setattr(
        mcp_gateway,
        "decrypt_credential",
        lambda value: "credential_a" if value == b"ciphertext" else "wrong",
    )

    credentials = mcp_gateway._load_connection_credentials("connection_a")

    assert credentials == {"account_marker": "credential_a"}
    assert len(engine._connection.statements) == 1
    sql, params = engine._connection.statements[0]
    assert "WHERE connection_id=:connection_id" in sql
    assert params == {"connection_id": "connection_a"}


def test_log_query_binds_tenant_and_connection_as_one_sql_boundary(monkeypatch):
    rows = [
        {"id": 1, "tenant_id": "tenant_one", "connection_id": "connection_a"},
    ]
    engine = _LogEngine(rows)
    monkeypatch.setattr(mcp_log_store, "_engine", lambda: engine)

    result = mcp_log_store.list_logs(
        LogFilters(tenant_id="tenant_one", connection_id="connection_a"),
        page=1,
        page_size=20,
    )

    assert result["total"] == 1
    assert [(item["tenant_id"], item["connection_id"]) for item in result["items"]] == [
        ("tenant_one", "connection_a")
    ]
    assert len(engine._connection.statements) == 2
    for sql, params in engine._connection.statements:
        assert "tenant_id = :tenant_id" in sql
        assert "connection_id = :connection_id" in sql
        assert params["tenant_id"] == "tenant_one"
        assert params["connection_id"] == "connection_a"
