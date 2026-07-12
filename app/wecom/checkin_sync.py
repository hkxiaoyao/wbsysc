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
from typing import List

from . import client as api

logger = logging.getLogger("wecom-sync")

SPAN = 30 * 86400   # 企微打卡时间跨度上限


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
    result: list = []
    if not useridlist:
        return result

    seg_start = starttime
    while seg_start < endtime:
        seg_end = min(seg_start + SPAN, endtime)
        # 逐人拉取（useridlist 的人数通常 = 企业员工数，打卡接口 600次/分 足够）
        for uid in useridlist:
            try:
                resp = api.get_checkin_data(corpid, secret, seg_start, seg_end,
                                            [uid], opencheckindatatype)
                ec = resp.get("errcode")
                if ec not in (0, None):
                    # 301021=人员不在可见范围；记debug不刷屏，其他人继续
                    logger.debug("打卡拉取 %s: [%s] %s", uid, ec, resp.get("errmsg", "")[:40])
                    continue
                result.extend(resp.get("checkindata", []) or [])
            except Exception as e:
                logger.debug("打卡拉取异常 %s: %s", uid, e)
                continue
        seg_start = seg_end

    return result