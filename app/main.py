"""
应用入口 - 将 MCP Streamable HTTP app 挂到 FastAPI，叠加 Bearer Token 鉴权
+ APScheduler 定时同步任务（游标驱动增量）

关键点（经官方文档核实）：
1. FastMCP(streamable_http_path="/") + app.mount("/mcp") → 对外路径是 /mcp
   （若内部仍用默认 /mcp 再 mount /mcp，会变成 /mcp/mcp，客户端 POST /mcp 会 405）
2. 挂载子应用时其内置 lifespan 不执行 → 顶层必须显式 mcp.session_manager.run()
3. session_manager 只在调用 streamable_http_app() 后才可访问（惰性创建）
4. 同步任务用单独线程池执行，不阻塞 asyncio 事件循环
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
from dataclasses import dataclass
import logging
from typing import Any

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI
from sqlalchemy import text
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

from . import db
from .auth import BearerTokenMiddleware
from .config import get_settings
from .connections.sync import ResolverConnectionContextBuilder, SyncOrchestrator
from .connectors.discovery import (
    ConnectorDiscoveryFailure,
    ConnectorDiscoveryResult,
    ValidatedConnector,
    discover_connector_packages,
    normalize_connector_name,
    parse_connector_allowlist,
    register_discovered_connectors,
    validate_active_connector_dependencies,
)
from .connectors.registry import ConnectorRegistry
from .mcp_audit import (
    McpProtocolAuditMiddleware,
    acquire_audit_writer,
    release_audit_writer,
)
from .mcp_gateway import ConnectionMcpGateway
from .mcp_service_gateway import ServiceMcpGateway
from .mcp_services import store as mcp_service_store
from .mcp_services.router import legacy_admin_router as legacy_mcp_admin_router
from .admin import router as admin_router
from .admin_connections import router as admin_connections_router
from .mcp_logs_admin import router as mcp_logs_admin_router
from .tenant_auth.router import router as tenant_auth_router
from .tenant_console import router as tenant_console_router
from .tenant_connections import router as tenant_connections_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("wecom-gateway")

# 全局调度器，lifespan 启动/关闭
_scheduler: AsyncIOScheduler | None = None
mcp_gateway = ConnectionMcpGateway()
connector_registry = mcp_gateway._runtime._registry
service_gateway = ServiceMcpGateway(
    runtime=mcp_gateway._runtime,
    connection_context_builder=mcp_gateway.resolver.execution_context,
)


@dataclass(frozen=True)
class _ConnectorDependency:
    connector_key: str
    status: str


def list_active_connector_dependencies() -> tuple[_ConnectorDependency, ...]:
    """Read only connector identity/status metadata after startup migrations."""
    statement = text("""
        SELECT connector_key, status
        FROM connection_instance
        WHERE status='active'
    """)
    with db.get_engine().connect() as connection:
        rows = connection.execute(statement).fetchall()
    dependencies = []
    for row in rows:
        values = dict(row._mapping) if hasattr(row, "_mapping") else dict(row)
        connector_key = values.get("connector_key")
        status = values.get("status")
        if isinstance(connector_key, str) and isinstance(status, str):
            dependencies.append(_ConnectorDependency(connector_key, status))
    return tuple(dependencies)


def _registerable_discovery_result(
    registry: ConnectorRegistry,
    result: ConnectorDiscoveryResult,
    *,
    preferred_keys: frozenset[str] = frozenset(),
) -> ConnectorDiscoveryResult:
    """Preflight built-in/package collisions without executing connector code."""
    failures = list(result.failures)
    ordered = tuple(
        sorted(
            result.connectors,
            key=lambda item: (
                normalize_connector_name(item.spec.connector_key) not in preferred_keys,
                normalize_connector_name(item.spec.connector_key),
            ),
        )
    )
    accepted, rejected = registry.partition_discovered_batch(
        item.spec for item in ordered
    )
    for index in rejected:
        failures.append(
            ConnectorDiscoveryFailure(ordered[index].spec.connector_key, "registration")
        )
    registerable = tuple(
        ValidatedConnector(ordered[index].connector, snapshot)
        for index, snapshot in accepted
    )
    return ConnectorDiscoveryResult(registerable, tuple(failures))


def configure_trusted_connectors(
    *, registry: ConnectorRegistry | None = None
) -> ConnectorDiscoveryResult:
    """Discover, validate active dependencies, and idempotently register packages."""
    target = connector_registry if registry is None else registry
    dependencies = list_active_connector_dependencies()
    settings = get_settings()
    allowed = parse_connector_allowlist(settings.connector_allowlist)
    required = frozenset(
        normalized
        for dependency in dependencies
        if dependency.status == "active"
        and (normalized := normalize_connector_name(dependency.connector_key))
        in allowed
    )
    result = _registerable_discovery_result(
        target,
        discover_connector_packages(),
        preferred_keys=required,
    )
    validate_active_connector_dependencies(
        dependencies,
        result,
        allowlist=settings.connector_allowlist,
    )
    register_discovered_connectors(target, result)
    for item in result.connectors:
        logger.info(
            "trusted connector available connector_key=%s version=%s",
            item.spec.connector_key,
            item.spec.version,
        )
    return result


def _build_connection_sync_orchestrator(
    registry: ConnectorRegistry | None = None,
    connector_resolver=None,
) -> SyncOrchestrator:
    return SyncOrchestrator(
        connector_registry if registry is None else registry,
        contexts=ResolverConnectionContextBuilder(),
        connector_resolver=(
            connector_resolver
            if connector_resolver is not None
            else mcp_gateway._runtime._connector_resolver
        ),
    )


connection_sync_orchestrator = _build_connection_sync_orchestrator()


def create_app(
    *,
    gateway: ConnectionMcpGateway | None = None,
    service_gateway: ServiceMcpGateway | None = None,
) -> FastAPI:
    settings = get_settings()
    gateway = gateway or mcp_gateway
    service_gateway = service_gateway or globals()["service_gateway"]
    app = FastAPI(title="企微数据中转 MCP Gateway", version="0.1.0")
    app.state.mcp_gateway = gateway
    app.state.mcp_service_gateway = service_gateway
    app.state.mcp_service_enabled = bool(
        getattr(settings, "mcp_service_enabled", False)
    )
    gateway_registry = getattr(
        getattr(gateway, "_runtime", None), "_registry", connector_registry
    )
    app.state.connection_sync_orchestrator = _build_connection_sync_orchestrator(
        gateway_registry,
        getattr(getattr(gateway, "_runtime", None), "_connector_resolver", None),
    )
    app.state.connector_registry = gateway_registry

    # WorkBuddy 等客户端 POST /mcp（无尾斜杠）。Starlette Mount("/mcp") 时
    # 子应用拿到 path=""，匹配不到 FastMCP 的 "/"，会 405。进入路由前补上斜杠。
    @app.middleware("http")
    async def _mcp_trailing_slash(request, call_next):
        path = request.scope.get("path")
        if path == "/mcp":
            request.scope["path"] = "/mcp/"
        elif (
            app.state.mcp_service_enabled
            and isinstance(path, str)
            and path.startswith("/mcp/service/")
            and path != "/mcp/service/"
            and path.count("/") == 3
        ):
            request.scope["path"] = f"{path}/"
        elif (
            isinstance(path, str)
            and path.startswith("/mcp/")
            and path != "/mcp/service"
            and path.count("/") == 2
        ):
            # A parameterized Mount needs its terminal slash to win over the
            # legacy `/mcp` catch-all mount for `/mcp/{connection_id}`.
            request.scope["path"] = f"{path}/"
        return await call_next(request)

    # 管理后台 API（独立路由，不经 MCP 鉴权，自带 session 校验）
    app.include_router(admin_router)
    app.include_router(admin_connections_router)
    app.include_router(mcp_logs_admin_router)
    app.include_router(tenant_auth_router)
    app.include_router(tenant_console_router)
    app.include_router(tenant_connections_router)
    app.include_router(legacy_mcp_admin_router)

    # 健康检查（鉴权白名单）
    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "env": settings.app_env,
            "mock": settings.wecom_use_mock,
            "scheduler": _scheduler.running if _scheduler else False,
            "mcp_service_enabled": app.state.mcp_service_enabled,
            "mcp_service_legacy_enabled": app.state.mcp_service_enabled,
        }

    @app.api_route(
        "/mcp/service/",
        methods=["GET", "POST", "DELETE", "PUT", "PATCH", "OPTIONS", "HEAD"],
        include_in_schema=False,
    )
    @app.api_route(
        "/mcp/service",
        methods=["GET", "POST", "DELETE", "PUT", "PATCH", "OPTIONS", "HEAD"],
        include_in_schema=False,
    )
    async def reserved_service_route():
        from fastapi.responses import JSONResponse

        return JSONResponse({"detail": "Not Found"}, status_code=404)

    if not app.state.mcp_service_enabled:

        @app.api_route(
            "/mcp/service/{service_id}",
            methods=["GET", "POST", "DELETE", "PUT", "PATCH", "OPTIONS", "HEAD"],
            include_in_schema=False,
        )
        @app.api_route(
            "/mcp/service/{service_id}/{child_path:path}",
            methods=["GET", "POST", "DELETE", "PUT", "PATCH", "OPTIONS", "HEAD"],
            include_in_schema=False,
        )
        async def disabled_service_route(service_id: str, child_path: str = ""):
            from fastapi.responses import JSONResponse

            return JSONResponse({"detail": "Not Found"}, status_code=404)

    # /admin 快捷入口 → 静态管理后台（避免访问 /admin 或 /admin/index.html 得到 404）
    from fastapi.responses import RedirectResponse, Response

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/", include_in_schema=False)
    @app.get("/admin/index.html", include_in_schema=False)
    async def admin_entry_redirect():
        return RedirectResponse(url="/admin/ui/", status_code=307)

    @app.get("/tenant", include_in_schema=False)
    @app.get("/tenant/", include_in_schema=False)
    @app.get("/tenant/index.html", include_in_schema=False)
    async def tenant_entry_redirect():
        return RedirectResponse(url="/tenant/ui/", status_code=307)

    # 企微可信域名校验文件：根路径 /xxx.txt 公开可访问（反代到本服务后企微可拉取）
    from .domain_verify import get_verify_file, is_safe_verify_filename

    @app.get("/{verify_filename}", include_in_schema=False)
    async def serve_domain_verify_file(verify_filename: str):
        # 仅放行安全文件名；避免吞掉 /health /mcp /admin 等（这些有更具体路由优先匹配）
        if not is_safe_verify_filename(verify_filename):
            from fastapi import HTTPException

            raise HTTPException(404, "Not Found")
        item = get_verify_file(verify_filename)
        if not item:
            from fastapi import HTTPException

            raise HTTPException(404, "Not Found")
        return Response(
            content=item["content"],
            media_type=item.get("content_type") or "text/plain; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )

    # 管理后台前端静态文件（构建产物 app/static/dist）—— 必须在 mcp_glyph 之前注册
    import os
    from fastapi.staticfiles import StaticFiles

    dist_dir = os.path.join(os.path.dirname(__file__), "static", "dist")
    if os.path.isdir(dist_dir):
        app.mount(
            "/admin/ui", StaticFiles(directory=dist_dir, html=True), name="admin-ui"
        )
        app.mount(
            "/tenant/ui", StaticFiles(directory=dist_dir, html=True), name="tenant-ui"
        )

    # Each route reaches the same connection-aware gateway, but authentication
    # happens before it can create or dispatch a protocol session.
    from starlette.applications import Starlette
    from starlette.routing import Mount

    def mcp_subapp(*, legacy: bool) -> Starlette:
        return Starlette(
            routes=[Mount("/", app=gateway)],
            middleware=[
                Middleware(
                    BearerTokenMiddleware, resolver=gateway.resolver, legacy=legacy
                ),
                Middleware(McpProtocolAuditMiddleware),
                Middleware(
                    CORSMiddleware,
                    allow_origins=["*"],
                    allow_methods=["GET", "POST", "DELETE"],
                    allow_headers=["*"],
                    expose_headers=["Mcp-Session-Id"],
                ),
            ],
        )

    dynamic_mcp_app = mcp_subapp(legacy=False)
    legacy_mcp_app = mcp_subapp(legacy=True)

    service_mcp_app = Starlette(
        routes=[Mount("/", app=service_gateway)],
        middleware=[
            Middleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_methods=["GET", "POST", "DELETE"],
                allow_headers=["*"],
                expose_headers=["Mcp-Session-Id"],
            )
        ],
    )

    # The parameterized mount must come first so a valid bearer token is bound
    # to the exact path connection ID before MCP handling.  `/mcp` remains for
    # callers using the Task 1 default legacy WeCom connection.
    if app.state.mcp_service_enabled:
        app.mount("/mcp/service/{service_id}", service_mcp_app)
    app.mount("/mcp/{connection_id}", dynamic_mcp_app)
    app.mount("/mcp", legacy_mcp_app)

    logger.info(
        "MCP Gateway mounted legacy_service_enabled=%s at connection and compatibility routes",
        app.state.mcp_service_enabled,
    )
    return app


async def _sync_job_async(orchestrator: SyncOrchestrator | None = None):
    """Legacy WeCom scheduler entrypoint delegated to connection-scoped sync."""
    settings = get_settings()
    if getattr(settings, "wecom_use_mock", False):
        logger.debug("mock 模式，跳过真实同步")
        return
    loop = asyncio.get_running_loop()
    selected_orchestrator = orchestrator or connection_sync_orchestrator
    try:
        await loop.run_in_executor(
            None,
            lambda: asyncio.run(selected_orchestrator.run_scheduled()),
        )
    except Exception:  # noqa: BLE001
        logger.error("Connection sync job failed code=sync_job_failed")


async def _cleanup_logs_job_async():
    """在线程池执行 MCP 日志保留清理，避免阻塞事件循环。"""
    from .mcp_log_store import cleanup_expired_logs

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, cleanup_expired_logs)
    except Exception as exc:  # noqa: BLE001
        logger.error("MCP 日志清理异常 type=%s", type(exc).__name__)


def _build_service_cache_invalidator(
    gateway: Any, loop: asyncio.AbstractEventLoop
) -> Callable[[str], None]:
    async def invalidate(service_id: str) -> None:
        lock = getattr(gateway, "_manager_lock", None)
        if lock is None:
            return
        async with lock:
            entries = getattr(gateway, "_entries", None)
            if not isinstance(entries, dict):
                return
            for key in tuple(entries):
                if isinstance(key, tuple) and key and key[0] == service_id:
                    entries.pop(key, None)

    def invalidate_from_any_thread(service_id: str) -> None:
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is loop:
            loop.create_task(invalidate(service_id))
            return
        asyncio.run_coroutine_threadsafe(invalidate(service_id), loop).result()

    return invalidate_from_any_thread


@contextlib.asynccontextmanager
async def lifespan(app):
    global _scheduler
    settings = get_settings()

    # 1. 启动迁移必须先于 MCP 会话、租户加载和调度器；失败直接阻止启动。
    db.run_startup_migrations()
    app_state = getattr(app, "state", None)
    gateway = getattr(app_state, "mcp_gateway", mcp_gateway)
    service_session_manager = getattr(
        app_state, "mcp_service_gateway", service_gateway
    )
    service_enabled = bool(
        getattr(
            app_state,
            "mcp_service_enabled",
            getattr(settings, "mcp_service_enabled", False),
        )
    )
    sync_orchestrator = getattr(
        app_state, "connection_sync_orchestrator", connection_sync_orchestrator
    )
    gateway_registry = getattr(
        getattr(gateway, "_runtime", None), "_registry", connector_registry
    )
    if app_state is not None:
        configure_trusted_connectors(registry=gateway_registry)
    acquire_audit_writer()
    audit_acquired = True
    audit_released = False

    async def release_audit_once() -> None:
        nonlocal audit_released
        if audit_released or not audit_acquired:
            return
        for _attempt in range(2):
            try:
                flushed = await asyncio.to_thread(release_audit_writer, 2.0)
                audit_released = True
                if not flushed:
                    logger.warning("MCP audit shutdown incomplete type=TimeoutError")
                return
            except Exception as exc:
                logger.warning(
                    "MCP audit shutdown failed type=%s",
                    type(exc).__name__,
                )

    # 2. Connection-scoped MCP session manager cache
    # The gateway owns a cache of low-level Streamable HTTP session managers;
    # retain this lifecycle name for the established startup ordering contract.
    session_manager = gateway
    invalidate_service_cache = _build_service_cache_invalidator(
        service_session_manager,
        asyncio.get_running_loop(),
    )

    if service_enabled:
        mcp_service_store.register_service_cache_invalidator(
            invalidate_service_cache
        )
    try:
        async with contextlib.AsyncExitStack() as stack:
            await stack.enter_async_context(session_manager.run())
            if service_enabled:
                await stack.enter_async_context(service_session_manager.run())
            scheduler = None
            try:
                # 3. APScheduler 定时同步
                scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
                _scheduler = scheduler
                # 间隔（分钟）；汇报、审批分别用各自配置，这里合并为同步轮次
                min_interval = max(
                    1,
                    min(
                        settings.sync_interval_report_min,
                        settings.sync_interval_approval_min,
                    ),
                )
                # 注意：不设 next_run_time=None（会让任务不自动调度），
                # 让 APScheduler 按 trigger 自动计算 next_run_time（启动后 min_interval 分钟触发首次）
                scheduler.add_job(
                    _sync_job_async,
                    IntervalTrigger(minutes=min_interval),
                    id="wecom_sync",
                    kwargs={"orchestrator": sync_orchestrator},
                    max_instances=1,
                    coalesce=True,
                )
                scheduler.add_job(
                    _cleanup_logs_job_async,
                    CronTrigger(hour=3, minute=17, timezone="Asia/Shanghai"),
                    id="mcp_log_cleanup",
                    max_instances=1,
                    coalesce=True,
                )
                scheduler.start()
                logger.info(
                    "同步调度已启动，间隔=%s 分钟（一期 tenant1）", min_interval
                )

                # 额外：启动后立即跑一次首次同步（独立后台任务，不依赖调度器）
                # 这样既快响应（立即拉一次），又不干扰调度器的周期触发
                asyncio.create_task(_sync_job_async(sync_orchestrator))
                yield
            finally:
                if scheduler is not None:
                    scheduler.shutdown(wait=False)
                    logger.info("同步调度已停止")
                    if _scheduler is scheduler:
                        _scheduler = None
    finally:
        if service_enabled:
            mcp_service_store.unregister_service_cache_invalidator(
                invalidate_service_cache
            )
        await release_audit_once()


app = create_app()
app.router.lifespan_context = lifespan


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.app_env == "dev",
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    main()
