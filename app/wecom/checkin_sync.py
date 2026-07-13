"""
打卡同步逻辑 - 多租户版
- checkin/getcheckindata 无游标，靠 useridlist + 时间窗口
- 时间跨度≤30天 → 分段
- useridlist≤100/批 → 可批量传，但遇到 301021(人员不在可见范围) 会整批失败
  → 故采用【逐人拉取 + 容错】：单人失败记错继续，不影响其他人
- 一次返回 checkindata 数组，直接落库
- 幂等键：userid+checkin_time+checkin_type
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List

from . import client as api

logger = logging.getLogger("wecom-sync")

SPAN = 30 * 86400   # 企微打卡时间跨度上限


@dataclass
class CheckinFetchResult:
    records: list[dict]
    attempted: int
    failed: int
    errors: list[dict]


def fetch_checkin_records_with_stats(
    corpid: str, secret: str,
    starttime: int, endtime: int,
    useridlist: List[str],
    opencheckindatatype: int = 3,
) -> CheckinFetchResult:
    """逐人拉取打卡，并保留失败统计供直连读取判断完整性。"""
    records: list[dict] = []
    attempted = 0
    failed = 0
    errors: list[dict] = []

    seg_start = starttime
    while seg_start < endtime:
        seg_end = min(seg_start + SPAN, endtime)
        for uid in useridlist:
            attempted += 1
            try:
                resp = api.get_checkin_data(
                    corpid,
                    secret,
                    seg_start,
                    seg_end,
                    [uid],
                    opencheckindatatype,
                )
                errcode = resp.get("errcode")
                if errcode not in (0, None):
                    failed += 1
                    errmsg = str(resp.get("errmsg") or "打卡数据不可用")
                    error = {
                        "userid": uid,
                        "errcode": errcode,
                        "errmsg": errmsg,
                    }
                    errors.append(error)
                    logger.debug(
                        "打卡拉取 %s: [%s] %s",
                        uid,
                        errcode,
                        error["errmsg"][:40],
                    )
                    continue
                records.extend(resp.get("checkindata", []) or [])
            except Exception as exc:
                failed += 1
                errors.append({
                    "userid": uid,
                    "errcode": None,
                    "errmsg": str(exc),
                })
                logger.debug("打卡拉取异常 %s: %s", uid, exc)
        seg_start = seg_end

    return CheckinFetchResult(
        records=records,
        attempted=attempted,
        failed=failed,
        errors=errors,
    )


def fetch_checkin_records(
    corpid: str, secret: str,
    starttime: int, endtime: int,
    useridlist: List[str],
    opencheckindatatype: int = 3,
) -> list:
    """拉取 [start,end] + useridlist 的全部打卡记录（分段 + 逐人容错）

    策略：逐人拉取，单人失败(如301021不在可见范围)记错继续，不丢弃其他人数据。
    Returns:
        打卡记录列表
    """
    return fetch_checkin_records_with_stats(
        corpid,
        secret,
        starttime,
        endtime,
        useridlist,
        opencheckindatatype,
    ).records
