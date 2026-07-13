"""
MCP Server - 暴露给 workbuddy 的 tools（多租户版）
- mock 模式：返回脱敏 mock 数据
- 真实模式：按当前 token 绑定的租户 schema 读库（强制隔离）
- 调用写审计日志到对应租户 schema
"""
from __future__ import annotations

import json
import time

from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .auth import current_ctx, require_tenant
from .config import get_settings
from .wecom.mock import (
    MOCK_APPROVAL_LIST,
    MOCK_REPORT_LIST,
    MOCK_SMARTTABLE_RECORDS,
)


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
        from . import db
        ctx = current_ctx()
        db.log_audit(ctx.schema_name, tool, target, params[:500], status, cost)
    except Exception:
        pass


# ============= 汇报类 =============
@mcp.tool()
def wecom_list_reports(starttime: int, endtime: int, limit: int = 100) -> str:
    """列出企业微信汇报记录（读已落库数据，按租户 schema 隔离）

    Args:
        starttime/endtime: Unix 秒; limit: ≤100
    """
    t0 = time.time()
    tenant = require_tenant()

    if _use_mock():
        return _ok({"tenant": tenant, "source": "mock",
                    "count": len(MOCK_REPORT_LIST), "records": MOCK_REPORT_LIST})

    from . import db
    schema = current_ctx().schema_name
    rows = db.query_reports_by_window(schema, starttime, endtime, limit)
    records = [
        {"journaluuid": r["journaluuid"], "template_id": r["template_id"],
         "template_name": r["template_name"], "report_time": r["report_time"],
         "submitter": r["submitter_userid"]}
        for r in rows
    ]
    _audit("wecom_list_reports", "", f"{starttime}-{endtime}#{limit}", "ok",
           int((time.time() - t0) * 1000))
    return _ok({"tenant": tenant, "source": "db", "count": len(records), "records": records})


@mcp.tool()
def wecom_get_report(journaluuid: str) -> str:
    """获取汇报详情（读该租户 schema 内的完整 detail_json）"""
    t0 = time.time()
    tenant = require_tenant()

    if _use_mock():
        from .wecom.mock import MOCK_REPORT_DETAIL
        return _ok(MOCK_REPORT_DETAIL.get(journaluuid, {"errcode": 404, "errmsg": "不存在"}))

    from . import db
    schema = current_ctx().schema_name
    detail = db.get_report_detail(schema, journaluuid)
    status = "ok" if detail else "notfound"
    _audit("wecom_get_report", journaluuid, journaluuid, status, int((time.time() - t0) * 1000))
    if not detail:
        return _ok({"errcode": 404, "errmsg": "汇报单号不存在"})
    return _ok({"source": "db", "detail": detail})


# ============= 审批类 =============
@mcp.tool()
def wecom_list_approvals(starttime: int, endtime: int, limit: int = 100) -> str:
    """列出企业微信审批记录（读已落库数据，按租户 schema 隔离）"""
    t0 = time.time()
    tenant = require_tenant()

    if _use_mock():
        return _ok({"tenant": tenant, "source": "mock",
                    "count": len(MOCK_APPROVAL_LIST), "records": MOCK_APPROVAL_LIST})

    from . import db
    schema = current_ctx().schema_name
    rows = db.query_approvals_by_window(schema, starttime, endtime, limit)
    records = [
        {"sp_no": r["sp_no"], "sp_name": r["sp_name"], "sp_status": r["sp_status"],
         "template_id": r["template_id"], "apply_time": r["apply_time"],
         "applyer": r["applyer_userid"]}
        for r in rows
    ]
    _audit("wecom_list_approvals", "", f"{starttime}-{endtime}#{limit}", "ok",
           int((time.time() - t0) * 1000))
    return _ok({"tenant": tenant, "source": "db", "count": len(records), "records": records})


@mcp.tool()
def wecom_get_approval_detail(sp_no: str) -> str:
    """获取审批详情（读该租户 schema 内的完整 detail_json）"""
    t0 = time.time()
    tenant = require_tenant()

    if _use_mock():
        from .wecom.mock import MOCK_APPROVAL_DETAIL
        return _ok(MOCK_APPROVAL_DETAIL.get(sp_no, {"errcode": 404, "errmsg": "不存在"}))

    from . import db
    schema = current_ctx().schema_name
    detail = db.get_approval_detail(schema, sp_no)
    status = "ok" if detail else "notfound"
    _audit("wecom_get_approval_detail", sp_no, sp_no, status, int((time.time() - t0) * 1000))
    if not detail:
        return _ok({"errcode": 404, "errmsg": "审批单号不存在"})
    return _ok({"source": "db", "detail": detail})


# ============= 打卡类 =============
@mcp.tool()
def wecom_list_checkins(starttime: int, endtime: int, limit: int = 100) -> str:
    """列出企业微信打卡记录（读已落库数据，按租户 schema 隔离）

    Args:
        starttime/endtime: Unix 秒（打卡时间范围）; limit: ≤100
    Returns:
        JSON: { records: [...], count }
    """
    t0 = time.time()
    tenant = require_tenant()

    if _use_mock():
        return _ok({"tenant": tenant, "source": "mock",
                    "count": 0, "records": []})

    from . import db
    schema = current_ctx().schema_name
    rows = db.query_checkins_by_window(schema, starttime, endtime, limit)
    records = [
        {"userid": r["userid"], "checkin_type": r["checkin_type"],
         "checkin_time": r["checkin_time"], "exception_type": r["exception_type"],
         "location_title": r["location_title"], "group_name": r["group_name"]}
        for r in rows
    ]
    _audit("wecom_list_checkins", "", f"{starttime}-{endtime}#{limit}", "ok",
           int((time.time() - t0) * 1000))
    return _ok({"tenant": tenant, "source": "db", "count": len(records), "records": records})


# ============= 智能表格类（一期搁置）=============
@mcp.tool()
def wecom_list_smart_table_records(docid: str, sheet_id: str, limit: int = 1000) -> str:
    """查询智能表格记录（一期暂搁置：企微docid限制）"""
    tenant = require_tenant()
    return _ok({"tenant": tenant, "source": "mock",
                 "note": "智能表格读取一期暂搁置",
                 "records": MOCK_SMARTTABLE_RECORDS[:limit]})


def list_tool_names():
    return list(mcp._tool_manager._tools.keys())  # noqa: SLF001