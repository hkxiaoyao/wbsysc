import asyncio
import contextlib
import threading
from types import SimpleNamespace

import pytest
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app import main, mcp_audit, mcp_log_store
from app.auth import BearerTokenMiddleware
from app.connectors.runtime import ConnectorAuditEvent
from app import mcp_gateway
from app.mcp_audit import McpProtocolAuditMiddleware


def test_runtime_registers_log_admin_before_static_and_mcp_mounts():
    runtime = main.create_app()
    paths = [getattr(route, "path", None) for route in runtime.routes]
    log_router_index = next(
        index
        for index, route in enumerate(runtime.routes)
        if getattr(route, "path", None) == "/admin/mcp-logs"
        or getattr(route, "original_router", None) is main.mcp_logs_admin_router
    )
    log_paths = [route.path for route in main.mcp_logs_admin_router.routes]

    assert "/admin/mcp-logs" in log_paths
    assert "/admin/mcp-log-settings" in log_paths
    assert log_router_index < paths.index("/mcp")
    if "/admin/ui" in paths:
        assert log_router_index < paths.index("/admin/ui")


def test_mcp_protocol_audit_runs_inside_bearer_authentication():
    runtime = main.create_app()
    mcp_mounts = [
        route
        for route in runtime.routes
        if getattr(route, "path", None) in {"/mcp", "/mcp/{connection_id}"}
    ]

    assert [route.path for route in mcp_mounts] == ["/mcp/{connection_id}", "/mcp"]
    for mcp_mount in mcp_mounts:
        middleware_classes = [entry.cls for entry in mcp_mount.app.user_middleware]
        assert BearerTokenMiddleware in middleware_classes
        assert McpProtocolAuditMiddleware in middleware_classes
        assert middleware_classes.index(BearerTokenMiddleware) < middleware_classes.index(
            McpProtocolAuditMiddleware
        )


def test_daily_cleanup_runs_in_executor(monkeypatch):
    calling_thread = threading.get_ident()
    cleanup_threads: list[int] = []

    def fake_cleanup():
        cleanup_threads.append(threading.get_ident())

    monkeypatch.setattr(mcp_log_store, "cleanup_expired_logs", fake_cleanup)

    asyncio.run(main._cleanup_logs_job_async())

    assert cleanup_threads
    assert cleanup_threads[0] != calling_thread


def test_runtime_audit_writes_server_resolved_connection_dimensions(monkeypatch):
    events = []
    monkeypatch.setattr(mcp_gateway, "write_event", events.append)
    monkeypatch.setattr(
        mcp_gateway,
        "current_request_metadata",
        lambda: {"request_id": "req-1", "client_ip": "203.0.113.8", "http_method": "POST"},
    )

    mcp_gateway.ConnectionMcpGateway._write_runtime_audit(
        object(),
        ConnectorAuditEvent(
            tenant_id="tenant-a",
            connection_id="conn-a",
            connector_key="wecom",
            tool_key="reports.list",
            status="ok",
            cost_ms=12,
        ),
    )

    assert len(events) == 1
    assert events[0].connection_id == "conn-a"
    assert events[0].connector_key == "wecom"
    assert events[0].tool_key == "reports.list"


def test_lifespan_schedules_daily_cleanup_without_running_it_immediately(monkeypatch):
    events: list[str] = []
    scheduled_jobs: list[tuple[object, object, dict]] = []
    immediately_started: list[str] = []
    shutdown_threads: list[int] = []
    event_loop_thread = threading.get_ident()

    class FakeGateway:
        @contextlib.asynccontextmanager
        async def run(self):
            events.append("session_enter")
            try:
                yield
            finally:
                events.append("session_exit")

    class FakeScheduler:
        def __init__(self, *, timezone):
            events.append("scheduler_init")
            self.timezone = timezone

        def add_job(self, job, trigger, **kwargs):
            scheduled_jobs.append((job, trigger, kwargs))

        def start(self):
            events.append("scheduler_start")

        def shutdown(self, *, wait):
            assert wait is False
            events.append("scheduler_shutdown")

    def fake_create_task(coroutine):
        immediately_started.append(coroutine.cr_code.co_name)
        coroutine.close()
        return object()

    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(
            sync_interval_report_min=15,
            sync_interval_approval_min=30,
        ),
    )
    monkeypatch.setattr(
        main,
        "mcp_gateway",
        FakeGateway(),
    )
    monkeypatch.setattr(
        main.db,
        "run_startup_migrations",
        lambda: events.append("migrations"),
    )
    monkeypatch.setattr(main, "AsyncIOScheduler", FakeScheduler)
    monkeypatch.setattr(main.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(main, "acquire_audit_writer", lambda: events.append("audit_start"))
    monkeypatch.setattr(
        main,
        "release_audit_writer",
        lambda timeout: (
            shutdown_threads.append(threading.get_ident()),
            events.append("audit_shutdown"),
            True,
        )[-1],
    )

    async def exercise_lifespan():
        async with main.lifespan(SimpleNamespace()):
            events.append("application_running")

    asyncio.run(exercise_lifespan())

    assert events.index("migrations") < events.index("session_enter")
    assert events.index("migrations") < events.index("audit_start")
    assert events.index("audit_start") < events.index("session_enter")
    assert events.index("session_enter") < events.index("scheduler_init")
    assert events.index("scheduler_init") < events.index("scheduler_start")
    assert events.index("scheduler_start") < events.index("application_running")
    assert events[-3:] == ["scheduler_shutdown", "session_exit", "audit_shutdown"]
    assert shutdown_threads
    assert shutdown_threads[0] != event_loop_thread

    jobs_by_id = {kwargs["id"]: (job, trigger, kwargs) for job, trigger, kwargs in scheduled_jobs}
    sync_job, sync_trigger, sync_options = jobs_by_id["wecom_sync"]
    assert sync_job is main._sync_job_async
    assert isinstance(sync_trigger, IntervalTrigger)
    assert sync_options["max_instances"] == 1
    assert sync_options["coalesce"] is True

    cleanup_job, cleanup_trigger, cleanup_options = jobs_by_id["mcp_log_cleanup"]
    assert cleanup_job is main._cleanup_logs_job_async
    assert isinstance(cleanup_trigger, CronTrigger)
    trigger_fields = {field.name: str(field) for field in cleanup_trigger.fields}
    assert trigger_fields["hour"] == "3"
    assert trigger_fields["minute"] == "17"
    assert str(cleanup_trigger.timezone) == "Asia/Shanghai"
    assert cleanup_options["max_instances"] == 1
    assert cleanup_options["coalesce"] is True

    assert immediately_started == ["_sync_job_async"]


def test_real_lifespans_share_then_restart_the_audit_writer(monkeypatch):
    class FakeGateway:
        @contextlib.asynccontextmanager
        async def run(self):
            yield

    class FakeScheduler:
        def __init__(self, *, timezone):
            self.running = False

        def add_job(self, *args, **kwargs):
            return None

        def start(self):
            self.running = True

        def shutdown(self, *, wait):
            self.running = False

    def fake_create_task(coroutine):
        coroutine.close()
        return object()

    initial_writer = mcp_audit.AuditEventWriter(insert=lambda event: None)
    monkeypatch.setattr(mcp_audit, "_audit_writer", initial_writer)
    monkeypatch.setattr(mcp_audit, "_audit_writer_refcount", 0)
    monkeypatch.setattr(main, "acquire_audit_writer", mcp_audit.acquire_audit_writer)
    monkeypatch.setattr(main, "release_audit_writer", mcp_audit.release_audit_writer)
    monkeypatch.setattr(main.db, "run_startup_migrations", lambda: None)
    monkeypatch.setattr(main, "mcp_gateway", FakeGateway())
    monkeypatch.setattr(main, "AsyncIOScheduler", FakeScheduler)
    monkeypatch.setattr(main.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(
            sync_interval_report_min=15,
            sync_interval_approval_min=30,
        ),
    )

    async def exercise_lifespans():
        first_lifespan = main.lifespan(SimpleNamespace())
        second_lifespan = main.lifespan(SimpleNamespace())
        await first_lifespan.__aenter__()
        first_writer = mcp_audit._audit_writer
        await second_lifespan.__aenter__()
        assert mcp_audit._audit_writer is first_writer
        assert mcp_audit._audit_writer_refcount == 2

        await first_lifespan.__aexit__(None, None, None)
        assert first_writer._thread is not None and first_writer._thread.is_alive()
        assert mcp_audit._audit_writer_refcount == 1

        await second_lifespan.__aexit__(None, None, None)
        assert not first_writer._thread.is_alive()
        assert mcp_audit._audit_writer_refcount == 0

        third_lifespan = main.lifespan(SimpleNamespace())
        await third_lifespan.__aenter__()
        restarted_writer = mcp_audit._audit_writer
        assert restarted_writer is not first_writer
        assert restarted_writer._thread is not None and restarted_writer._thread.is_alive()
        await third_lifespan.__aexit__(None, None, None)

    asyncio.run(exercise_lifespans())


def test_lifespan_retries_audit_release_after_thread_dispatch_failure(monkeypatch):
    events: list[str] = []

    class FailingGateway:
        @contextlib.asynccontextmanager
        async def run(self):
            events.append("session_enter_failed")
            raise RuntimeError("session startup failed")
            yield

    attempts = 0

    async def flaky_to_thread(operation, *args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("executor unavailable")
        return operation(*args)

    monkeypatch.setattr(main.db, "run_startup_migrations", lambda: None)
    monkeypatch.setattr(main, "mcp_gateway", FailingGateway())
    monkeypatch.setattr(main, "acquire_audit_writer", lambda: events.append("audit_start"))
    monkeypatch.setattr(
        main,
        "release_audit_writer",
        lambda timeout: events.append("audit_release") or True,
    )
    monkeypatch.setattr(main.asyncio, "to_thread", flaky_to_thread)

    async def exercise():
        with pytest.raises(RuntimeError, match="session startup failed"):
            async with main.lifespan(SimpleNamespace()):
                pass

    asyncio.run(exercise())

    assert attempts == 2
    assert events == ["audit_start", "session_enter_failed", "audit_release"]
