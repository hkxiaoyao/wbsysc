from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi.testclient import TestClient

from app import main
from app.auth import ConnectionCtx
from app.connections.models import ConnectionRecord
from app.connectors import (
    ConnectionContext,
    ConnectorRegistry,
    ConnectorRuntime,
    ConnectorSpec,
    ExecutionResult,
    ToolSpec,
)
from app.mcp_gateway import ConnectionMcpGateway


TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}


def _bearer(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }


def _tool_contracts(response) -> list[dict[str, Any]]:
    return response.json()["result"]["tools"]


def _ctx() -> ConnectionCtx:
    return ConnectionCtx(
        tenant_id="migration_tenant",
        connection_id="migration_wecom",
        connector_key="wecom",
        data_mode="stored",
        public_config={"schema_name": "wbd_migration_test"},
        config_version=1,
    )


@dataclass
class _MutableResolver:
    current_tokens: set[str]

    def resolve(self, connection_id: str, bearer_token: str) -> ConnectionCtx | None:
        current = _ctx()
        if connection_id == current.connection_id and bearer_token in self.current_tokens:
            return current
        return None

    def resolve_legacy(self, bearer_token: str) -> ConnectionCtx | None:
        return _ctx() if bearer_token in self.current_tokens else None

    def execution_context(self, ctx: ConnectionCtx) -> ConnectionContext:
        return ConnectionContext(
            connection=ConnectionRecord(
                connection_id=ctx.connection_id,
                tenant_id=ctx.tenant_id,
                connector_key=ctx.connector_key,
                display_name="Migration WeCom",
                status="active",
                data_mode=ctx.data_mode,
                public_config=dict(ctx.public_config),
                config_version=ctx.config_version,
            ),
            credentials={},
        )

    def rotate(self, old_token: str, new_token: str) -> None:
        self.current_tokens.discard(old_token)
        self.current_tokens.add(new_token)

    def revoke(self, token: str) -> None:
        self.current_tokens.discard(token)


class _WeComParityConnector:
    def spec(self) -> ConnectorSpec:
        tools = tuple(
            ToolSpec(
                tool_key=tool_key,
                mcp_name=mcp_name,
                description=description,
                input_schema={"type": "object", "additionalProperties": False},
                output_schema={"type": "object"},
                operation_kind="read",
                default_timeout_ms=1_000,
                cache_ttl_seconds=None,
            )
            for tool_key, mcp_name, description in (
                ("reports.list", "wecom_list_reports", "List WeCom reports."),
                ("approvals.list", "wecom_list_approvals", "List WeCom approvals."),
                ("checkins.list", "wecom_list_checkins", "List WeCom check-ins."),
            )
        )
        return ConnectorSpec(connector_key="wecom", tools=tools)

    async def execute(self, context, tool_key, args) -> ExecutionResult:
        return ExecutionResult.ok({"tenant": context.tenant_id, "items": []})

    async def sync(self, context, resource_key):  # pragma: no cover
        raise NotImplementedError


def _client() -> tuple[TestClient, _MutableResolver]:
    resolver = _MutableResolver({"legacy_token_value"})
    runtime = ConnectorRuntime(
        ConnectorRegistry([_WeComParityConnector()]),
    )
    gateway = ConnectionMcpGateway(resolver=resolver, runtime=runtime)
    app = main.create_app(gateway=gateway)
    app.router.lifespan_context = lambda _app: gateway.run()
    return TestClient(app), resolver


def test_legacy_and_connection_wecom_endpoints_have_identical_tool_contracts():
    client, _resolver = _client()

    with client:
        legacy = client.post("/mcp", headers=_bearer("legacy_token_value"), json=TOOLS_LIST)
        modern = client.post(
            "/mcp/migration_wecom",
            headers=_bearer("legacy_token_value"),
            json=TOOLS_LIST,
        )

    assert legacy.status_code == modern.status_code == 200
    assert _tool_contracts(legacy) == _tool_contracts(modern)


def test_connection_endpoint_rejects_missing_invalid_and_wrong_connection_tokens():
    client, _resolver = _client()

    with client:
        missing = client.post("/mcp/migration_wecom", json=TOOLS_LIST)
        invalid = client.post(
            "/mcp/migration_wecom",
            headers=_bearer("invalid_token_value"),
            json=TOOLS_LIST,
        )
        wrong_connection = client.post(
            "/mcp/other_connection",
            headers=_bearer("legacy_token_value"),
            json=TOOLS_LIST,
        )

    assert (missing.status_code, invalid.status_code, wrong_connection.status_code) == (
        401,
        401,
        401,
    )


def test_rotated_and_revoked_tokens_take_effect_on_both_routes():
    client, resolver = _client()

    with client:
        resolver.rotate("legacy_token_value", "rotated_token_value")
        assert client.post(
            "/mcp/migration_wecom",
            headers=_bearer("legacy_token_value"),
            json=TOOLS_LIST,
        ).status_code == 401
        assert client.post(
            "/mcp/migration_wecom",
            headers=_bearer("rotated_token_value"),
            json=TOOLS_LIST,
        ).status_code == 200
        assert client.post(
            "/mcp",
            headers=_bearer("rotated_token_value"),
            json=TOOLS_LIST,
        ).status_code == 200

        resolver.revoke("rotated_token_value")
        assert client.post(
            "/mcp/migration_wecom",
            headers=_bearer("rotated_token_value"),
            json=TOOLS_LIST,
        ).status_code == 401
        assert client.post(
            "/mcp",
            headers=_bearer("rotated_token_value"),
            json=TOOLS_LIST,
        ).status_code == 401
