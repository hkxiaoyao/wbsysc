"""
多租户同步调度 - 遍历所有启用租户
- 每租户：取 corpid/secret/schema_name → 增量同步 → 落到该租户 schema
- 游标驱动：从 {schema}.sync_cursor 续传到 now
- 容错：单租户失败不影响其他；单条详情失败不中断
"""
from __future__ import annotations

import logging
import time
from typing import List

from .. import db
from ..tenant import get_all_tenants, _TenantCtx
from .approval_sync import fetch_approval_detail, sync_approvals_window
from .checkin_sync import fetch_checkin_records
from .contact import fetch_all_userids
from .sync import fetch_report_detail, sync_reports_window

logger = logging.getLogger("wecom-sync")

BACKFILL_DAYS = 30
FULL_WINDOW_DAYS = 180
MAX_DETAIL_PER_RUN = 500

# userid 缓存：tenant_id -> (userids, expire_at)，避免每次同步都拉通讯录
_userid_cache: dict = {}
_USERID_CACHE_TTL = 600   # 10分钟


def _resolve_checkin_userids(t: _TenantCtx) -> list:
    """解析打卡用 useridlist：优先自动拉通讯录，失败回退手动配"""
    import time as _t
    now = _t.time()
    cached = _userid_cache.get(t.tenant_id)
    if cached and now < cached[1]:
        return cached[0]

    # 1) 优先：有通讯录同步secret → 自动拉
    if t.contact_secret:
        try:
            uids = fetch_all_userids(t.corpid, t.contact_secret)
            _userid_cache[t.tenant_id] = (uids, now + _USERID_CACHE_TTL)
            logger.info("租户 %s 自动拉取userid=%s人", t.tenant_id, len(uids))
            return uids
        except Exception as e:
            logger.warning("租户 %s 自动拉userid失败，回退手动配置: %s", t.tenant_id, e)

    # 2) 回退：手动配的 checkin_userids
    return t.checkin_userids


def _sync_one_checkin(t: _TenantCtx, lookback_days: int) -> dict:
    """打卡同步：需租户配置了 checkin_userids"""
    now = int(time.time())
    last = db.get_cursor(t.schema_name, "checkin", "")
    starttime = int(last) if (last and last.isdigit()) else now - lookback_days * 86400
    endtime = now
    stats = {"pulled": 0, "stored": 0, "err": 0}

    if not t.contact_secret and not t.checkin_userids:
        logger.warning("租户 %s 启用打卡但既无通讯录secret也无checkin_userids，跳过", t.tenant_id)
        return {**stats, "error": "no userid source (需配contact_secret或checkin_userids)"}

    userids = _resolve_checkin_userids(t)
    if not userids:
        return {**stats, "error": "userid list 为空"}

    try:
        records = fetch_checkin_records(t.corpid, t.secret, starttime, endtime, userids)
    except Exception as e:
        logger.error("打卡拉取失败 tenant=%s: %s", t.tenant_id, e)
        return {**stats, "error": str(e)}

    stats["pulled"] = len(records)
    for rec in records[:MAX_DETAIL_PER_RUN]:
        try:
            db.upsert_checkin(t.schema_name, rec)
            stats["stored"] += 1
        except Exception as e:
            logger.warning("打卡落库失败 %s: %s", rec.get("userid"), e)
            stats["err"] += 1
    db.save_cursor(t.schema_name, "checkin", "", str(endtime))
    logger.info("打卡 tenant=%s pulled=%s stored=%s err=%s",
                t.tenant_id, stats["pulled"], stats["stored"], stats["err"])
    return stats


def _sync_one_report(t: _TenantCtx, lookback_days: int) -> dict:
    now = int(time.time())
    last = db.get_cursor(t.schema_name, "report", "")
    starttime = int(last) if (last and last.isdigit()) else now - lookback_days * 86400
    endtime = now
    stats = {"pulled": 0, "stored": 0, "err": 0}

    try:
        uuids: List[str] = sync_reports_window(t.corpid, t.secret, starttime, endtime)
    except Exception as e:
        logger.error("汇报拉取失败 tenant=%s: %s", t.tenant_id, e)
        return {"error": str(e), **stats}

    stats["pulled"] = len(uuids)
    for ju in uuids[:MAX_DETAIL_PER_RUN]:
        try:
            info = fetch_report_detail(t.corpid, t.secret, ju)
            if isinstance(info, dict) and "journal_uuid" in info:
                db.upsert_report(t.schema_name, ju, info)
                stats["stored"] += 1
            else:
                stats["err"] += 1
        except Exception as e:
            logger.warning("汇报详情失败 %s: %s", ju[:16], e)
            stats["err"] += 1
    db.save_cursor(t.schema_name, "report", "", str(endtime))
    logger.info("汇报 tenant=%s pulled=%s stored=%s err=%s",
                t.tenant_id, stats["pulled"], stats["stored"], stats["err"])
    return stats


def _sync_one_approval(t: _TenantCtx, lookback_days: int) -> dict:
    now = int(time.time())
    last = db.get_cursor(t.schema_name, "approval", "")
    starttime = int(last) if (last and last.isdigit()) else now - lookback_days * 86400
    endtime = now
    stats = {"pulled": 0, "stored": 0, "err": 0}

    try:
        sps: List[str] = sync_approvals_window(t.corpid, t.secret, starttime, endtime)
    except Exception as e:
        logger.error("审批拉取失败 tenant=%s: %s", t.tenant_id, e)
        return {"error": str(e), **stats}

    stats["pulled"] = len(sps)
    for sp in sps[:MAX_DETAIL_PER_RUN]:
        try:
            info = fetch_approval_detail(t.corpid, t.secret, sp)
            if isinstance(info, dict) and "sp_no" in info:
                db.upsert_approval(t.schema_name, sp, info)
                stats["stored"] += 1
            else:
                stats["err"] += 1
        except Exception as e:
            logger.warning("审批详情失败 %s: %s", sp, e)
            stats["err"] += 1
    db.save_cursor(t.schema_name, "approval", "", str(endtime))
    logger.info("审批 tenant=%s pulled=%s stored=%s err=%s",
                t.tenant_id, stats["pulled"], stats["stored"], stats["err"])
    return stats


def run_sync_all(lookback_days: int = BACKFILL_DAYS) -> dict:
    """一次完整同步轮次：遍历所有启用租户，按各租户 enabled_modules 执行"""
    tenants = get_all_tenants()
    logger.info("=== 同步轮次开始 租户数=%s ===", len(tenants))
    result = {}
    for t in tenants:
        logger.info("-- 租户 %s (schema=%s modules=%s) --",
                    t.tenant_id, t.schema_name, t.enabled_modules)
        entry = {}
        try:
            if "report" in t.enabled_modules:
                entry["report"] = _sync_one_report(t, lookback_days)
            if "approval" in t.enabled_modules:
                entry["approval"] = _sync_one_approval(t, lookback_days)
            if "checkin" in t.enabled_modules:
                entry["checkin"] = _sync_one_checkin(t, lookback_days)
        except Exception as e:
            logger.error("租户 %s 同步异常: %s", t.tenant_id, e)
            entry["error"] = str(e)
        result[t.tenant_id] = entry
    logger.info("=== 同步轮次结束 ===")
    return result