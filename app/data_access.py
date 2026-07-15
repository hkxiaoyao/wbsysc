from __future__ import annotations

import re
from typing import Any

from . import db
from .auth import TenantCtx
from .connectors.contracts import ConnectionContext
from .wecom.approval_sync import fetch_approval_detail, sync_approvals_window
from .wecom.checkin_sync import fetch_checkin_records_with_stats
from .wecom.contact import fetch_all_userids
from .wecom.sync import fetch_report_detail, sync_reports_window


class PublicDataAccessError(Exception):
    """可安全返回给 MCP 调用方的数据访问错误。"""

    def __init__(self, errcode: int, public_message: str, source: str):
        super().__init__(public_message)
        self.errcode = int(errcode)
        self.public_message = public_message
        self.source = source


class WeComStorageAdapter:
    """Keeps the existing per-tenant WeCom schema behind a connection context.

    Connection instances own their configuration, while the first migration
    deliberately continues to use the established tenant schemas for stored
    WeCom records.  This adapter is the narrow compatibility boundary between
    those two representations.
    """

    def __init__(self, context: ConnectionContext | TenantCtx) -> None:
        self._context = context

    @property
    def schema_name(self) -> str:
        if isinstance(self._context, ConnectionContext):
            value = self._context.public_config.get("schema_name", "")
        else:
            value = self._context.schema_name
        if not isinstance(value, str) or not value:
            raise ValueError("WeCom stored mode requires a schema_name")
        return value

    def list_reports(self, starttime: int, endtime: int, limit: int) -> list[dict]:
        return db.query_reports_by_window(self.schema_name, starttime, endtime, limit)

    def get_report(self, journaluuid: str) -> dict | None:
        return db.get_report_detail(self.schema_name, journaluuid)

    def list_approvals(self, starttime: int, endtime: int, limit: int) -> list[dict]:
        return db.query_approvals_by_window(self.schema_name, starttime, endtime, limit)

    def get_approval(self, sp_no: str) -> dict | None:
        return db.get_approval_detail(self.schema_name, sp_no)

    def list_checkins(self, starttime: int, endtime: int, limit: int) -> list[dict]:
        return db.query_checkins_by_window(self.schema_name, starttime, endtime, limit)


def _config_value(
    context: ConnectionContext | TenantCtx,
    key: str,
    legacy_attribute: str,
    default: Any = "",
) -> Any:
    if isinstance(context, ConnectionContext):
        return context.public_config.get(key, default)
    return getattr(context, legacy_attribute, default)


def _credential_value(
    context: ConnectionContext | TenantCtx,
    key: str,
    legacy_attribute: str,
) -> str:
    if isinstance(context, ConnectionContext):
        value = context.credentials.get(key)
        if value is None:
            # The two aliases make direct construction of a ConnectionContext
            # ergonomic without changing the persisted credential key names.
            value = context.credentials.get(legacy_attribute)
        return value if isinstance(value, str) else ""
    value = getattr(context, legacy_attribute, "")
    return value if isinstance(value, str) else ""


def _corpid(context: ConnectionContext | TenantCtx) -> str:
    value = _config_value(context, "corpid", "corpid")
    return value if isinstance(value, str) else ""


def _app_secret(context: ConnectionContext | TenantCtx) -> str:
    return _credential_value(context, "wecom_app_secret", "secret")


def _contact_secret(context: ConnectionContext | TenantCtx) -> str:
    return _credential_value(context, "wecom_contact_secret", "contact_secret")


def _checkin_userids(context: ConnectionContext | TenantCtx) -> list[str]:
    values = _config_value(context, "checkin_userids", "checkin_userids", [])
    if isinstance(values, str):
        return [value.strip() for value in values.split(",") if value.strip()]
    if isinstance(values, (list, tuple, set)):
        return [value for value in values if isinstance(value, str) and value]
    return []


def _limit(value: int) -> int:
    return max(1, min(int(value or 100), 100))


def _safe_errcode(value, default: int = 502) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _known_wecom_call(call, public_message: str):
    errcode = None
    try:
        return call()
    except PublicDataAccessError:
        raise
    except Exception as exc:
        match = re.search(r"\[(-?\d+)\]", str(exc))
        if not match:
            raise
        errcode = int(match.group(1))
    raise PublicDataAccessError(errcode, public_message, "wecom")


def _wecom_partial(identifier: str, response: dict, public_message: str) -> dict:
    return {
        "id": identifier,
        "_partial": True,
        "errcode": _safe_errcode(response.get("errcode")),
        "errmsg": public_message,
    }


def list_reports(
    ctx: ConnectionContext | TenantCtx,
    starttime: int,
    endtime: int,
    limit: int,
) -> dict:
    size = _limit(limit)
    if ctx.data_mode in {"stored", "hybrid"}:
        rows = WeComStorageAdapter(ctx).list_reports(starttime, endtime, size)
        records = [{
            "journaluuid": row["journaluuid"],
            "template_id": row["template_id"],
            "template_name": row["template_name"],
            "report_time": row["report_time"],
            "submitter": row["submitter_userid"],
            "_partial": bool(row.get("is_partial", 0)),
        } for row in rows]
        stored_result = {
            "tenant": ctx.tenant_id,
            "source": "db",
            "count": len(records),
            "records": records,
            "partial_count": sum(bool(record["_partial"]) for record in records),
        }
        if ctx.data_mode == "stored" or records:
            return stored_result

    identifiers = _known_wecom_call(
        lambda: sync_reports_window(
            _corpid(ctx), _app_secret(ctx), starttime, endtime, max_records=size
        ),
        "企微汇报请求失败",
    )
    records = []
    for identifier in identifiers:
        detail = fetch_report_detail(_corpid(ctx), _app_secret(ctx), identifier)
        if detail.get("errcode") not in (None, 0):
            partial = _wecom_partial(identifier, detail, "企微汇报详情请求失败")
            partial["journaluuid"] = identifier
            records.append(partial)
            continue
        records.append(detail)
    records.sort(
        key=lambda record: int(record.get("report_time", 0) or 0),
        reverse=True,
    )
    records = records[:size]
    partial_count = sum(bool(record.get("_partial")) for record in records)
    return {
        "tenant": ctx.tenant_id,
        "source": "wecom",
        "count": len(records),
        "records": records,
        "partial_count": partial_count,
    }


def get_report(ctx: ConnectionContext | TenantCtx, journaluuid: str) -> dict:
    if ctx.data_mode in {"stored", "hybrid"}:
        detail = WeComStorageAdapter(ctx).get_report(journaluuid)
        if not detail:
            if ctx.data_mode == "stored":
                return {"source": "db", "errcode": 404, "errmsg": "汇报单号不存在"}
        else:
            return {"source": "db", "detail": detail}
    detail = fetch_report_detail(_corpid(ctx), _app_secret(ctx), journaluuid)
    if detail.get("errcode") not in (None, 0):
        raise PublicDataAccessError(
            _safe_errcode(detail.get("errcode")), "企微汇报请求失败", "wecom"
        )
    return {"source": "wecom", "detail": detail}


def list_approvals(
    ctx: ConnectionContext | TenantCtx,
    starttime: int,
    endtime: int,
    limit: int,
) -> dict:
    size = _limit(limit)
    if ctx.data_mode in {"stored", "hybrid"}:
        rows = WeComStorageAdapter(ctx).list_approvals(starttime, endtime, size)
        records = [{
            "sp_no": row["sp_no"],
            "sp_name": row["sp_name"],
            "sp_status": row["sp_status"],
            "template_id": row["template_id"],
            "apply_time": row["apply_time"],
            "applyer": row["applyer_userid"],
            "_partial": bool(row.get("is_partial", 0)),
        } for row in rows]
        stored_result = {
            "tenant": ctx.tenant_id,
            "source": "db",
            "count": len(records),
            "records": records,
            "partial_count": sum(bool(record["_partial"]) for record in records),
        }
        if ctx.data_mode == "stored" or records:
            return stored_result

    identifiers = _known_wecom_call(
        lambda: sync_approvals_window(
            _corpid(ctx), _app_secret(ctx), starttime, endtime, max_records=size
        ),
        "企微审批请求失败",
    )
    records = []
    for identifier in identifiers:
        detail = fetch_approval_detail(_corpid(ctx), _app_secret(ctx), identifier)
        if detail.get("errcode") not in (None, 0):
            partial = _wecom_partial(identifier, detail, "企微审批详情请求失败")
            partial["sp_no"] = identifier
            records.append(partial)
            continue
        applyer = detail.get("applyer") or {}
        records.append({
            "sp_no": detail.get("sp_no", identifier),
            "sp_name": detail.get("sp_name", ""),
            "sp_status": detail.get("sp_status", 0),
            "template_id": detail.get("template_id", ""),
            "apply_time": detail.get("apply_time", 0),
            "applyer": applyer.get("userid", "") if isinstance(applyer, dict) else applyer,
            "_partial": False,
        })
    records.sort(
        key=lambda record: int(record.get("apply_time", 0) or 0),
        reverse=True,
    )
    records = records[:size]
    partial_count = sum(bool(record.get("_partial")) for record in records)
    return {
        "tenant": ctx.tenant_id,
        "source": "wecom",
        "count": len(records),
        "records": records,
        "partial_count": partial_count,
    }


def get_approval(ctx: ConnectionContext | TenantCtx, sp_no: str) -> dict:
    if ctx.data_mode in {"stored", "hybrid"}:
        detail = WeComStorageAdapter(ctx).get_approval(sp_no)
        if not detail:
            if ctx.data_mode == "stored":
                return {"source": "db", "errcode": 404, "errmsg": "审批单号不存在"}
        else:
            return {"source": "db", "detail": detail}
    detail = fetch_approval_detail(_corpid(ctx), _app_secret(ctx), sp_no)
    if detail.get("errcode") not in (None, 0):
        raise PublicDataAccessError(
            _safe_errcode(detail.get("errcode")), "企微审批请求失败", "wecom"
        )
    return {"source": "wecom", "detail": detail}


def list_checkins(
    ctx: ConnectionContext | TenantCtx,
    starttime: int,
    endtime: int,
    limit: int,
) -> dict:
    size = _limit(limit)
    if ctx.data_mode in {"stored", "hybrid"}:
        rows = WeComStorageAdapter(ctx).list_checkins(starttime, endtime, size)
        records = [{
            "userid": row["userid"],
            "checkin_type": row["checkin_type"],
            "checkin_time": row["checkin_time"],
            "exception_type": row["exception_type"],
            "location_title": row["location_title"],
            "group_name": row["group_name"],
        } for row in rows]
        stored_result = {
            "tenant": ctx.tenant_id,
            "source": "db",
            "count": len(records),
            "records": records,
            "partial_count": 0,
        }
        if ctx.data_mode == "stored" or records:
            return stored_result

    contact_secret = _contact_secret(ctx)
    if contact_secret:
        userids = _known_wecom_call(
            lambda: fetch_all_userids(_corpid(ctx), contact_secret),
            "企微打卡请求失败",
        )
    else:
        userids = _checkin_userids(ctx)
    if not userids:
        raise PublicDataAccessError(
            400,
            "直连打卡需要配置通讯录 Secret 或手工 userid",
            "wecom",
        )
    fetch_result = fetch_checkin_records_with_stats(
        _corpid(ctx), _app_secret(ctx), starttime, endtime, userids
    )
    if fetch_result.attempted and fetch_result.failed == fetch_result.attempted:
        first_error = fetch_result.errors[0] if fetch_result.errors else {}
        raise PublicDataAccessError(
            _safe_errcode(first_error.get("errcode")),
            "企微打卡请求失败",
            "wecom",
        )
    records = fetch_result.records
    records.sort(
        key=lambda record: int(record.get("checkin_time", 0) or 0),
        reverse=True,
    )
    selected = records[:size]
    public_errors = [{
        "userid": error.get("userid", ""),
        "errcode": (
            _safe_errcode(error.get("errcode"))
            if error.get("errcode") is not None
            else None
        ),
        "errmsg": "企微打卡请求失败",
    } for error in fetch_result.errors]
    return {
        "tenant": ctx.tenant_id,
        "source": "wecom",
        "count": len(selected),
        "records": selected,
        "partial_count": fetch_result.failed,
        "errors": public_errors,
    }
