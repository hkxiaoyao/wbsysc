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
import contextlib
import logging

import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

from . import db
from .auth import BearerTokenMiddleware
from .config import get_settings
from .mcp_audit import (
    McpProtocolAuditMiddleware,
    acquire_audit_writer,
    release_audit_writer,
)
from .mcp_gateway import ConnectionMcpGateway
from .admin import router as admin_router
from .mcp_logs_admin import router as mcp_logs_admin_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("wecom-gateway")

# 全局调度器，lifespan 启动/关闭
_scheduler: AsyncIOScheduler | None = None
mcp_gateway = ConnectionMcpGateway()


def create_app(*, gateway: ConnectionMcpGateway | None = None) -> FastAPI:
    settings = get_settings()
    gateway = gateway or mcp_gateway
    app = FastAPI(title="企微数据中转 MCP Gateway", version="0.1.0")
    app.state.mcp_gateway = gateway

    # WorkBuddy 等客户端 POST /mcp（无尾斜杠）。Starlette Mount("/mcp") 时
    # 子应用拿到 path=""，匹配不到 FastMCP 的 "/"，会 405。进入路由前补上斜杠。
    @app.middleware("http")
    async def _mcp_trailing_slash(request, call_next):
        path = request.scope.get("path")
        if path == "/mcp":
            request.scope["path"] = "/mcp/"
        elif isinstance(path, str) and path.startswith("/mcp/") and path.count("/") == 2:
            # A parameterized Mount needs its terminal slash to win over the
            # legacy `/mcp` catch-all mount for `/mcp/{connection_id}`.
            request.scope["path"] = f"{path}/"
        return await call_next(request)

    # 管理后台 API（独立路由，不经 MCP 鉴权，自带 session 校验）
    app.include_router(admin_router)
    app.include_router(mcp_logs_admin_router)

    # 健康检查（鉴权白名单）
    @app.get("/health")
    async def health() -> dict:
        return {
            "status": "ok",
            "env": settings.app_env,
            "mock": settings.wecom_use_mock,
            "scheduler": _scheduler.running if _scheduler else False,
        }

    # /admin 快捷入口 → 静态管理后台（避免访问 /admin 或 /admin/index.html 得到 404）
    from fastapi.responses import RedirectResponse, Response

    @app.get("/admin", include_in_schema=False)
    @app.get("/admin/", include_in_schema=False)
    @app.get("/admin/index.html", include_in_schema=False)
    async def admin_entry_redirect():
        return RedirectResponse(url="/admin/ui/", status_code=307)

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
        app.mount("/admin/ui", StaticFiles(directory=dist_dir, html=True), name="admin-ui")

    # Each route reaches the same connection-aware gateway, but authentication
    # happens before it can create or dispatch a protocol session.
    from starlette.applications import Starlette
    from starlette.routing import Mount

    def mcp_subapp(*, legacy: bool) -> Starlette:
        return Starlette(
            routes=[Mount("/", app=gateway)],
            middleware=[
                Middleware(BearerTokenMiddleware, resolver=gateway.resolver, legacy=legacy),
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

    # The parameterized mount must come first so a valid bearer token is bound
    # to the exact path connection ID before MCP handling.  `/mcp` remains for
    # callers using the Task 1 default legacy WeCom connection.
    app.mount("/mcp/{connection_id}", dynamic_mcp_app)
    app.mount("/mcp", legacy_mcp_app)

    logger.info("MCP Gateway mounted at /mcp/{connection_id} and legacy /mcp")
    return app


async def _sync_job_async():
    """同步任务异步包装：遍历所有启用租户，线程池执行不阻塞事件循环"""
    settings = get_settings()
    if settings.wecom_use_mock:
        logger.debug("mock 模式，跳过真实同步")
        return
    from .wecom.dispatch import run_sync_all
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, lambda: run_sync_all())
    except Exception as e:  # noqa: BLE001
        logger.error("同步任务异常: %s", e)


async def _cleanup_logs_job_async():
    """在线程池执行 MCP 日志保留清理，避免阻塞事件循环。"""
    from .mcp_log_store import cleanup_expired_logs

    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(None, cleanup_expired_logs)
    except Exception as exc:  # noqa: BLE001
        logger.error("MCP 日志清理异常 type=%s", type(exc).__name__)


@contextlib.asynccontextmanager
async def lifespan(app):
    global _scheduler
    settings = get_settings()

    # 1. 启动迁移必须先于 MCP 会话、租户加载和调度器；失败直接阻止启动。
    db.run_startup_migrations()
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
    gateway = getattr(getattr(app, "state", None), "mcp_gateway", mcp_gateway)
    # The gateway owns a cache of low-level Streamable HTTP session managers;
    # retain this lifecycle name for the established startup ordering contract.
    session_manager = gateway
    try:
        async with session_manager.run():
            scheduler = None
            try:
                # 3. APScheduler 定时同步
                scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
                _scheduler = scheduler
                # 间隔（分钟）；汇报、审批分别用各自配置，这里合并为同步轮次
                min_interval = max(1, min(
                    settings.sync_interval_report_min,
                    settings.sync_interval_approval_min,
                ))
                # 注意：不设 next_run_time=None（会让任务不自动调度），
                # 让 APScheduler 按 trigger 自动计算 next_run_time（启动后 min_interval 分钟触发首次）
                scheduler.add_job(
                    _sync_job_async,
                    IntervalTrigger(minutes=min_interval),
                    id="wecom_sync",
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
                logger.info("同步调度已启动，间隔=%s 分钟（一期 tenant1）", min_interval)

                # 额外：启动后立即跑一次首次同步（独立后台任务，不依赖调度器）
                # 这样既快响应（立即拉一次），又不干扰调度器的周期触发
                asyncio.create_task(_sync_job_async())
                yield
            finally:
                if scheduler is not None:
                    scheduler.shutdown(wait=False)
                    logger.info("同步调度已停止")
                    if _scheduler is scheduler:
                        _scheduler = None
    finally:
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
