"""
汇报同步逻辑 - 分段拉取 + 游标分页 + 幂等
- 跨度超1月自动按月切分（企微限制）
- 段内用 next_cursor 翻页，endflag=1 结束
- 返回去重后的 journaluuid 列表，供落库/拉详情
"""
from __future__ import annotations

import logging
from typing import List

from . import client as api   # 改为函数式调用，按租户传凭证

logger = logging.getLogger("wecom-sync")

MONTH = 30 * 86400   # 企微汇报时间跨度上限


def sync_reports_window(
    corpid: str, secret: str,
    starttime: int, endtime: int,
    limit: int = 100, template_id: str | None = None,
    max_records: int | None = None,
) -> List[str]:
    """拉取 [starttime, endtime] 区间内所有汇报单号（自动分段+分页）"""
    filters = [{"key": "template_id", "value": template_id}] if template_id else None
    seen: set[str] = set()
    result: list[str] = []
    first_resp_logged = False

    segments: list[tuple[int, int]] = []
    if max_records is None:
        seg_start = starttime
        while seg_start < endtime:
            seg_end = min(seg_start + MONTH, endtime)
            segments.append((seg_start, seg_end))
            seg_start = seg_end
    else:
        seg_end = endtime
        while seg_end > starttime:
            seg_start = max(starttime, seg_end - MONTH)
            segments.append((seg_start, seg_end))
            seg_end = seg_start

    for seg_start, seg_end in segments:
        cursor = 0
        while True:
            resp = api.list_report_records(corpid, secret, seg_start, seg_end, cursor, limit, filters)
            if resp.get("errcode") not in (0, None):
                raise RuntimeError(f"拉取汇报失败 [{resp.get('errcode')}] {resp.get('errmsg')}")
            uuids = resp.get("journaluuid_list", []) or []
            if not first_resp_logged:
                first_resp_logged = True
                logger.info(
                    "汇报列表首包 errcode=%s errmsg=%s list_len=%s endflag=%s window=[%s,%s]",
                    resp.get("errcode", 0),
                    resp.get("errmsg", "ok"),
                    len(uuids),
                    resp.get("endflag"),
                    starttime,
                    endtime,
                )
            for u in uuids:
                if u not in seen:
                    seen.add(u)
                    result.append(u)
                    if max_records is not None and len(result) >= max_records:
                        return result
            # 官方：endflag=1 表示已无数据；空列表且无 next_cursor 也结束
            # 不要仅因 uuids 为空就 break（兼容异常分页）
            next_cursor = resp.get("next_cursor", 0)
            if resp.get("endflag") == 1:
                break
            if not uuids and not next_cursor:
                break
            if not next_cursor:
                break
            cursor = next_cursor

    if not result:
        logger.warning(
            "汇报列表为空 journaluuid_list_len=0 window=[%s,%s]（检查权限/可见范围/时间窗/游标）",
            starttime, endtime,
        )
    else:
        logger.info("汇报列表合计 unique=%s window=[%s,%s]", len(result), starttime, endtime)
    return result


def fetch_report_detail(corpid: str, secret: str, journaluuid: str) -> dict:
    resp = api.get_report_detail(corpid, secret, journaluuid)
    if resp.get("errcode") not in (0, None):
        return {"errcode": resp.get("errcode"), "errmsg": resp.get("errmsg")}
    return resp.get("info", resp)
