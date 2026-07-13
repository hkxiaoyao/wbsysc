"""
企微 OpenAPI 客户端 - 多租户版
- 每个租户独立的 corpid/secret（从租户上下文传入）
- token 缓存按 corpid 隔离（不同租户 token 互不污染）
- 一期：同步函数按租户显式传 (corpid, secret) 调用
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
from typing import Any, Dict, List, Optional

import httpx

QYAPI = "https://qyapi.weixin.qq.com"

# token 缓存：(corpid+secret)->(token, expires_at)
# 注意：同一corpid可能有自建应用secret和通讯录同步secret两个，必须按secret区分，
# 否则通讯录token会覆盖自建应用token，导致调业务接口报48002
_token_cache: Dict[str, tuple] = {}
_token_lock = threading.Lock()
# 关闭 httpx 默认把完整 URL（含 access_token）打到 INFO 的行为
logging.getLogger("httpx").setLevel(logging.WARNING)
_http = httpx.Client(timeout=30.0)


def _cache_key(corpid: str, secret: str) -> str:
    """token缓存key: corpid + secret的md5前8位（区分同corpid不同secret）"""
    h = hashlib.md5(secret.encode()).hexdigest()[:8]
    return f"{corpid}:{h}"


def get_token(corpid: str, secret: str) -> str:
    """按 (corpid, secret) 缓存 access_token（提前5分钟续期）"""
    key = _cache_key(corpid, secret)
    cached = _token_cache.get(key)
    if cached and time.time() < cached[1] - 300:
        return cached[0]
    with _token_lock:
        cached = _token_cache.get(key)
        if cached and time.time() < cached[1] - 300:
            return cached[0]
        if not corpid or not secret:
            raise RuntimeError(f"缺少 corpid/secret (corpid={corpid})")
        r = _http.get(f"{QYAPI}/cgi-bin/gettoken",
                      params={"corpid": corpid, "corpsecret": secret})
        data = r.json()
        if data.get("errcode") != 0:
            raise RuntimeError(f"gettoken 失败 (corpid={corpid}): {data}")
        tok = data["access_token"]
        _token_cache[key] = (tok, time.time() + data.get("expires_in", 7200))
        return tok


def invalidate_token(corpid: str, secret: str) -> None:
    """token 失效时清除（按 secret 区分）"""
    _token_cache.pop(_cache_key(corpid, secret), None)


def _post(corpid: str, secret: str, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
    tok = get_token(corpid, secret)
    r = _http.post(f"{QYAPI}{path}", params={"access_token": tok}, json=body)
    data = r.json()
    # token 失效则清缓存重试一次（42001 token失效 / 41401 无效 / 40014）
    if data.get("errcode") in (42001, 41401, 40014):
        invalidate_token(corpid, secret)
        tok = get_token(corpid, secret)
        r = _http.post(f"{QYAPI}{path}", params={"access_token": tok}, json=body)
        data = r.json()
    return data


# ===== 三类接口（按租户凭证调用）=====
def list_report_records(corpid, secret, starttime, endtime, cursor=0, limit=100, filters=None):
    body = {"starttime": starttime, "endtime": endtime, "cursor": cursor, "limit": limit}
    if filters:
        body["filters"] = filters
    return _post(corpid, secret, "/cgi-bin/oa/journal/get_record_list", body)


def get_report_detail(corpid, secret, journaluuid):
    return _post(corpid, secret, "/cgi-bin/oa/journal/get_record_detail", {"journaluuid": journaluuid})


def list_approvals(corpid, secret, starttime, endtime, cursor="", size=100, filters=None):
    body = {"starttime": str(starttime), "endtime": str(endtime),
            "new_cursor": cursor or "", "size": size}
    if filters:
        body["filters"] = filters
    return _post(corpid, secret, "/cgi-bin/oa/getapprovalinfo", body)


def get_approval_detail(corpid, secret, sp_no):
    return _post(corpid, secret, "/cgi-bin/oa/getapprovaldetail", {"sp_no": sp_no})


def get_smarttable_records(corpid, secret, docid, sheet_id, offset=0, limit=1000, key_type=None):
    body = {"docid": docid, "sheet_id": sheet_id, "offset": offset, "limit": limit}
    if key_type:
        body["key_type"] = key_type
    return _post(corpid, secret, "/cgi-bin/wedoc/smartsheet/get_records", body)


# ===== 打卡 =====
def get_checkin_data(corpid, secret, starttime, endtime, useridlist,
                     opencheckindatatype=3):
    """获取打卡记录数据 GETCHECKINDATA
    - opencheckindatatype: 1上下班 2外出 3全部
    - useridlist: 最多100个 userid
    - 时间跨度 ≤30天
    - 返回 checkindata 数组（一次拿全部字段，无游标分页）
    """
    body = {
        "opencheckindatatype": opencheckindatatype,
        "starttime": starttime, "endtime": endtime,
        "useridlist": useridlist,
    }
    return _post(corpid, secret, "/cgi-bin/checkin/getcheckindata", body)


# ===== 通讯录（用通讯录同步secret，corpid同）=====
def list_user_ids(corpid, contact_secret, cursor="", limit=10000):
    """获取成员ID列表 USER/LIST_ID
    - 必须用「通讯录同步secret」调用（非自建应用secret）
    - 返回 dept_user: [{userid, department}, ...]，userid多部门会有多条
    - next_cursor 为空表示无更多
    """
    body = {"limit": limit}
    if cursor:
        body["cursor"] = cursor
    return _post(corpid, contact_secret, "/cgi-bin/user/list_id", body)