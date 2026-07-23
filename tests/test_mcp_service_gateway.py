from __future__ import annotations

import asyncio
from dataclasses import dataclass
import threading

import pytest
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Mount

from app.auth import ConnectionCtx
from app import db, main
from app.connections import store as connection_store
from app.connections.models import ConnectionRecord, ToolPolicy
from app.connectors import (
    ConnectionContext,
    ConnectorRegistry,
    ConnectorRuntime,
    ConnectorSpec,
    ExecutionResult,
    RateLimitError,
    ToolDisabledError,
    ToolSpec,
)
from app.mcp_service_gateway import ServiceContext, ServiceMcpGateway, ServiceResolver
from app.mcp_services.models import McpService, ServiceToolBinding
from app.mcp_services import store as service_store


def _tool(key: str, name: str) -> ToolSpec:
    return ToolSpec(
        tool_key=key,
        mcp_name=name,
        description=f"Execute {key}",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        operation_kind="read",
        default_timeout_ms=1_000,
        cache_ttl_seconds=None,
    )


class _Connector:
    def __init__(self, connector_key: str, *tools: ToolSpec) -> None:
        self.connector_key = connector_key
        self.tools = tools
        self.calls: list[tuple[str, str, dict]] = []

    def spec(self) -> ConnectorSpec:
        return ConnectorSpec(connector_key=self.connector_key, tools=self.tools)

    async def execute(self, context, tool_key, args) -> ExecutionResult:
        self.calls.append((context.connection_id, tool_key, args))
        return ExecutionResult.ok({"connection_id": context.connection_id})

    async def sync(self, context, resource_key):  # pragma: no cover
        raise NotImplementedError


def _record(connection_id: str, connector_key: str, tenant_id: str = "tenant-a"):
    return ConnectionRecord(
        connection_id=connection_id,
        tenant_id=tenant_id,
        connector_key=connector_key,
        display_name=connection_id,
        status="active",
        data_mode="direct",
        public_config={},
        config_version=1,
    )


def _binding(
    binding_id: str,
    connection_id: str,
    source_tool_key: str,
    alias: str,
    *,
    status: str = "active",
    policy: dict | None = None,
) -> ServiceToolBinding:
    return ServiceToolBinding(
        binding_id=binding_id,
        service_id="service-a",
        connection_id=connection_id,
        source_tool_key=source_tool_key,
        tool_alias=alias,
        binding_status=status,
        policy=policy or {},
    )


@dataclass
class _Fixture:
    gateway: ServiceMcpGateway
    wecom: _Connector
    erp: _Connector


def _gateway(
    *,
    bindings: list[ServiceToolBinding] | None = None,
    policies: dict[tuple[str, str], ToolPolicy] | None = None,
    connections: dict[str, ConnectionRecord] | None = None,
    resolver: ServiceResolver | None = None,
    audit_writer=lambda event: None,
    clock=lambda: 100.0,
) -> _Fixture:
    wecom = _Connector("wecom", _tool("users.get", "wecom_get_users"))
    erp = _Connector("erp", _tool("orders.get", "erp_get_orders"))
    runtime = ConnectorRuntime(
        ConnectorRegistry([wecom, erp]),
        policy_store=policies,
        clock=clock,
    )
    records = connections or {
        "conn-a": _record("conn-a", "wecom"),
        "conn-b": _record("conn-b", "erp"),
    }

    def build_context(ctx: ConnectionCtx) -> ConnectionContext:
        return ConnectionContext(connection=records[ctx.connection_id], credentials={})

    gateway = ServiceMcpGateway(
        resolver=resolver,
        runtime=runtime,
        binding_loader=lambda service_id, tenant_id: bindings
        or [
            _binding("binding-a", "conn-a", "users.get", "hq_wecom__get_users"),
            _binding("binding-b", "conn-b", "orders.get", "erp__get_orders"),
        ],
        connection_loader=lambda connection_id, tenant_id: records.get(connection_id),
        connection_context_builder=build_context,
        audit_writer=audit_writer,
        clock=clock,
    )
    return _Fixture(gateway=gateway, wecom=wecom, erp=erp)


def _service() -> ServiceContext:
    return ServiceContext("service-a", "tenant-a", 7)


class _InvalidationResult:
    def __init__(self, *, rows=(), scalar=None):
        self._rows = list(rows)
        self._scalar = scalar

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def scalar(self):
        return self._scalar


class _InvalidationConnection:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, statement, params=None):
        sql = " ".join(str(statement).split())
        if "SELECT config_version FROM connection_instance" in sql:
            return _InvalidationResult(scalar=7)
        if "SELECT tenant_id FROM connection_instance" in sql:
            return _InvalidationResult(scalar="tenant-a")
        if "FROM mcp_service_tool_binding" in sql:
            return _InvalidationResult(
                rows=[{"service_id": "service-a"}, {"service_id": "service-b"}]
            )
        if "FROM mcp_service WHERE tenant_id" in sql:
            return _InvalidationResult(
                rows=[
                    {"service_id": "service-a"},
                    {"service_id": "service-b"},
                    {"service_id": "service-c"},
                ]
            )
        return _InvalidationResult()


class _InvalidationEngine:
    def connect(self):
        return _InvalidationConnection()

    def begin(self):
        return _InvalidationConnection()


def test_connection_policy_change_invalidates_only_referencing_services(monkeypatch):
    monkeypatch.setattr(db, "get_engine", _InvalidationEngine)
    invalidated = set()
    service_store.register_service_cache_invalidator(invalidated.add)
    try:
        connection_store.set_tool_policy(
            "conn-a",
            "users.get",
            enabled=False,
            expected_config_version=7,
        )
    finally:
        service_store.unregister_service_cache_invalidator(invalidated.add)

    assert invalidated == {"service-a", "service-b"}


def test_service_cache_invalidation_falls_back_to_all_tenant_services(monkeypatch):
    monkeypatch.setattr(db, "get_engine", _InvalidationEngine)
    invalidated = []
    failed_once = False

    def invalidate(service_id):
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise RuntimeError("exact invalidation failed")
        invalidated.append(service_id)

    service_store.register_service_cache_invalidator(invalidate)
    try:
        service_store.invalidate_services_for_connection("conn-a")
    finally:
        service_store.unregister_service_cache_invalidator(invalidate)

    assert set(invalidated) == {"service-a", "service-b", "service-c"}


def test_service_cache_fail_safe_isolates_persistently_failing_invalidators(
    monkeypatch,
):
    monkeypatch.setattr(db, "get_engine", _InvalidationEngine)
    failed_calls = []
    healthy_calls = []

    def failing(service_id):
        failed_calls.append(service_id)
        raise RuntimeError("cache unavailable")

    service_store.register_service_cache_invalidator(failing)
    service_store.register_service_cache_invalidator(healthy_calls.append)
    try:
        service_store.invalidate_services_for_connection("conn-a")
    finally:
        service_store.unregister_service_cache_invalidator(failing)
        service_store.unregister_service_cache_invalidator(healthy_calls.append)

    assert set(healthy_calls) == {"service-a", "service-b", "service-c"}
    assert set(failed_calls) == {"service-a", "service-b", "service-c"}
    assert len(healthy_calls) == len(failed_calls) == 5


@pytest.mark.asyncio
async def test_service_cache_invalidator_uses_gateway_loop_and_manager_lock():
    loop = asyncio.get_running_loop()
    loop_thread_id = threading.get_ident()

    class TrackingEntries(dict):
        def __init__(self, *args):
            super().__init__(*args)
            self.mutation_threads = []

        def pop(self, key, default=None):
            self.mutation_threads.append(threading.get_ident())
            return super().pop(key, default)

    gateway = type("Gateway", (), {})()
    gateway._entries = TrackingEntries(
        {
            ("service-a", 1): object(),
            ("service-b", 1): object(),
        }
    )
    gateway._manager_lock = asyncio.Lock()
    callback = main._build_service_cache_invalidator(gateway, loop)

    await gateway._manager_lock.acquire()
    worker = asyncio.create_task(asyncio.to_thread(callback, "service-a"))
    await asyncio.sleep(0)
    gateway._entries[("service-a", 2)] = object()
    assert not worker.done()
    gateway._manager_lock.release()
    await worker

    assert set(gateway._entries) == {("service-b", 1)}
    assert gateway._entries.mutation_threads == [loop_thread_id, loop_thread_id]


@pytest.mark.asyncio
async def test_service_projects_aliases_from_multiple_connections():
    fixture = _gateway()

    listed = await fixture.gateway.list_tools(_service())

    assert [tool.name for tool in listed] == [
        "hq_wecom__get_users",
        "erp__get_orders",
    ]


@pytest.mark.asyncio
async def test_service_executes_stable_source_key_not_materialized_alias():
    fixture = _gateway()

    result = await fixture.gateway.call_tool(
        _service(), "hq_wecom__get_users", {"department": 7}
    )

    assert result == ExecutionResult.ok({"connection_id": "conn-a"})
    assert fixture.wecom.calls == [("conn-a", "users.get", {"department": 7})]


@pytest.mark.asyncio
async def test_service_binding_cannot_reenable_connection_disabled_tool():
    disabled = {
        ("conn-a", "users.get"): ToolPolicy("conn-a", "users.get", False, {})
    }
    fixture = _gateway(
        policies=disabled,
        bindings=[
            _binding(
                "binding-a",
                "conn-a",
                "users.get",
                "hq_wecom__get_users",
                policy={"allow_write": True},
            )
        ],
    )

    assert await fixture.gateway.list_tools(_service()) == []
    with pytest.raises(ToolDisabledError):
        await fixture.gateway.call_tool(_service(), "hq_wecom__get_users", {})
    assert fixture.wecom.calls == []


@pytest.mark.asyncio
async def test_service_rejects_disabled_binding_and_cross_tenant_connection():
    other_tenant = {"conn-a": _record("conn-a", "wecom", "tenant-b")}
    cross_tenant = _gateway(
        connections=other_tenant,
        bindings=[_binding("binding-a", "conn-a", "users.get", "users")],
    )
    disabled = _gateway(
        bindings=[
            _binding(
                "binding-a",
                "conn-a",
                "users.get",
                "users",
                status="disabled",
            )
        ]
    )

    assert await cross_tenant.gateway.list_tools(_service()) == []
    assert await disabled.gateway.list_tools(_service()) == []
    with pytest.raises(ToolDisabledError):
        await cross_tenant.gateway.call_tool(_service(), "users", {})
    with pytest.raises(ToolDisabledError):
        await disabled.gateway.call_tool(_service(), "users", {})


@pytest.mark.asyncio
async def test_service_and_source_rate_limits_are_both_enforced():
    source_limited = _gateway(
        policies={
            ("conn-a", "users.get"): ToolPolicy(
                "conn-a",
                "users.get",
                True,
                {"rate_limit": {"limit": 1, "window_seconds": 60}},
            )
        },
        bindings=[
            _binding(
                "binding-a",
                "conn-a",
                "users.get",
                "users",
                policy={"rate_limit": {"limit": 10, "window_seconds": 60}},
            )
        ],
    )
    alias_limited = _gateway(
        bindings=[
            _binding(
                "binding-a",
                "conn-a",
                "users.get",
                "users",
                policy={"rate_limit": {"limit": 1, "window_seconds": 60}},
            )
        ],
    )

    await source_limited.gateway.call_tool(_service(), "users", {})
    with pytest.raises(RateLimitError):
        await source_limited.gateway.call_tool(_service(), "users", {})

    await alias_limited.gateway.call_tool(_service(), "users", {})
    with pytest.raises(RateLimitError):
        await alias_limited.gateway.call_tool(_service(), "users", {})


@pytest.mark.asyncio
async def test_service_alias_rate_limit_denial_is_audited_with_service_dimensions():
    events = []
    fixture = _gateway(
        bindings=[
            _binding(
                "binding-a",
                "conn-a",
                "users.get",
                "users",
                policy={"rate_limit": {"limit": 1, "window_seconds": 60}},
            )
        ],
        audit_writer=events.append,
    )

    await fixture.gateway.call_tool(_service(), "users", {})
    with pytest.raises(RateLimitError):
        await fixture.gateway.call_tool(_service(), "users", {})

    assert [(event.result_status, event.error_code) for event in events] == [
        ("ok", ""),
        ("denied", "rate_limited"),
    ]
    assert all(event.service_id == "service-a" for event in events)
    assert all(event.tool_alias == "users" for event in events)


def test_service_resolver_binds_token_to_requested_service_path():
    service = McpService(
        service_id="service-a",
        tenant_id="tenant-a",
        display_name="Operations",
        service_key="operations",
        status="active",
        config_version=7,
    )
    calls = []
    resolver = ServiceResolver(
        token_resolver=lambda raw_token, service_id: calls.append(
            (raw_token, service_id)
        )
        or (service if (raw_token, service_id) == ("token-a", "service-a") else None)
    )

    assert resolver.resolve("service-a", "token-a") == _service()
    assert resolver.resolve("service-b", "token-a") is None
    assert calls == [("token-a", "service-a"), ("token-a", "service-b")]


def test_service_resolver_usage_persistence_failure_is_safe_and_fail_closed(caplog):
    raw_token = "mcp_svc_do-not-log"
    calls = []

    def fail(token, service_id):
        calls.append((token, service_id))
        raise RuntimeError(f"commit failed for {token}")

    resolver = ServiceResolver(token_resolver=fail)

    assert resolver.resolve("service-a", raw_token) is None
    assert calls == [(raw_token, "service-a")]
    assert raw_token not in caplog.text
    assert "RuntimeError" in caplog.text


def test_service_streamable_http_auth_cache_projection_and_logs():
    service = McpService(
        service_id="service-a",
        tenant_id="tenant-a",
        display_name="Operations",
        service_key="operations",
        status="active",
        config_version=7,
    )
    resolver = ServiceResolver(
        token_resolver=lambda raw_token, service_id: (
            service if (raw_token, service_id) == ("token-a", "service-a") else None
        )
    )
    events = []
    fixture = _gateway(resolver=resolver, audit_writer=events.append)
    app = Starlette(
        routes=[Mount("/mcp/service/{service_id}", app=fixture.gateway)]
    )
    app.router.lifespan_context = lambda _app: fixture.gateway.run()
    headers = {
        "Authorization": "Bearer token-a",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }

    with TestClient(app) as client:
        denied = client.post(
            "/mcp/service/service-b/",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        listed = client.post(
            "/mcp/service/service-a/",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        called = client.post(
            "/mcp/service/service-a/",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "hq_wecom__get_users", "arguments": {}},
            },
        )
        assert fixture.gateway.cached_session_keys == (("service-a", 7),)

    assert denied.status_code == 401
    assert [item["name"] for item in listed.json()["result"]["tools"]] == [
        "hq_wecom__get_users",
        "erp__get_orders",
    ]
    assert called.json()["result"]["structuredContent"] == {
        "connection_id": "conn-a"
    }
    invalid = next(event for event in events if event.event_name == "auth_invalid")
    valid = next(event for event in events if event.event_name == "auth_ok")
    tool = next(event for event in events if event.category == "tool")
    assert invalid.service_id is None
    assert invalid.connection_id is None
    assert valid.service_id == "service-a"
    assert valid.connection_id is None
    assert (tool.service_id, tool.tool_alias, tool.connection_id, tool.tool_key) == (
        "service-a",
        "hq_wecom__get_users",
        "conn-a",
        "users.get",
    )
    assert "token-a" not in repr(events)
