"""
审批同步逻辑 - 多租户版（按 corpid/secret 调企微，按 schema 落库）
- 跨度超31天自动分段
- 游标 new_cursor 首次传空串
"""
from __future__ import annotations

from typing import List

from . import client as api

SPAN = 31 * 86400


def sync_approvals_window(
    corpid: str, secret: str,
    starttime: int, endtime: int,
    size: int = 100, template_id: str | None = None, creator: str | None = None,
) -> List[str]:
    filters = []
    if template_id:
        filters.append({"key": "template_id", "value": template_id})
    if creator:
        filters.append({"key": "creator", "value": creator})
    flt = filters or None

    seen: set[str] = set()
    result: list[str] = []
    seg_start = starttime
    while seg_start < endtime:
        seg_end = min(seg_start + SPAN, endtime)
        cursor = ""
        while True:
            resp = api.list_approvals(corpid, secret, seg_start, seg_end, cursor, size, flt)
            if resp.get("errcode") not in (0, None):
                raise RuntimeError(f"拉取审批失败 [{resp.get('errcode')}] {resp.get('errmsg')}")
            sp_list = resp.get("sp_no_list", []) or []
            for sp in sp_list:
                if sp not in seen:
                    seen.add(sp)
                    result.append(sp)
            next_cursor = resp.get("new_next_cursor", "")
            if not sp_list or not next_cursor:
                break
            cursor = next_cursor
        seg_start = seg_end
    return result


def fetch_approval_detail(corpid: str, secret: str, sp_no: str) -> dict:
    resp = api.get_approval_detail(corpid, secret, sp_no)
    if resp.get("errcode") not in (0, None):
        return {"errcode": resp.get("errcode"), "errmsg": resp.get("errmsg")}
    return resp.get("info", resp)