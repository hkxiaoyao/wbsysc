"""
通讯录 userid 同步 - 拉全企业可见 userid 列表
- 用「通讯录同步secret」调 user/list_id（非自建应用secret）
- 游标分页（next_cursor），limit≤10000
- 返回去重 userid 列表（打卡用它当 useridlist）
"""
from __future__ import annotations

from typing import List

from . import client as api


def fetch_all_userids(corpid: str, contact_secret: str) -> List[str]:
    """拉企业全量 userid（去重）。

    Args:
        corpid: 企业corpid
        contact_secret: 通讯录同步secret（来自企微后台-管理工具-通讯录同步）
    Returns:
        去重 userid 列表
    """
    if not contact_secret:
        return []

    seen: set[str] = set()
    result: list[str] = []
    cursor = ""

    while True:
        resp = api.list_user_ids(corpid, contact_secret, cursor, limit=10000)
        if resp.get("errcode") not in (0, None):
            raise RuntimeError(
                f"拉取userid失败 [{resp.get('errcode')}] {resp.get('errmsg')}"
            )
        dept_user = resp.get("dept_user", []) or []
        for du in dept_user:
            uid = du.get("userid") or du.get("open_userid") or ""
            if uid and uid not in seen:
                seen.add(uid)
                result.append(uid)
        cursor = resp.get("next_cursor", "")
        if not cursor:   # 空表示无更多
            break

    return result