"""
租户配置 - 从中心库 tenant_config 读取租户信息
- token → tenant 全套上下文（corpid / secret明文 / schema_name / sync_interval）
- 内存缓存（60s），新增/改租户后调 reload
- 不含业务数据，所有业务数据在各租户 schema
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Dict, Literal, Optional

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from .crypto import decrypt_secret
from .db import get_engine

logger = logging.getLogger("wecom-tenant")

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
    data_mode: Literal["stored", "direct"] = "stored"


def _is_missing_data_mode_column_error(exc: Exception) -> bool:
    if not isinstance(exc, OperationalError):
        return False
    args = getattr(exc.orig, "args", ())
    code = args[0] if args else None
    message = " ".join(str(value) for value in args).lower()
    return code == 1054 and "unknown column" in message and "data_mode" in message


def _load_all() -> Dict[str, _TenantCtx]:
    """全量加载启用的租户到缓存"""
    sql = text("""SELECT tenant_id, corpid, secret_encrypted, mcp_token,
                         schema_name, sync_interval_min,
                         enabled_modules, checkin_userids,
                         contact_secret_encrypted,
                         IFNULL(data_mode, 'stored') AS data_mode
                  FROM tenant_config
                  WHERE enabled=1
                    AND corpid IS NOT NULL AND corpid <> ''
                    AND secret_encrypted IS NOT NULL
                    AND mcp_token IS NOT NULL AND mcp_token <> ''
                    AND schema_name IS NOT NULL AND schema_name <> ''""")
    legacy_sql = text("""SELECT tenant_id, corpid, secret_encrypted, mcp_token,
                                schema_name, sync_interval_min,
                                enabled_modules, checkin_userids,
                                contact_secret_encrypted
                         FROM tenant_config
                         WHERE enabled=1
                           AND corpid IS NOT NULL AND corpid <> ''
                           AND secret_encrypted IS NOT NULL
                           AND mcp_token IS NOT NULL AND mcp_token <> ''
                           AND schema_name IS NOT NULL AND schema_name <> ''""")
    out: Dict[str, _TenantCtx] = {}
    with get_engine().connect() as conn:
        try:
            rows = conn.execute(sql).fetchall()
            has_data_mode = True
        except OperationalError as exc:
            if not _is_missing_data_mode_column_error(exc):
                raise
            logger.warning(
                "tenant loader falling back to stored mode because data_mode "
                "column is missing: %s",
                exc,
            )
            rows = conn.execute(legacy_sql).fetchall()
            has_data_mode = False
    for r in rows:
        tenant_id = r[0]
        try:
            if r[2]:
                secret = decrypt_secret(r[2])
            else:
                secret = ""
                logger.warning(
                    "租户 %s 应用 secret 密文为空（管理后台需重新填写自建应用 Secret）",
                    tenant_id,
                )
        except Exception as e:
            secret = ""
            logger.warning(
                "租户 %s 应用 secret 解密失败（多为 CREDENTIAL_KEY 变更，需后台重填 Secret）: %s",
                tenant_id, type(e).__name__,
            )
        try:
            if r[8]:
                contact_secret = decrypt_secret(r[8])
            else:
                contact_secret = ""
        except Exception as e:
            contact_secret = ""
            logger.warning(
                "租户 %s 通讯录 secret 解密失败（需后台重填通讯录 Secret）: %s",
                tenant_id, type(e).__name__,
            )
        mods = {m.strip() for m in (r[6] or "").split(",") if m.strip()}
        uids = [u.strip() for u in (r[7] or "").split(",") if u.strip()] if r[7] else []
        data_mode_value = r[9] if has_data_mode else "stored"
        data_mode = data_mode_value if data_mode_value in {"stored", "direct"} else "stored"
        ctx = _TenantCtx(
            tenant_id=tenant_id, corpid=r[1], secret=secret,
            schema_name=r[4] or f"wbd_{_hash_corpid(r[1])}",
            sync_interval_min=r[5] or 30,
            enabled_modules=mods or {"report", "approval", "checkin"},
            checkin_userids=uids,
            contact_secret=contact_secret,
            data_mode=data_mode,
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
