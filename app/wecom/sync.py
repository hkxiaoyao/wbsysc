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
MIN_CAPPED_WINDOW = 60
MAX_PAGES_PER_WINDOW = 1000


def _cursor_as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _validate_next_cursor(current, next_cursor, seen_cursors: set) -> None:
    if next_cursor in seen_cursors:
        raise RuntimeError("拉取汇报失败：分页游标重复")
    current_number = _cursor_as_int(current)
    next_number = _cursor_as_int(next_cursor)
    if (
        current_number is not None
        and next_number is not None
        and next_number <= current_number
    ):
        raise RuntimeError("拉取汇报失败：分页游标倒退")


def _fetch_report_identifiers(
    corpid: str,
    secret: str,
    starttime: int,
    endtime: int,
    limit: int,
    filters,
) -> list[str]:
    """完整分页读取单个时间窗，按 API 顺序去重。"""
    seen: set[str] = set()
    result: list[str] = []
    cursor = 0
    seen_cursors = {cursor}
    page_count = 0
    while True:
        if page_count >= MAX_PAGES_PER_WINDOW:
            raise RuntimeError("拉取汇报失败：分页超过安全上限")
        page_count += 1
        resp = api.list_report_records(
            corpid, secret, starttime, endtime, cursor, limit, filters
        )
        if resp.get("errcode") not in (0, None):
            raise RuntimeError(
                f"拉取汇报失败 [{resp.get('errcode')}] {resp.get('errmsg')}"
            )
        uuids = resp.get("journaluuid_list", []) or []
        for identifier in uuids:
            if identifier not in seen:
                seen.add(identifier)
                result.append(identifier)
        next_cursor = resp.get("next_cursor", 0)
        if resp.get("endflag") == 1:
            break
        if not uuids and not next_cursor:
            break
        if not next_cursor:
            break
        _validate_next_cursor(cursor, next_cursor, seen_cursors)
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return result


def _sync_reports_capped(
    corpid: str,
    secret: str,
    starttime: int,
    endtime: int,
    limit: int,
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
            for identifier in _fetch_report_identifiers(
                corpid, secret, window_start, window_end, limit, filters
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
                "汇报候选在最小时间窗内超限，按 API 顺序截断 "
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
        segment_start = max(starttime, segment_end - MONTH)
        result.extend(
            collect_window(
                segment_start,
                segment_end,
                max_records - len(result),
            )
        )
        segment_end = segment_start
    return result[:max_records]


def sync_reports_window(
    corpid: str, secret: str,
    starttime: int, endtime: int,
    limit: int = 100, template_id: str | None = None,
    max_records: int | None = None,
) -> List[str]:
    """拉取 [starttime, endtime] 区间内所有汇报单号（自动分段+分页）"""
    filters = [{"key": "template_id", "value": template_id}] if template_id else None
    if max_records is not None:
        result = _sync_reports_capped(
            corpid,
            secret,
            starttime,
            endtime,
            limit,
            filters,
            max_records,
        )
        if not result:
            logger.warning(
                "汇报列表为空 journaluuid_list_len=0 window=[%s,%s]（检查权限/可见范围/时间窗/游标）",
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
        seg_end = min(seg_start + MONTH, endtime)
        segments.append((seg_start, seg_end))
        seg_start = seg_end

    for seg_start, seg_end in segments:
        cursor = 0
        seen_cursors = {cursor}
        page_count = 0
        while True:
            if page_count >= MAX_PAGES_PER_WINDOW:
                raise RuntimeError("拉取汇报失败：分页超过安全上限")
            page_count += 1
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
            # 官方：endflag=1 表示已无数据；空列表且无 next_cursor 也结束
            # 不要仅因 uuids 为空就 break（兼容异常分页）
            next_cursor = resp.get("next_cursor", 0)
            if resp.get("endflag") == 1:
                break
            if not uuids and not next_cursor:
                break
            if not next_cursor:
                break
            _validate_next_cursor(cursor, next_cursor, seen_cursors)
            seen_cursors.add(next_cursor)
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
