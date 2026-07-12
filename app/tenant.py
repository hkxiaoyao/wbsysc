"""
租户配置 - 从中心库 tenant_config 读取租户信息
- token → tenant 全套上下文（corpid / secret明文 / schema_name / sync_interval）
- 内存缓存（60s），新增/改租户后调 reload
- 不含业务数据，所有业务数据在各租户 schema
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, Optional

from sqlalchemy import text

from .crypto import decrypt_secret
from .db import get_engine

_CACHE: Dict[str, "_TenantCtx"] = {}   # token -> ctx
_CACHE_AT = 0.0
_CACHE_TTL = 60.0


@dataclass
class _TenantCtx:
    tenant_id: str
    corpid: str
    secret: str        # 自建应用secret（解密后明文，运行时用，不落日志）
    schema_name: str
    sync_interval_min: int
    enabled_modules: set
    checkin_userids: list       # 手动配的打卡userid
    contact_secret: str = ""    # 通讯录同步secret（解密后，可选，用于自动拉userid）


def _load_all() -> Dict[str, _TenantCtx]:
    """全量加载启用的租户到缓存"""
    sql = text("""SELECT tenant_id, corpid, secret_encrypted, mcp_token,
                         schema_name, sync_interval_min,
                         enabled_modules, checkin_userids,
                         contact_secret_encrypted
                  FROM tenant_config WHERE enabled=1""")
    out: Dict[str, _TenantCtx] = {}
    with get_engine().connect() as conn:
        rows = conn.execute(sql).fetchall()
    for r in rows:
        try:
            secret = decrypt_secret(r[2]) if r[2] else ""
        except Exception:
            secret = ""
        try:
            contact_secret = decrypt_secret(r[8]) if r[8] else ""
        except Exception:
            contact_secret = ""
        mods = {m.strip() for m in (r[6] or "").split(",") if m.strip()}
        uids = [u.strip() for u in (r[7] or "").split(",") if u.strip()] if r[7] else []
        ctx = _TenantCtx(
            tenant_id=r[0], corpid=r[1], secret=secret,
            schema_name=r[4] or f"wbd_{_hash_corpid(r[1])}",
            sync_interval_min=r[5] or 30,
            enabled_modules=mods or {"report", "approval", "checkin"},
            checkin_userids=uids,
            contact_secret=contact_secret,
        )
        out[r[3]] = ctx
    return out


def _hash_corpid(corpid: str) -> str:
    import hashlib
    return hashlib.md5(corpid.encode()).hexdigest()[:12]


def reload_tenants() -> None:
    global _CACHE, _CACHE_AT
    _CACHE = _load_all()
    _CACHE_AT = time.time()


def _ensure_cache():
    global _CACHE, _CACHE_AT
    if not _CACHE or (time.time() - _CACHE_AT > _CACHE_TTL):
        reload_tenants()


def get_tenant_by_token(token: str) -> Optional[_TenantCtx]:
    """鉴权入口：token → 租户上下文"""
    _ensure_cache()
    return _CACHE.get(token)


def get_all_tenants() -> list[_TenantCtx]:
    """调度器遍历租户用"""
    _ensure_cache()
    return list(_CACHE.values())