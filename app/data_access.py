from __future__ import annotations

from . import db
from .auth import TenantCtx
from .wecom.approval_sync import fetch_approval_detail, sync_approvals_window
from .wecom.checkin_sync import fetch_checkin_records
from .wecom.contact import fetch_all_userids
from .wecom.sync import fetch_report_detail, sync_reports_window


def _limit(value: int) -> int:
    return max(1, min(int(value or 100), 100))


def _wecom_partial(identifier: str, response: dict) -> dict:
    return {
        "id": identifier,
        "_partial": True,
        "errcode": response.get("errcode"),
        "errmsg": response.get("errmsg", "detail unavailable"),
    }


def list_reports(ctx: TenantCtx, starttime: int, endtime: int, limit: int) -> dict:
    size = _limit(limit)
    if ctx.data_mode == "stored":
        rows = db.query_reports_by_window(ctx.schema_name, starttime, endtime, size)
        records = [{
            "journaluuid": row["journaluuid"],
            "template_id": row["template_id"],
            "template_name": row["template_name"],
            "report_time": row["report_time"],
            "submitter": row["submitter_userid"],
            "_partial": bool(row.get("is_partial", 0)),
        } for row in rows]
        return {
            "tenant": ctx.tenant_id,
            "source": "db",
            "count": len(records),
            "records": records,
            "partial_count": sum(bool(record["_partial"]) for record in records),
        }

    identifiers = sync_reports_window(
        ctx.corpid, ctx.secret, starttime, endtime, max_records=size
    )
    records = []
    for identifier in identifiers:
        detail = fetch_report_detail(ctx.corpid, ctx.secret, identifier)
        if detail.get("errcode") not in (None, 0):
            partial = _wecom_partial(identifier, detail)
            partial["journaluuid"] = identifier
            records.append(partial)
            continue
        records.append(detail)
    partial_count = sum(bool(record.get("_partial")) for record in records)
    return {
        "tenant": ctx.tenant_id,
        "source": "wecom",
        "count": len(records),
        "records": records,
        "partial_count": partial_count,
    }


def get_report(ctx: TenantCtx, journaluuid: str) -> dict:
    if ctx.data_mode == "stored":
        detail = db.get_report_detail(ctx.schema_name, journaluuid)
        if not detail:
            return {"source": "db", "errcode": 404, "errmsg": "汇报单号不存在"}
        return {"source": "db", "detail": detail}
    detail = fetch_report_detail(ctx.corpid, ctx.secret, journaluuid)
    if detail.get("errcode") not in (None, 0):
        return {"source": "wecom", **detail}
    return {"source": "wecom", "detail": detail}


def list_approvals(ctx: TenantCtx, starttime: int, endtime: int, limit: int) -> dict:
    size = _limit(limit)
    if ctx.data_mode == "stored":
        rows = db.query_approvals_by_window(ctx.schema_name, starttime, endtime, size)
        records = [{
            "sp_no": row["sp_no"],
            "sp_name": row["sp_name"],
            "sp_status": row["sp_status"],
            "template_id": row["template_id"],
            "apply_time": row["apply_time"],
            "applyer": row["applyer_userid"],
            "_partial": bool(row.get("is_partial", 0)),
        } for row in rows]
        return {
            "tenant": ctx.tenant_id,
            "source": "db",
            "count": len(records),
            "records": records,
            "partial_count": sum(bool(record["_partial"]) for record in records),
        }

    identifiers = sync_approvals_window(
        ctx.corpid, ctx.secret, starttime, endtime, max_records=size
    )
    records = []
    for identifier in identifiers:
        detail = fetch_approval_detail(ctx.corpid, ctx.secret, identifier)
        if detail.get("errcode") not in (None, 0):
            partial = _wecom_partial(identifier, detail)
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
    partial_count = sum(bool(record.get("_partial")) for record in records)
    return {
        "tenant": ctx.tenant_id,
        "source": "wecom",
        "count": len(records),
        "records": records,
        "partial_count": partial_count,
    }


def get_approval(ctx: TenantCtx, sp_no: str) -> dict:
    if ctx.data_mode == "stored":
        detail = db.get_approval_detail(ctx.schema_name, sp_no)
        if not detail:
            return {"source": "db", "errcode": 404, "errmsg": "审批单号不存在"}
        return {"source": "db", "detail": detail}
    detail = fetch_approval_detail(ctx.corpid, ctx.secret, sp_no)
    if detail.get("errcode") not in (None, 0):
        return {"source": "wecom", **detail}
    return {"source": "wecom", "detail": detail}


def list_checkins(ctx: TenantCtx, starttime: int, endtime: int, limit: int) -> dict:
    size = _limit(limit)
    if ctx.data_mode == "stored":
        rows = db.query_checkins_by_window(ctx.schema_name, starttime, endtime, size)
        records = [{
            "userid": row["userid"],
            "checkin_type": row["checkin_type"],
            "checkin_time": row["checkin_time"],
            "exception_type": row["exception_type"],
            "location_title": row["location_title"],
            "group_name": row["group_name"],
        } for row in rows]
        return {
            "tenant": ctx.tenant_id,
            "source": "db",
            "count": len(records),
            "records": records,
            "partial_count": 0,
        }

    userids = (
        fetch_all_userids(ctx.corpid, ctx.contact_secret)
        if ctx.contact_secret
        else list(ctx.checkin_userids)
    )
    if not userids:
        raise ValueError("直连打卡需要通讯录 Secret 或手工 userid")
    records = fetch_checkin_records(
        ctx.corpid, ctx.secret, starttime, endtime, userids
    )
    records.sort(
        key=lambda record: int(record.get("checkin_time", 0) or 0),
        reverse=True,
    )
    selected = records[:size]
    return {
        "tenant": ctx.tenant_id,
        "source": "wecom",
        "count": len(selected),
        "records": selected,
        "partial_count": 0,
    }
