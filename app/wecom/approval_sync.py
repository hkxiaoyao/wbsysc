"""
审批同步逻辑 - 多租户版（按 corpid/secret 调企微，按 schema 落库）
- 跨度超31天自动分段
- 游标 new_cursor 首次传空串
"""
from __future__ import annotations

import logging
from typing import List

from . import client as api

logger = logging.getLogger("wecom-sync")

SPAN = 31 * 86400
MIN_CAPPED_WINDOW = 60


def _fetch_approval_identifiers(
    corpid: str,
    secret: str,
    starttime: int,
    endtime: int,
    size: int,
    filters,
) -> list[str]:
    """完整分页读取单个时间窗，按 API 顺序去重。"""
    seen: set[str] = set()
    result: list[str] = []
    cursor = ""
    seen_cursors = {cursor}
    while True:
        resp = api.list_approvals(
            corpid, secret, starttime, endtime, cursor, size, filters
        )
        if resp.get("errcode") not in (0, None):
            raise RuntimeError(
                f"拉取审批失败 [{resp.get('errcode')}] {resp.get('errmsg')}"
            )
        identifiers = resp.get("sp_no_list", []) or []
        for identifier in identifiers:
            if identifier not in seen:
                seen.add(identifier)
                result.append(identifier)
        next_cursor = resp.get("new_next_cursor", "")
        if not identifiers or not next_cursor:
            break
        if next_cursor in seen_cursors:
            raise RuntimeError(f"拉取审批失败：分页游标重复 {next_cursor}")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return result


def _sync_approvals_capped(
    corpid: str,
    secret: str,
    starttime: int,
    endtime: int,
    size: int,
    filters,
    max_records: int,
) -> list[str]:
    if max_records <= 0:
        return []

    seen: set[str] = set()

    def collect_window(window_start: int, window_end: int, budget: int) -> list[str]:
        if budget <= 0 or window_start >= window_end:
            return []
        candidates = [
            identifier
            for identifier in _fetch_approval_identifiers(
                corpid, secret, window_start, window_end, size, filters
            )
            if identifier not in seen
        ]
        if len(candidates) <= budget:
            seen.update(candidates)
            return candidates

        span = window_end - window_start
        if span <= MIN_CAPPED_WINDOW:
            selected = candidates[:budget]
            seen.update(selected)
            logger.warning(
                "审批候选在最小时间窗内超限，按 API 顺序截断 "
                "window=[%s,%s] candidates=%s budget=%s",
                window_start,
                window_end,
                len(candidates),
                budget,
            )
            return selected

        midpoint = window_start + span // 2
        newer = collect_window(midpoint, window_end, budget)
        older = collect_window(window_start, midpoint, budget - len(newer))
        return newer + older

    result: list[str] = []
    segment_end = endtime
    while segment_end > starttime and len(result) < max_records:
        segment_start = max(starttime, segment_end - SPAN)
        result.extend(
            collect_window(
                segment_start,
                segment_end,
                max_records - len(result),
            )
        )
        segment_end = segment_start
    return result[:max_records]


def sync_approvals_window(
    corpid: str, secret: str,
    starttime: int, endtime: int,
    size: int = 100, template_id: str | None = None, creator: str | None = None,
    max_records: int | None = None,
) -> List[str]:
    filters = []
    if template_id:
        filters.append({"key": "template_id", "value": template_id})
    if creator:
        filters.append({"key": "creator", "value": creator})
    flt = filters or None
    if max_records is not None:
        result = _sync_approvals_capped(
            corpid,
            secret,
            starttime,
            endtime,
            size,
            flt,
            max_records,
        )
        if not result:
            logger.warning(
                "审批列表为空 sp_no_list_len=0 window=[%s,%s]（检查权限/可见范围/时间窗/游标）",
                starttime,
                endtime,
            )
        return result

    seen: set[str] = set()
    result: list[str] = []
    first_resp_logged = False
    segments: list[tuple[int, int]] = []
    seg_start = starttime
    while seg_start < endtime:
        seg_end = min(seg_start + SPAN, endtime)
        segments.append((seg_start, seg_end))
        seg_start = seg_end

    for seg_start, seg_end in segments:
        cursor = ""
        while True:
            resp = api.list_approvals(corpid, secret, seg_start, seg_end, cursor, size, flt)
            if resp.get("errcode") not in (0, None):
                raise RuntimeError(f"拉取审批失败 [{resp.get('errcode')}] {resp.get('errmsg')}")
            sp_list = resp.get("sp_no_list", []) or []
            if not first_resp_logged:
                first_resp_logged = True
                logger.info(
                    "审批列表首包 errcode=%s errmsg=%s list_len=%s window=[%s,%s]",
                    resp.get("errcode", 0),
                    resp.get("errmsg", "ok"),
                    len(sp_list),
                    starttime,
                    endtime,
                )
            for sp in sp_list:
                if sp not in seen:
                    seen.add(sp)
                    result.append(sp)
            next_cursor = resp.get("new_next_cursor", "")
            if not sp_list or not next_cursor:
                break
            cursor = next_cursor
    if not result:
        logger.warning(
            "审批列表为空 sp_no_list_len=0 window=[%s,%s]（检查权限/可见范围/时间窗/游标）",
            starttime, endtime,
        )
    return result


def fetch_approval_detail(corpid: str, secret: str, sp_no: str) -> dict:
    resp = api.get_approval_detail(corpid, secret, sp_no)
    if resp.get("errcode") not in (0, None):
        return {"errcode": resp.get("errcode"), "errmsg": resp.get("errmsg")}
    return resp.get("info", resp)
