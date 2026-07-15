from dataclasses import FrozenInstanceError

import pytest

from app.connections.models import ConnectionRecord, ToolPolicy
from app.connectors.contracts import (
    ConnectionContext,
    ConnectorSpec,
    ExecutionResult,
    SyncResult,
    ToolSpec,
    ConnectionUnavailableError,
    ToolDisabledError,
    WritePolicyError,
)
from app.connectors.registry import ConnectorRegistry
from app.connectors.runtime import (
    ConnectorRuntime,
    ExecutionPlanner,
    InvalidToolPolicyError,
    RateLimitError,
    UnsupportedDataModeError,
)


READ_TOOL = ToolSpec(
    tool_key="reports.list",
    mcp_name="wecom_list_reports",
    description="List reports.",
    input_schema={"type": "object"},
    output_schema={"type": "object"},
    operation_kind="read",
    default_timeout_ms=125,
    cache_ttl_seconds=60,
)
WRITE_TOOL = ToolSpec(
    tool_key="reports.delete",
    mcp_name="wecom_delete_report",
    description="Delete a report.",
    input_schema={"type": "object"},
    output_schema={"type": "object"},
    operation_kind="write",
    default_timeout_ms=125,
    cache_ttl_seconds=None,
)


class FakePolicyStore:
    def __init__(self, policies=()):
        self._policies = {
            (policy.connection_id, policy.tool_name): policy for policy in policies
        }

    def get(self, connection_id, tool_key):
        return self._policies.get((connection_id, tool_key))


class FakeConnector:
    def __init__(
        self,
        connector_key="wecom",
        tools=(READ_TOOL, WRITE_TOOL),
        supports_data_modes=("direct", "stored", "hybrid"),
    ):
        self.connector_key = connector_key
        self._spec = ConnectorSpec(
            connector_key=connector_key,
            tools=tools,
            supports_data_modes=supports_data_modes,
        )
        self.calls = []

    def spec(self):
        return self._spec

    async def execute(self, context, tool_key, args):
        self.calls.append((context.connection.data_mode, tool_key, dict(args)))
        return ExecutionResult.ok({"source": "connector"})

    async def sync(self, context, resource_key):
        return SyncResult.ok(context.connection.connection_id, resource_key)


def connection(data_mode="direct", status="active"):
    return ConnectionRecord(
        connection_id="conn-a",
        tenant_id="tenant-a",
        connector_key="wecom",
        display_name="WeCom",
        status=status,
        data_mode=data_mode,
        public_config={"corpid": "ww123"},
        config_version=1,
    )


def context(data_mode="direct", credentials=None, status="active"):
    return ConnectionContext(
        connection=connection(data_mode, status),
        credentials=credentials or {},
        request_metadata={"request_id": "request-a"},
    )


def registry_with(connector):
    registry = ConnectorRegistry()
    registry.register(connector)
    return registry


def test_registry_rejects_duplicate_connector_keys():
    registry = ConnectorRegistry()

    registry.register(FakeConnector("wecom"))

    with pytest.raises(ValueError, match="duplicate connector_key"):
        registry.register(FakeConnector("wecom"))


def test_contracts_are_immutable_and_resolve_tool_keys_and_mcp_names():
    spec = ConnectorSpec(connector_key="wecom", tools=(READ_TOOL, WRITE_TOOL))

    assert spec.tool("reports.list") is READ_TOOL
    assert spec.tool("wecom_delete_report") is WRITE_TOOL
    assert ExecutionResult.ok({"ok": True}).status == "ok"
    assert SyncResult.ok("conn-a", "reports").connection_id == "conn-a"
    with pytest.raises(FrozenInstanceError):
        READ_TOOL.tool_key = "changed"

    with pytest.raises(ValueError, match="duplicate tool"):
        ConnectorSpec(connector_key="wecom", tools=(READ_TOOL, READ_TOOL))


@pytest.mark.asyncio
async def test_runtime_rejects_disabled_or_write_prohibited_tools():
    connector = FakeConnector()
    policies = FakePolicyStore(
        (
            ToolPolicy("conn-a", "reports.list", enabled=False, policy={}),
            ToolPolicy("conn-a", "reports.delete", enabled=True, policy={}),
        )
    )
    runtime = ConnectorRuntime(registry_with(connector), policy_store=policies)
    ctx = context()

    with pytest.raises(ToolDisabledError):
        await runtime.execute(ctx, "reports.list", {})
    with pytest.raises(WritePolicyError):
        await runtime.execute(ctx, "reports.delete", {"id": "1"})

    assert connector.calls == []


@pytest.mark.asyncio
async def test_runtime_allows_a_write_only_with_an_explicit_connection_policy():
    connector = FakeConnector()
    policies = FakePolicyStore(
        (
            ToolPolicy(
                "conn-a",
                "reports.delete",
                enabled=True,
                policy={"allow_write": True},
            ),
        )
    )
    runtime = ConnectorRuntime(registry_with(connector), policy_store=policies)

    result = await runtime.execute(context(), "reports.delete", {"id": "1"})

    assert result == ExecutionResult.ok({"source": "connector"})
    assert connector.calls == [("direct", "reports.delete", {"id": "1"})]


@pytest.mark.asyncio
async def test_runtime_fails_closed_for_a_non_active_connection():
    connector = FakeConnector()
    runtime = ConnectorRuntime(registry_with(connector))
    unavailable_context = context(status="disabled")

    assert runtime.list_enabled_tools(unavailable_context) == ()
    with pytest.raises(ConnectionUnavailableError):
        await runtime.execute(unavailable_context, "reports.list", {})

    assert connector.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("data_mode", ("direct", "stored", "hybrid"))
async def test_runtime_selects_the_executor_for_the_connection_data_mode(data_mode):
    calls = []

    def executor_for(mode):
        async def execute(context, connector, tool, args):
            calls.append((mode, tool.tool_key, dict(args)))
            return ExecutionResult.ok({"mode": mode})

        return execute

    planner = ExecutionPlanner(
        direct_executor=executor_for("direct"),
        stored_executor=executor_for("stored"),
        hybrid_executor=executor_for("hybrid"),
    )
    runtime = ConnectorRuntime(registry_with(FakeConnector()), planner=planner)

    result = await runtime.execute(context(data_mode), "reports.list", {"limit": 5})

    assert result.data == {"mode": data_mode}
    assert calls == [(data_mode, "reports.list", {"limit": 5})]


@pytest.mark.asyncio
async def test_runtime_rejects_a_data_mode_not_declared_by_the_connector():
    connector = FakeConnector(supports_data_modes=("direct",))
    runtime = ConnectorRuntime(registry_with(connector))

    with pytest.raises(UnsupportedDataModeError):
        await runtime.execute(context("stored"), "reports.list", {})

    assert connector.calls == []


@pytest.mark.asyncio
async def test_runtime_normalizes_policy_timeouts_before_waiting(monkeypatch):
    from app.connectors import runtime as runtime_module

    observed_timeouts = []

    async def capture_wait_for(awaitable, timeout):
        observed_timeouts.append(timeout)
        return await awaitable

    monkeypatch.setattr(runtime_module.asyncio, "wait_for", capture_wait_for)
    connector = FakeConnector()
    policies = FakePolicyStore(
        (
            ToolPolicy(
                "conn-a", "reports.list", enabled=True, policy={"timeout_ms": -1}
            ),
        )
    )
    runtime = ConnectorRuntime(registry_with(connector), policy_store=policies)

    await runtime.execute(context(), "reports.list", {})

    assert observed_timeouts == [0.125]


@pytest.mark.asyncio
async def test_runtime_applies_a_per_connection_tool_rate_limit():
    connector = FakeConnector()
    policies = FakePolicyStore(
        (
            ToolPolicy(
                "conn-a",
                "reports.list",
                enabled=True,
                policy={"rate_limit": {"limit": 1, "window_seconds": 60}},
            ),
        )
    )
    runtime = ConnectorRuntime(
        registry_with(connector), policy_store=policies, clock=lambda: 100.0
    )
    ctx = context()

    await runtime.execute(ctx, "reports.list", {})

    with pytest.raises(RateLimitError):
        await runtime.execute(ctx, "reports.list", {})

    assert len(connector.calls) == 1


@pytest.mark.asyncio
async def test_runtime_fails_closed_for_an_invalid_rate_limit_policy():
    connector = FakeConnector()
    policies = FakePolicyStore(
        (
            ToolPolicy(
                "conn-a",
                "reports.list",
                enabled=True,
                policy={"rate_limit": {"limit": 0, "window_seconds": 60}},
            ),
        )
    )
    runtime = ConnectorRuntime(registry_with(connector), policy_store=policies)

    with pytest.raises(InvalidToolPolicyError):
        await runtime.execute(context(), "reports.list", {})

    assert connector.calls == []


def test_runtime_lists_only_tools_allowed_for_the_connection():
    connector = FakeConnector()
    policies = FakePolicyStore(
        (
            ToolPolicy("conn-a", "reports.list", enabled=False, policy={}),
            ToolPolicy("conn-a", "reports.delete", enabled=True, policy={}),
        )
    )
    runtime = ConnectorRuntime(registry_with(connector), policy_store=policies)

    assert runtime.list_enabled_tools(context()) == ()


@pytest.mark.asyncio
async def test_runtime_audit_handoff_omits_credentials_bodies_and_exception_text():
    class OpaqueFailure(RuntimeError):
        def __str__(self):
            raise AssertionError("runtime must not stringify connector exceptions")

    class FailingConnector(FakeConnector):
        async def execute(self, context, tool_key, args):
            raise OpaqueFailure()

    events = []
    runtime = ConnectorRuntime(registry_with(FailingConnector()), audit_sink=events.append)
    ctx = context(
        credentials={"Authorization": "Bearer secret-value", "Cookie": "session=abc"}
    )

    with pytest.raises(OpaqueFailure):
        await runtime.execute(
            ctx,
            "reports.list",
            {"body": {"secret": "raw-request-body"}},
        )

    assert len(events) == 1
    event = events[0]
    assert event.tenant_id == "tenant-a"
    assert event.connection_id == "conn-a"
    assert event.connector_key == "wecom"
    assert event.tool_key == "reports.list"
    assert event.status == "error"
    assert event.error_code == "OpaqueFailure"
    assert event.error_summary == "connector execution failed"
    assert event.args_summary == "omitted"
    assert event.result_summary == "omitted"
    assert "secret-value" not in repr(event)
    assert "raw-request-body" not in repr(event)
