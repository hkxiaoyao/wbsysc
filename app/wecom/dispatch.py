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


def _window(schema: str, data_source: str, lookback_days: int, force: bool = False) -> tuple[int, int]:
    """计算同步时间窗。force=True 时忽略游标，强制从 now-lookback 拉起。"""
    now = int(time.time())
    endtime = now
    if force:
        starttime = now - max(1, lookback_days) * 86400
        return starttime, endtime
    last = db.get_cursor(schema, data_source, "")
    starttime = int(last) if (last and last.isdigit()) else now - lookback_days * 86400
    # 游标异常超前时兜底
    if starttime > endtime:
        starttime = endtime - lookback_days * 86400
    return starttime, endtime


def reset_cursors(schema: str, lookback_days: int, modules: set | None = None) -> dict:
    """把指定模块游标回拨到 now-lookback_days，返回新游标值。"""
    now = int(time.time())
    start = now - max(1, min(int(lookback_days), FULL_WINDOW_DAYS)) * 86400
    targets = modules or {"report", "approval", "checkin"}
    for ds in ("report", "approval", "checkin"):
        if ds in targets:
            db.save_cursor(schema, ds, "", str(start))
    logger.info("schema=%s 游标已回拨 lookback_days=%s start=%s modules=%s",
                schema, lookback_days, start, sorted(targets))
    return {"start": start, "lookback_days": lookback_days, "modules": sorted(targets)}


def _sync_one_checkin(t: _TenantCtx, lookback_days: int, force: bool = False) -> dict:
    """打卡同步：需租户配置了 checkin_userids / contact_secret"""
    starttime, endtime = _window(t.schema_name, "checkin", lookback_days, force=force)
    stats = {"pulled": 0, "stored": 0, "err": 0, "starttime": starttime, "endtime": endtime}

    if not t.secret:
        logger.error("打卡跳过 tenant=%s: 应用 secret 为空", t.tenant_id)
        return {**stats, "error": "empty app secret"}
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
    if stats["pulled"] == 0:
        logger.warning(
            "打卡列表为空 tenant=%s userid_count=%s window=[%s,%s]",
            t.tenant_id, len(userids), starttime, endtime,
        )
    for rec in records[:MAX_DETAIL_PER_RUN]:
        try:
            db.upsert_checkin(t.schema_name, rec)
            stats["stored"] += 1
        except Exception as e:
            logger.warning("打卡落库失败 %s: %s", rec.get("userid"), e)
            stats["err"] += 1
    db.save_cursor(t.schema_name, "checkin", "", str(endtime))
    logger.info("打卡 tenant=%s pulled=%s stored=%s err=%s window=[%s,%s]",
                t.tenant_id, stats["pulled"], stats["stored"], stats["err"],
                starttime, endtime)
    return stats


def _detail_ok_report(info: dict) -> bool:
    if not isinstance(info, dict):
        return False
    if info.get("errcode") not in (None, 0) and "errmsg" in info and "journal" not in info:
        return False
    # 企微字段 journal_uuid / journaluuid 都兼容
    return bool(info.get("journal_uuid") or info.get("journaluuid") or info.get("report_time") is not None)


def _sync_one_report(t: _TenantCtx, lookback_days: int, force: bool = False) -> dict:
    starttime, endtime = _window(t.schema_name, "report", lookback_days, force=force)
    stats = {"pulled": 0, "stored": 0, "err": 0, "starttime": starttime, "endtime": endtime}

    if not t.secret:
        logger.error("汇报跳过 tenant=%s: 应用 secret 为空（解密失败或未配置）", t.tenant_id)
        return {**stats, "error": "empty app secret"}

    try:
        uuids: List[str] = sync_reports_window(t.corpid, t.secret, starttime, endtime)
    except Exception as e:
        logger.error("汇报拉取失败 tenant=%s: %s", t.tenant_id, e)
        return {"error": str(e), **stats}

    stats["pulled"] = len(uuids)
    for ju in uuids[:MAX_DETAIL_PER_RUN]:
        try:
            info = fetch_report_detail(t.corpid, t.secret, ju)
            if _detail_ok_report(info):
                # 统一补 journaluuid 便于落库
                if isinstance(info, dict) and not info.get("journaluuid"):
                    info = {**info, "journaluuid": ju}
                db.upsert_report(t.schema_name, ju, info)
                stats["stored"] += 1
            else:
                logger.warning(
                    "汇报详情不可落库 ju=%s keys=%s errcode=%s errmsg=%s",
                    ju[:16],
                    list(info.keys())[:12] if isinstance(info, dict) else type(info).__name__,
                    (info or {}).get("errcode") if isinstance(info, dict) else None,
                    (info or {}).get("errmsg") if isinstance(info, dict) else None,
                )
                stats["err"] += 1
        except Exception as e:
            logger.warning("汇报详情失败 %s: %s", ju[:16], e)
            stats["err"] += 1
    db.save_cursor(t.schema_name, "report", "", str(endtime))
    logger.info("汇报 tenant=%s pulled=%s stored=%s err=%s window=[%s,%s]",
                t.tenant_id, stats["pulled"], stats["stored"], stats["err"],
                starttime, endtime)
    return stats


def _sync_one_approval(t: _TenantCtx, lookback_days: int, force: bool = False) -> dict:
    starttime, endtime = _window(t.schema_name, "approval", lookback_days, force=force)
    stats = {"pulled": 0, "stored": 0, "err": 0, "starttime": starttime, "endtime": endtime}

    if not t.secret:
        logger.error("审批跳过 tenant=%s: 应用 secret 为空（解密失败或未配置）", t.tenant_id)
        return {**stats, "error": "empty app secret"}

    try:
        sps: List[str] = sync_approvals_window(t.corpid, t.secret, starttime, endtime)
    except Exception as e:
        logger.error("审批拉取失败 tenant=%s: %s", t.tenant_id, e)
        return {"error": str(e), **stats}

    stats["pulled"] = len(sps)
    for sp in sps[:MAX_DETAIL_PER_RUN]:
        try:
            info = fetch_approval_detail(t.corpid, t.secret, sp)
            if isinstance(info, dict) and ("sp_no" in info or info.get("sp_status") is not None):
                if "sp_no" not in info:
                    info = {**info, "sp_no": sp}
                db.upsert_approval(t.schema_name, sp, info)
                stats["stored"] += 1
            else:
                logger.warning(
                    "审批详情不可落库 sp=%s keys=%s errcode=%s errmsg=%s",
                    sp,
                    list(info.keys())[:12] if isinstance(info, dict) else type(info).__name__,
                    (info or {}).get("errcode") if isinstance(info, dict) else None,
                    (info or {}).get("errmsg") if isinstance(info, dict) else None,
                )
                stats["err"] += 1
        except Exception as e:
            logger.warning("审批详情失败 %s: %s", sp, e)
            stats["err"] += 1
    db.save_cursor(t.schema_name, "approval", "", str(endtime))
    logger.info("审批 tenant=%s pulled=%s stored=%s err=%s window=[%s,%s]",
                t.tenant_id, stats["pulled"], stats["stored"], stats["err"],
                starttime, endtime)
    return stats


def run_sync_tenant(
    t: _TenantCtx,
    lookback_days: int = BACKFILL_DAYS,
    force: bool = False,
    reset_cursor: bool = False,
) -> dict:
    """同步单个租户。reset_cursor=True 时先把游标回拨到 now-lookback。"""
    if reset_cursor or force:
        reset_cursors(t.schema_name, lookback_days, t.enabled_modules)
        force = True  # 回拨后本轮强制按 lookback 窗口拉

    entry: dict = {
        "lookback_days": lookback_days,
        "force": force,
        "reset_cursor": reset_cursor,
    }
    if "report" in t.enabled_modules:
        entry["report"] = _sync_one_report(t, lookback_days, force=force)
    if "approval" in t.enabled_modules:
        entry["approval"] = _sync_one_approval(t, lookback_days, force=force)
    if "checkin" in t.enabled_modules:
        entry["checkin"] = _sync_one_checkin(t, lookback_days, force=force)
    return entry


def run_sync_all(lookback_days: int = BACKFILL_DAYS) -> dict:
    """一次完整同步轮次：遍历所有启用租户，按各租户 enabled_modules 执行"""
    tenants = get_all_tenants()
    logger.info("=== 同步轮次开始 租户数=%s ===", len(tenants))
    result = {}
    for t in tenants:
        logger.info("-- 租户 %s (schema=%s modules=%s) --",
                    t.tenant_id, t.schema_name, t.enabled_modules)
        try:
            result[t.tenant_id] = run_sync_tenant(t, lookback_days=lookback_days, force=False)
        except Exception as e:
            logger.error("租户 %s 同步异常: %s", t.tenant_id, e)
            result[t.tenant_id] = {"error": str(e)}
    logger.info("=== 同步轮次结束 ===")
    return result