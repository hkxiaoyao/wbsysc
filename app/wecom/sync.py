"""
汇报同步逻辑 - 分段拉取 + 游标分页 + 幂等
- 跨度超1月自动按月切分（企微限制）
- 段内用 next_cursor 翻页，endflag=1 结束
- 返回去重后的 journaluuid 列表，供落库/拉详情
"""
from __future__ import annotations

from typing import List

from . import client as api   # 改为函数式调用，按租户传凭证

MONTH = 30 * 86400   # 企微汇报时间跨度上限


def sync_reports_window(
    corpid: str, secret: str,
    starttime: int, endtime: int,
    limit: int = 100, template_id: str | None = None,
) -> List[str]:
    """拉取 [starttime, endtime] 区间内所有汇报单号（自动分段+分页）"""
    filters = [{"key": "template_id", "value": template_id}] if template_id else None
    seen: set[str] = set()
    result: list[str] = []

    seg_start = starttime
    while seg_start < endtime:
        seg_end = min(seg_start + MONTH, endtime)
        cursor = 0
        while True:
            resp = api.list_report_records(corpid, secret, seg_start, seg_end, cursor, limit, filters)
            if resp.get("errcode") not in (0, None):
                raise RuntimeError(f"拉取汇报失败 [{resp.get('errcode')}] {resp.get('errmsg')}")
            uuids = resp.get("journaluuid_list", []) or []
            for u in uuids:
                if u not in seen:
                    seen.add(u)
                    result.append(u)
            if resp.get("endflag") == 1 or not uuids:
                break
            cursor = resp.get("next_cursor", 0)
            if not cursor:
                break
        seg_start = seg_end
    return result


def fetch_report_detail(corpid: str, secret: str, journaluuid: str) -> dict:
    resp = api.get_report_detail(corpid, secret, journaluuid)
    if resp.get("errcode") not in (0, None):
        return {"errcode": resp.get("errcode"), "errmsg": resp.get("errmsg")}
    return resp.get("info", resp)