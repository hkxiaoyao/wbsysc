"""
管理后台 API - 租户 CRUD + 手动同步
- 鉴权：单密码登录 → 签发 session token（Cookie），中间件校验
- 密码存 .env ADMIN_PASSWORD（禁止硬编码）
- 所有租户操作用中心库 tenant_config（管理操作不经 MCP 鉴权）
"""
from __future__ import annotations

import hashlib
import secrets
import time
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import text

from .config import get_settings
from .crypto import encrypt_secret, decrypt_secret
from .db import get_engine, ensure_schema
from .tenant import _hash_corpid, reload_tenants
from .wecom.dispatch import run_sync_all

router = APIRouter(prefix="/admin", tags=["admin"])

# session 存储：token -> expire_at（内存，重启失效；PoC 够用）
_sessions: dict[str, float] = {}
SESSION_COOKIE = "wbg_admin_session"


def _is_authed(request: Request) -> bool:
    # Cookie 头 + 兜底 Authorization: Bearer（前端 fetch 用 Bearer 更可靠跨域）
    tok = request.cookies.get(SESSION_COOKIE)
    if not tok:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            tok = auth[len("Bearer "):].strip()
    if not tok:
        return False
    exp = _sessions.get(tok, 0)
    if time.time() > exp:
        _sessions.pop(tok, None)
        return False
    return True


class LoginReq(BaseModel):
    password: str


@router.post("/login")
def login(body: LoginReq, response: Response):
    """单密码登录 → 签发 session token（HttpOnly Cookie + 返回 token 给前端）"""
    s = get_settings()
    if not s.admin_password:
        raise HTTPException(500, "未配置 ADMIN_PASSWORD，无法登录")
    if not secrets.compare_digest(body.password, s.admin_password):
        raise HTTPException(401, "认证失败")
    tok = secrets.token_urlsafe(32)
    _sessions[tok] = time.time() + s.admin_session_ttl_min * 60
    response.set_cookie(
        SESSION_COOKIE, tok,
        httponly=True, samesite="lax",
        max_age=s.admin_session_ttl_min * 60,
        secure=(s.app_env == "prod"),
    )
    return {"ok": True, "token": tok}   # 同时返回 token 供前端放 Authorization 头


@router.post("/logout")
def logout(request: Request, response: Response):
    tok = request.cookies.get(SESSION_COOKIE)
    if tok:
        _sessions.pop(tok, None)
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@router.get("/session")
def check_session(request: Request):
    """前端校验是否已登录"""
    return {"authed": _is_authed(request)}


# ===== 租户 CRUD =====
def _require_auth(request: Request):
    if not _is_authed(request):
        raise HTTPException(401, "未登录或会话过期")


class TenantUpsert(BaseModel):
    tenant_id: str
    corpid: str
    # 编辑时可不传 secret/contact_secret（留空=不修改）
    secret: Optional[str] = None
    contact_secret: Optional[str] = None
    mcp_token: str
    display_name: str = ""
    sync_interval_min: int = 30
    enabled_modules: str = "report,approval,checkin"
    checkin_userids: str = ""
    enabled: bool = True


@router.get("/tenants")
def list_tenants(request: Request):
    """列出所有租户（secret 字段不回传明文，仅返回是否已配置）"""
    _require_auth(request)
    sql = text("""SELECT tenant_id, display_name, corpid, mcp_token, schema_name,
                         sync_interval_min, enabled_modules, checkin_userids,
                         IFNULL(contact_secret_encrypted IS NOT NULL, 0) AS has_contact_secret,
                         IFNULL(secret_encrypted IS NOT NULL, 0) AS has_secret,
                         enabled, created_at, updated_at
                  FROM tenant_config ORDER BY created_at""")
    with get_engine().connect() as conn:
        rows = conn.execute(sql).fetchall()
    return {
        "items": [
            {
                "tenant_id": r[0], "display_name": r[1], "corpid": r[2],
                "mcp_token": r[3], "schema_name": r[4],
                "sync_interval_min": r[5], "enabled_modules": r[6],
                "checkin_userids": r[7],
                "has_contact_secret": bool(r[8]),
                "has_secret": bool(r[9]),
                "enabled": bool(r[10]),
                "created_at": str(r[11]), "updated_at": str(r[12]),
            }
            for r in rows
        ]
    }


@router.post("/tenants")
def create_tenant(body: TenantUpsert, request: Request):
    """新增租户：写配置 + 建 schema + 刷缓存"""
    _require_auth(request)
    if not body.secret:
        raise HTTPException(400, "新增租户必须填 secret")
    eng = get_engine()
    schema_name = f"wbd_{_hash_corpid(body.corpid)}"
    enc = encrypt_secret(body.secret)
    contact_enc = encrypt_secret(body.contact_secret) if body.contact_secret else None

    sql = text("""
        INSERT INTO tenant_config
            (tenant_id, display_name, corpid, secret_encrypted, mcp_token, schema_name,
             sync_interval_min, enabled_modules, checkin_userids, contact_secret_encrypted, enabled)
        VALUES (:t,:dn,:c,:se,:mt,:sn,:si,:em,:cu,:cs,:en)
    """)
    try:
        with eng.begin() as conn:
            conn.execute(sql, {
                "t": body.tenant_id, "dn": body.display_name, "c": body.corpid,
                "se": enc, "mt": body.mcp_token, "sn": schema_name, "si": body.sync_interval_min,
                "em": body.enabled_modules, "cu": body.checkin_userids or None,
                "cs": contact_enc, "en": 1 if body.enabled else 0,
            })
    except Exception as e:
        raise HTTPException(400, f"写入失败(可能租户ID/corpid/token重复): {e}")

    ensure_schema(schema_name)   # 建该租户 schema + 业务表
    reload_tenants()
    return {"ok": True, "schema_name": schema_name}


@router.put("/tenants/{tenant_id}")
def update_tenant(tenant_id: str, body: TenantUpsert, request: Request):
    """编辑租户：留空的 secret/contact_secret 不修改（保持原值）"""
    _require_auth(request)
    eng = get_engine()

    # 先取现有加密值（secret/contact_secret 留空时保留）
    with eng.connect() as conn:
        cur = conn.execute(text(
            "SELECT secret_encrypted, contact_secret_encrypted FROM tenant_config WHERE tenant_id=:t"
        ), {"t": tenant_id}).fetchone()
    if not cur:
        raise HTTPException(404, "租户不存在")

    secret_enc = cur[0]
    contact_enc = cur[1]
    if body.secret:
        secret_enc = encrypt_secret(body.secret)
    if body.contact_secret:
        contact_enc = encrypt_secret(body.contact_secret)

    new_schema = f"wbd_{_hash_corpid(body.corpid)}"
    sql = text("""
        UPDATE tenant_config SET
            display_name=:dn, corpid=:c, secret_encrypted=:se, mcp_token=:mt,
            schema_name=:sn, sync_interval_min=:si, enabled_modules=:em,
            checkin_userids=:cu, contact_secret_encrypted=:cs, enabled=:en
        WHERE tenant_id=:t
    """)
    with eng.begin() as conn:
        conn.execute(sql, {
            "t": tenant_id, "dn": body.display_name, "c": body.corpid,
            "se": secret_enc, "mt": body.mcp_token, "sn": new_schema,
            "si": body.sync_interval_min, "em": body.enabled_modules,
            "cu": body.checkin_userids or None, "cs": contact_enc,
            "en": 1 if body.enabled else 0,
        })
    ensure_schema(new_schema)   # 新 schema 建表（若改了 corpid）
    reload_tenants()
    return {"ok": True}


@router.delete("/tenants/{tenant_id}")
def delete_tenant(tenant_id: str, request: Request):
    """删除租户配置（保留历史数据 schema，需另行手动删）"""
    _require_auth(request)
    with get_engine().begin() as conn:
        r = conn.execute(text("DELETE FROM tenant_config WHERE tenant_id=:t"), {"t": tenant_id})
    if r.rowcount == 0:
        raise HTTPException(404, "租户不存在")
    reload_tenants()
    return {"ok": True}


@router.post("/tenants/{tenant_id}/sync")
def trigger_sync(tenant_id: str, request: Request):
    """手动触发该租户同步（后台执行，立即返回）"""
    _require_auth(request)
    import asyncio
    import threading

    # 单租户同步：在线程池跑 run_sync_one_tenant，不阻塞响应
    from .tenant import get_all_tenants
    from .wecom.dispatch import _sync_one_report, _sync_one_approval, _sync_one_checkin, BACKFILL_DAYS

    tenants = {t.tenant_id: t for t in get_all_tenants()}
    t = tenants.get(tenant_id)
    if not t:
        raise HTTPException(404, "租户不存在或已禁用")

    def _run():
        try:
            if "report" in t.enabled_modules:
                _sync_one_report(t, BACKFILL_DAYS)
            if "approval" in t.enabled_modules:
                _sync_one_approval(t, BACKFILL_DAYS)
            if "checkin" in t.enabled_modules:
                _sync_one_checkin(t, BACKFILL_DAYS)
        except Exception:
            pass  # 调度内已记日志

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "msg": f"租户 {tenant_id} 同步已触发(后台执行)"}
