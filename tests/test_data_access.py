import pytest

from app.auth import TenantCtx
from app import data_access


def ctx(mode):
    return TenantCtx(
        tenant_id="tenant-a",
        corpid="ww123",
        secret="secret",
        schema_name="wbd_123",
        contact_secret="contact",
        checkin_userids=["user-a"],
        enabled_modules={"report", "approval", "checkin"},
        data_mode=mode,
    )


def test_stored_reports_use_database(monkeypatch):
    monkeypatch.setattr(
        data_access.db,
        "query_reports_by_window",
        lambda schema, start, end, limit: [{
            "journaluuid": "r1", "template_id": "t1", "template_name": "日报",
            "report_time": 100, "submitter_userid": "u1", "is_partial": 0,
        }],
    )
    result = data_access.list_reports(ctx("stored"), 1, 200, 20)
    assert result["source"] == "db"
    assert result["records"][0]["journaluuid"] == "r1"


def test_direct_reports_keep_failed_detail_as_partial(monkeypatch):
    monkeypatch.setattr(data_access, "sync_reports_window", lambda *args, **kwargs: ["r1", "r2"])
    monkeypatch.setattr(
        data_access,
        "fetch_report_detail",
        lambda corpid, secret, value: {"journaluuid": "r1", "report_time": 100}
        if value == "r1" else {"errcode": 40001, "errmsg": "invalid credential"},
    )
    result = data_access.list_reports(ctx("direct"), 1, 200, 20)
    assert result["source"] == "wecom"
    assert result["partial_count"] == 1
    assert result["records"][1]["_partial"] is True


def test_direct_limit_is_bounded_to_100(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        data_access,
        "sync_reports_window",
        lambda *args, **kwargs: captured.setdefault("limit", kwargs["max_records"]) and [],
    )
    data_access.list_reports(ctx("direct"), 1, 200, 1000)
    assert captured["limit"] == 100


def test_direct_approval_detail_failure_is_partial(monkeypatch):
    monkeypatch.setattr(data_access, "sync_approvals_window", lambda *args, **kwargs: ["sp1"])
    monkeypatch.setattr(
        data_access,
        "fetch_approval_detail",
        lambda *args: {"errcode": 301055, "errmsg": "not authorized"},
    )
    result = data_access.list_approvals(ctx("direct"), 1, 200, 20)
    assert result["partial_count"] == 1
    assert result["records"][0]["sp_no"] == "sp1"


def test_direct_checkins_require_userids(monkeypatch):
    context = ctx("direct")
    context.contact_secret = ""
    context.checkin_userids = []
    with pytest.raises(ValueError, match="userid"):
        data_access.list_checkins(context, 1, 200, 20)


def test_direct_reports_do_not_query_database(monkeypatch):
    monkeypatch.setattr(
        data_access.db,
        "query_reports_by_window",
        lambda *args: (_ for _ in ()).throw(AssertionError("database read is forbidden")),
    )
    monkeypatch.setattr(data_access, "sync_reports_window", lambda *args, **kwargs: [])
    result = data_access.list_reports(ctx("direct"), 1, 200, 20)
    assert result["source"] == "wecom"
