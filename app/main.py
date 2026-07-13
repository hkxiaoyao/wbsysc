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
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware

from .auth import BearerTokenMiddleware
from .config import get_settings
from .mcp_server import mcp
from .admin import router as admin_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("wecom-gateway")

# 全局调度器，lifespan 启动/关闭
_scheduler: AsyncIOScheduler | None = None


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="企微数据中转 MCP Gateway", version="0.1.0")

    # WorkBuddy 等客户端 POST /mcp（无尾斜杠）。Starlette Mount("/mcp") 时
    # 子应用拿到 path=""，匹配不到 FastMCP 的 "/"，会 405。进入路由前补上斜杠。
    @app.middleware("http")
    async def _mcp_trailing_slash(request, call_next):
        if request.scope.get("path") == "/mcp":
            request.scope["path"] = "/mcp/"
        return await call_next(request)

    # 管理后台 API（独立路由，不经 MCP 鉴权，自带 session 校验）
    app.include_router(admin_router)

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

    # MCP Streamable HTTP 子 app（内部路径已设为 "/"，见 mcp_server.py）
    mcp_app = mcp.streamable_http_app()

    from starlette.applications import Starlette
    from starlette.routing import Mount

    mcp_glyph = Starlette(
        routes=[Mount("/", app=mcp_app)],
        middleware=[
            Middleware(BearerTokenMiddleware),
            Middleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_methods=["GET", "POST", "DELETE"],
                allow_headers=["*"],
                expose_headers=["Mcp-Session-Id"],
            ),
        ],
    )
    # 关键：只挂 /mcp，不挂根 /（避免吞掉 /admin/ui 导致 401）。
    # 最终对外 endpoint = /mcp（兼容 WorkBuddy streamableHttp + 后台复制的配置）
    app.mount("/mcp", mcp_glyph)

    logger.info("MCP Gateway 挂载于 /mcp (Streamable HTTP)，含 Bearer Token 鉴权")
    logger.info("已注册工具: %s", list(mcp._tool_manager._tools.keys()))  # noqa: SLF001
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


@contextlib.asynccontextmanager
async def lifespan(app):
    global _scheduler
    settings = get_settings()

    # 1. MCP 会话管理器
    async with mcp.session_manager.run():
        # 2. APScheduler 定时同步
        _scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")
        # 间隔（分钟）；汇报、审批分别用各自配置，这里合并为同步轮次
        min_interval = max(1, min(
            settings.sync_interval_report_min,
            settings.sync_interval_approval_min,
        ))
        # 注意：不设 next_run_time=None（会让任务不自动调度），
        # 让 APScheduler 按 trigger 自动计算 next_run_time（启动后 min_interval 分钟触发首次）
        _scheduler.add_job(
            _sync_job_async,
            IntervalTrigger(minutes=min_interval),
            id="wecom_sync",
            max_instances=1,
            coalesce=True,
        )
        _scheduler.start()
        logger.info("同步调度已启动，间隔=%s 分钟（一期 tenant1）", min_interval)

        # 额外：启动后立即跑一次首次同步（独立后台任务，不依赖调度器）
        # 这样既快响应（立即拉一次），又不干扰调度器的周期触发
        asyncio.create_task(_sync_job_async())

        try:
            yield
        finally:
            _scheduler.shutdown(wait=False)
            logger.info("同步调度已停止")


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