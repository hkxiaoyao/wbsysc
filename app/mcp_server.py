"""
MCP Server - 暴露给 workbuddy 的 tools（多租户版）
- mock 模式：返回脱敏 mock 数据
- 真实模式：stored 读租户 schema，direct 实时请求企微
- 调用写入统一中心审计日志
"""
from __future__ import annotations

import json
import logging
import time

from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from . import data_access
from .auth import current_ctx, require_tenant
from .config import get_settings
from .mcp_audit import current_request_metadata, safe_summary, write_event
from .mcp_log_models import McpLogEvent
from .wecom.mock import (
    MOCK_APPROVAL_LIST,
    MOCK_REPORT_LIST,
    MOCK_SMARTTABLE_RECORDS,
)

logger = logging.getLogger(__name__)


def _build_transport_security() -> TransportSecuritySettings:
    """配置 MCP Host/Origin 校验。

    FastMCP 默认 host=127.0.0.1 时会自动只放行 localhost，反代域名会报
    Invalid Host header (421)。生产在 Nginx 后应显式放行对外域名，或关闭校验
    （已有 Bearer Token 鉴权）。
    """
    s = get_settings()
    hosts: list[str] = []
    origins: list[str] = []

    def _host_base(h: str) -> str:
        h = (h or "").strip().lower().rstrip(".")
        if h.endswith(":*"):
            h = h[:-2]
        if h.startswith("[") and "]" in h:
            return h[1:h.index("]")]
        # hostname 或 hostname:port
        return h.split(":")[0]

    def _is_local_host(h: str) -> bool:
        return _host_base(h) in ("127.0.0.1", "localhost", "::1")

    def _add_host(h: str) -> None:
        h = (h or "").strip().lower().rstrip(".")
        if not h:
            return
        if h not in hosts:
            hosts.append(h)
        # 兼容带端口的 Host 头
        if not h.endswith(":*") and ":" not in h.strip("[]"):
            star = f"{h}:*"
            if star not in hosts:
                hosts.append(star)
        base = _host_base(h)
        for scheme in ("https", "http"):
            o = f"{scheme}://{base}"
            if o not in origins:
                origins.append(o)
            star_o = f"{o}:*"
            if star_o not in origins:
                origins.append(star_o)

    # 环境变量显式白名单
    for part in (s.mcp_allowed_hosts or "").split(","):
        _add_host(part)

    # 从 MCP_BASE_URL 推导
    if s.mcp_base_url:
        try:
            p = urlparse(s.mcp_base_url)
            if p.hostname:
                _add_host(p.hostname if not p.port else f"{p.hostname}:{p.port}")
        except Exception:
            pass

    # 仅当配置了非本机域名时启用 Host 校验；否则关闭（反代+Bearer 默认可用）
    external = [h for h in hosts if not _is_local_host(h)]
    if not external:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)

    # 附带本机，方便本地联调
    for h in ("127.0.0.1", "localhost", "[::1]"):
        _add_host(h)

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


# streamable_http_path="/"：外层 main.py 会 mount 到 /mcp，
# 若这里仍用默认 "/mcp"，最终变成 /mcp/mcp，WorkBuddy POST /mcp 会 405。
# host 不用 127.0.0.1，避免 FastMCP 自动把 allowed_hosts 锁死为 localhost。
mcp = FastMCP(
    "wecom-mcp-gateway",
    streamable_http_path="/",
    host="0.0.0.0",
    transport_security=_build_transport_security(),
)


def _use_mock() -> bool:
    return get_settings().wecom_use_mock


def _ok(data) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


def _audit(tool, target, params, status, cost):
    try:
        ctx = current_ctx()
        metadata = current_request_metadata()
        write_event(
            McpLogEvent(
                tenant_id=ctx.tenant_id,
                category="tool",
                event_name=safe_summary(tool, 96),
                target=safe_summary(target, 256),
                params_summary=safe_summary(params, 512),
                result_status=status,
                cost_ms=max(0, int(cost)),
                request_id=metadata.get("request_id", ""),
                client_ip=metadata.get("client_ip", ""),
                http_method=metadata.get("http_method", ""),
            )
        )
    except Exception as exc:
        logger.warning("MCP tool audit failed type=%s", type(exc).__name__)


def _run_mock(tool, target, params, started_at, result):
    status = (
        "partial"
        if result.get("partial_count")
        else ("error" if result.get("errcode") else "ok")
    )
    _audit(tool, target, params, status, int((time.time() - started_at) * 1000))
    return _ok(result)


def _run_real(tool, target, params, started_at, call):
    ctx = current_ctx()
    try:
        result = call(ctx)
        status = (
            "partial"
            if result.get("partial_count")
            else ("error" if result.get("errcode") else "ok")
        )
    except data_access.PublicDataAccessError as exc:
        logger.warning(
            "MCP data access failed tool=%s: %s", tool, type(exc).__name__
        )
        result = {
            "tenant": ctx.tenant_id,
            "source": exc.source,
            "errcode": exc.errcode,
            "errmsg": exc.public_message,
        }
        status = "error"
    except Exception as exc:
        logger.warning(
            "MCP data access failed tool=%s: %s", tool, type(exc).__name__
        )
        result = {
            "tenant": ctx.tenant_id,
            "source": "wecom" if ctx.data_mode == "direct" else "db",
            "errcode": 502,
            "errmsg": "数据访问失败",
        }
        status = "error"
    _audit(tool, target, params, status, int((time.time() - started_at) * 1000))
    return _ok(result)


# ============= 汇报类 =============
@mcp.tool()
def wecom_list_reports(starttime: int, endtime: int, limit: int = 100) -> str:
    """列出企业微信汇报记录。

    stored 模式读取租户 schema；direct 模式实时请求企业微信。

    Args:
        starttime/endtime: Unix 秒; limit: ≤100
    """
    t0 = time.time()
    tenant = require_tenant()

    if _use_mock():
        return _run_mock(
            "wecom_list_reports", "", f"{starttime}-{endtime}#{limit}", t0,
            {"tenant": tenant, "source": "mock", "count": len(MOCK_REPORT_LIST),
             "records": MOCK_REPORT_LIST},
        )

    return _run_real(
        "wecom_list_reports", "", f"{starttime}-{endtime}#{limit}", t0,
        lambda ctx: data_access.list_reports(ctx, starttime, endtime, limit),
    )


@mcp.tool()
def wecom_get_report(journaluuid: str) -> str:
    """获取汇报详情；stored 读租户 schema，direct 实时请求企业微信。"""
    t0 = time.time()
    tenant = require_tenant()

    if _use_mock():
        from .wecom.mock import MOCK_REPORT_DETAIL
        return _run_mock(
            "wecom_get_report", journaluuid, journaluuid, t0,
            MOCK_REPORT_DETAIL.get(journaluuid, {"errcode": 404, "errmsg": "不存在"}),
        )

    return _run_real(
        "wecom_get_report", journaluuid, journaluuid, t0,
        lambda ctx: data_access.get_report(ctx, journaluuid),
    )


# ============= 审批类 =============
@mcp.tool()
def wecom_list_approvals(starttime: int, endtime: int, limit: int = 100) -> str:
    """列出审批记录；stored 读租户 schema，direct 实时请求企业微信。"""
    t0 = time.time()
    tenant = require_tenant()

    if _use_mock():
        return _run_mock(
            "wecom_list_approvals", "", f"{starttime}-{endtime}#{limit}", t0,
            {"tenant": tenant, "source": "mock", "count": len(MOCK_APPROVAL_LIST),
             "records": MOCK_APPROVAL_LIST},
        )

    return _run_real(
        "wecom_list_approvals", "", f"{starttime}-{endtime}#{limit}", t0,
        lambda ctx: data_access.list_approvals(ctx, starttime, endtime, limit),
    )


@mcp.tool()
def wecom_get_approval_detail(sp_no: str) -> str:
    """获取审批详情；stored 读租户 schema，direct 实时请求企业微信。"""
    t0 = time.time()
    tenant = require_tenant()

    if _use_mock():
        from .wecom.mock import MOCK_APPROVAL_DETAIL
        return _run_mock(
            "wecom_get_approval_detail", sp_no, sp_no, t0,
            MOCK_APPROVAL_DETAIL.get(sp_no, {"errcode": 404, "errmsg": "不存在"}),
        )

    return _run_real(
        "wecom_get_approval_detail", sp_no, sp_no, t0,
        lambda ctx: data_access.get_approval(ctx, sp_no),
    )


# ============= 打卡类 =============
@mcp.tool()
def wecom_list_checkins(starttime: int, endtime: int, limit: int = 100) -> str:
    """列出企业微信打卡记录。

    stored 模式读取租户 schema；direct 模式实时请求企业微信。

    Args:
        starttime/endtime: Unix 秒（打卡时间范围）; limit: ≤100
    Returns:
        JSON: { records: [...], count }
    """
    t0 = time.time()
    tenant = require_tenant()

    if _use_mock():
        return _run_mock(
            "wecom_list_checkins", "", f"{starttime}-{endtime}#{limit}", t0,
            {"tenant": tenant, "source": "mock", "count": 0, "records": []},
        )

    return _run_real(
        "wecom_list_checkins", "", f"{starttime}-{endtime}#{limit}", t0,
        lambda ctx: data_access.list_checkins(ctx, starttime, endtime, limit),
    )


# ============= 智能表格类（一期搁置）=============
@mcp.tool()
def wecom_list_smart_table_records(docid: str, sheet_id: str, limit: int = 1000) -> str:
    """查询智能表格记录（一期暂搁置：企微docid限制）"""
    t0 = time.time()
    tenant = require_tenant()
    return _run_mock(
        "wecom_list_smart_table_records", f"{docid}/{sheet_id}",
        f"{docid}#{sheet_id}#{limit}", t0,
        {"tenant": tenant, "source": "mock", "note": "智能表格读取一期暂搁置",
         "records": MOCK_SMARTTABLE_RECORDS[:limit]},
    )


def list_tool_names():
    return list(mcp._tool_manager._tools.keys())  # noqa: SLF001
