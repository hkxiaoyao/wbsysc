"""
管理后台 API - 租户 CRUD + 手动同步
- 鉴权：单密码登录 → 签发 session token（Cookie），中间件校验
- 密码存 .env ADMIN_PASSWORD（禁止硬编码）
- 所有租户操作用中心库 tenant_config（管理操作不经 MCP 鉴权）
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import time
from typing import Literal, Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy import text

from .config import get_settings
from .crypto import encrypt_secret, decrypt_secret
from .db import get_engine, ensure_schema
from .domain_verify import (
    delete_verify_by_tenant,
    ensure_domain_tables,
    get_verify_by_tenant,
    normalize_domain,
    save_verify_file,
)
from .tenant import _hash_corpid, reload_tenants
from .wecom.dispatch import run_sync_all

router = APIRouter(prefix="/admin", tags=["admin"])
logger = logging.getLogger("wecom-gateway")
MCP_TOKEN_MIN_LENGTH = 16

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
    # 新建时必填；编辑时留空表示保留现有 Token。
    mcp_token: str = ""
    data_mode: Literal["stored", "direct"] = "stored"
    display_name: str = ""
    sync_interval_min: int = 30
    enabled_modules: str = "report,approval,checkin"
    checkin_userids: str = ""
    trusted_domain: str = ""  # 反代对外域名，如 mcp.example.com
    enabled: bool = True


def _mcp_token_hint(token: str) -> str:
    if not token:
        return ""
    if len(token) <= 4:
        return "****"
    return token[-4:]


def _validate_mcp_token(token: str) -> None:
    if token != token.strip():
        raise HTTPException(400, "MCP Token 不能包含首尾空格")
    if len(token) < MCP_TOKEN_MIN_LENGTH:
        raise HTTPException(
            400,
            f"MCP Token 长度不能少于 {MCP_TOKEN_MIN_LENGTH} 个字符",
        )


def _is_missing_data_mode_column_error(exc: Exception) -> bool:
    """仅识别 MySQL 1054 且明确指向 data_mode 的缺列错误。"""
    db_error = getattr(exc, "orig", exc)
    args = getattr(db_error, "args", ())
    code = args[0] if args else getattr(db_error, "errno", None)
    message = " ".join(str(arg) for arg in args).lower()
    return code == 1054 and "unknown column" in message and "data_mode" in message


def _tenant_item(r) -> dict:
    """list/create 共用的租户序列化 + 校验文件信息"""
    item = {
        "tenant_id": r[0], "display_name": r[1], "corpid": r[2],
        "has_mcp_token": bool(r[3]),
        "mcp_token_hint": _mcp_token_hint(r[3]),
        "schema_name": r[4],
        "sync_interval_min": r[5], "enabled_modules": r[6],
        "checkin_userids": r[7],
        "has_contact_secret": bool(r[8]),
        "has_secret": bool(r[9]),
        "enabled": bool(r[10]),
        "created_at": str(r[11]), "updated_at": str(r[12]),
        "trusted_domain": (r[13] if len(r) > 13 else "") or "",
        "data_mode": (r[14] if len(r) > 14 else "stored") or "stored",
        "verify_filename": "",
        "verify_url": "",
    }
    vf = get_verify_by_tenant(item["tenant_id"])
    if vf:
        item["verify_filename"] = vf["filename"]
        # 优先可信域名；否则前端用当前 origin 拼
        if item["trusted_domain"]:
            item["verify_url"] = f"https://{item['trusted_domain']}/{vf['filename']}"
        else:
            item["verify_url"] = f"/{vf['filename']}"
    return item


@router.get("/tenants")
def list_tenants(request: Request):
    """列出所有租户（secret 字段不回传明文，仅返回是否已配置）"""
    _require_auth(request)
    try:
        ensure_domain_tables()
    except Exception as e:
        # 补表/补列失败不阻断列表（旧库兼容）；域名相关字段降级为空
        logger.warning("ensure_domain_tables failed: %s", e)

    # 优先带 trusted_domain/data_mode；列尚未加上时回退旧 SQL，避免 500
    sql_full = text("""SELECT tenant_id, display_name, corpid, mcp_token, schema_name,
                         sync_interval_min, enabled_modules, checkin_userids,
                         IFNULL(contact_secret_encrypted IS NOT NULL, 0) AS has_contact_secret,
                         IFNULL(secret_encrypted IS NOT NULL, 0) AS has_secret,
                         enabled, created_at, updated_at,
                         IFNULL(trusted_domain, '') AS trusted_domain,
                         IFNULL(data_mode, 'stored') AS data_mode
                  FROM tenant_config ORDER BY created_at""")
    sql_legacy = text("""SELECT tenant_id, display_name, corpid, mcp_token, schema_name,
                         sync_interval_min, enabled_modules, checkin_userids,
                         IFNULL(contact_secret_encrypted IS NOT NULL, 0) AS has_contact_secret,
                         IFNULL(secret_encrypted IS NOT NULL, 0) AS has_secret,
                         enabled, created_at, updated_at
                  FROM tenant_config ORDER BY created_at""")
    with get_engine().connect() as conn:
        try:
            rows = conn.execute(sql_full).fetchall()
        except Exception as e:
            if not _is_missing_data_mode_column_error(e):
                raise
            logger.warning(
                "tenant list falling back to legacy query because data_mode column is missing: %s",
                e,
            )
            rows = conn.execute(sql_legacy).fetchall()
    items = []
    for r in rows:
        try:
            items.append(_tenant_item(r))
        except Exception:
            # 校验文件表异常时仍返回基础租户信息
            items.append({
                "tenant_id": r[0], "display_name": r[1], "corpid": r[2],
                "has_mcp_token": bool(r[3]),
                "mcp_token_hint": _mcp_token_hint(r[3]),
                "schema_name": r[4],
                "sync_interval_min": r[5], "enabled_modules": r[6],
                "checkin_userids": r[7],
                "has_contact_secret": bool(r[8]),
                "has_secret": bool(r[9]),
                "enabled": bool(r[10]),
                "created_at": str(r[11]), "updated_at": str(r[12]),
                "trusted_domain": (r[13] if len(r) > 13 else "") or "",
                "data_mode": (r[14] if len(r) > 14 else "stored") or "stored",
                "verify_filename": "",
                "verify_url": "",
            })
    return {"items": items}


@router.get("/tenants/{tenant_id}/mcp-config")
def get_mcp_config(tenant_id: str, request: Request, response: Response):
    """鉴权后按需返回单个租户的 MCP 连接配置。"""
    _require_auth(request)
    response.headers["Cache-Control"] = "no-store"
    with get_engine().connect() as conn:
        row = conn.execute(text(
            "SELECT tenant_id, mcp_token, IFNULL(trusted_domain, '') "
            "FROM tenant_config WHERE tenant_id=:t"
        ), {"t": tenant_id}).fetchone()
    if not row:
        raise HTTPException(404, "租户不存在")
    return {
        "tenant_id": row[0],
        "mcp_token": row[1],
        "trusted_domain": row[2] or "",
    }


@router.post("/tenants")
def create_tenant(body: TenantUpsert, request: Request):
    """新增租户：写配置 + 建 schema + 刷缓存"""
    _require_auth(request)
    if not body.secret:
        raise HTTPException(400, "新增租户必须填 secret")
    if not body.mcp_token:
        raise HTTPException(400, "新增租户必须填 MCP Token")
    _validate_mcp_token(body.mcp_token)
    ensure_domain_tables()
    try:
        trusted_domain = normalize_domain(body.trusted_domain) if body.trusted_domain else ""
    except ValueError as e:
        raise HTTPException(400, str(e))
    eng = get_engine()
    schema_name = f"wbd_{_hash_corpid(body.corpid)}"
    enc = encrypt_secret(body.secret)
    contact_enc = encrypt_secret(body.contact_secret) if body.contact_secret else None

    sql = text("""
        INSERT INTO tenant_config
            (tenant_id, display_name, corpid, secret_encrypted, mcp_token, schema_name,
             sync_interval_min, enabled_modules, checkin_userids, contact_secret_encrypted,
             trusted_domain, data_mode, enabled)
        VALUES (:t,:dn,:c,:se,:mt,:sn,:si,:em,:cu,:cs,:td,:dm,:en)
    """)
    try:
        with eng.begin() as conn:
            conn.execute(sql, {
                "t": body.tenant_id, "dn": body.display_name, "c": body.corpid,
                "se": enc, "mt": body.mcp_token, "sn": schema_name, "si": body.sync_interval_min,
                "em": body.enabled_modules, "cu": body.checkin_userids or None,
                "cs": contact_enc, "td": trusted_domain, "dm": body.data_mode,
                "en": 1 if body.enabled else 0,
            })
    except Exception as exc:
        logger.warning(
            "tenant create write failed tenant=%s error_type=%s",
            body.tenant_id,
            type(exc).__name__,
        )
        raise HTTPException(
            400,
            "写入失败，可能租户 ID、企业 ID 或 MCP Token 重复",
        ) from None

    ensure_schema(schema_name)   # 建该租户 schema + 业务表
    reload_tenants()
    return {"ok": True, "schema_name": schema_name, "trusted_domain": trusted_domain}


@router.put("/tenants/{tenant_id}")
def update_tenant(tenant_id: str, body: TenantUpsert, request: Request):
    """编辑租户：留空的 secret/contact_secret 不修改（保持原值）"""
    _require_auth(request)
    if body.mcp_token:
        _validate_mcp_token(body.mcp_token)
    ensure_domain_tables()
    eng = get_engine()
    try:
        trusted_domain = normalize_domain(body.trusted_domain) if body.trusted_domain else ""
    except ValueError as e:
        raise HTTPException(400, str(e))

    # 先取现有加密值与 Token（编辑时留空均保留）。
    with eng.connect() as conn:
        cur = conn.execute(text(
            "SELECT secret_encrypted, contact_secret_encrypted, mcp_token "
            "FROM tenant_config WHERE tenant_id=:t"
        ), {"t": tenant_id}).fetchone()
    if not cur:
        raise HTTPException(404, "租户不存在")

    secret_enc = cur[0]
    contact_enc = cur[1]
    mcp_token = body.mcp_token or cur[2]
    if body.secret:
        secret_enc = encrypt_secret(body.secret)
    if body.contact_secret:
        contact_enc = encrypt_secret(body.contact_secret)

    new_schema = f"wbd_{_hash_corpid(body.corpid)}"
    sql = text("""
        UPDATE tenant_config SET
            display_name=:dn, corpid=:c, secret_encrypted=:se, mcp_token=:mt,
            schema_name=:sn, sync_interval_min=:si, enabled_modules=:em,
            checkin_userids=:cu, contact_secret_encrypted=:cs,
            trusted_domain=:td, data_mode=:dm, enabled=:en
        WHERE tenant_id=:t
    """)
    with eng.begin() as conn:
        conn.execute(sql, {
            "t": tenant_id, "dn": body.display_name, "c": body.corpid,
            "se": secret_enc, "mt": mcp_token, "sn": new_schema,
            "si": body.sync_interval_min, "em": body.enabled_modules,
            "cu": body.checkin_userids or None, "cs": contact_enc,
            "td": trusted_domain, "dm": body.data_mode,
            "en": 1 if body.enabled else 0,
        })
        # 同步校验文件上的域名展示字段
        if trusted_domain:
            conn.execute(
                text("UPDATE domain_verify_file SET trusted_domain=:d WHERE tenant_id=:t"),
                {"d": trusted_domain, "t": tenant_id},
            )
    ensure_schema(new_schema)   # 新 schema 建表（若改了 corpid）
    reload_tenants()
    return {"ok": True, "trusted_domain": trusted_domain}


@router.delete("/tenants/{tenant_id}")
def delete_tenant(tenant_id: str, request: Request):
    """删除租户配置（保留历史数据 schema，需另行手动删）"""
    _require_auth(request)
    with get_engine().begin() as conn:
        r = conn.execute(text("DELETE FROM tenant_config WHERE tenant_id=:t"), {"t": tenant_id})
    if r.rowcount == 0:
        raise HTTPException(404, "租户不存在")
    try:
        delete_verify_by_tenant(tenant_id)
    except Exception:
        pass
    reload_tenants()
    return {"ok": True}


@router.post("/tenants/{tenant_id}/sync")
def trigger_sync(
    tenant_id: str,
    request: Request,
    lookback_days: int = 30,
    force: bool = False,
    reset_cursor: bool = False,
):
    """手动触发该租户同步（后台执行，立即返回）

    Query:
      lookback_days: 回拨/强制窗口天数，默认 30，最大 180
      force: true=忽略现有游标，按 lookback 窗口拉
      reset_cursor: true=先把游标写回 now-lookback，再强制同步（全量回拨）
    """
    _require_auth(request)
    import threading

    from .tenant import get_all_tenants, reload_tenants
    from .wecom.dispatch import FULL_WINDOW_DAYS, run_sync_tenant

    # 刷新缓存，避免刚改 secret 仍用旧空值
    reload_tenants()
    tenants = {t.tenant_id: t for t in get_all_tenants()}
    t = tenants.get(tenant_id)
    if not t:
        raise HTTPException(404, "租户不存在或已禁用")
    if t.data_mode == "direct":
        raise HTTPException(409, "企微直连模式不支持同步")

    days = max(1, min(int(lookback_days or 30), FULL_WINDOW_DAYS))
    do_force = bool(force) or bool(reset_cursor)
    do_reset = bool(reset_cursor)

    def _run():
        try:
            run_sync_tenant(
                t,
                lookback_days=days,
                force=do_force,
                reset_cursor=do_reset,
            )
        except Exception:
            pass  # 调度内已记日志

    threading.Thread(target=_run, daemon=True).start()
    mode = "全量回拨" if do_reset else ("强制窗口" if do_force else "增量")
    return {
        "ok": True,
        "msg": f"租户 {tenant_id} {mode}同步已触发(后台执行) lookback_days={days}",
        "lookback_days": days,
        "force": do_force,
        "reset_cursor": do_reset,
    }


@router.get("/tenants/{tenant_id}/sync-diagnose")
def sync_diagnose(tenant_id: str, request: Request, lookback_days: int = 90):
    """诊断：直接调企微汇报列表，返回条数/errcode（不落库、不推游标）"""
    _require_auth(request)
    from .tenant import get_all_tenants, reload_tenants
    from .wecom.dispatch import FULL_WINDOW_DAYS, diagnose_report_pull

    reload_tenants()
    tenants = {t.tenant_id: t for t in get_all_tenants()}
    t = tenants.get(tenant_id)
    if not t:
        raise HTTPException(404, "租户不存在或已禁用")
    if t.data_mode == "direct":
        raise HTTPException(409, "企微直连模式不支持同步诊断")
    days = max(1, min(int(lookback_days or 90), FULL_WINDOW_DAYS))
    result = diagnose_report_pull(t, lookback_days=days)
    # 附带库内条数，方便对比「企微N条 vs 库M条」
    try:
        from sqlalchemy import text as sqltext
        with get_engine().connect() as conn:
            row = conn.execute(sqltext(
                f"SELECT COUNT(*) FROM `{t.schema_name}`.`wecom_report`"
            )).fetchone()
            db_count = int(row[0]) if row else 0
            curs = conn.execute(sqltext(
                f"SELECT last_value, last_sync_at FROM `{t.schema_name}`.`sync_cursor` "
                f"WHERE data_source='report' AND filter_key='' LIMIT 1"
            )).fetchone()
    except Exception as e:
        db_count = -1
        curs = None
        result["db_error"] = str(e)
    result["tenant_id"] = tenant_id
    result["schema_name"] = t.schema_name
    result["db_report_count"] = db_count
    result["db_report_cursor"] = (curs[0] if curs else None)
    result["db_report_cursor_at"] = (str(curs[1]) if curs and curs[1] is not None else None)
    return result


@router.get("/tenants/{tenant_id}/domain-verify")
def get_domain_verify(tenant_id: str, request: Request):
    """查询租户可信域名与校验文件"""
    _require_auth(request)
    ensure_domain_tables()
    with get_engine().connect() as conn:
        row = conn.execute(
            text("SELECT IFNULL(trusted_domain,'') FROM tenant_config WHERE tenant_id=:t"),
            {"t": tenant_id},
        ).fetchone()
    if not row:
        raise HTTPException(404, "租户不存在")
    vf = get_verify_by_tenant(tenant_id)
    domain = row[0] or (vf or {}).get("trusted_domain") or ""
    filename = (vf or {}).get("filename") or ""
    verify_url = f"https://{domain}/{filename}" if domain and filename else (f"/{filename}" if filename else "")
    return {
        "tenant_id": tenant_id,
        "trusted_domain": domain,
        "verify_filename": filename,
        "verify_url": verify_url,
        "has_file": bool(filename),
        "updated_at": (vf or {}).get("updated_at") or "",
    }


@router.post("/tenants/{tenant_id}/domain-verify")
async def upload_domain_verify(
    tenant_id: str,
    request: Request,
    file: UploadFile = File(...),
    trusted_domain: str = Form(""),
):
    """上传/覆盖企微可信域名校验文件；新文件会替换该租户旧文件"""
    _require_auth(request)
    ensure_domain_tables()
    with get_engine().connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM tenant_config WHERE tenant_id=:t"),
            {"t": tenant_id},
        ).fetchone()
    if not exists:
        raise HTTPException(404, "租户不存在")

    raw_name = (file.filename or "").split("\\")[-1].split("/")[-1].strip()
    if not raw_name:
        raise HTTPException(400, "缺少文件名")
    body = await file.read()
    try:
        text_body = body.decode("utf-8")
    except UnicodeDecodeError:
        # 企微校验文件通常是 ASCII/UTF-8；非文本拒绝
        raise HTTPException(400, "校验文件必须是 UTF-8 文本")

    try:
        saved = save_verify_file(
            filename=raw_name,
            content=text_body,
            tenant_id=tenant_id,
            trusted_domain=trusted_domain,
            content_type=file.content_type or "text/plain; charset=utf-8",
        )
    except ValueError as e:
        raise HTTPException(400, str(e))

    domain = saved.get("trusted_domain") or ""
    # 若表单没传域名但租户已有域名，回填
    if not domain:
        with get_engine().connect() as conn:
            r = conn.execute(
                text("SELECT IFNULL(trusted_domain,'') FROM tenant_config WHERE tenant_id=:t"),
                {"t": tenant_id},
            ).fetchone()
            domain = (r[0] if r else "") or ""

    filename = saved["filename"]
    verify_url = f"https://{domain}/{filename}" if domain else f"/{filename}"
    return {
        "ok": True,
        "tenant_id": tenant_id,
        "trusted_domain": domain,
        "verify_filename": filename,
        "verify_url": verify_url,
        "msg": "已上传；同租户旧校验文件已替换",
    }


@router.delete("/tenants/{tenant_id}/domain-verify")
def remove_domain_verify(tenant_id: str, request: Request):
    """删除该租户的校验文件（不删 trusted_domain）"""
    _require_auth(request)
    if not delete_verify_by_tenant(tenant_id):
        raise HTTPException(404, "该租户没有校验文件")
    return {"ok": True}
