from __future__ import annotations

import asyncio

import pytest

from app.connections.cache import ConnectionCache
from app.connections.models import ConnectionRecord, ToolPolicy
from app.connectors.contracts import (
    ConnectionContext,
    ConnectorSpec,
    ExecutionResult,
    SyncResult,
    ToolSpec,
)
from app.connectors.registry import ConnectorRegistry
from app.connectors.runtime import ConnectorRuntime


READ_TOOL = ToolSpec(
    tool_key="reports.list",
    mcp_name="wecom_list_reports",
    description="List reports.",
    input_schema={"type": "object"},
    output_schema={"type": "object"},
    operation_kind="read",
    default_timeout_ms=5_000,
    cache_ttl_seconds=60,
)
UNCACHED_READ_TOOL = ToolSpec(
    tool_key="reports.fresh",
    mcp_name="wecom_list_fresh_reports",
    description="List reports without a declared TTL.",
    input_schema={"type": "object"},
    output_schema={"type": "object"},
    operation_kind="read",
    default_timeout_ms=5_000,
    cache_ttl_seconds=None,
)
WRITE_TOOL = ToolSpec(
    tool_key="reports.create",
    mcp_name="wecom_create_report",
    description="Create a report.",
    input_schema={"type": "object"},
    output_schema={"type": "object"},
    operation_kind="write",
    default_timeout_ms=5_000,
    cache_ttl_seconds=60,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _CountingConnector:
    """Return a distinct payload per invocation so cache hits are observable."""

    def __init__(
        self,
        *,
        status: str = "ok",
        payload: dict[str, object] | None = None,
    ) -> None:
        self.connector_key = "wecom"
        self._spec = ConnectorSpec(
            connector_key="wecom",
            tools=(READ_TOOL, UNCACHED_READ_TOOL, WRITE_TOOL),
        )
        self._status = status
        self._payload = payload
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def spec(self) -> ConnectorSpec:
        return self._spec

    async def execute(self, context, tool_key, args) -> ExecutionResult:
        self.calls.append((context.connection.connection_id, tool_key, dict(args)))
        payload = (
            dict(self._payload)
            if self._payload is not None
            else {"call": len(self.calls)}
        )
        return ExecutionResult(data=payload, status=self._status)

    async def sync(self, context, resource_key) -> SyncResult:
        return SyncResult.ok(context.connection.connection_id, resource_key)


def _context(
    *,
    connection_id: str = "conn-a",
    data_mode: str = "hybrid",
) -> ConnectionContext:
    return ConnectionContext(
        connection=ConnectionRecord(
            connection_id=connection_id,
            tenant_id="tenant-a",
            connector_key="wecom",
            display_name="WeCom",
            status="active",
            data_mode=data_mode,  # type: ignore[arg-type]
            public_config={},
            config_version=1,
        ),
        credentials={},
    )


def _runtime(
    connector: _CountingConnector,
    *,
    clock: _Clock | None = None,
) -> ConnectorRuntime:
    cache = ConnectionCache(clock=clock or _Clock())
    return ConnectorRuntime(ConnectorRegistry([connector]), data_cache=cache)


@pytest.mark.asyncio
async def test_declared_ttl_serves_a_repeated_read_from_cache() -> None:
    connector = _CountingConnector()
    runtime = _runtime(connector)
    context = _context()

    first = await runtime.execute(context, "reports.list", {"limit": 10})
    second = await runtime.execute(context, "reports.list", {"limit": 10})

    assert first.data == second.data == {"call": 1}
    assert len(connector.calls) == 1


@pytest.mark.asyncio
async def test_explicit_bypass_executes_a_cached_read_against_the_connector() -> None:
    connector = _CountingConnector()
    runtime = _runtime(connector)
    context = _context()

    cached = await runtime.execute(context, "reports.list", {})
    repeated = await runtime.execute(context, "reports.list", {})
    fresh = await runtime.execute(
        context,
        "reports.list",
        {},
        bypass_cache=True,
    )

    assert cached.data == repeated.data == {"call": 1}
    assert fresh.data == {"call": 2}
    assert len(connector.calls) == 2


@pytest.mark.asyncio
async def test_direct_mode_honors_an_explicit_tool_cache_ttl() -> None:
    connector = _CountingConnector()
    runtime = _runtime(connector)
    context = _context(data_mode="direct")

    first = await runtime.execute(context, "reports.list", {"limit": 10})
    second = await runtime.execute(context, "reports.list", {"limit": 10})

    assert first.data == second.data == {"call": 1}
    assert len(connector.calls) == 1


@pytest.mark.asyncio
async def test_stored_mode_bypasses_the_runtime_response_cache() -> None:
    connector = _CountingConnector()
    runtime = _runtime(connector)
    context = _context(data_mode="stored")

    first = await runtime.execute(context, "reports.list", {"limit": 10})
    second = await runtime.execute(context, "reports.list", {"limit": 10})

    assert first.data == {"call": 1}
    assert second.data == {"call": 2}
    assert len(connector.calls) == 2


@pytest.mark.asyncio
async def test_entries_expire_after_the_declared_ttl() -> None:
    clock = _Clock()
    connector = _CountingConnector()
    runtime = _runtime(connector, clock=clock)
    context = _context()

    await runtime.execute(context, "reports.list", {})
    clock.advance(59)
    await runtime.execute(context, "reports.list", {})
    assert len(connector.calls) == 1

    clock.advance(2)
    await runtime.execute(context, "reports.list", {})
    assert len(connector.calls) == 2


@pytest.mark.asyncio
async def test_write_tools_are_never_cached() -> None:
    connector = _CountingConnector()
    policies = {
        ("conn-a", "reports.create"): ToolPolicy(
            "conn-a",
            "reports.create",
            enabled=True,
            policy={"allow_write": True},
        )
    }
    runtime = ConnectorRuntime(
        ConnectorRegistry([connector]),
        policy_store=policies,
        data_cache=ConnectionCache(clock=_Clock()),
    )
    context = _context()

    await runtime.execute(context, "reports.create", {})
    await runtime.execute(context, "reports.create", {})

    assert len(connector.calls) == 2


@pytest.mark.asyncio
async def test_reads_without_a_declared_ttl_are_never_cached() -> None:
    connector = _CountingConnector()
    runtime = _runtime(connector)
    context = _context()

    await runtime.execute(context, "reports.fresh", {})
    await runtime.execute(context, "reports.fresh", {})

    assert len(connector.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["error", "partial"])
async def test_only_successful_results_are_cached(status: str) -> None:
    connector = _CountingConnector(status=status)
    runtime = _runtime(connector)
    context = _context()

    await runtime.execute(context, "reports.list", {})
    await runtime.execute(context, "reports.list", {})

    assert len(connector.calls) == 2


@pytest.mark.asyncio
async def test_distinct_arguments_are_cached_independently() -> None:
    connector = _CountingConnector()
    runtime = _runtime(connector)
    context = _context()

    await runtime.execute(context, "reports.list", {"limit": 10})
    await runtime.execute(context, "reports.list", {"limit": 20})
    await runtime.execute(context, "reports.list", {"limit": 10})

    assert len(connector.calls) == 2


@pytest.mark.asyncio
async def test_connections_never_share_a_cached_result() -> None:
    connector = _CountingConnector()
    runtime = _runtime(connector)

    first = await runtime.execute(_context(connection_id="conn-a"), "reports.list", {})
    second = await runtime.execute(_context(connection_id="conn-b"), "reports.list", {})
    repeat = await runtime.execute(_context(connection_id="conn-a"), "reports.list", {})

    assert first.data == {"call": 1}
    assert second.data == {"call": 2}
    assert repeat.data == {"call": 1}
    assert len(connector.calls) == 2


@pytest.mark.asyncio
async def test_results_carrying_sensitive_fields_bypass_the_cache() -> None:
    connector = _CountingConnector(payload={"access_token": "secret-value"})
    runtime = _runtime(connector)
    context = _context()

    first = await runtime.execute(context, "reports.list", {})
    second = await runtime.execute(context, "reports.list", {})

    assert first.data == second.data == {"access_token": "secret-value"}
    assert len(connector.calls) == 2


@pytest.mark.asyncio
async def test_concurrent_identical_reads_collapse_into_one_execution() -> None:
    connector = _CountingConnector()
    runtime = _runtime(connector)
    context = _context()

    results = await asyncio.gather(
        *(runtime.execute(context, "reports.list", {"limit": 5}) for _ in range(5))
    )

    assert all(result.data == {"call": 1} for result in results)
    assert len(connector.calls) == 1


@pytest.mark.asyncio
async def test_invalidating_a_connection_drops_only_its_cached_reads() -> None:
    connector = _CountingConnector()
    runtime = _runtime(connector)

    await runtime.execute(_context(connection_id="conn-a"), "reports.list", {})
    await runtime.execute(_context(connection_id="conn-b"), "reports.list", {})
    assert len(connector.calls) == 2

    await runtime.invalidate_cached_data("conn-a")

    await runtime.execute(_context(connection_id="conn-b"), "reports.list", {})
    assert len(connector.calls) == 2

    await runtime.execute(_context(connection_id="conn-a"), "reports.list", {})
    assert len(connector.calls) == 3


@pytest.mark.asyncio
async def test_caching_is_available_without_an_explicit_cache_argument() -> None:
    connector = _CountingConnector()
    runtime = ConnectorRuntime(ConnectorRegistry([connector]))
    context = _context()

    await runtime.execute(context, "reports.list", {"limit": 1})
    await runtime.execute(context, "reports.list", {"limit": 1})

    assert len(connector.calls) == 1
