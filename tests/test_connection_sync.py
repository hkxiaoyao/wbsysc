from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.connections.cache import ConnectionCache
from app import main
from app.connections.models import ConnectionRecord
from app.connections.sync import SyncOrchestrator
from app.connectors.contracts import ConnectionContext, ConnectorSpec, SyncResult
from app.connectors.registry import ConnectorRegistry


def connection(
    connection_id: str = "conn-a",
    *,
    status: str = "active",
    data_mode: str = "stored",
) -> ConnectionRecord:
    return ConnectionRecord(
        connection_id=connection_id,
        tenant_id="tenant-a",
        connector_key="demo",
        display_name="Demo",
        status=status,
        data_mode=data_mode,
        public_config={},
        config_version=1,
    )


def wecom_connection_context(connection_id: str) -> ConnectionContext:
    return ConnectionContext(
        connection=ConnectionRecord(
            connection_id=connection_id,
            tenant_id="tenant-a",
            connector_key="wecom",
            display_name="WeCom",
            status="active",
            data_mode="stored",
            public_config={"corpid": "corp-a", "schema_name": "wbd_shared"},
            config_version=1,
        ),
        credentials={"wecom_app_secret": "app-secret"},
    )


class Contexts:
    def __init__(self) -> None:
        self.connections: list[ConnectionRecord] = []

    def build(self, record: ConnectionRecord) -> ConnectionContext:
        self.connections.append(record)
        return ConnectionContext(connection=record, credentials={})


class SyncingConnector:
    def __init__(self) -> None:
        self.calls: list[tuple[ConnectionContext, str]] = []

    def spec(self) -> ConnectorSpec:
        return ConnectorSpec(connector_key="demo", tools=(), supports_sync=True)

    async def sync(self, context: ConnectionContext, resource_key: str) -> SyncResult:
        self.calls.append((context, resource_key))
        return SyncResult.ok(context.connection_id, resource_key, {"stored": 1})


@pytest.mark.asyncio
async def test_sync_orchestrator_never_syncs_direct_connection():
    connector = SyncingConnector()
    contexts = Contexts()
    orchestrator = SyncOrchestrator(ConnectorRegistry([connector]), contexts=contexts)

    result = await orchestrator.run_connection(connection(data_mode="direct"))

    assert result is None
    assert connector.calls == []
    assert contexts.connections == []


@pytest.mark.asyncio
async def test_sync_orchestrator_scopes_context_and_resource_to_connection():
    connector = SyncingConnector()
    contexts = Contexts()
    orchestrator = SyncOrchestrator(ConnectorRegistry([connector]), contexts=contexts)

    result = await orchestrator.run_connection(connection("conn-b"), "reports.list")

    assert result == SyncResult.ok("conn-b", "reports.list", {"stored": 1})
    assert contexts.connections == [connection("conn-b")]
    assert [(context.connection_id, resource_key) for context, resource_key in connector.calls] == [
        ("conn-b", "reports.list")
    ]


def test_connection_locks_serialize_across_event_loop_threads():
    from app.connections.sync import _ConnectionLocks

    locks = _ConnectionLocks()
    first_started = threading.Event()
    release_first = threading.Event()
    same_connection_entered = threading.Event()
    different_connection_entered = threading.Event()
    failures: list[BaseException] = []

    async def hold(connection_id: str, phase: str) -> None:
        async with locks.acquire(connection_id):
            if phase == "first":
                first_started.set()
                await asyncio.to_thread(release_first.wait, 1)
            elif phase == "same":
                same_connection_entered.set()
            else:
                different_connection_entered.set()

    def run(connection_id: str, phase: str) -> None:
        try:
            asyncio.run(asyncio.wait_for(hold(connection_id, phase), timeout=1.5))
        except BaseException as exc:  # keep thread failures observable to pytest
            failures.append(exc)

    first = threading.Thread(target=run, args=("conn-a", "first"), daemon=True)
    same = threading.Thread(target=run, args=("conn-a", "same"), daemon=True)
    different = threading.Thread(target=run, args=("conn-b", "different"), daemon=True)
    first.start()
    assert first_started.wait(0.5)
    same.start()
    different.start()

    assert different_connection_entered.wait(0.5)
    assert not same_connection_entered.wait(0.1)
    release_first.set()
    first.join(1)
    same.join(1)
    different.join(1)

    assert not first.is_alive()
    assert not same.is_alive()
    assert not different.is_alive()
    assert failures == []
    assert same_connection_entered.is_set()


def test_connection_sync_db_lock_uses_distinct_validated_advisory_keys(monkeypatch):
    from app import db

    calls = []

    @contextmanager
    def fake_tenant_lock(schema, timeout=0, *, _lock_name=None):
        calls.append((schema, timeout, _lock_name))
        yield True

    monkeypatch.setattr(db, "tenant_sync_lock", fake_tenant_lock)

    with db.connection_sync_lock("wbd_shared", "conn-a", timeout=0) as acquired:
        assert acquired is True
    with db.connection_sync_lock("wbd_shared", "conn-a", timeout=0):
        pass
    with db.connection_sync_lock("wbd_shared", "conn-b", timeout=0):
        pass

    assert calls[0][0:2] == ("wbd_shared", 0)
    assert calls[0][2] == calls[1][2]
    assert calls[0][2] != calls[2][2]
    assert calls[0][2].startswith("wbsysc:connection-sync:")
    with pytest.raises(ValueError, match="connection_id"):
        with db.connection_sync_lock("wbd_shared", "token=top-secret"):
            pass


def test_wecom_connection_sync_busy_lock_is_scoped_to_connection(monkeypatch):
    from app.wecom import dispatch

    calls = []

    @contextmanager
    def connection_lock(schema, connection_id, timeout=0):
        calls.append((schema, connection_id, timeout))
        yield connection_id != "conn-a"

    def legacy_lock_must_not_be_used(*args, **kwargs):
        raise AssertionError("connection sync must not take the tenant-wide lock")

    monkeypatch.setattr(dispatch.db, "connection_sync_lock", connection_lock)
    monkeypatch.setattr(dispatch.db, "tenant_sync_lock", legacy_lock_must_not_be_used)
    monkeypatch.setattr(
        dispatch,
        "_sync_one_report",
        lambda *args, **kwargs: {"pulled": 1, "stored": 1},
    )

    busy = dispatch.run_sync_connection(wecom_connection_context("conn-a"), "reports")
    ready = dispatch.run_sync_connection(wecom_connection_context("conn-b"), "reports")

    assert busy == {"busy": True}
    assert ready == {"pulled": 1, "stored": 1}
    assert calls == [
        ("wbd_shared", "conn-a", 0),
        ("wbd_shared", "conn-b", 0),
    ]


@pytest.mark.asyncio
async def test_scheduler_runs_only_active_stored_and_eligible_hybrid_connections():
    connector = SyncingConnector()
    contexts = Contexts()
    orchestrator = SyncOrchestrator(ConnectorRegistry([connector]), contexts=contexts)
    disabled_hybrid = connection("conn-disabled", data_mode="hybrid")
    object.__setattr__(disabled_hybrid, "public_config", {"sync_enabled": False})

    results = await orchestrator.run_scheduled(
        connections=(
            connection("conn-stored", data_mode="stored"),
            connection("conn-hybrid", data_mode="hybrid"),
            disabled_hybrid,
            connection("conn-direct", data_mode="direct"),
            connection("conn-inactive", status="disabled", data_mode="stored"),
        )
    )

    assert [result.connection_id for result in results] == ["conn-stored", "conn-hybrid"]
    assert [context.connection_id for context, _ in connector.calls] == [
        "conn-stored",
        "conn-hybrid",
    ]


@pytest.mark.asyncio
async def test_scheduler_drops_credential_bearing_resource_labels_before_logging():
    connector = SyncingConnector()
    orchestrator = SyncOrchestrator(ConnectorRegistry([connector]), contexts=Contexts())
    unsafe = connection("conn-a")
    object.__setattr__(unsafe, "public_config", {"sync_resources": ["token=top-secret"]})

    results = await orchestrator.run_scheduled(connections=(unsafe,))

    assert results == ()
    assert connector.calls == []


@pytest.mark.asyncio
async def test_hybrid_cache_is_partitioned_by_connection_and_tool():
    cache = ConnectionCache()

    await cache.put("conn-a", "reports.list", {"count": 1}, ttl_seconds=60)

    assert await cache.get("conn-b", "reports.list") is None
    assert await cache.get("conn-a", "approvals.list") is None
    assert await cache.get("conn-a", "reports.list") == {"count": 1}


@pytest.mark.asyncio
async def test_cache_rejects_credential_bearing_dimension_labels():
    cache = ConnectionCache()

    with pytest.raises(ValueError, match="tool_key"):
        await cache.put("conn-a", "token=top-secret", {"count": 1}, ttl_seconds=60)


@pytest.mark.asyncio
async def test_cache_does_not_retain_a_top_level_raw_response_body():
    cache = ConnectionCache()

    await cache.put(
        "conn-a",
        "reports.list",
        '{"access_token":"top-secret"}',
        ttl_seconds=60,
    )

    assert await cache.get("conn-a", "reports.list") is None


@pytest.mark.asyncio
async def test_cache_bypasses_nested_sensitive_values_under_benign_keys():
    cache = ConnectionCache()

    await cache.put(
        "conn-a",
        "reports.list",
        {"result": "token=top-secret raw-response-body"},
        ttl_seconds=60,
    )

    assert await cache.get("conn-a", "reports.list") is None


@pytest.mark.asyncio
async def test_cache_owner_cancellation_releases_same_key_waiter():
    cache = ConnectionCache()
    owner_started = asyncio.Event()
    never_complete = asyncio.Event()

    async def blocked_loader():
        owner_started.set()
        await never_complete.wait()
        return {"count": 1}

    async def waiter_loader():
        raise AssertionError("same-key waiter must join the owner")

    owner = asyncio.create_task(
        cache.get_or_load("conn-a", "reports.list", loader=blocked_loader)
    )
    await owner_started.wait()
    waiter = asyncio.create_task(
        cache.get_or_load("conn-a", "reports.list", loader=waiter_loader)
    )
    await asyncio.sleep(0)

    owner.cancel()

    with pytest.raises(asyncio.CancelledError):
        await owner
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(waiter, timeout=0.5)

    assert await cache.get_or_load(
        "conn-a",
        "reports.list",
        loader=lambda: {"count": 2},
    ) == {"count": 2}


@pytest.mark.asyncio
async def test_direct_cache_load_bypasses_retained_entries():
    cache = ConnectionCache()
    calls = 0

    async def load():
        nonlocal calls
        calls += 1
        return {"count": calls}

    first = await cache.get_or_load(
        "conn-a",
        "reports.list",
        loader=load,
        data_mode="direct",
    )
    second = await cache.get_or_load(
        "conn-a",
        "reports.list",
        loader=load,
        data_mode="direct",
    )

    assert (first, second, calls) == ({"count": 1}, {"count": 2}, 2)


@pytest.mark.asyncio
async def test_sync_failures_return_a_fixed_safe_summary(caplog):
    class FailingConnector(SyncingConnector):
        async def sync(self, context: ConnectionContext, resource_key: str) -> SyncResult:
            raise RuntimeError("token=top-secret raw response body")

    contexts = Contexts()
    orchestrator = SyncOrchestrator(
        ConnectorRegistry([FailingConnector()]),
        contexts=contexts,
    )

    with caplog.at_level(logging.WARNING, logger="app.connections.sync"):
        result = await orchestrator.run_connection(connection(), "reports.list")

    assert result == SyncResult(
        connection_id="conn-a",
        resource_key="reports.list",
        data={"error": "sync_failed"},
        status="error",
    )
    assert "top-secret" not in caplog.text
    assert "raw response body" not in caplog.text


@pytest.mark.asyncio
async def test_legacy_wecom_scheduler_job_delegates_to_connection_orchestrator(
    monkeypatch,
):
    calls = []

    class Orchestrator:
        async def run_scheduled(self):
            calls.append("run_scheduled")
            return ()

    monkeypatch.setattr(main, "connection_sync_orchestrator", Orchestrator())
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(wecom_use_mock=False),
    )

    await main._sync_job_async()

    assert calls == ["run_scheduled"]
