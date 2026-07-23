"""
管理后台 API - 租户 CRUD + 手动同步
- 鉴权：单密码登录 → 签发 session token（Cookie），中间件校验
- 密码存 .env ADMIN_PASSWORD（禁止硬编码）
- 所有租户操作用中心库 tenant_config（管理操作不经 MCP 鉴权）
"""
from __future__ import annotations

import logging
import secrets
import time
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, SecretStr
from sqlalchemy import text

from .config import get_settings
from .db import get_engine
from .domain_verify import ensure_domain_tables  # noqa: F401 - legacy test/plugin seam
from .tenant import reload_tenants
from .tenant_auth import store as tenant_auth_store
from .tenant_auth.dependencies import require_same_origin
from .tenant_auth.passwords import validate_password
from .tenant_lifecycle import (
    TenantNotFoundError,
    invalidate_retired_tenant,
    retire_tenant,
)

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


class TenantCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    display_name: str = ""
    enabled: bool = True
    tenant_password: SecretStr


class TenantUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str = ""
    enabled: bool = True
    tenant_password: Optional[SecretStr] = None


class TenantPasswordRequest(BaseModel):
    password: SecretStr


class TenantLoginStatusRequest(BaseModel):
    status: Literal["active", "disabled"]


def _tenant_exists(tenant_id: str) -> bool:
    with get_engine().connect() as conn:
        row = conn.execute(
            text("SELECT 1 FROM tenant_config WHERE tenant_id=:tenant_id"),
            {"tenant_id": tenant_id},
        ).fetchone()
    return row is not None


def _tenant_enabled(tenant_id: str) -> bool:
    with get_engine().connect() as conn:
        row = conn.execute(
            text("SELECT enabled FROM tenant_config WHERE tenant_id=:tenant_id"),
            {"tenant_id": tenant_id},
        ).fetchone()
    return bool(row and row[0])


def _fetchone(result):
    fetchone = getattr(result, "fetchone", None)
    return fetchone() if callable(fetchone) else None


@router.put("/tenants/{tenant_id}/login-password")
def reset_tenant_login_password(
    tenant_id: str,
    body: TenantPasswordRequest,
    request: Request,
):
    _require_auth(request)
    require_same_origin(request)
    if not _tenant_exists(tenant_id):
        raise HTTPException(404, "租户不存在")
    try:
        tenant_auth_store.upsert_account(
            tenant_id,
            body.password.get_secret_value(),
            status="active" if _tenant_enabled(tenant_id) else "disabled",
        )
    except ValueError:
        raise HTTPException(422, "密码不符合要求") from None
    return {"ok": True}


@router.put("/tenants/{tenant_id}/login-status")
def set_tenant_login_status(
    tenant_id: str,
    body: TenantLoginStatusRequest,
    request: Request,
):
    _require_auth(request)
    require_same_origin(request)
    if not _tenant_exists(tenant_id):
        raise HTTPException(404, "租户不存在")
    if not tenant_auth_store.set_account_status(tenant_id, body.status):
        raise HTTPException(409, "租户尚未设置登录密码")
    return {"ok": True, "status": body.status}


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
    """Serialize only tenant identity and login metadata."""
    has_login_account = bool(r[5])
    return {
        "tenant_id": r[0],
        "display_name": r[1],
        "enabled": bool(r[2]),
        "created_at": str(r[3]),
        "updated_at": str(r[4]),
        "has_login_account": has_login_account,
        "login_status": r[6] if has_login_account else None,
    }


@router.get("/tenants")
def list_tenants(request: Request):
    """列出租户身份、登录状态和时间元数据。"""
    _require_auth(request)
    sql = text("""SELECT tenant_config.tenant_id, tenant_config.display_name,
                         tenant_config.enabled, tenant_config.created_at,
                         tenant_config.updated_at,
                         IF(tenant_account.tenant_id IS NULL, 0, 1) AS has_login_account,
                         tenant_account.status AS login_status
                  FROM tenant_config
                  LEFT JOIN tenant_account USING (tenant_id)
                  ORDER BY tenant_config.created_at""")
    with get_engine().connect() as conn:
        rows = conn.execute(sql).fetchall()
    return {"items": [_tenant_item(row) for row in rows]}


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
def create_tenant(body: TenantCreate, request: Request):
    """新增租户身份，并可原子创建登录账号。"""
    _require_auth(request)
    require_same_origin(request)
    try:
        validate_password(body.tenant_password.get_secret_value())
    except ValueError:
        raise HTTPException(422, "租户登录密码不符合要求") from None
    eng = get_engine()

    sql = text("""
        INSERT INTO tenant_config (tenant_id, display_name, enabled)
        VALUES (:t, :dn, :en)
    """)
    with eng.begin() as conn:
        try:
            existing_tenant = _fetchone(conn.execute(
                text("""
                    SELECT tenant_id FROM tenant_config
                    WHERE tenant_id=:t LIMIT 1 FOR UPDATE
                """),
                {"t": body.tenant_id},
            ))
            if existing_tenant is not None:
                raise HTTPException(409, "租户 ID 已存在")
            retained_connection = _fetchone(conn.execute(
                text("""
                    SELECT connection_id FROM connection_instance
                    WHERE tenant_id=:t ORDER BY connection_id LIMIT 1 FOR UPDATE
                """),
                {"t": body.tenant_id},
            ))
            retained_service = _fetchone(conn.execute(
                text("""
                    SELECT service_id FROM mcp_service
                    WHERE tenant_id=:t ORDER BY service_id LIMIT 1 FOR UPDATE
                """),
                {"t": body.tenant_id},
            ))
            if retained_connection is not None or retained_service is not None:
                raise HTTPException(409, "租户 ID 存在保留历史，不能直接重建")
            conn.execute(sql, {
                "t": body.tenant_id,
                "dn": body.display_name,
                "en": 1 if body.enabled else 0,
            })
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning(
                "tenant create write failed tenant=%s error_type=%s",
                body.tenant_id,
                type(exc).__name__,
            )
            raise HTTPException(
                400,
                "写入失败，可能租户 ID 重复",
            ) from None
        try:
            tenant_auth_store.upsert_account(
                body.tenant_id,
                body.tenant_password.get_secret_value(),
                status="active" if body.enabled else "disabled",
                conn=conn,
            )
        except ValueError:
            raise HTTPException(422, "租户登录密码不符合要求") from None
    reload_tenants()
    return {"ok": True}


@router.put("/tenants/{tenant_id}")
def update_tenant(tenant_id: str, body: TenantUpdate, request: Request):
    """只更新租户名称、隔离状态与显式提供的登录密码。"""
    _require_auth(request)
    if body.tenant_password is not None:
        require_same_origin(request)
    if body.tenant_password is not None:
        try:
            validate_password(body.tenant_password.get_secret_value())
        except ValueError:
            raise HTTPException(422, "租户登录密码不符合要求") from None
    eng = get_engine()
    sql = text("""
        UPDATE tenant_config SET
            display_name=:dn, enabled=:en
        WHERE tenant_id=:t
    """)
    with eng.begin() as conn:
        result = conn.execute(sql, {
            "t": tenant_id,
            "dn": body.display_name,
            "en": 1 if body.enabled else 0,
        })
        if getattr(result, "rowcount", None) == 0:
            raise HTTPException(404, "租户不存在")
        account_status = "active" if body.enabled else "disabled"
        if body.tenant_password is not None:
            tenant_auth_store.upsert_account(
                tenant_id,
                body.tenant_password.get_secret_value(),
                status=account_status,
                conn=conn,
            )
        elif not body.enabled:
            tenant_auth_store.set_account_status(tenant_id, "disabled", conn=conn)
    reload_tenants()
    return {"ok": True}


@router.delete("/tenants/{tenant_id}")
def delete_tenant(tenant_id: str, request: Request):
    """Atomically retire authorization while preserving historical data."""
    _require_auth(request)
    try:
        retirement = retire_tenant(
            tenant_id,
            request_id=request.headers.get("x-request-id", ""),
            client_ip=request.client.host if request.client else "",
            http_method=request.method,
        )
    except TenantNotFoundError:
        raise HTTPException(404, "租户不存在") from None
    except Exception as exc:
        logger.warning(
            "tenant retirement failed tenant=%s error_type=%s",
            tenant_id,
            type(exc).__name__,
        )
        raise HTTPException(409, "租户删除失败，请重试") from None
    invalidate_retired_tenant(retirement, reload_tenants=reload_tenants)
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
