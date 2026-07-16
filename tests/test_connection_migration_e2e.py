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
from app.mcp_gateway import (
    ConnectionMcpGateway,
    ConnectionResolver,
    default_wecom_connection_id,
)


TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
TOOLS_CALL = {
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {"name": "wecom_list_reports", "arguments": {}},
}
CONNECTION_ID = default_wecom_connection_id("migration_tenant")


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
        connection_id=CONNECTION_ID,
        connector_key="wecom",
        data_mode="stored",
        public_config={"schema_name": "wbd_migration_test"},
        config_version=1,
    )


@dataclass
class _TokenDirectory:
    """Component-test storage boundary; production SQL/HMAC has separate store tests."""

    current_tokens: set[str]

    def record(self) -> ConnectionRecord:
        current = _ctx()
        return ConnectionRecord(
            connection_id=current.connection_id,
            tenant_id=current.tenant_id,
            connector_key=current.connector_key,
            display_name="Migration WeCom",
            status="active",
            data_mode=current.data_mode,
            public_config=dict(current.public_config),
            config_version=current.config_version,
        )

    def resolve_token(
        self, bearer_token: str, connection_id: str
    ) -> ConnectionRecord | None:
        if bearer_token not in self.current_tokens:
            return None
        record = self.record()
        return record if record.connection_id == connection_id else None

    def legacy_tenant(self, bearer_token: str):
        if bearer_token not in self.current_tokens:
            return None
        return type("Tenant", (), {"tenant_id": _ctx().tenant_id})()

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


def _client() -> tuple[TestClient, _TokenDirectory]:
    tokens = _TokenDirectory({"legacy_token_value"})
    resolver = ConnectionResolver(
        token_resolver=tokens.resolve_token,
        legacy_tenant_lookup=tokens.legacy_tenant,
        legacy_tenant_reload=lambda: None,
        credential_loader=lambda _connection_id: {},
    )
    runtime = ConnectorRuntime(
        ConnectorRegistry([_WeComParityConnector()]),
    )
    gateway = ConnectionMcpGateway(resolver=resolver, runtime=runtime)
    app = main.create_app(gateway=gateway)
    app.router.lifespan_context = lambda _app: gateway.run()
    return TestClient(app), tokens


def test_component_legacy_and_connection_routes_have_identical_tools_and_results():
    client, _resolver = _client()

    with client:
        legacy = client.post("/mcp", headers=_bearer("legacy_token_value"), json=TOOLS_LIST)
        modern = client.post(
            f"/mcp/{CONNECTION_ID}",
            headers=_bearer("legacy_token_value"),
            json=TOOLS_LIST,
        )
        legacy_call = client.post(
            "/mcp", headers=_bearer("legacy_token_value"), json=TOOLS_CALL
        )
        modern_call = client.post(
            f"/mcp/{CONNECTION_ID}",
            headers=_bearer("legacy_token_value"),
            json=TOOLS_CALL,
        )

    assert legacy.status_code == modern.status_code == 200
    assert _tool_contracts(legacy) == _tool_contracts(modern)
    assert legacy_call.status_code == modern_call.status_code == 200
    assert legacy_call.json()["result"] == modern_call.json()["result"]


def test_connection_endpoint_rejects_missing_invalid_and_wrong_connection_tokens():
    client, _resolver = _client()

    with client:
        missing = client.post(f"/mcp/{CONNECTION_ID}", json=TOOLS_LIST)
        invalid = client.post(
            f"/mcp/{CONNECTION_ID}",
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


def test_component_real_resolver_observes_rotated_and_revoked_tokens_on_both_routes():
    client, tokens = _client()

    with client:
        tokens.current_tokens.remove("legacy_token_value")
        tokens.current_tokens.add("rotated_token_value")
        assert client.post(
            f"/mcp/{CONNECTION_ID}",
            headers=_bearer("legacy_token_value"),
            json=TOOLS_LIST,
        ).status_code == 401
        assert client.post(
            f"/mcp/{CONNECTION_ID}",
            headers=_bearer("rotated_token_value"),
            json=TOOLS_LIST,
        ).status_code == 200
        assert client.post(
            "/mcp",
            headers=_bearer("rotated_token_value"),
            json=TOOLS_LIST,
        ).status_code == 200

        tokens.current_tokens.remove("rotated_token_value")
        assert client.post(
            f"/mcp/{CONNECTION_ID}",
            headers=_bearer("rotated_token_value"),
            json=TOOLS_LIST,
        ).status_code == 401
        assert client.post(
            "/mcp",
            headers=_bearer("rotated_token_value"),
            json=TOOLS_LIST,
        ).status_code == 401
