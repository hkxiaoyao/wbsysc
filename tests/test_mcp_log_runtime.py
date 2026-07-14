import asyncio
import contextlib
import threading
from types import SimpleNamespace

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app import main, mcp_log_store
from app.auth import BearerTokenMiddleware
from app.mcp_audit import McpProtocolAuditMiddleware


def test_runtime_registers_log_admin_before_static_and_mcp_mounts():
    runtime = main.create_app()
    paths = [getattr(route, "path", None) for route in runtime.routes]
    log_router_index = paths.index("/admin/mcp-logs")
    log_paths = [route.path for route in main.mcp_logs_admin_router.routes]

    assert "/admin/mcp-logs" in log_paths
    assert "/admin/mcp-log-settings" in log_paths
    assert log_router_index < paths.index("/mcp")
    if "/admin/ui" in paths:
        assert log_router_index < paths.index("/admin/ui")


def test_mcp_protocol_audit_runs_inside_bearer_authentication():
    runtime = main.create_app()
    mcp_mount = next(
        route for route in runtime.routes if getattr(route, "path", None) == "/mcp"
    )
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


def test_lifespan_schedules_daily_cleanup_without_running_it_immediately(monkeypatch):
    events: list[str] = []
    scheduled_jobs: list[tuple[object, object, dict]] = []
    immediately_started: list[str] = []

    class FakeSessionManager:
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
        "mcp",
        SimpleNamespace(session_manager=FakeSessionManager()),
    )
    monkeypatch.setattr(
        main.db,
        "run_startup_migrations",
        lambda: events.append("migrations"),
    )
    monkeypatch.setattr(main, "AsyncIOScheduler", FakeScheduler)
    monkeypatch.setattr(main.asyncio, "create_task", fake_create_task)

    async def exercise_lifespan():
        async with main.lifespan(SimpleNamespace()):
            events.append("application_running")

    asyncio.run(exercise_lifespan())

    assert events.index("migrations") < events.index("session_enter")
    assert events.index("session_enter") < events.index("scheduler_init")
    assert events.index("scheduler_init") < events.index("scheduler_start")
    assert events.index("scheduler_start") < events.index("application_running")
    assert events[-2:] == ["scheduler_shutdown", "session_exit"]

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
